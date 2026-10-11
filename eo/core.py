"""平台无关的 IP 优选核心引擎。

思路与原 eo.py 保持一致，但把「目标站点」抽离成平台配置：

1. 展开 CIDR -> 待测 IP 列表；
2. (可选) ping 预筛选，先剔除高延迟/不可达的 IP，减少后续 TLS 探测开销；
3. 用 curl 的 --resolve 把目标域名强制解析到该 IP，发起真实 HTTPS 请求，
   校验 HTTP 状态码，并记录连接耗时/总耗时（真实可用延迟）；
4. 并发执行，收集通过校验的 IP。

平台之间只是「目标域名 / 期望状态码 / 端口」不同，因此同一批 IP 可以
同时为多个平台做优选。
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .ratelimit import NullRateLimiter, make_rate_limiter

IS_WINDOWS = platform.system().lower() == "windows"


# 单个 CIDR 最大展开数量，避免误填 0.0.0.0/0 之类的网段时把内存打爆。
# 注意：这个上限只管「全量展开」路径；大网段请用 --sample-per-24 抽样，
# 抽样是按网段直接生成代表 IP，不受此限制。
MAX_EXPAND_HOSTS = 1_048_576

# 关闭 Windows 下 curl 弹出的控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# 运营商代号与中文名
NETWORK_LABELS = {
    "lt": "联通",
    "dx": "电信",
    "yd": "移动",
    "dx-lt": "电信联通",
}


def network_label(code: str) -> str:
    return NETWORK_LABELS.get(code, code or "")


def safe_log(message: str = "") -> None:
    """绝对不会因为控制台编码（Windows GBK）而崩溃的 print。

    Python 在管道/重定向下默认用 ANSI 代码页（GBK）编码，中文没问题，
    但 ✅ ❌ 这类符号会抛 UnicodeEncodeError 并中断整个优选流程 —— 所以在这里兜底。
    """
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


# --------------------------------------------------------------------------
# 1. 目标网段解析
# --------------------------------------------------------------------------
def expand_targets(line: str) -> list[str]:
    """解析一行目标：支持单个 IP、CIDR 网段、以及 ``起始-结束`` 区间。

    非法内容返回空列表（由调用方决定是否告警）。
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return []

    # 起止区间写法：43.152.135.1-43.152.135.254
    if "-" in text:
        start_s, _, end_s = text.partition("-")
        try:
            start = ipaddress.ip_address(start_s.strip())
            end = ipaddress.ip_address(end_s.strip())
        except ValueError:
            pass
        else:
            if start.version != end.version or int(end) < int(start):
                return []
            count = int(end) - int(start) + 1
            if count > MAX_EXPAND_HOSTS:
                raise ValueError(f"区间过大（{count} 个地址）: {text}")
            return [str(ipaddress.ip_address(int(start) + i)) for i in range(count)]

    try:
        # strict=False：容忍 43.152.135.10/24 这类主机位非零的写法
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return []

    if net.num_addresses == 1:
        return [str(net.network_address)]

    hosts = [str(ip) for ip in net.hosts()]
    if len(hosts) > MAX_EXPAND_HOSTS:
        raise ValueError(f"网段过大（{len(hosts)} 个地址）: {text}")
    return hosts


def load_targets(path: Path, sample_per_24: int = 0) -> list[str]:
    """读取 txt 文件，返回去重且保持顺序的 IP 列表。

    sample_per_24 > 0 时按网段直接抽样（走 sample_targets_file，不会先全量展开）。
    默认 0 = 全量展开，此时遇到超大网段会抛错；有 /15 这种大段的平台请开抽样。
    """
    if sample_per_24 and sample_per_24 > 0:
        return sample_targets_file(path, sample_per_24)

    seen: set[str] = set()
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        for ip in expand_targets(raw):
            if ip not in seen:
                seen.add(ip)
                result.append(ip)
    return result


# 每 /24 抽样时取第几个地址（1 开始，取 4 个点覆盖网段前中后）
SAMPLE_OFFSETS = (1, 64, 128, 192)


def sample_network(network: ipaddress.IPv4Network, per_24: int) -> list[str]:
    """对单个网段按「每 /24 取 per_24 个代表 IP」抽样。

    阿里云一个 /16 有 65536 个地址，真正跑 CDN 的只有极少数，
    全量扫一遍要几十小时。抽样先把「哪几个 /24 有节点」找出来，
    再对命中的 /24 做深扫（见 run.py scan --deep）。
    """
    per_24 = max(1, min(int(per_24), 254))
    offsets = SAMPLE_OFFSETS[:per_24] if per_24 <= len(SAMPLE_OFFSETS) else None

    def picks_in(net24: ipaddress.IPv4Network) -> list[str]:
        base = int(net24.network_address)
        if offsets is None:
            step = max(1, 256 // per_24)
            chosen = [base + min(255, i * step + 1) for i in range(per_24)]
        else:
            chosen = [base + off for off in offsets]
        host_count = net24.num_addresses - 2
        return [str(ipaddress.IPv4Address(pick)) for pick in chosen[:max(1, host_count)]]

    # /24 及以上：每个 /24 取 per_24 个代表 IP
    if network.prefixlen <= 24:
        if network.prefixlen == 24:
            return picks_in(network)
        result: list[str] = []
        for block in network.subnets(new_prefix=24):
            result.extend(picks_in(block))
        return result

    # 比 /24 小（/25 ~ /32）：整段保留。
    # 这类网段已经是深扫/精扫范围，再抽样会把目标 IP 丢掉。
    if network.num_addresses == 1:
        return [str(network.network_address)]
    hosts = [str(ip) for ip in network.hosts()]
    return hosts or [str(network.network_address)]


def sample_targets(ips: Sequence[str], per_24: int) -> list[str]:
    """对一组目标（IP 或网段混排）抽样，每 /24 取 per_24 个代表。

    ⚠ 对 ESA 这种上千万地址的网段，请用 sample_targets_file —— 它直接读网段文件抽样，
    不需要先把几千万个 IP 物化出来（那一步光展开就要 20 秒、占几个 GB 内存），
    而且展开后的 IP 已经丢失了「原本属于哪个大网段」的信息，抽样语义会退化。

    本函数只接受「网段 + 少量零散 IP」形式的输入；输入条数过多会直接报错，
    提示改用 sample_targets_file。

    语义（与 sample_targets_file 保持一致）：
      * /24 及更小的网段：整个保留（深扫阶段不能再丢目标 IP）；
      * 大网段（小于 /24 的前缀）：切成 /24，逐个 /24 取代表 IP；
      * 零散单 IP：归到它所在的 /24，只保留落在代表集合里的那些。
    """
    items = [text.strip() for text in ips if text and text.strip()]
    # 已经展开成几万个 IP 的输入无法再正确抽样：展开后丢失了「原本属于哪个大网段」，
    # 会被当成一堆 /32 处理。这种情况必须走 sample_targets_file。
    if len(items) > 4096:
        raise ValueError(
            f"sample_targets 收到 {len(items)} 条输入，已经是被展开的 IP 列表，"
            f"无法正确抽样。请改用 sample_targets_file(文件路径, N) 直接按网段抽样。"
        )

    from .core import SAMPLE_OFFSETS as _OFFSETS

    per_24 = max(1, min(int(per_24), 254))
    offsets = _OFFSETS[:per_24] if per_24 <= len(_OFFSETS) else None

    blocks: list[ipaddress.IPv4Network] = []
    seen_blocks: set[str] = set()
    singles: set[str] = set()

    def add_block(block: ipaddress.IPv4Network) -> None:
        key = str(block)
        if key not in seen_blocks:
            seen_blocks.add(key)
            blocks.append(block)

    for text in items:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue

        if network.prefixlen == 32:
            singles.add(str(network.network_address))
        elif network.prefixlen == 24:
            add_block(network)                     # /24：整段保留
        elif network.prefixlen > 24:
            add_block(network)                     # 比 /24 小：整段保留
        else:
            for block in network.subnets(new_prefix=24):
                add_block(block)

    result: list[str] = []
    seen: set[str] = set()

    for block in blocks:
        for candidate in sample_network(block, per_24):
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)

    # 零散单 IP：只有当它正好是该 /24 的代表 IP 时才保留，保证与网段路径一致
    for text in singles:
        address = ipaddress.ip_address(text)
        block = ipaddress.ip_network(f"{address}/24", strict=False)
        if text in set(sample_network(block, per_24)) and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def sample_targets_file(path: Path, per_24: int, log: Callable[[str], None] | None = None) -> list[str]:
    """按网段文件直接抽样（不经过全量展开）。

    对 targets/esa.txt 这种 2000+ 个网段、上千万地址的文件，
    这个函数比先 load_targets 再 sample_targets 快一个数量级、也省内存。

    文件不存在时抛 FileNotFoundError，由调用方转成带指引的扫描错误
    （ESA / CFT 默认走抽样，这条路径很容易被触发，不能裸崩）。
    """
    if not path.exists():
        raise FileNotFoundError(f"网段文件不存在: {path}")

    seen: set[str] = set()
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        # 起止区间先展开（区间通常不大），再抽样
        base_items = expand_targets(text) if "-" in text else [text]
        for item in base_items:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                continue
            if network.version != 4:
                continue
            for candidate in sample_network(network, per_24):
                if candidate not in seen:
                    seen.add(candidate)
                    result.append(candidate)
    if log:
        log(f"抽样完成：{result.__len__()} 个待测 IP")
    return result


def parent_24s(ips: Sequence[str]) -> list[str]:
    """把 IP 列表还原成它所在的 /24 网段（用于深扫命中区域）。"""
    networks: list[str] = []
    seen: set[str] = set()
    for text in ips:
        try:
            network = ipaddress.ip_network(f"{text.strip()}/24", strict=False)
        except ValueError:
            continue
        value = str(network)
        if value not in seen:
            seen.add(value)
            networks.append(value)
    return networks


# --------------------------------------------------------------------------
# 2. curl 定位
# --------------------------------------------------------------------------
def resolve_curl() -> str:
    """定位可用的 curl 可执行文件。

    Windows 自带 C:\\Windows\\System32\\curl.exe；注意 PowerShell 里 ``curl``
    是 Invoke-WebRequest 的别名，所以这里必须找到真正的 curl.exe。
    """
    env_curl = os.environ.get("EO_CURL")
    if env_curl and Path(env_curl).exists():
        return env_curl

    if IS_WINDOWS:
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if candidate.exists():
            return str(candidate)

    found = shutil.which("curl") or shutil.which("curl.exe")
    if found:
        return found
    raise RuntimeError("未找到 curl，可设置环境变量 EO_CURL 指向 curl.exe")


_CURL_BIN: str | None = None


def curl_bin() -> str:
    global _CURL_BIN
    if _CURL_BIN is None:
        _CURL_BIN = resolve_curl()
    return _CURL_BIN


# --------------------------------------------------------------------------
# 3. 探测
# --------------------------------------------------------------------------
@dataclass
class Probe:
    """单个 IP 的探测结果。"""

    ip: str
    ok: bool = False
    status: str = ""
    connect_ms: float = 0.0
    total_ms: float = 0.0
    latency_ms: float = 0.0
    error: str = ""
    remote_ip: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _stderr_hint(proc) -> str:
    """从 curl 的 stderr 里提取一行像是原因的信息（TLS 告警等）。"""
    for line in (proc.stderr or "").splitlines():
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(key in lowered for key in ("alert", "certificate", "ssl", "tls",
                                          "refused", "timeout", "reset", "resolve")):
            return text[:140]
    return ""


def ping(ip: str, timeout_ms: int = 300) -> bool:
    """ICMP 预筛选。Windows/Linux 参数不同，注意 -w 单位差异。"""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        success_marker = "TTL="
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(round(timeout_ms / 1000)))), ip]
        success_marker = ""

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout_ms / 1000 + 1.5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return False

    if proc.returncode != 0:
        return False
    if success_marker:
        return success_marker in (proc.stdout or "").upper()
    return True


_WRITE_OUT = re.compile(
    r"^(?P<code>\d{3})\s+(?P<connect>[\d.]+)\s+(?P<total>[\d.]+)\s*(?P<remote>[0-9a-fA-F:.]*)$"
)


def host_reachable(host: str, timeout: float = 3.0, port: int = 443,
                   status_codes: Sequence[str] = ("200",)) -> Probe:
    """用【默认 DNS 解析】访问目标域名，不做 IP 钉定。

    用来判断 host 是否填对：host 配错时这里会拿到 404/403。
    注意此函数不走 --resolve，因此不能拿来做优选，只能做体检。
    """
    result = Probe(ip=host)
    scheme = "https" if port == 443 else "http"
    url = f"{scheme}://{host}:{port}/" if port not in (80, 443) else f"{scheme}://{host}/"

    cmd = [
        curl_bin(), "-s", "-o", os.devnull if IS_WINDOWS else "/dev/null",
        "-w", "%{http_code} %{time_connect} %{time_total} %{remote_ip}",
        "--max-time", f"{timeout:g}",
        "--connect-timeout", f"{timeout:g}",
        "--http1.1", "--insecure", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                              timeout=timeout + 3, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        result.error = "timeout"
        return result
    except Exception as exc:  # pragma: no cover
        result.error = f"exec: {exc}"
        return result

    match = _WRITE_OUT.match((proc.stdout or "").strip())
    if not match:
        result.status = "000"
        detail = (proc.stderr or "").strip().splitlines()
        result.error = detail[0][:120] if detail else "连接失败"
        return result

    result.status = match.group("code")
    result.connect_ms = float(match.group("connect")) * 1000
    result.total_ms = float(match.group("total")) * 1000
    result.latency_ms = result.connect_ms or result.total_ms
    result.remote_ip = match.group("remote") or ""
    result.ok = result.status in set(status_codes)
    if not result.ok:
        result.error = f"HTTP {result.status}"
    return result


def http_probe(
    ip: str,
    host: str,
    timeout: float = 3.0,
    port: int = 443,
    scheme: str = "https",
    path: str = "/",
    status_codes: Sequence[str] = ("200",),
) -> Probe:
    """把 host 强制解析到 ip，发起真实 HTTPS 请求并校验状态码。"""
    result = Probe(ip=ip)
    url = f"{scheme}://{host}:{port}{path}" if port not in (80, 443) else f"{scheme}://{host}{path}"

    cmd = [
        curl_bin(),
        "-s",
        "-o", os.devnull if IS_WINDOWS else "/dev/null",
        # 末尾输出真实对端 IP：用于校验请求确实发给了我们要测的那个 IP，
        # 而不是被本机代理 / 透明网关接管（那样所有 IP 都会返回同一个结果）。
        "-w", "%{http_code} %{time_connect} %{time_total} %{remote_ip}",
        "--resolve", f"{host}:{port}:{ip}",
        "--max-time", f"{timeout:g}",
        "--connect-timeout", f"{timeout:g}",
        "--http1.1",
        "--insecure",
        url,
    ]

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout + 3,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        result.error = "timeout"
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result
    except Exception as exc:  # pragma: no cover - 环境异常
        result.error = f"exec: {exc}"
        return result

    out = (proc.stdout or "").strip()
    match = _WRITE_OUT.match(out)
    if not match:
        # curl 失败时 -w 会把错误信息塞进 %{remote_ip} 字段，
        # 这类输出按「连接失败」处理，不要当成畸形结果展示。
        result.status = "000"
        result.latency_ms = (time.perf_counter() - started) * 1000
        # stderr 里通常有真正的原因（TLS 告警 / 证书问题 / 连接被拒），
        # 直接带上能让「整批都失败」时一眼看出是证书没配还是网络不通。
        hint = _stderr_hint(proc)
        if hint:
            result.error = hint
        else:
            detail = (proc.stderr or out or "").strip().splitlines()
            result.error = detail[0][:120] if detail else "连接失败"
        return result

    result.status = match.group("code")
    result.connect_ms = float(match.group("connect")) * 1000
    result.total_ms = float(match.group("total")) * 1000
    result.latency_ms = result.connect_ms or result.total_ms
    result.remote_ip = match.group("remote") or ""

    # 校验请求确实落在了目标 IP 上；否则结果不可信（本机代理/透明网关接管）
    if result.remote_ip and result.remote_ip != ip:
        result.ok = False
        result.error = f"对端 IP 不符（实际 {result.remote_ip}，链路可能被代理接管）"
        return result

    if result.status in set(status_codes):
        result.ok = True
    else:
        result.error = f"HTTP {result.status}"
    return result


def check_network_integrity(host: str, timeout: float = 3.0, port: int = 443,
                            status_codes: Sequence[str] = ("200",)) -> tuple[bool, str]:
    """判断本机链路是否可信：优选依赖「把域名钉到指定 IP」，如果本机装了代理、
    透明网关或运营商劫持，钉过去的请求会被接管，于是**任何 IP 都会返回同样的结果**，
    优选就完全失去意义。

    做法：拿几个保留地址（RFC5737 的 192.0.2.x / 198.51.100.x，正常永远不可达）
    去请求同一域名。若它们竟然返回了正常状态码，说明链路不可信。

    返回 (是否可信, 说明)。
    """
    probes = ["192.0.2.1", "198.51.100.7"]
    suspicious: list[str] = []

    for ip in probes:
        probe = http_probe(ip, host=host, timeout=timeout, port=port,
                           status_codes=status_codes)
        if probe.status in set(status_codes):
            suspicious.append(f"{ip} 竟返回 HTTP {probe.status}（{probe.connect_ms:.0f}ms）")

    if suspicious:
        detail = ("本机链路可能在接管 HTTPS 请求：" + "；".join(suspicious) +
                  "。保留地址本应不可达，因此本机扫描结果不可信，"
                  "请在没有代理的服务器上运行（容器内通常不受影响）。")
        return False, detail
    return True, "链路检查通过：保留地址如预期不可达，钉 IP 探测可信"


# --------------------------------------------------------------------------
# 4. 平台扫描
# --------------------------------------------------------------------------
@dataclass
class Platform:
    """一个待优选平台的配置。"""

    key: str                      # 唯一标识，如 eo / eo-dx / cft
    name: str                     # 显示名
    host: str                     # 边缘节点上用于校验的站点域名（CNAME 到该平台）
    network: str = ""             # 运营商：lt=联通 / dx=电信 / yd=移动（空=不区分）
    port: int = 443
    scheme: str = "https"
    path: str = "/"
    status_codes: tuple[str, ...] = ("200",)
    timeout: float = 3.0          # 单次 HTTPS 探测超时（秒）
    ping_timeout_ms: int = 300    # ICMP 预筛选阈值
    # ICMP 预筛选默认关闭：实测这些边缘节点对 ICMP 响应极不稳定（同一 IP 时而通时而不通），
    # 而 HTTPS 一直是 200，开着会丢掉大量真实可用节点。需要时用 --ping 显式打开。
    precheck_ping: bool = False
    retries: int = 1              # 探测失败后的重试次数（抗瞬时丢包/限流）
    workers: int = 50
    # 稀疏大网段（如 ESA 的 /16）用抽样粗扫：每 /24 只取 N 个代表 IP；0 = 全量
    sample_per_24: int = 0
    # 深扫：以上一轮命中 IP 所在的 /24 为范围做全量扫描
    deep_scan: bool = False
    targets_file: str = ""        # 该平台的网段文件（相对项目根目录）
    output_file: str = ""         # 结果落盘文件
    history_file: str = ""        # 历史记录(jsonl)，用于持续监控
    enabled: bool = True
    dns: dict = field(default_factory=dict)   # 该平台的 DNS 下发配置
    note: str = ""

    def describe(self) -> str:
        if not self.enabled:
            return f"{self.name}({self.key}) [未启用]"
        tag = f"[{NETWORK_LABELS.get(self.network, self.network)}] " if self.network else ""
        return (
            f"{tag}{self.name}({self.key}) -> {self.scheme}://{self.host}:{self.port}{self.path} "
            f"期望 {','.join(self.status_codes)}，超时 {self.timeout:g}s"
            f"{f'，重试 {self.retries} 次' if self.retries else ''}"
        )


@dataclass
class ScanOutcome:
    """一个平台的扫描汇总。"""

    platform: Platform
    total: int
    passed: list[Probe]
    failed: int
    elapsed: float
    error: str = ""

    @property
    def ips(self) -> list[str]:
        return [p.ip for p in self.passed]

    def summary_line(self) -> str:
        if self.error:
            return f"{self.platform.name}: 扫描失败 - {self.error}"
        return (
            f"{self.platform.name}: {len(self.passed)}/{self.total} 可用 "
            f"({self.elapsed:.1f}s)"
        )


def sample_platform_targets(platform: Platform, targets: Sequence[str] | None = None,
                            log: Callable[[str], None] | None = None) -> list[str] | None:
    """按平台的 sample_per_24 / deep_scan 设置，把网段整理成待扫 IP 列表。"""
    from .core import load_targets as _load  # 兼容热重载

    sample = getattr(platform, "sample_per_24", 0) or 0
    deep = bool(getattr(platform, "deep_scan", False))
    if not sample and not deep:
        return list(targets) if targets is not None else None

    source = list(targets) if targets is not None else None
    if source is None:
        path = Path(platform.targets_file)
        source = _load(path)

    if deep and platform.output_file and Path(platform.output_file).exists():
        # 深扫：以上一轮命中的 /24 为范围，做全量（不再抽样）
        seeds = [line.split("\t")[0].strip()
                 for line in Path(platform.output_file).read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        networks = parent_24s(seeds)
        if networks:
            if log:
                log(f"[{platform.name}] 深扫模式：以上一轮命中 IP 所在的 {len(networks)} 个 /24 为范围")
            from .core import load_targets as _load_net

            expanded: list[str] = []
            for network in networks:
                expanded.extend(expand_targets(network))
            return expanded

    if sample:
        if log:
            log(f"[{platform.name}] 抽样模式：每 /24 取 {sample} 个代表 IP（粗扫）")
        return sample_targets(source, sample)
    return source


def sample_platform_targets(platform: Platform, targets: Sequence[str] | None = None,
                            log: Callable[[str], None] | None = None) -> list[str] | None:
    """按平台设置准备待扫 IP 列表。

    返回 None 表示「没有特殊设置，调用方按原逻辑 load_targets 全量展开」。
    返回列表表示已经准备好，调用方直接用。

    处理优先级：
      1. deep_scan 且上一轮有结果 -> 以命中的 /24 为范围全量展开；
      2. sample_per_24 > 0      -> 按网段抽样（不会物化全部 IP）；
      3. 否则返回 None          -> 走全量展开（只适合小网段）。

    网段文件不存在时抛 FileNotFoundError，由 scan_platform 转成带修复指引的错误，
    避免抽样路径（ESA / CFT 默认）直接裸崩。
    """
    sample = getattr(platform, "sample_per_24", 0) or 0
    deep = bool(getattr(platform, "deep_scan", False))
    if not sample and not deep:
        return None

    if deep and platform.output_file:
        output = Path(platform.output_file)
        if output.exists():
            seeds = [line.split("\t")[0].strip()
                     for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
            networks = parent_24s(seeds)
            if networks:
                if log:
                    log(f"[{platform.name}] 深扫模式：以上一轮命中 IP 所在的 {len(networks)} 个 /24 "
                        f"为范围（共约 {len(networks) * 254} 个地址）")
                expanded: list[str] = []
                for network in networks:
                    expanded.extend(expand_targets(network))
                return expanded
        elif log:
            log(f"[{platform.name}] 还没有上一轮结果，深扫先退化为抽样")

    if sample:
        if log:
            log(f"[{platform.name}] 抽样模式：每 /24 取 {sample} 个代表 IP（粗扫）")
        if targets is not None:
            return sample_targets(targets, sample)
        return sample_targets_file(Path(platform.targets_file), sample, log=log)
    return None


def _targets_file_hint(platform) -> str:
    """网段文件缺失时给出可操作的修复指引。"""
    key = (platform.key or "").split("-")[0].lower()
    if key == "cft":
        return ("CloudFront 网段可用以下命令生成：\n"
                "    python run.py ranges --source cloudfront --output targets/cft.txt\n"
                "  （Docker：docker compose run --rm optimizer ranges "
                "--source cloudfront --output targets/cft.txt）")
    if key == "esa":
        return ("ESA 网段可用以下命令生成（从 BGP 公告前缀发现并剔除中国大陆）：\n"
                "    python run.py esa-ranges --output targets/esa.txt\n"
                "  （Docker：docker compose run --rm optimizer esa-ranges --output targets/esa.txt）")
    if key == "eo":
        return ("EdgeOne 网段需要自行准备（腾讯云未公开完整列表），"
                "格式为每行一个 CIDR 或单个 IP：\n"
                "    targets/eo.txt\n"
                "  仓库已自带一份，若丢失可从 git 恢复：git checkout -- targets/eo.txt")
    return "请检查平台配置里的 targets_file，或补齐该网段文件后重试。"


def scan_platform(
    platform: Platform,
    targets: Sequence[str] | None = None,
    log: Callable[[str], None] | None = None,
    on_result: Callable[[Platform, Probe], None] | None = None,
    quiet: bool = False,
    step: int = 1,
    limiter: NullRateLimiter | None = None,
    stop_event=None,
    on_plan: Callable[[Platform, int], None] | None = None,
) -> ScanOutcome:
    """扫描单个平台。

    targets 为 None 时从 platform.targets_file 读取网段。
    limiter 为全局连接限速器（多平台共享，保证每秒 TCP 连接数不超上限）。
    stop_event（threading.Event）被 set 后会尽快停止：未提交的任务不再提交。
    on_plan 在读取完网段后回调一次，用于界面显示总量与进度条。
    """
    log = log or safe_log
    limiter = limiter or NullRateLimiter()
    started = time.perf_counter()

    # 先按平台设置准备目标（抽样 / 深扫都走这里），需要全量加载时再 load_targets
    try:
        prepared = sample_platform_targets(platform, targets, log=log if not quiet else None)
    except FileNotFoundError:
        # 抽样模式下网段文件缺失：给出与全量模式一致的、可操作的错误
        return ScanOutcome(platform, 0, [], 0, 0.0,
                           f"网段文件不存在: {platform.targets_file or '(未配置)'}。"
                           f"{_targets_file_hint(platform)}")
    if prepared is not None:
        targets = prepared
    elif targets is None:
        targets_path = Path(platform.targets_file)
        if not platform.targets_file or not targets_path.exists():
            # 网段文件是配置问题（不是"没有可用节点"），必须给出可操作的指引，
            # 否则容易被误解为该平台扫不到节点。
            hint = _targets_file_hint(platform)
            return ScanOutcome(platform, 0, [], 0, 0.0,
                               f"网段文件不存在: {platform.targets_file or '(未配置)'}。{hint}")
        try:
            targets = load_targets(targets_path)
        except ValueError as exc:
            return ScanOutcome(platform, 0, [], 0, 0.0,
                               f"网段展开失败（{exc}）；大网段请改用 --sample-per-24 抽样")

    targets = list(targets)
    if not targets:
        return ScanOutcome(platform, 0, [], 0, 0.0, "网段列表为空")
    if on_plan:
        on_plan(platform, len(targets))

    if not quiet:
        safe_log(f"[{platform.name}] 加载 {len(targets)} 个 IP，并发 {platform.workers}"
                 f"{'，启用 ping 预筛选' if platform.precheck_ping else ''}"
                 f"（{platform.scheme}://{platform.host}:{platform.port}）")

    # 先探一个头，尽早发现「整批都会失败」的配置问题（域名没绑证书、host 写错等）
    if len(targets) >= 8:
        head_errors: list[str] = []
        for probe_ip in targets[:3]:
            probe = http_probe(probe_ip, host=platform.host, timeout=platform.timeout,
                               port=platform.port, scheme=platform.scheme,
                               path=platform.path, status_codes=platform.status_codes)
            if not probe.ok and probe.error:
                head_errors.append(probe.error)
        if len(head_errors) == 3 and not quiet:
            safe_log(f"[{platform.name}] ⚠ 前 3 个 IP 全部失败，首个错误：{head_errors[0]}")
            if any("alert" in e.lower() for e in head_errors):
                safe_log(f"[{platform.name}]   看起来是服务端 TLS 告警（域名可能还没绑定 HTTPS 证书）。"
                         f"可在平台配置里把 scheme 改成 http 先用 80 端口优选。")

    candidates: list[str] = targets
    if platform.precheck_ping:
        alive: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=platform.workers) as pool:
            for ip, ok in zip(
                candidates,
                pool.map(lambda x: ping(x, platform.ping_timeout_ms), candidates),
            ):
                if ok:
                    alive.append(ip)
        if not quiet:
            log(f"[{platform.name}] ping 通过 {len(alive)}/{len(targets)}")
        if alive:
            candidates = alive
        else:
            # 部分机房屏蔽 ICMP，预筛选全部落空时回退为直接 TLS 探测，
            # 避免「一个都不剩」的假阴性。
            if not quiet:
                log(f"[{platform.name}] ⚠ ping 全部未通过，回退为直接 TLS 探测")
            candidates = targets

    passed: list[Probe] = []
    counter = {"n": 0}
    total = len(candidates)
    lock_total = total or 1

    def work(ip: str) -> Probe:
        limiter.acquire()          # ★ 全局限速：每次真实 TCP 连接前取一个令牌
        probe = http_probe(
            ip,
            host=platform.host,
            timeout=platform.timeout,
            port=platform.port,
            scheme=platform.scheme,
            path=platform.path,
            status_codes=platform.status_codes,
        )
        # 边缘节点在大并发下偶发丢包/限流，失败后重试可显著减少假阴性
        attempt = 0
        while not probe.ok and attempt < platform.retries:
            attempt += 1
            time.sleep(0.15 * attempt)
            limiter.acquire()      # 重试同样计入限速
            probe = http_probe(
                ip,
                host=platform.host,
                timeout=platform.timeout,
                port=platform.port,
                scheme=platform.scheme,
                path=platform.path,
                status_codes=platform.status_codes,
            )
        return probe

    # 分块提交：既保持线程池占满，又能及时响应停止请求
    # （否则一次性提交 7000+ 个任务后，停止也要等它们全部跑完）。
    chunk = max(platform.workers * 4, 64)
    with concurrent.futures.ThreadPoolExecutor(max_workers=platform.workers) as pool:
        pending: set = set()
        stopped = False
        iterator = iter(candidates)
        while True:
            while not stopped and len(pending) < chunk:
                try:
                    ip = next(iterator)
                except StopIteration:
                    break
                pending.add(pool.submit(work, ip))
            if not pending:
                break
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                probe = future.result()
                counter["n"] += 1
                if probe.ok:
                    passed.append(probe)
                    if not quiet:
                        safe_log(f"  [+] {platform.name} {probe.ip} ✅ {probe.latency_ms:.0f}ms")
                elif not quiet and step:
                    safe_log(f"  [-] {platform.name} {probe.ip} ❌ "
                             f"({probe.error or ('HTTP ' + probe.status)})")
                if on_result:
                    on_result(platform, probe)
            if stop_event is not None and stop_event.is_set():
                if not stopped:
                    stopped = True
                    safe_log(f"[{platform.name}] 收到停止请求，正在收尾…")
                # 取消尚未开始的任务
                for future in list(pending):
                    future.cancel()
                pending = {f for f in pending if not f.cancelled()}

    passed.sort(key=lambda p: p.latency_ms or 1e9)
    error = "已被用户停止（结果不完整）" if stop_event is not None and stop_event.is_set() else ""
    return ScanOutcome(platform, len(targets), passed, total - len(passed),
                       time.perf_counter() - started, error)


def scan_platforms(
    platforms: Iterable[Platform],
    targets: Sequence[str] | None = None,
    log: Callable[[str], None] | None = None,
    quiet: bool = False,
    parallel_platforms: bool = True,
    limiter: NullRateLimiter | None = None,
    stop_event=None,
    on_result: Callable[[Platform, Probe], None] | None = None,
    on_plan: Callable[[Platform, int], None] | None = None,
) -> list[ScanOutcome]:
    """扫描多个平台。

    默认并行：每个平台独立线程池。共享同一份 IP 列表时，ping 预筛选会
    各做一遍（实现简单、互不干扰）；如需节省 ICMP 开销可设置
    parallel_platforms=False，顺序执行并按平台分别预筛选。

    limiter 由所有平台共用，因此 --rate 限的是「所有平台合计」的连接速率。
    """
    log = log or safe_log
    limiter = limiter or NullRateLimiter()
    plats = [p for p in platforms if p.enabled]
    if not plats:
        return []

    kwargs = dict(targets=targets, log=log, quiet=quiet, limiter=limiter,
                  stop_event=stop_event, on_result=on_result, on_plan=on_plan)

    if not parallel_platforms or len(plats) == 1:
        return [scan_platform(p, **kwargs) for p in plats]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(plats)) as pool:
        futures = [pool.submit(scan_platform, p, **kwargs) for p in plats]
        return [f.result() for f in futures]


# --------------------------------------------------------------------------
# 5. 结果落盘 / 历史记录
# --------------------------------------------------------------------------
def write_result(path: Path, probes: Sequence[Probe], with_latency: bool = False) -> None:
    """写入结果文件。默认只写 IP，保持与原 yes.txt 完全兼容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if with_latency:
        lines = [f"{p.ip}\t{p.latency_ms:.0f}ms" for p in probes]
    else:
        lines = [p.ip for p in probes]
    path.write_text("\n".join(lines), encoding="utf-8")


def append_history(path: Path, platform: Platform, outcome: ScanOutcome) -> None:
    """追加一条 JSONL 历史记录，便于长期监控节点变化。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(time.time()),
        "platform": platform.key,
        "host": platform.host,
        "total": outcome.total,
        "alive": len(outcome.passed),
        "ips": [{"ip": p.ip, "ms": round(p.latency_ms, 1)} for p in outcome.passed],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
