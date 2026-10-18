"""命令行入口：统一编排「优选 -> 落盘 -> (可选) DNS 下发 -> (可选) 持续监控」。

常用示例（在项目根目录执行）：

    # 扫描全部已启用平台（EO/ESA/CFT 同时跑）
    python run.py

    # 只跑 EO 与 ESA
    python run.py scan -p eo esa

    # 扫描并把结果下发到各平台各自的 DNS 记录（危险操作需 --yes 确认）
    python run.py scan --dns --yes

    # 只预览要下发什么，不动线上解析
    python run.py scan --dns --dry-run

    # 持续监控：每 300 秒一轮，只在 00:00-07:00 生效，连续跑 10 轮
    python run.py monitor --interval 300 --cycles 10 --window 00:00-07:00

    # 单独诊断某个 IP 对某平台的可用性
    python run.py test -p eo --ip 43.174.247.25

    # 看平台清单
    python run.py list
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .core import (
    Platform,
    append_history,
    check_network_integrity,
    host_reachable,
    http_probe,
    load_targets,
    network_label,
    safe_log,
    scan_platform,
    scan_platforms,
    write_result,
)
from .platforms import PLATFORMS, get_platforms
from .ratelimit import make_rate_limiter
from .runner import CycleRunner, RunConfig, window_problem
from .runner import run_once as runner_run_once

MIN_IPS_WARN = 3

# 默认全平台合计连接速率上限（连接/秒）。服务器上通常还和别人共用出口，
# 所以默认就限速；用 --rate 0 可以关闭。
DEFAULT_RATE = 100.0


def looks_suspicious(outcome) -> bool:
    """判断一轮结果是否「好得不真实」。

    真实环境下，一个 /24 网段通常只有个位数节点为该域名提供服务，
    延迟也在几十到几百毫秒。若几乎全部通过且延迟只有 1~2ms，
    基本可以断定本机 HTTPS 被代理/透明网关接管，结果不可用。
    """
    if outcome.total < 50 or not outcome.passed:
        return False
    ratio = len(outcome.passed) / outcome.total
    if ratio < 0.95:
        return False
    sample = sorted(p.latency_ms for p in outcome.passed)
    median = sample[len(sample) // 2]
    return median < 5.0


# --------------------------------------------------------------------------
# 输出辅助（safe_log 在 core 中实现，避免 Windows GBK 控制台编码崩溃）
# --------------------------------------------------------------------------
def banner(text: str) -> None:
    safe_log("")
    safe_log("=" * 62)
    safe_log(text)
    safe_log("=" * 62)


# --------------------------------------------------------------------------
# 平台准备
# --------------------------------------------------------------------------
def prepare_platforms(args) -> list[Platform]:
    """按命令行参数取出平台列表，并应用通用覆盖项。"""
    keys = getattr(args, "platform", None)
    if not keys and os.environ.get("EO_PLATFORMS"):
        # 容器里常用环境变量指定平台，省得每次都写 -p
        keys = [k.strip() for k in os.environ["EO_PLATFORMS"].replace(",", " ").split() if k.strip()]
    include_disabled = getattr(args, "all_platforms", False)
    networks = getattr(args, "network", None)
    try:
        platforms = get_platforms(keys, include_disabled=include_disabled, networks=networks)
    except KeyError as exc:
        safe_log(str(exc))
        sys.exit(2)

    if networks:
        labels = "、".join(network_label(n) for n in networks)
        safe_log(f"已按运营商筛选：{labels}（共 {len(platforms)} 个平台变体）")

    if keys and networks and platforms:
        # -p 与 -n 同时给出且互相矛盾时，被筛掉的平台要明确提示，
        # 否则「少跑了一个平台」是无声的
        got = {p.key for p in platforms}
        dropped = sorted({k.strip() for k in keys if k.strip()} - got)
        if dropped:
            safe_log(f"提示：{'、'.join(dropped)} 与所选运营商不符，本轮已跳过。")

    if not platforms:
        if keys and networks:
            safe_log(f"没有匹配的平台：-p 指定的 {'、'.join(keys)} "
                     f"与运营商筛选（{'、'.join(network_label(n) for n in networks)}）不一致。")
        else:
            safe_log("没有匹配的平台。用 -p 指定平台、--network 指定运营商，")
            safe_log("或到 eo/platforms.py 把对应运营商的 enabled 打开。")
        sys.exit(2)

    overrides = {
        "workers": getattr(args, "workers", None),
        "timeout": getattr(args, "timeout", None),
        "ping_timeout_ms": getattr(args, "ping_timeout", None),
    }
    if getattr(args, "ping", False):
        overrides["precheck_ping"] = True
    if getattr(args, "retries", None) is not None:
        overrides["retries"] = args.retries
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "status", None):
        overrides["status_codes"] = tuple(args.status)

    for platform in platforms:
        for field, value in overrides.items():
            if value is not None:
                setattr(platform, field, value)
    return platforms


def load_target_override(platforms: list[Platform], args) -> list[str] | None:
    """--targets 指定网段文件时，所有平台共用该文件。

    文件不存在时直接退出：静默回退到平台默认网段会让用户误扫全量。
    """
    targets_file = getattr(args, "targets", None)
    if not targets_file:
        return None
    path = Path(targets_file)
    if not path.exists():
        safe_log(f"网段文件不存在: {path}")
        safe_log("  已中止（不会改用平台默认网段，避免误扫全量）")
        sys.exit(2)
    ips = load_targets(path)
    safe_log(f"使用统一网段文件 {path.name}，共 {len(ips)} 个 IP（所有平台共用）")
    return ips


# --------------------------------------------------------------------------
# scan / monitor 共用的一轮执行
# --------------------------------------------------------------------------
def build_run_config(args, platforms: list[Platform]) -> RunConfig:
    """把 CLI 参数翻译成 runner 的配置对象（Web 端复用同一套执行逻辑）。"""
    rate = getattr(args, "rate", None)
    return RunConfig(
        rate=DEFAULT_RATE if rate is None else rate,
        targets=getattr(args, "targets", None),
        timeout=getattr(args, "timeout", None),
        workers=getattr(args, "workers", None),
        retries=getattr(args, "retries", None),
        ping=bool(getattr(args, "ping", False)),
        host_override=getattr(args, "host", None),
        status=tuple(args.status) if getattr(args, "status", None) else None,
        with_latency=bool(getattr(args, "with_latency", False)),
        no_write=bool(getattr(args, "no_write", False)),
        write_json=getattr(args, "json", None),
        webhook=getattr(args, "webhook", None) or os.environ.get("EO_WEBHOOK"),
        notify_always=bool(getattr(args, "notify_always", False)),
        skip_check=bool(getattr(args, "skip_check", False)),
        dns_mode=dns_mode(args),
        dns_yes=bool(getattr(args, "yes", False)),
        dns_force=bool(getattr(args, "force", False)),
        quiet=bool(getattr(args, "quiet", False)),
        verbose=not getattr(args, "quiet", False),
        sample_per_24=getattr(args, "sample_per_24", None),
        deep_scan=bool(getattr(args, "deep", False)),
        max_latency_ms=_opt_number(getattr(args, "max_ms", None), "EO_MAX_MS", float),
        top_n=_opt_number(getattr(args, "top", None), "EO_TOP", int),
    )


def _opt_number(cli_value, env_key: str, caster):
    """命令行优先，其次环境变量；都没有返回 None（表示不覆盖平台配置）。"""
    if cli_value is not None:
        return caster(cli_value)
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return None
    try:
        return caster(float(raw))
    except ValueError:
        safe_log(f"{env_key}={raw!r} 不是数字，已忽略")
        return None


def run_once(platforms: list[Platform], args, round_no: int = 1,
             stop_event=None) -> list:
    """CLI 的一轮执行：组装配置后交给 runner，与 Web 端完全同源。"""
    quiet = bool(getattr(args, "quiet", False))

    # --targets 指定了文件但不存在时必须立刻停下：
    # 以前这里没做校验（runner 里是 if exists 静默跳过），结果是
    # 「以为只扫个小文件」→ 实际把平台默认的几千个 IP 全扫了。
    targets_arg = getattr(args, "targets", None)
    if targets_arg and not Path(targets_arg).exists():
        safe_log(f"网段文件不存在: {targets_arg}")
        safe_log("  已中止（不会改用平台默认网段，避免误扫全量）")
        sys.exit(2)

    config = build_run_config(args, platforms)

    if len(platforms) > 1 and not quiet:
        banner(f"第 {round_no} 轮：同时扫描 {len(platforms)} 个平台")
        for platform in platforms:
            safe_log(f"  - {platform.describe()}")

    alerts: list[str] = []
    dns_failures: list[str] = []

    def emit(event: dict) -> None:
        # CLI 只需要收集告警用于通知；日志由 runner 内部直接打印
        if event.get("type") == "dns" and not event.get("ok"):
            dns_failures.append(f"{event.get('platform', '?')}: {event.get('summary', '')}")
            return
        line = event.get("line", "") if event.get("type") == "log" else ""
        if line.startswith("  ⚠"):
            alerts.append(line.strip())

    started = time.perf_counter()
    outcomes = runner_run_once(platforms, config, emit=emit, stop_event=stop_event)
    elapsed = time.perf_counter() - started
    banner(f"第 {round_no} 轮结果（总耗时 {elapsed:.1f}s）")

    if config.webhook:
        from .notify import notify_alert, notify_outcomes

        if dns_failures:
            notify_alert(config.webhook, "DNS 下发失败", "\n".join(dns_failures), log=safe_log)
        elif alerts:
            notify_alert(config.webhook, "边缘节点优选异常", "\n".join(alerts), log=safe_log)
        elif config.notify_always:
            notify_outcomes(config.webhook, outcomes, log=safe_log)

    return outcomes


def dns_mode(args) -> str:
    """归一化 --dns-sync / --dns / --dry-run 三种写法。"""
    mode = getattr(args, "dns_sync", "none") or "none"
    if getattr(args, "dns", False):
        mode = "push"
    if getattr(args, "dry_run", False) and mode == "push":
        mode = "dry-run"
    return mode


def apply_dns(outcomes, args) -> None:
    """按平台把优选结果下发到 DNS。"""
    from .dns import sync_platform

    mode = dns_mode(args)
    dry_run = mode == "dry-run"
    confirmed = getattr(args, "yes", False)

    if not dry_run and not confirmed:
        safe_log("")
        safe_log("⚠ 即将修改线上 DNS 解析。确认无误请追加 --yes 重新执行，")
        safe_log("  或先用 --dry-run 预览将要下发的记录。")
        return

    banner("DNS 下发" + ("（dry-run 预览）" if dry_run else ""))
    for outcome in outcomes:
        platform = outcome.platform
        if not platform.dns:
            safe_log(f"跳过 {platform.name}: 未配置 dns")
            continue
        if outcome.error or not outcome.passed:
            safe_log(f"跳过 {platform.name}: 本轮没有可用结果，保持线上解析不变")
            continue
        if looks_suspicious(outcome):
            safe_log(f"跳过 {platform.name}: 结果可疑（几乎全量通过且延迟极低），"
                     f"疑似链路被接管，拒绝下发")
            continue
        if not platform.output_file:
            safe_log(f"跳过 {platform.name}: 未配置 output_file")
            continue
        result = sync_platform(platform, log=safe_log, dry_run=dry_run,
                               force=getattr(args, "force", False))
        safe_log(f"  -> {result.summary()}")
        if not result.ok:
            webhook = getattr(args, "webhook", None) or os.environ.get("EO_WEBHOOK")
            if webhook:
                from .notify import notify_alert

                notify_alert(webhook, f"{platform.name} DNS 下发失败", result.summary(),
                             log=safe_log)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_scan(args) -> int:
    platforms = prepare_platforms(args)
    outcomes = run_once(platforms, args)
    if not outcomes:
        # 没有产生任何结果（例如指定的网段文件不存在）—— 不能当成成功
        return 2
    if dns_mode(args) != "none":
        apply_dns(outcomes, args)

    failed = [o for o in outcomes if o.error]
    return 1 if failed and len(failed) == len(outcomes) else 0


class _Shutdown:
    """把 SIGTERM/SIGINT 变成「跑完当前这轮就退出」。

    Docker 里 ``docker stop`` 默认先发 SIGTERM，如果直接被杀掉，
    正在写的结果文件和正在下发的 DNS 记录可能处于中间状态，所以这里优雅退出。
    """

    def __init__(self) -> None:
        self.requested = False
        self._originals: dict[int, object] = {}
        self._warned = False

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._originals[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle)
            except (ValueError, OSError):   # 非主线程或平台不支持
                pass

    def restore(self) -> None:
        for sig, handler in self._originals.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def _handle(self, signum, _frame) -> None:
        self.requested = True
        if not self._warned:
            self._warned = True
            name = signal.Signals(signum).name
            safe_log(f"\n收到 {name}，本轮结束后退出（再按一次立即终止）")
            # 第二次信号恢复默认行为，避免卡住关不掉
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (ValueError, OSError):
                pass


def cmd_monitor(args) -> int:
    shutdown = _Shutdown()
    shutdown.install()
    try:
        return _monitor_loop(args, shutdown)
    finally:
        shutdown.restore()


def _monitor_loop(args, shutdown: _Shutdown) -> int:
    """持续监控：复用 runner.CycleRunner，与 Web 端同一套循环逻辑。"""
    platforms = prepare_platforms(args)
    interval = max(60, args.interval)
    cycle_count = 0

    safe_log(f"持续监控已启动：间隔 {interval}s，"
             f"{'无限循环（Ctrl+C 结束）' if not args.cycles else f'共 {args.cycles} 轮'}")

    def job(cycle_no: int, stop_event) -> None:
        # 时间窗口的判断交给 CycleRunner（window 参数），这里只跑一轮。
        # stop_event 必须透传：否则收到 SIGTERM / Ctrl+C 后本轮仍要把全部 IP
        # 扫完才退出（实测 7000+ 个 IP 时 20 秒都退不出来，docker stop 只能强杀）。
        run_once(platforms, args, round_no=cycle_no, stop_event=stop_event)
        if stop_event.is_set():
            safe_log("本轮未跑完（收到停止信号），已提前收尾")

    def emit(event: dict) -> None:
        # 循环层面的提示（例如「不在时间窗口 X 内，等待」）必须让用户看到：
        # 否则加了 --window 之后程序静静待着，用户无法判断是否正常。
        if event.get("type") == "log":
            line = event.get("line", "")
            if line:
                safe_log(line)

    runner = CycleRunner(emit=emit)
    # 时间窗口写法有问题就立刻说清楚，别等跑了一轮才发现
    _problem = window_problem(getattr(args, "window", "") or "")
    if _problem:
        safe_log(f"⚠ {_problem}")
    runner.start(job, interval=interval, cycles=args.cycles or 0, window=args.window or "")

    # 主线程等待；收到 SIGTERM/SIGINT 时请求停止（当前轮收尾后退出）。
    # 但不能无限等：docker stop 只给 10 秒宽限期，等不到就强杀。
    grace = _shutdown_grace()
    stop_deadline: float | None = None
    while runner.running:
        if shutdown.requested:
            if stop_deadline is None:
                runner.stop()
                stop_deadline = time.monotonic() + grace
            elif time.monotonic() >= stop_deadline:
                safe_log(f"等待当前这一轮收尾超过 {grace:g}s，先退出")
                break
        time.sleep(0.5)

    cycles_done = runner.cycle
    if shutdown.requested:
        _finish_after_signal(f"监控结束（已运行 {cycles_done} 轮）")
    safe_log(f"监控结束（已运行 {cycles_done} 轮）")
    return 0


def in_window(spec: str) -> bool:
    """判断当前时间是否落在 HH:MM-HH:MM 窗口内（支持跨零点）。"""
    try:
        start_s, _, end_s = spec.partition("-")
        start_h, start_m = (int(x) for x in start_s.strip().split(":"))
        end_h, end_m = (int(x) for x in end_s.strip().split(":"))
    except Exception:
        safe_log(f"时间窗口格式错误: {spec}（应为 00:00-07:00）")
        return True

    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start <= end:
        return start <= minutes <= end
    return minutes >= start or minutes <= end


def cmd_test(args) -> int:
    platforms = prepare_platforms(args)
    ips = args.ip or []
    if not ips:
        safe_log("请用 --ip 指定要诊断的 IP（可多个）")
        return 2

    exit_code = 0
    for platform in platforms:
        safe_log("")
        safe_log(f"平台 {platform.describe()}")
        for ip in ips:
            probe = http_probe(
                ip,
                host=platform.host,
                timeout=platform.timeout,
                port=platform.port,
                scheme=platform.scheme,
                path=platform.path,
                status_codes=platform.status_codes,
            )
            flag = "✅ 可用" if probe.ok else f"❌ {probe.error or probe.status}"
            safe_log(f"  {ip:<16} {flag:<18} connect={probe.connect_ms:.1f}ms total={probe.total_ms:.1f}ms")
            if not probe.ok:
                exit_code = 1
    return exit_code


def cmd_dns_check(args) -> int:
    """只读查看各平台 DNS 记录当前解析（不修改任何东西）。"""
    from .dns import DnsTarget, inspect

    platforms = prepare_platforms(args)
    exit_code = 0
    for platform in platforms:
        if not platform.dns:
            safe_log(f"跳过 {platform.name}: 未配置 dns")
            continue
        target = DnsTarget.from_dict(platform.dns, platform=platform.key)
        safe_log("")
        safe_log(f"== {platform.name} -> {target.normalized_name()} "
                 f"(zone={target.zone_name}, region={target.region}) ==")
        try:
            result = inspect(target, log=safe_log)
        except RuntimeError as exc:
            safe_log(f"  ✗ {exc}")
            exit_code = 1
            continue
        if result.ok:
            safe_log(f"  当前共 {len(result.deleted)} 个 IP")
        else:
            safe_log(f"  ✗ {'; '.join(result.errors)}")
            exit_code = 1
    return exit_code


def cmd_selftest(args) -> int:
    """离线自检：不需要网络，验证核心逻辑与 DNS 下发逻辑是否正确。"""
    from .core import expand_targets
    from .dns import DnsTarget, sync_records

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        safe_log(f"  {'✅' if condition else '❌'} {name}{(' -> ' + detail) if detail else ''}")
        if not condition:
            failures.append(name)

    banner("离线自检")

    # --- 包结构完整性（防止根目录同名文件遮蔽 eo/ 包） ---
    # 症状：ModuleNotFoundError: No module named 'eo.core'; 'eo' is not a package
    # 成因：项目根目录存在 eo.py 之类的同名文件，Python 会优先导入它而不是 eo/ 包。
    # 这个坑在 Docker 镜像里尤其致命（容器会反复重启），所以放进自检。
    try:
        import pathlib
        import eo as _eo_pkg
        _pkg_file = pathlib.Path(_eo_pkg.__file__)
        check("eo 是包（未被同名文件遮蔽）", _pkg_file.name == "__init__.py",
              f"实际解析到 {_pkg_file}")
        _shadows = [p.name for p in _pkg_file.parent.glob("eo.py")]
        check("根目录无 eo.py 冲突文件", not _shadows,
              f"发现 {_shadows}，请删除：它会遮蔽 eo/ 包")
    except Exception as exc:  # pragma: no cover - 仅在环境异常时触发
        check("eo 包可导入", False, f"{type(exc).__name__}: {exc}")

    # --- 网段解析 ---
    check("CIDR 解析", len(expand_targets("10.0.0.0/24")) == 254)
    check("单 IP 解析", expand_targets("1.2.3.4") == ["1.2.3.4"])
    check("起止区间解析",
          expand_targets("1.2.3.1-1.2.3.4") == ["1.2.3.1", "1.2.3.2", "1.2.3.3", "1.2.3.4"])
    check("注释行忽略", expand_targets("# 这是注释") == [])
    check("非法输入忽略", expand_targets("not-an-ip") == [])
    try:
        expand_targets("10.0.0.0/8")
        check("超大网段保护", False, "未抛出异常")
    except ValueError:
        check("超大网段保护", True)

    # --- DNS 下发逻辑（用假客户端，不碰线上） ---
    class _Links:
        next = None

    class _Set:
        def __init__(self, i, name, records):
            self.id, self.name, self.records = i, name, records

    class _Resp:
        def __init__(self, sets):
            self.recordsets, self.links = sets, _Links()

    class _Zone:
        zones = [type("Z", (), {"id": "zone-1"})()]

    class _Client:
        def __init__(self):
            self.updates, self.creates, self.deletes = [], [], []

        def list_public_zones(self, request):
            return _Zone()

        def list_record_sets(self, request):
            return _Resp([_Set("rs1", "t.1.060730.xyz.", ["1.1.1.1"] * 50),
                          _Set("rs2", "t.1.060730.xyz.", ["2.2.2.2"] * 50)])

        def update_record_set(self, request):
            self.updates.append((request.recordset_id, list(request.body.records)))

        def create_record_set_with_line(self, request):
            self.creates.append(list(request.body.records))

        def delete_record_set(self, request):
            self.deletes.append(request.recordset_id)

    target = DnsTarget(zone_name="1.060730.xyz", record_name="t.1.060730.xyz")
    client = _Client()
    ips = [f"10.0.0.{i}" for i in range(1, 141)]          # 140 个 -> 3 批
    result = sync_records(target, ips, log=lambda _: None, client=client)
    check("按批下发生效", result.ok and result.total_ips == 140, result.summary())
    check("优先就地更新（无空窗期）",
          [u[0] for u in client.updates] == ["rs1", "rs2"] and len(client.creates) == 1,
          f"updated={len(client.updates)} created={len(client.creates)}")

    guard = sync_records(target, ["10.0.0.1"], log=lambda _: None, client=_Client())
    check("解析量骤降保护", (not guard.ok) and "骤降保护" in guard.errors[0])
    forced = sync_records(target, ["10.0.0.1"], log=lambda _: None, client=_Client(), force=True)
    check("--force 可越过保护", forced.ok)
    empty = sync_records(target, [], log=lambda _: None, client=_Client())
    check("空列表拒绝下发", (not empty.ok) and "为空" in empty.errors[0])

    safe_log("")
    if failures:
        safe_log(f"自检未通过 {len(failures)} 项: {', '.join(failures)}")
        return 1
    safe_log("全部自检通过 ✅")
    return 0


def cmd_doctor(args) -> int:
    """环境体检：把影响优选准确性的因素逐项检查出来。"""
    from .core import curl_bin, ping

    banner("环境体检")
    problems: list[str] = []

    # 1) curl 可用性
    try:
        binary = curl_bin()
        safe_log(f"  ✅ curl: {binary}")
    except RuntimeError as exc:
        safe_log(f"  ❌ curl 不可用: {exc}")
        return 1

    # 2) 平台配置
    platforms = prepare_platforms(args)
    for platform in platforms:
        targets_path = Path(platform.targets_file) if platform.targets_file else None
        n_targets = len(load_targets(targets_path)) if targets_path and targets_path.exists() else 0
        if n_targets:
            safe_log(f"  ✅ 平台 {platform.name}: 网段 {n_targets} 个 IP，目标 {platform.host}")
        else:
            safe_log(f"  ⚠ 平台 {platform.name}: 网段文件为空或不存在（{platform.targets_file}）")
            problems.append(f"{platform.name} 没有可扫描的网段")

    # 3) 链路可信度（最关键）
    for platform in platforms:
        trusted, detail = check_network_integrity(
            platform.host, timeout=platform.timeout, port=platform.port,
            status_codes=platform.status_codes)
        if trusted:
            safe_log(f"  ✅ 链路可信（{platform.name}）：{detail}")
        else:
            safe_log(f"  ❌ 链路不可信（{platform.name}）：{detail}")
            problems.append(f"{platform.name} 所在链路被接管，扫描结果不可信")

    # 4) 目标域名用默认解析是否可达（host 填错的常见症状：返回 404/403）
    for platform in platforms:
        probe = host_reachable(platform.host, timeout=platform.timeout,
                               port=platform.port, status_codes=platform.status_codes)
        if probe.ok:
            safe_log(f"  ✅ 目标可达（{platform.name}）：{platform.host} -> HTTP {probe.status}"
                     f"（{probe.connect_ms:.0f}ms，默认解析到 {probe.remote_ip or '?'}）")
        else:
            safe_log(f"  ❌ 目标不可达（{platform.name}）：{platform.host} -> "
                     f"{probe.error or ('HTTP ' + probe.status)}")
            problems.append(f"{platform.name} 的 host 可能填错，或本机无法访问该域名")

    # 5) ICMP 是否可用（仅提示，因为默认不用）
    if platforms:
        if ping("223.5.5.5", 500):
            safe_log("  ✅ ICMP 可用（但默认不用于预筛选，因为对这些节点不可靠）")
        else:
            safe_log("  ⚠ ICMP 不可用（不影响默认流程；--ping 会因此失效）")

    safe_log("")
    if problems:
        safe_log(f"发现 {len(problems)} 个问题：")
        for item in problems:
            safe_log(f"  - {item}")
        return 1
    safe_log("体检通过：可以直接开始优选 ✅")
    return 0


def cmd_ranges(args) -> int:
    """自动拉取 CloudFront / Cloudflare 等平台的节点网段。"""
    from . import ranges as ranges_mod

    source = args.source
    if source == "list":
        safe_log(f"支持的自动拉取源: {', '.join(ranges_mod.SOURCES)}")
        safe_log("用法: python run.py ranges --source cloudfront --output targets/cft.txt")
        return 0

    if not args.output:
        safe_log("请用 --output 指定写入的文件，例如 --output targets/cft.txt")
        return 2
    try:
        ranges_mod.update_targets_file(source, Path(args.output), log=safe_log)
    except Exception as exc:
        safe_log(f"拉取失败: {exc}")
        return 1
    return 0


def cmd_esa_ranges(args) -> int:
    """发现 ESA（阿里云）境外网段：BGP 前缀 - 中国大陆。"""
    from . import esa_ranges

    asns = None
    if args.asn:
        asns = []
        for item in args.asn:
            for part in str(item).replace(",", " ").split():
                part = part.strip().upper().lstrip("AS")
                if part.isdigit():
                    asns.append(int(part))
        if not asns:
            safe_log("--asn 解析失败，示例：--asn 45102 24429")
            return 2

    banner("ESA 网段发现（阿里云 BGP 前缀 - 中国大陆）")
    try:
        result = esa_ranges.discover_esa_prefixes(asns=asns, log=safe_log,
                                                  timeout=args.timeout or 30.0)
    except Exception as exc:
        safe_log(f"发现失败: {exc}")
        return 1

    if args.show_dropped:
        safe_log("")
        safe_log(f"被剔除的中国大陆前缀（{len(result['dropped_china'])} 个，仅显示前 30 个）：")
        for prefix in result["dropped_china"][:30]:
            safe_log(f"  - {prefix}")

    if args.output:
        esa_ranges.write_targets(result, Path(args.output), log=safe_log)
        safe_log("")
        safe_log("下一步：")
        safe_log("  1) 把 eo/platforms.py 里 esa 的 host 改成你的 ESA 加速域名")
        safe_log("  2) python run.py doctor                     # 确认链路与 host 都正常")
        safe_log(f"  3) python run.py scan -p esa -q             # 开始优选")
    else:
        safe_log("")
        safe_log(f"（未指定 --output，仅预览）网段示例：{result['prefixes'][:10]}")
    return 0


def _shutdown_grace(default: float = 5.0) -> float:
    """停止时留给「当前这一轮」收尾的秒数。

    ``docker stop`` 默认只给 10 秒宽限期，超时就直接 SIGKILL（退出码 137）。
    留 5 秒给正在跑的一轮收尾，加上关闭服务本身的开销也在宽限期内。
    正在下发 DNS 的轮次如果来不及，可以调大本值并同时加大
    ``docker stop -t``（例如 EO_SHUTDOWN_GRACE=20 + docker stop -t 30）。
    """
    raw = os.environ.get("EO_SHUTDOWN_GRACE", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        safe_log(f"EO_SHUTDOWN_GRACE={raw!r} 不是数字，按默认 {default:g}s 处理")
        return default
    return max(0.0, min(value, 60.0))


def _finish_after_signal(message: str) -> None:
    """信号触发的退出路径：把最后一行日志打完就退出进程。

    为什么不能正常返回让解释器收尾：``concurrent.futures`` 注册了 atexit 钩子，
    解释器退出时会去 join 线程池里的工作线程。收到停止请求时那些线程可能还在
    探测（每次连接最长 timeout 秒），于是又要多等好几秒 —— 加起来就超过
    ``docker stop`` 的宽限期被 SIGKILL（退出码 137），前功尽弃。
    这里把该说的话说完、刷掉缓冲就退出，耗时可控。
    """
    safe_log(message)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:                            # noqa: BLE001
        pass
    os._exit(0)


class _WebShutdown:
    """把 SIGTERM/SIGINT 变成「停止循环 -> 关闭 HTTP 服务 -> 干净退出」。

    容器里 PID 1 就是本进程，``docker stop`` 发的 SIGTERM 直接送到这里。
    只捕获 KeyboardInterrupt 是不够的：SIGTERM 默认行为是立即终止进程，
    正在写的结果文件和正在下发的 DNS 记录会停在中间状态。
    """

    def __init__(self, service, server, grace: float = 8.0) -> None:
        self.service = service
        self.server = server
        self.grace = grace
        self.requested = False
        self._originals: dict[int, object] = {}
        self._warned = False
        self._lock = threading.Lock()
        self._closing = False
        self._closed = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._originals[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle)
            except (ValueError, OSError):   # 非主线程或平台不支持
                pass

    def restore(self) -> None:
        for sig, handler in self._originals.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def _handle(self, signum, _frame) -> None:
        self.requested = True
        if not self._warned:
            self._warned = True
            safe_log(f"\n收到 {signal.Signals(signum).name}，正在优雅退出…")
        # 信号处理是在主线程里打断 serve_forever() 执行的，
        # 不能在这里直接调用 server.shutdown()：它会阻塞等待 serve_forever
        # 返回，而主线程还停在处理函数里 —— 直接死锁。交给后台线程做。
        threading.Thread(target=self.close, daemon=True).start()

    def close(self) -> None:
        """幂等收尾：停止循环、关闭 HTTP 服务、给当前这轮留出收尾时间。"""
        with self._lock:
            if self._closing:
                self._closed.wait(timeout=self.grace + 5)
                return
            self._closing = True
        try:
            try:
                self.service.runner.stop()          # 请求停止（不阻塞）
            except Exception as exc:                # noqa: BLE001
                safe_log(f"停止循环时出错: {type(exc).__name__}: {exc}")
            try:
                self.server.shutdown()              # 让 serve_forever() 返回
            except Exception as exc:                # noqa: BLE001
                safe_log(f"关闭 Web 服务时出错: {type(exc).__name__}: {exc}")
            try:
                if not self.service.runner.wait(self.grace):
                    safe_log(f"等待当前这一轮收尾超过 {self.grace:g}s，先退出")
            except Exception as exc:                # noqa: BLE001
                safe_log(f"等待循环结束时出错: {type(exc).__name__}: {exc}")
        finally:
            self._closed.set()
        if self.requested:
            # 收尾已完成，别再让解释器去 join 线程池（见 _finish_after_signal）
            _finish_after_signal("Web 控制台已关闭")


def cmd_web(args) -> int:
    """启动 Web 控制台（配置管理 / 进度 / 实时日志）。"""
    from .webui import WebServer, make_service

    service = make_service(Path(args.config) if getattr(args, "config", None) else None)
    host = args.host_bind or os.environ.get("EO_WEB_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("EO_WEB_PORT", "8787"))
    auth_on = bool(service.store.get().web_auth)
    token = service.store.web_token

    banner("Web 控制台已启动")
    safe_log(f"  监听地址   : http://{host}:{port}")
    if auth_on:
        safe_log(f"  访问令牌   : {token}")
        safe_log(f"  带令牌访问 : http://<服务器IP>:{port}/?token={token}")
    else:
        safe_log("  访问令牌   : 已关闭（EO_WEB_AUTH=off）")
        safe_log("  ⚠ 任何能访问该端口的人都能改配置、触发扫描、下发 DNS —— 仅限可信内网")
        safe_log(f"  直接访问   : http://<服务器IP>:{port}/")
    safe_log(f"  配置文件   : {service.store.path}")
    if auth_on:
        safe_log("  提示：令牌也写在配置文件里（config/web_token），可随时更换。")

    if service.store.get().auto_run:
        ok, message = service.start()
        safe_log(f"  自动启动循环: {message if not ok else '已启动'}")

    server = WebServer(service, host=host, port=port)
    grace = _shutdown_grace()
    shutdown = _WebShutdown(service, server, grace=grace)
    shutdown.install()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass                       # 已由 _WebShutdown 处理，这里只是兜底
    finally:
        shutdown.restore()
        shutdown.close()           # 幂等：确保循环与 HTTP 服务都已收尾
    if not shutdown.requested:
        # 信号触发的那条路径由 _finish_after_signal 负责打印并退出，
        # 这里再打一次会两个线程交叉输出（日志粘成一行）
        safe_log("Web 控制台已关闭")
    return 0


def cmd_list(args) -> int:
    show_all = args.all_platforms
    networks = [n.lower() for n in (getattr(args, "network", None) or [])]
    safe_log("")
    safe_log(f"{'KEY':<9} {'名称':<16} {'运营商':<7} {'状态':<6} 目标")
    safe_log("-" * 78)
    for key, platform in PLATFORMS.items():
        if not platform.enabled and not show_all:
            continue
        if networks and (platform.network or "lt") not in networks:
            continue
        state = "启用" if platform.enabled else "未启用"
        net = network_label(platform.network) or "—"
        safe_log(f"{key:<9} {platform.name:<16} {net:<7} {state:<6} "
                 f"{platform.scheme}://{platform.host}:{platform.port}{platform.path}")
        record = (platform.dns or {}).get("record_name")
        if record:
            safe_log(f"{'':<9} └ DNS: {record}")
    safe_log("")
    safe_log("用法提示：")
    safe_log("  python run.py scan --network lt        # 只跑联通那一套（在联通机器上执行）")
    safe_log("  python run.py scan -p eo-lt esa-lt     # 指定具体变体")
    safe_log("  python run.py list --all-platforms     # 连未启用的也列出来")
    safe_log("")
    safe_log("★ 运营商不是代码区分的，而是「在哪台机器上跑」：延迟必须从该运营商网络内部测。")
    safe_log("  联通机 -> --network lt 下发 eo-lt；电信机 -> --network dx 下发 eo-dx；移动机同理。")
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------
def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--platform", nargs="+", metavar="KEY",
                        help=f"要处理的平台，可多选；默认全部已启用（{', '.join(PLATFORMS)}）")
    parser.add_argument("-n", "--network", nargs="+", metavar="NET",
                        help="按运营商筛选：lt=联通 / dx=电信 / yd=移动，可多选（如 -n lt dx）")
    parser.add_argument("--all-platforms", action="store_true",
                        help="包含配置里 enabled=False 的平台")
    parser.add_argument("--targets", metavar="FILE",
                        help="统一指定网段文件，覆盖各平台自己的 targets_file")
    parser.add_argument("--host", help="覆盖平台的校验域名（临时调试用）")
    parser.add_argument("--status", nargs="+", help="可接受的 HTTP 状态码，如 --status 200 301")
    parser.add_argument("-w", "--workers", type=int,
                        help="并发数，默认取平台配置（80，实测比 50 快约 35%% 且检出量相同）")
    parser.add_argument("--timeout", type=float, help="单次 HTTPS 探测超时（秒）")
    parser.add_argument("--ping-timeout", type=int, help="ICMP 预筛选阈值（毫秒）")
    parser.add_argument("--retries", type=int, help="探测失败后的重试次数，默认取平台配置（1）")
    parser.add_argument("--sample-per-24", type=int, metavar="N",
                        help="粗扫抽样：每个 /24 只取 N 个代表 IP（ESA 这类稀疏大网段必备，"
                             "如 --sample-per-24 4 可把 1400 万 IP 压到 22 万）")
    parser.add_argument("--deep", action="store_true",
                        help="深扫：以上一轮结果命中的 /24 为范围做全量扫描")
    parser.add_argument("--ping", action="store_true",
                        help="启用 ICMP 预筛选（默认关闭：这些节点 ICMP 响应不可靠，开启会漏掉可用 IP）")
    parser.add_argument("--rate", type=float, metavar="N",
                        help=f"全平台合计 TCP 连接速率上限（连接/秒），默认 {DEFAULT_RATE:g}；0 表示不限速")
    parser.add_argument("--max-ms", type=float, metavar="MS",
                        help="DNS 下发门槛：延迟超过这个毫秒数的节点不下发"
                             "（结果文件仍保留全部可用 IP）")
    parser.add_argument("--top", type=int, metavar="N",
                        help="DNS 下发只取延迟最低的 N 个节点"
                             "（结果文件仍保留全部可用 IP）")
    parser.add_argument("--with-latency", action="store_true", help="结果文件写成 ip<TAB>延迟")
    parser.add_argument("--no-write", action="store_true", help="不写结果文件与历史记录")
    parser.add_argument("--skip-check", action="store_true",
                        help="跳过链路可信度自检（可信网络里可省这一次探测）")
    parser.add_argument("--json", metavar="FILE", help="额外输出 JSON 汇总")
    parser.add_argument("--webhook", metavar="URL",
                        help="异常时发送通知（飞书/企业微信/通用 webhook；也可用环境变量 EO_WEBHOOK）")
    parser.add_argument("--notify-always", action="store_true",
                        help="每一轮都发送通知，而不只在异常时发送")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出汇总，不逐个打印")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="多平台边缘节点优选：EO / ESA / CFT 等可同时监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="扫描一轮（默认命令）")
    add_common_options(scan)
    scan.add_argument("--dns-sync", choices=["none", "push", "dry-run"], default="none",
                      help="扫描后是否下发 DNS：none(默认)/dry-run(预览)/push(执行)")
    scan.add_argument("--dns", action="store_true", help="等价于 --dns-sync push（兼容写法）")
    scan.add_argument("--dry-run", action="store_true", help="等价于 --dns-sync dry-run")
    scan.add_argument("--yes", action="store_true", help="确认执行 DNS 修改")
    scan.add_argument("--force", action="store_true", help="忽略「解析量骤降保护」，允许大幅缩容")
    scan.set_defaults(func=cmd_scan)

    monitor = subparsers.add_parser("monitor", help="持续多轮监控")
    add_common_options(monitor)
    monitor.add_argument("--dns-sync", choices=["none", "push", "dry-run"], default="none",
                         help="每轮结束后是否下发 DNS：none(默认)/dry-run(预览)/push(执行)")
    monitor.add_argument("--dns", action="store_true", help="等价于 --dns-sync push（兼容写法）")
    monitor.add_argument("--dry-run", action="store_true", help="等价于 --dns-sync dry-run")
    monitor.add_argument("--yes", action="store_true", help="确认执行 DNS 修改")
    monitor.add_argument("--force", action="store_true", help="忽略「解析量骤降保护」，允许大幅缩容")
    monitor.add_argument("--interval", type=int, default=300, help="轮询间隔秒数（最小 60），默认 300")
    monitor.add_argument("--cycles", type=int, default=0,
                         help="运行轮数，0 表示无限。注意：配合 --window 时，"
                              "窗口外的等待也算一轮（否则在窗口外启动会干等十几小时）")
    monitor.add_argument("--window", metavar="HH:MM-HH:MM",
                         help="仅在指定时间窗口内执行（例如 00:00-07:00，支持跨零点）；"
                              "写法无法识别时会被忽略并全天运行")
    monitor.set_defaults(func=cmd_monitor)

    test = subparsers.add_parser("test", help="诊断指定 IP 对某平台的可用性")
    add_common_options(test)
    test.add_argument("--ip", nargs="+", required=True, help="要诊断的 IP")
    test.set_defaults(func=cmd_test)

    listing = subparsers.add_parser("list", help="列出所有平台配置")
    listing.add_argument("--all-platforms", action="store_true", help="包含未启用的平台")
    listing.add_argument("-n", "--network", nargs="+", metavar="NET",
                         help="只看某个运营商：lt=联通 / dx=电信 / yd=移动")
    listing.set_defaults(func=cmd_list)

    dns_check = subparsers.add_parser("dns-check", help="只读查看各平台 DNS 记录当前解析")
    add_common_options(dns_check)
    dns_check.set_defaults(func=cmd_dns_check)

    selftest = subparsers.add_parser("selftest", help="离线自检（不需要网络）")
    selftest.set_defaults(func=cmd_selftest)

    doctor = subparsers.add_parser("doctor", help="环境体检：检查链路是否可信、配置是否可用")
    add_common_options(doctor)
    doctor.set_defaults(func=cmd_doctor)

    web = subparsers.add_parser("web", help="启动 Web 控制台（配置/进度/实时日志）")
    web.add_argument("--port", type=int, default=0, help="监听端口，默认 8787（或 EO_WEB_PORT）")
    web.add_argument("--host-bind", metavar="ADDR", help="监听地址，默认 0.0.0.0（或 EO_WEB_HOST）")
    web.add_argument("--config", metavar="FILE", help="配置文件路径，默认 config/config.json")
    web.set_defaults(func=cmd_web)

    ranges_parser = subparsers.add_parser("ranges", help="拉取平台官方网段（CloudFront/Cloudflare）")
    ranges_parser.add_argument("--source", default="list",
                               help="数据源：cloudfront / cloudflare / list")
    ranges_parser.add_argument("--output", metavar="FILE", help="写入哪个文件，如 targets/cft.txt")
    ranges_parser.set_defaults(func=cmd_ranges)

    esa = subparsers.add_parser(
        "esa-ranges",
        help="发现 ESA 网段（阿里云 BGP 前缀，自动排除中国大陆）")
    esa.add_argument("--output", metavar="FILE", default="targets/esa.txt",
                     help="写入哪个文件，默认 targets/esa.txt")
    esa.add_argument("--asn", nargs="+", metavar="ASN",
                     help="指定 ASN，如 --asn 45102 24429；默认自动搜索阿里云全部 ASN")
    esa.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数")
    esa.add_argument("--show-dropped", action="store_true", help="打印被剔除的中国大陆前缀")
    esa.set_defaults(func=cmd_esa_ranges)

    return parser


# 所有子命令名。默认命令是 scan：`python run.py -p eo` 等价于
# `python run.py scan -p eo`（README 里就是这么写的，属于对外承诺的用法）。
SUBCOMMANDS = ("scan", "monitor", "test", "list", "dns-check", "selftest", "doctor",
               "web", "ranges", "esa-ranges")
DEFAULT_COMMAND = "scan"


def normalize_argv(argv: list[str]) -> list[str]:
    """把「省略子命令」的写法补全，例如 ``-p eo`` -> ``scan -p eo``。

    抽成函数是为了让测试走同一条路径：直接对着 build_parser() 调 parse_args
    会绕过这里，从而把文档里本来可用的命令误判成不可用。
    """
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return [DEFAULT_COMMAND, *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> int:
    argv = normalize_argv(list(sys.argv[1:] if argv is None else argv))

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        safe_log("\n已中断")
        return 130
