"""可复用的运行层：CLI 与 Web 界面共用同一套执行逻辑。

设计要点：
  * RunConfig 是一个纯配置对象（不再是 argparse 的 Namespace），
    Web 端可以直接构造它，也可以在运行中热更新它（下一轮生效）；
  * run_once / run_loop 支持 stop_event，能在「每个 IP 探测之间」被打断；
  * 通过 on_event 回调实时上报日志与进度，Web 端据此渲染进度条和日志流。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .core import (
    Platform,
    ScanOutcome,
    append_history,
    check_network_integrity,
    safe_log,
    scan_platforms,
    write_result,
)
from .ratelimit import make_rate_limiter

MIN_IPS_WARN = 3

# 一轮内每个平台最多向界面推送多少条「单个 IP」日志，避免刷屏
MAX_PER_IP_EVENTS = 400


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class RunConfig:
    """一轮运行所需的全部配置。字段与 CLI 参数一一对应。"""

    rate: float = 100.0              # 全平台合计连接上限（连接/秒），<=0 不限速
    targets: str | None = None       # 统一网段文件（覆盖各平台自己的）
    timeout: float | None = None
    workers: int | None = None
    retries: int | None = None
    ping: bool = False               # 是否启用 ICMP 预筛选
    host_override: str | None = None
    status: tuple[str, ...] | None = None
    with_latency: bool = False
    no_write: bool = False
    write_json: str | None = None
    webhook: str | None = None
    notify_always: bool = False
    skip_check: bool = False
    dns_mode: str = "none"           # none / dry-run / push
    dns_yes: bool = False
    dns_force: bool = False
    quiet: bool = False
    verbose: bool = False            # 是否逐 IP 推送日志（Web 上默认只推汇总）
    sample_per_24: int | None = None  # 粗扫抽样：每 /24 取几个代表 IP
    deep_scan: bool = False           # 深扫：以上一轮命中的 /24 为范围
    max_latency_ms: float | None = None  # 延迟门槛：超过就丢弃（None/0 = 不设）
    top_n: int | None = None             # 只保留最快的 N 个（None/0 = 全部）

    def apply_to(self, platforms: Iterable[Platform]) -> list[Platform]:
        """把通用覆盖项作用到平台配置上，返回平台列表。"""
        result = list(platforms)
        for platform in result:
            if self.workers is not None:
                platform.workers = self.workers
            if self.timeout is not None:
                platform.timeout = self.timeout
            if self.retries is not None:
                platform.retries = self.retries
            if self.status:
                platform.status_codes = tuple(self.status)
            if self.host_override:
                platform.host = self.host_override
            if self.sample_per_24 is not None:
                platform.sample_per_24 = self.sample_per_24
            if self.max_latency_ms is not None:
                platform.max_latency_ms = self.max_latency_ms
            if self.top_n is not None:
                platform.top_n = self.top_n
            platform.deep_scan = bool(self.deep_scan)
            platform.precheck_ping = bool(self.ping)
        return result


# 事件类型：log（一行日志）/ progress（进度）/ state（状态变化）
Event = dict


# --------------------------------------------------------------------------
# 单轮执行
# --------------------------------------------------------------------------
def run_once(
    platforms: Sequence[Platform],
    config: RunConfig,
    emit: Callable[[Event], None] | None = None,
    stop_event: threading.Event | None = None,
    load_targets_fn: Callable[[Sequence[Platform], str | None], list[str] | None] | None = None,
) -> list[ScanOutcome]:
    """执行一轮扫描（可选下发 DNS），并通过 emit 实时上报日志与进度。

    load_targets_fn 由调用方注入，用于解析 --targets 覆盖文件；
    Web 端会传入自己的实现，CLI 端用默认实现。
    """
    emit = emit or (lambda _event: None)
    stop_event = stop_event or threading.Event()

    def log(message: str = "") -> None:
        # 扫描过程中的日志：Web 端收进环形缓冲，CLI 端直接打印
        if not config.quiet:
            safe_log(message)
        emit({"type": "log", "line": message})

    def log_error(message: str = "") -> None:
        """错误必须可见：quiet 模式下也输出。

        -q 只应抑制进度类噪音，不应把「网段文件不存在」这类配置错误一起吞掉，
        否则用户只看到一个空的结果标题，无从排查。
        """
        safe_log(message)
        emit({"type": "log", "line": message, "level": "error"})

    platforms = config.apply_to(platforms)
    targets = None
    if load_targets_fn is not None:
        targets = load_targets_fn(platforms, config.targets)
    elif config.targets:
        from .core import load_targets as _load

        path = Path(config.targets)
        if not path.exists():
            # 指定了网段文件却不存在时，绝不能「静默改用平台默认网段」——
            # 那会让用户以为只扫了测试文件，实际把全量网段扫了一遍。
            log_error(f"✗ 指定的网段文件不存在: {path}")
            log_error("  已中止本轮（不会改用平台默认网段，避免误扫全量）")
            emit({"type": "error", "scope": "targets-missing", "path": str(path)})
            emit({"type": "state", "state": "idle"})
            return []
        targets = _load(path)
        log(f"使用统一网段文件 {path.name}，共 {len(targets)} 个 IP（所有平台共用）")

    # 链路可信度检查：本机若有代理/透明网关接管 HTTPS，钉 IP 探测会全部「可用」
    if not config.skip_check:
        # 先上报阶段：这一步要对每个平台做真实网络探测，可能耗时十几秒，
        # 否则网页会长时间停在「无进度」的状态，让人以为卡死。
        emit({"type": "progress", "phase": "checking", "done": 0, "total": 0,
              "platforms": {p.key: {"name": p.name, "total": 0, "done": 0, "alive": 0}
                            for p in platforms}})
        emit({"type": "log",
              "line": f"正在检查链路可信度（{len(platforms)} 个平台，"
                      f"每个平台探测 2 个保留地址）…"})
        pending = []
        for platform in platforms:
            pending.append((platform, None))
        for index, (platform, _) in enumerate(list(pending)):
            trusted, detail = check_network_integrity(
                platform.host, timeout=platform.timeout or 3.0,
                port=platform.port, status_codes=platform.status_codes)
            if not trusted:
                emit({"type": "log", "line": "!" * 58})
                emit({"type": "log", "line": f"⚠ 链路不可信（{platform.name}）：{detail}"})
                emit({"type": "log", "line": "!" * 58})
                emit({"type": "log", "line": "本轮结果仅作流程验证，不会下发 DNS。"})
                config = replace(config, dns_mode="none")
                break

    limiter = make_rate_limiter(config.rate)
    if limiter.enabled:
        emit({"type": "log", "line": f"连接限速：全平台合计 {limiter.rate:g} 连接/秒"})

    # 进度统计：每个平台的 total 在读取网段后由 on_plan 回填
    progress = {
        "phase": "scanning",
        "platforms": {p.key: {"name": p.name, "total": 0, "done": 0, "alive": 0} for p in platforms},
        "done": 0,
        "total": 0,
    }

    def push_progress() -> None:
        emit({"type": "progress", **progress})

    started = time.perf_counter()
    push_progress()

    def on_plan(platform: Platform, total: int) -> None:
        entry = progress["platforms"].setdefault(
            platform.key, {"name": platform.name, "total": 0, "done": 0, "alive": 0})
        entry["total"] = total
        progress["total"] = sum(p["total"] for p in progress["platforms"].values())
        push_progress()

    def on_result(platform: Platform, probe) -> None:
        entry = progress["platforms"].setdefault(
            platform.key, {"name": platform.name, "total": 0, "done": 0, "alive": 0})
        entry["done"] += 1
        if probe.ok:
            entry["alive"] += 1
        progress["done"] = sum(p["done"] for p in progress["platforms"].values())
        # 每 5 个 IP 或命中时推送一次，避免事件过于频繁
        if probe.ok or entry["done"] % 5 == 0:
            push_progress()
        if config.verbose and (probe.ok or entry["done"] <= MAX_PER_IP_EVENTS):
            flag = "✅" if probe.ok else f"❌ {probe.error or probe.status}"
            emit({"type": "log", "line": f"  {'[+]' if probe.ok else '[-]'} "
                                        f"{platform.name} {probe.ip} {flag}"})

    outcomes = scan_platforms(platforms, targets=targets, log=log, quiet=True,
                              limiter=limiter, stop_event=stop_event,
                              on_result=on_result, on_plan=on_plan)

    elapsed = time.perf_counter() - started
    progress["phase"] = "finished"
    progress["done"] = progress["total"] = sum(
        p["done"] for p in progress["platforms"].values())
    push_progress()

    results: list[tuple[ScanOutcome, bool]] = []
    for outcome in outcomes:
        entry = progress["platforms"].get(outcome.platform.key)
        if entry:
            entry["total"] = outcome.total or entry["total"]
            entry["alive"] = len(outcome.passed)
        log(f"● {outcome.summary_line()}")

        suspended = _is_suspicious(outcome)
        if outcome.error:
            log_error(f"  ⚠ {outcome.error}")
        elif len(outcome.passed) < MIN_IPS_WARN:
            log(f"  ⚠ 可用节点仅 {len(outcome.passed)} 个，低于警戒值 {MIN_IPS_WARN}")
        elif suspended:
            log("  ⚠ 通过率异常高且延迟异常低，疑似本机链路被代理接管，"
                "该结果不可用于下发 DNS")

        if outcome.passed:
            top = ", ".join(f"{p.ip}({p.latency_ms:.0f}ms)" for p in outcome.passed[:5])
            log(f"  最快节点: {top}")

        if not config.no_write:
            if outcome.platform.output_file:
                write_result(Path(outcome.platform.output_file), outcome.passed,
                             with_latency=config.with_latency)
                log(f"  结果 -> {outcome.platform.output_file}")
            if outcome.platform.history_file:
                append_history(Path(outcome.platform.history_file), outcome.platform, outcome)

        results.append((outcome, suspended))

    if len(results) > 1:
        _report_coverage(results, log)

    if config.write_json:
        payload = [
            {
                "platform": o.platform.key,
                "host": o.platform.host,
                "total": o.total,
                "alive": len(o.passed),
                "elapsed": round(o.elapsed, 2),
                "error": o.error,
                "suspicious": suspended,
                "ips": [{"ip": p.ip, "ms": round(p.latency_ms, 1)} for p in o.passed],
            }
            for o, suspended in results
        ]
        # 原子写：这个文件是给外部程序读的，半截 JSON 会让对方解析失败
        from .core import atomic_write_text
        atomic_write_text(Path(config.write_json),
                          json.dumps(payload, ensure_ascii=False, indent=2))
        log(f"JSON 汇总 -> {config.write_json}")

    # 全部平台都失败时，这是配置问题（如网段文件缺失），必须显式报错。
    # 否则日志里只有一行 ⚠，容易被误解为"该平台扫不到可用节点"。
    if results and all(o.error for o, _ in results):
        stopped = all("停止" in (o.error or "") for o, _ in results)
        log_error("")
        log_error("=" * 62)
        if stopped:
            # 用户主动停止不是故障，不要给出"补齐网段文件"这种误导性建议
            log_error("本轮已被手动停止，没有产出结果（这是正常的，不是故障）")
            for outcome, _ in results:
                log_error(f"  - {outcome.platform.key}: {outcome.error}")
        else:
            log_error(f"✗ 本轮全部 {len(results)} 个平台均扫描失败 "
                      f"—— 这是配置问题，不是「没有可用节点」")
            for outcome, _ in results:
                log_error(f"  - {outcome.platform.key}: {outcome.error}")
            log_error("  请按上面的提示补齐网段文件后重试"
                      "（run.py list 可查看各平台期望的文件）")
        log_error("=" * 62)
        emit({"type": "error",
              "scope": "stopped" if stopped else "all-platforms-failed",
              "errors": {o.platform.key: o.error for o, _ in results}})

    # DNS 下发
    if config.dns_mode != "none" and not stop_event.is_set():
        _apply_dns(results, config, emit, log)

    log(f"本轮结束（总耗时 {elapsed:.1f}s）")
    emit({"type": "state", "state": "idle", "finished_at": int(time.time())})
    return [o for o, _ in results]


def _is_suspicious(outcome: ScanOutcome) -> bool:
    """通过率 >95% 且中位延迟 <5ms 视为「好得不真实」。"""
    if outcome.total < 50 or not outcome.passed:
        return False
    if len(outcome.passed) / outcome.total < 0.95:
        return False
    sample = sorted(p.latency_ms for p in outcome.passed)
    return sample[len(sample) // 2] < 5.0


def _report_coverage(results: Sequence[tuple[ScanOutcome, bool]],
                     log: Callable[[str], None]) -> None:
    coverage: dict[str, list[str]] = {}
    for outcome, _ in results:
        for probe in outcome.passed:
            coverage.setdefault(probe.ip, []).append(outcome.platform.key)
    if not coverage:
        return
    log("多平台覆盖情况（同一 IP 可被多个平台命中）：")
    for ip, keys in sorted(coverage.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:30]:
        mark = "★ 通用" if len(keys) == len(results) else "  "
        log(f"  {mark} {ip:<16} 命中 {len(keys)}/{len(results)}: {', '.join(keys)}")


def _apply_dns(results: Sequence[tuple[ScanOutcome, bool]], config: RunConfig,
               emit: Callable[[Event], None], log: Callable[[str], None]) -> None:
    from .dns import sync_platform

    dry_run = config.dns_mode == "dry-run"
    if not dry_run and not config.dns_yes:
        log("⚠ 未确认，跳过 DNS 下发（需要 --yes / Web 上勾选确认）")
        return

    log("DNS 下发" + ("（dry-run 预览）" if dry_run else ""))
    for outcome, suspended in results:
        platform = outcome.platform
        if not platform.dns:
            log(f"跳过 {platform.name}: 未配置 dns")
            continue
        if outcome.error or not outcome.passed:
            log(f"跳过 {platform.name}: 本轮没有可用结果，保持线上解析不变")
            continue
        if suspended:
            log(f"跳过 {platform.name}: 结果可疑，拒绝下发")
            continue
        # 单个平台的 DNS 故障（网络抖动、权限不足等）不应影响其它平台，
        # 更不该让整轮运行中断 —— 兜住后如实上报即可。
        try:
            result = sync_platform(platform, log=log, dry_run=dry_run,
                                   force=config.dns_force)
        except Exception as exc:
            summary = f"DNS 下发异常: {type(exc).__name__}: {exc}"
            log(f"  ✗ {platform.name} {summary}")
            emit({"type": "dns", "platform": platform.key, "ok": False,
                  "summary": summary})
            continue
        log(f"  -> {result.summary()}")
        emit({"type": "dns", "platform": platform.key, "ok": result.ok,
              "summary": result.summary()})


# --------------------------------------------------------------------------
# 循环执行（Web / monitor 共用）
# --------------------------------------------------------------------------
class CycleRunner:
    """在后台线程里跑一轮轮循环，支持随时停止与「立即跑一轮」。"""

    def __init__(self, emit: Callable[[Event], None] | None = None,
                 settings: Callable[[], dict] | None = None) -> None:
        """settings 是一个回调，每次准备下一轮时调用，返回
        {"interval": int, "window": str}。这样网页上改了间隔/时间窗口，
        正在跑的循环会**在下一轮自动采用**，不需要重启。
        """
        self._emit = emit or (lambda _event: None)
        self._settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.state = "idle"          # idle / running / sleeping / stopping
        self.cycle = 0
        self.last_error = ""
        self.next_run_at = 0.0
        self._window_warned: set[str] = set()   # 时间窗口写法问题只提醒一次
        self.current_interval = 0
        self.manual = False          # 当前是否是「立刻跑一轮」触发的一次性任务

    # ---- 状态 ----
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict:
        # configured_interval：网页上当前配置的间隔。
        # 与 interval（本轮实际使用的）区分开：改了间隔要下一轮才生效，
        # 若只显示 interval，用户会以为设置没生效。
        configured = 0
        if self._settings is not None:
            try:
                latest = self._settings() or {}
                configured = int(latest.get("interval") or 0)
            except Exception:                 # pragma: no cover - 配置异常不影响状态
                configured = 0
        return {
            "state": self.state,
            "cycle": self.cycle,
            "running": self.running,
            "manual": self.manual,
            "interval": self.current_interval,
            "configured_interval": configured,
            "last_error": self.last_error,
            "next_run_at": int(self.next_run_at),
            "now": int(time.time()),
        }

    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit({"type": "state", "state": state})

    # ---- 控制 ----
    def start(self, job: Callable[[int, threading.Event], None], interval: int,
              cycles: int = 0, window: str = "", first_delay: int = 0) -> bool:
        """job(cycle_no, stop_event) 负责跑一轮；interval 为轮询间隔秒数。"""
        with self._lock:
            if self.running:
                return False
            self._stop = threading.Event()
            self._wake = threading.Event()
            self._set_state("running")
            self._thread = threading.Thread(
                target=self._loop, args=(job, max(1, interval), cycles, window, first_delay),
                name="eo-runner", daemon=True)
            self._thread.start()
            return True

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        self._wake.set()
        self._set_state("stopping")
        if wait and self._thread:
            self._thread.join(timeout=30)

    def wait(self, timeout: float | None = None) -> bool:
        """等待循环线程结束，返回是否已经结束。

        停止服务时用它给「正在跑的这一轮」留一点收尾时间（写结果 / 下发 DNS），
        但不能无限等 —— 超过宽限期进程会被 SIGKILL。
        """
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def trigger_now(self) -> None:
        """打断当前睡眠，立刻进入下一轮。"""
        self._wake.set()

    # ---- 主循环 ----
    def _loop(self, job, interval: int, cycles: int, window: str, first_delay: int) -> None:
        try:
            if first_delay > 0 and self._sleep(first_delay):
                return
            while not self._stop.is_set():
                # 每轮开始前重新读取设置，网页上改了间隔/窗口立刻生效
                if self._settings is not None:
                    try:
                        latest = self._settings() or {}
                        interval = max(1, int(latest.get("interval", interval)))
                        window = latest.get("window", window)
                    except Exception:
                        pass
                self.current_interval = interval

                self.cycle += 1
                # 窗口写法有问题就先说清楚。注意必须独立判断，不能放在
                # 「不在窗口内」的分支里 —— 写法非法时 in_window() 一律返回 True，
                # 那个分支根本不会进，提醒就永远发不出来。
                problem = window_problem(window)
                if problem and problem not in self._window_warned:
                    self._window_warned.add(problem)
                    self._emit({"type": "log", "line": f"⚠ {problem}", "level": "warn"})
                if window and not in_window(window):
                    self._emit({"type": "log",
                                "line": f"[{time.strftime('%H:%M:%S')}] 不在时间窗口 {window} 内，等待"})
                else:
                    self._set_state("running")
                    try:
                        job(self.cycle, self._stop)
                    except Exception as exc:      # 单轮失败不应终止整个循环
                        self.last_error = str(exc)
                        self._emit({"type": "log", "line": f"⚠ 本轮异常: {exc}"})
                    if self._stop.is_set():
                        break
                if cycles and self.cycle >= cycles:
                    break
                self.next_run_at = time.time() + interval
                self._set_state("sleeping")
                if self._sleep(interval):
                    break
                self.next_run_at = 0.0
        finally:
            self.next_run_at = 0.0
            self.manual = False
            self._set_state("idle")

    def _sleep(self, seconds: float) -> bool:
        """分片睡眠；返回 True 表示需要退出。"""
        slept = 0.0
        while slept < seconds:
            if self._stop.is_set():
                return True
            if self._wake.is_set():
                self._wake.clear()
                if not self._stop.is_set():
                    self._emit({"type": "log", "line": "手动触发：立即开始下一轮"})
                return self._stop.is_set()
            time.sleep(min(0.5, seconds - slept))
            slept += 0.5
        return self._stop.is_set()


def parse_window(spec: str) -> tuple[int, int] | None:
    """解析 ``HH:MM-HH:MM``，返回 (起始分钟, 结束分钟)；空或非法返回 None。

    必须校验时/分范围：以前 ``25:00-07:00`` 这种写错的值会被算成
    start=1500 > end=420，于是永远判定"不在窗口内" —— 监控从此静默停摆，
    日志里只有一句"不在时间窗口内，等待"，看起来一切正常。
    """
    text = (spec or "").strip()
    if not text:
        return None
    start_s, sep, end_s = text.partition("-")
    if not sep:
        return None
    try:
        start_h, start_m = (int(x) for x in start_s.strip().split(":"))
        end_h, end_m = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, TypeError):
        return None
    for value, limit in ((start_h, 23), (end_h, 23)):
        if not 0 <= value <= limit:
            return None
    for value in (start_m, end_m):
        if not 0 <= value <= 59:
            return None
    return start_h * 60 + start_m, end_h * 60 + end_m


def window_problem(spec: str) -> str:
    """检查时间窗口写法，返回给用户看的提示；写法没问题时返回空串。"""
    text = (spec or "").strip()
    if not text:
        return ""
    parsed = parse_window(text)
    if parsed is None:
        return (f"时间窗口 {text!r} 写法无法识别（应为 HH:MM-HH:MM，例如 00:00-07:00）。"
                f"已忽略该限制，将全天运行")
    start, end = parsed
    if start == end:
        return (f"时间窗口 {text!r} 的起止时间相同，实际只有 1 分钟有效；"
                f"想全天运行请留空")
    return ""


def in_window(spec: str) -> bool:
    """判断当前时间是否落在 HH:MM-HH:MM 窗口内（支持跨零点）。

    写法无法识别时返回 True（继续跑）——宁可多跑，也不要因为一个笔误
    让监控悄无声息地停下。window_problem() 会给出提示。
    """
    parsed = parse_window(spec)
    if parsed is None:
        return True
    start, end = parsed
    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    if start <= end:
        return start <= minutes <= end
    return minutes >= start or minutes <= end
