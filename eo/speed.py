"""按「域名 SNI + 指定 IP」做下载测速（可限速），用来淘汰「能连上但带宽差」的节点。

为什么单独一个模块：
  * 延迟探测只做 TCP+TLS 握手、读一行状态行，几毫秒就结束；
    测速要真下载几 MB，耗时与带宽都是另一个数量级，混在一起会让延迟探测变慢。
  * 测速的失败（样本太小、限速太狠、服务端不返回大文件）不能算节点不可用 ——
    节点是活的，只是"测不出来"，所以结果单独记在 Probe.speed_mbps / speed_error。

口径说明（都很容易被误解，写在这里免得踩坑）：
  * 测速走的是**同样的钉 IP 请求**：连 ip:port，SNI/Host 用目标域名，
    所以测到的是"这个边缘节点回源取数据的真实吞吐"，不是本机到自己的速度。
  * 限速（limit_mbps）是**客户端主动放慢读取**：读够 n 字节后 sleep，
    让平均速率不超过设定值。因此高于限速的节点都会被测成"约等于限速"，
    限速只用来省带宽，不能用它区分"谁更快"；阈值要设在限速以下才有区分度。
  * 样本太小（服务端只回几 KB）时速度数字没有意义，直接判为"测不出"，
    避免把 200KB 的首页当成 200Mbps。
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Sequence

# 少于这个字节数就不值得相信：200KB 在本地回环上一瞬间就传完，
# 算出来的"速度"只反映缓冲区，不反映链路。
MIN_SAMPLE_BYTES = 128 * 1024

CHUNK = 64 * 1024

# 测速是唯一按流量计费的功能，几个上限必须硬编码，避免手误把带宽/流量打爆：
#   * 一次最多测 1000 个（speed_top=0 表示"全部"，但全量几万个 = 几百 GB）
#   * 单个 IP 最多测 30 秒、最多下载 100MB
MAX_CANDIDATES = 1000
MAX_SECONDS = 30.0
MAX_BYTES_MB = 100.0
HEADER_LIMIT = 32 * 1024


@dataclass
class SpeedResult:
    """单个 IP 的测速结果。"""

    ip: str
    ok: bool = False
    mbps: float = 0.0           # 实测平均速率（Mbps）
    bytes: int = 0              # 实际读到的 body 字节数
    seconds: float = 0.0        # 实际测量时长（不含握手）
    status: str = ""
    error: str = ""

    def line(self) -> str:
        if self.ok:
            return f"{self.ip} {self.mbps:.1f}Mbps（{self.bytes / 1024:.0f}KB / {self.seconds:.1f}s）"
        return f"{self.ip} 测速失败: {self.error}"


def speed_probe(
    ip: str,
    host: str,
    *,
    port: int = 443,
    scheme: str = "https",
    path: str = "/",
    seconds: float = 3.0,
    limit_mbps: float = 0.0,
    max_bytes: int = 6 * 1024 * 1024,
    timeout: float = 6.0,
    user_agent: str = "eo-optimizer/1.0",
    stop_event=None,
) -> SpeedResult:
    """下载测速：连 ip:port，SNI/Host 用 host，读最多 max_bytes 或 seconds 秒。

    返回的 mbps 是 body 的平均吞吐（不含握手时间）。
    """
    res = SpeedResult(ip=ip)
    started = time.monotonic()
    sock: socket.socket | None = None
    limit_bps = max(0.0, float(limit_mbps or 0.0)) * 1e6 / 8.0
    budget = max(0.05, float(seconds or 3.0))
    cap_bytes = max(MIN_SAMPLE_BYTES, int(max_bytes or 6 * 1024 * 1024))

    def fail(message: str) -> SpeedResult:
        res.ok = False
        res.error = message
        return res

    try:
        sock = socket.create_connection((ip, port), timeout=max(0.5, min(timeout, 5.0)))
        if scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False          # 等价 curl --insecure
            ctx.verify_mode = ssl.CERT_NONE
            try:                                # 兼容只支持较低安全等级的边缘节点
                ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            except ssl.SSLError:                # pragma: no cover
                pass
            sock.settimeout(max(0.5, min(timeout, 5.0)))
            sock = ctx.wrap_socket(sock, server_hostname=host)

        request = (
            f"GET {path or '/'} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {user_agent}\r\n"
            "Accept: */*\r\n"
            "Accept-Encoding: identity\r\n"     # 请求不压缩：压缩后的字节数不代表链路吞吐
            "Connection: close\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("latin-1", "replace"))

        # ---- 读响应头 ----
        buf = b""
        head_deadline = started + max(1.0, budget)
        while b"\r\n\r\n" not in buf and len(buf) < HEADER_LIMIT:
            if time.monotonic() > head_deadline:
                return fail("等待响应头超时")
            sock.settimeout(max(0.2, head_deadline - time.monotonic()))
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status_line = lines[0].decode("latin-1", "replace").strip() if lines else ""
        parts = status_line.split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            return fail(f"非 HTTP 响应: {status_line[:60] or '空'}")
        res.status = parts[1]
        if res.status not in ("200", "206"):
            return fail(f"HTTP {res.status}（测速需要一个能下载到大文件的地址）")

        declared: int | None = None
        for raw in lines[1:]:
            name, _, value = raw.decode("latin-1", "replace").partition(":")
            if name.strip().lower() == "content-length":
                try:
                    declared = int(value.strip())
                except ValueError:
                    declared = None
        if declared is not None and declared < MIN_SAMPLE_BYTES:
            return fail(f"响应只有 {declared / 1024:.0f}KB，不足以测速"
                        f"（把「测速路径」指向一个大文件）")

        total = len(rest)
        body_started = time.monotonic()
        while True:
            # 停止请求要能打断测速：否则一次 docker stop 得等满整个测速时长
            if stop_event is not None and stop_event.is_set():
                break
            elapsed = time.monotonic() - body_started
            if elapsed >= budget or total >= cap_bytes:
                break
            # 单次 read 最多等 2 秒：服务端中途卡住时也要能及时收尾（停止/退出更快）
            sock.settimeout(min(max(0.3, budget - elapsed), 2.0))
            try:
                chunk = sock.recv(CHUNK)
            except (socket.timeout, TimeoutError):
                # 读了半天不再有数据：按已读到的字节算（样本不足会判"测不出"），
                # 不要把"慢"直接当成"失败"
                break
            if not chunk:
                break
            total += len(chunk)
            if limit_bps > 0:
                # 客户端限速：按"已读字节对应的理想耗时"补 sleep
                want = total / limit_bps
                lag = want - (time.monotonic() - body_started)
                if lag > 0:
                    time.sleep(min(lag, 0.5))

        elapsed = max(1e-6, time.monotonic() - body_started)
        res.bytes = total
        res.seconds = elapsed
        res.mbps = total * 8.0 / elapsed / 1e6

        if total < MIN_SAMPLE_BYTES:
            return fail(f"只读到 {total / 1024:.0f}KB，样本太小无法判断速度"
                        f"（把「测速路径」指向一个大文件，或放宽限速/时长）")
        res.ok = True
        return res

    except (socket.timeout, TimeoutError):
        return fail(f"测速超时（{budget:g}s 内没能读到足够数据）")
    except ssl.SSLError as exc:
        return fail(f"TLS 失败: {exc}")
    except OSError as exc:
        return fail(f"连接失败: {exc}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# 批量测速：只测延迟最好的一批，结果写回 Probe
# --------------------------------------------------------------------------
def run_speed_tests(
    platform,
    probes: Sequence,
    *,
    log: Callable[[str], None] | None = None,
    quiet: bool = False,
    stop_event=None,
    limiter=None,
    prober: Callable[..., SpeedResult] | None = None,
) -> dict:
    """对延迟最好的前 speed_top 个 IP 测速，把结果写回每个 Probe。

    为什么只测前 N 个：全量可用 IP 可能有几万个，每个下载几 MB 会跑几天，
    而 DNS 只下发最快的一小批 —— 只测这一小批就够做淘汰。
    返回统计字典（tested/ok/below/failed/skipped）。
    """
    import concurrent.futures

    log = log or (lambda _m: None)
    prober = prober or speed_probe
    probes = [p for p in probes if getattr(p, "ok", True)]
    stats = {"tested": 0, "ok": 0, "below": 0, "failed": 0, "skipped": 0}

    top = int(getattr(platform, "speed_top", 0) or 0)
    if top <= 0:
        top = len(probes)
    if top > MAX_CANDIDATES:
        log(f"[{platform.name}] ⚠ 「只测前 N 个」={top} 超过硬上限，最多测 {MAX_CANDIDATES} 个"
            f"（避免一次测掉几百 GB 流量；需要更省就把 N 调小或设限速）")
        top = MAX_CANDIDATES

    # 只在「延迟门槛内」的 IP 里挑：超过门槛的反正不会被下发，
    # 测它纯属白烧流量。没设门槛时就是全部可用 IP。
    max_ms = float(getattr(platform, "max_latency_ms", 0) or 0)
    pool = probes
    trimmed = 0
    if max_ms > 0:
        pool = [p for p in probes if (p.latency_ms or 0) <= max_ms]
        trimmed = len(probes) - len(pool)
        if trimmed and not quiet:
            log(f"[{platform.name}] 延迟门槛 {max_ms:g}ms 内的 IP 只有 {len(pool)} 个，"
                f"其余 {trimmed} 个不测速（反正不会下发）")
    candidates = pool[:top]
    stats["skipped"] = max(0, len(probes) - len(candidates))
    if not candidates:
        return stats

    path = getattr(platform, "speed_path", "") or getattr(platform, "path", "") or "/"
    seconds = float(getattr(platform, "speed_seconds", 3.0) or 3.0)
    if seconds > MAX_SECONDS:
        log(f"[{platform.name}] ⚠ 单个 IP 测速时长 {seconds:g}s 超过上限，按 {MAX_SECONDS:g}s 执行")
        seconds = MAX_SECONDS
    limit = float(getattr(platform, "speed_limit_mbps", 0.0) or 0.0)
    bytes_mb = float(getattr(platform, "speed_bytes_mb", 6.0) or 6.0)
    if bytes_mb > MAX_BYTES_MB:
        log(f"[{platform.name}] ⚠ 单个 IP 最多下载 {bytes_mb:g}MB 超过上限，按 {MAX_BYTES_MB:g}MB 执行")
        bytes_mb = MAX_BYTES_MB
    max_bytes = int(bytes_mb * 1024 * 1024)
    min_speed = float(getattr(platform, "min_speed_mbps", 0.0) or 0.0)
    workers = max(1, min(int(getattr(platform, "speed_concurrency", 3) or 3), 16))

    if not quiet:
        log(f"[{platform.name}] 测速：延迟最好的前 {len(candidates)} 个 IP"
            f"（{platform.scheme}://{platform.host}{path}，每个最多 {seconds:g}s / "
            f"{max_bytes / 1024 / 1024:.1f}MB，限速 "
            f"{f'{limit:g}Mbps' if limit > 0 else '不限'}，并发 {workers}）")

    done = {"n": 0}
    total = len(candidates)
    step = max(1, total // 10)

    def work(probe) -> None:
        try:
            if limiter is not None:
                limiter.acquire()   # 测速也是一次真实 TCP 连接，计入全局限速
            got = prober(
                probe.ip,
                host=platform.host,
                port=getattr(platform, "port", 443),
                scheme=getattr(platform, "scheme", "https"),
                path=path,
                seconds=seconds,
                limit_mbps=limit,
                max_bytes=max_bytes,
                timeout=max(seconds + 3.0, float(getattr(platform, "timeout", 3.0) or 3.0)),
                stop_event=stop_event,
            )
            probe.speed_mbps = got.mbps if got.ok else None
            probe.speed_error = "" if got.ok else (got.error or "测速失败")
        except Exception as exc:                                # noqa: BLE001
            # 单个 IP 的意外不能带走整批测速（也不该让本轮扫描失败）
            probe.speed_mbps = None
            probe.speed_error = f"测速异常: {type(exc).__name__}: {exc}"

    try:
        chunk = max(workers * 2, 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending: set = set()
            iterator = iter(candidates)
            stopped = False
            while True:
                while not stopped and len(pending) < chunk:
                    try:
                        probe = next(iterator)
                    except StopIteration:
                        break
                    pending.add(pool.submit(work, probe))
                if not pending:
                    break
                done_set, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done_set:
                    future.result()
                    done["n"] += 1
                if not quiet and done["n"] % step == 0:
                    log(f"[{platform.name}] 测速进度 {done['n']}/{total}")
                if stop_event is not None and stop_event.is_set():
                    if not stopped:
                        stopped = True
                        log(f"[{platform.name}] 收到停止请求，测速收尾…")
                    for future in list(pending):
                        future.cancel()
                    pending = {f for f in pending if not f.cancelled()}
    except Exception as exc:                                # noqa: BLE001
        log(f"[{platform.name}] ⚠ 测速异常（{type(exc).__name__}: {exc}），本轮按未测速处理")

    for probe in candidates:
        stats["tested"] += 1
        speed = getattr(probe, "speed_mbps", None)
        if speed is None:
            stats["failed"] += 1
        else:
            stats["ok"] += 1
            if min_speed > 0 and speed < min_speed:
                stats["below"] += 1

    if not quiet or stats["tested"]:
        summary = (f"[{platform.name}] 测速完成：测了 {stats['tested']} 个，成功 {stats['ok']}，"
                   f"失败 {stats['failed']}")
        if min_speed > 0:
            summary += f"，低于 {min_speed:g}Mbps 的 {stats['below']} 个将在下发时丢弃"
        if stats["skipped"]:
            summary += f"；另有 {stats['skipped']} 个没测（只测前 {len(candidates)} 个）"
        log(summary)

    failed = [p for p in candidates if getattr(p, "speed_mbps", None) is None]
    if failed:
        # 这条必须"安静模式"也输出：测速全失败时用户唯一的线索就是这里
        reason = getattr(failed[0], "speed_error", "") or "未知原因"
        log(f"[{platform.name}] ⚠ 有 {len(failed)} 个测速没成功，首个原因：{reason}")
        if len(failed) == len(candidates):
            log(f"[{platform.name}]   提示：测速要能下载到**大文件**。"
                f"请在「平台设置 → 测速路径」填一个返回大文件的路径（例如 /10mb.bin），"
                f"否则只会读到首页那几 KB，永远测不出速度。")
    return stats


def median(values: Sequence[float]) -> float | None:
    """小工具：给未来做多次采样取中位数用（现在默认一次采样）。"""
    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


__all__ = ["SpeedResult", "speed_probe", "run_speed_tests", "median", "MIN_SAMPLE_BYTES"]
