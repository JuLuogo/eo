"""ESA（阿里云边缘安全加速）网段发现：BGP 网段 + 排除中国大陆。

背景：ESA 不公布节点网段，只能从「阿里云对外宣告的 BGP 前缀」里筛，
同时把中国大陆的地址段剔除掉（只要境外节点）。

三层数据来源，全部是公开接口：
  1. **发现 ASN**：https://bgp.he.net/search?search[]=alibaba
     解析页面里的 ASN 列表（阿里云有十几个 ASN）。
  2. **ASN -> 前缀**：https://stat.ripe.net/data/announced-prefixes/data.json
     RIPEstat 的官方数据接口，稳定且有 JSON 输出。
  3. **排除中国大陆**：https://ftp.apnic.net/stats/apnic/delegated-apnic-latest
     APNIC 官方 RIR 委派文件（每行 `<registry>|CN|ipv4|<起始IP>|<数量>|...`），
     权威且不需要任何第三方数据库，凡是落在 CN 委派段内的前缀全部剔除。

产出：一行一个 CIDR 写入 targets/esa.txt。
"""

from __future__ import annotations

import bisect
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Sequence

Log = Callable[[str], None]

HE_SEARCH = "https://bgp.he.net/search?search%5Bsearch%5D={keyword}&commit=Search"
RIPESTAT_PREFIXES = "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}"
APNIC_DELEGATED = "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest"

USER_AGENT = "Mozilla/5.0 (compatible; edge-optimizer/1.0)"

# 阿里云/阿里相关的常见 ASN（HE 搜索不全时的兜底；45102 是国际站主力，
# 24429 为阿里云 CDN，其余为国内主体，通常会被 CN 过滤掉）
KNOWN_ALIBABA_ASNS = [
    45102,   # Alibaba (US) Technology Co., Ltd. —— 国际站主力
    24429,   # Alibaba Cloud CDN
    37963,   # Hangzhou Alibaba Advertising Co., Ltd.
    134963,  # Alibaba Cloud LLC (US)
    59028, 59051, 59052, 59053, 59054, 59055,  # 阿里系
    45103, 45104, 34947, 203513, 402205, 402206, 402207,
]

# 单个前缀超过这么多地址就跳过（避免把 /8 这种巨型段塞进扫描列表）
MAX_PREFIX_ADDRESSES = 65536


def _get(url: str, timeout: float = 30.0, retries: int = 2) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last_error}")


# --------------------------------------------------------------------------
# 1. 发现 ASN
# --------------------------------------------------------------------------
def search_asns(keyword: str = "alibaba", timeout: float = 30.0) -> list[int]:
    """从 bgp.he.net 搜索页解析出 ASN 列表。"""
    html = _get(HE_SEARCH.format(keyword=urllib.parse.quote(keyword)), timeout=timeout)
    text = html.decode("utf-8", "replace")
    asns = {int(m) for m in re.findall(r"\bAS(\d{1,10})\b", text)}
    return sorted(asns)


def default_asns(log: Log = print, timeout: float = 30.0) -> list[int]:
    """优先用 HE 搜索结果，失败或结果太少时回退到内置列表。"""
    try:
        found = search_asns("alibaba", timeout=timeout)
    except Exception as exc:
        log(f"  HE 搜索失败（{exc}），使用内置 ASN 列表")
        return list(KNOWN_ALIBABA_ASNS)

    merged = sorted(set(found) | set(KNOWN_ALIBABA_ASNS))
    log(f"  HE 搜索到 {len(found)} 个 ASN，合并内置列表后共 {len(merged)} 个")
    return merged


# --------------------------------------------------------------------------
# 2. ASN -> 前缀
# --------------------------------------------------------------------------
def asn_prefixes(asn: int, timeout: float = 30.0, log: Log = print) -> list[str]:
    """取某个 ASN 宣告的 IPv4 前缀（RIPEstat）。"""
    url = RIPESTAT_PREFIXES.format(asn=asn)
    try:
        data = json.loads(_get(url, timeout=timeout).decode("utf-8", "replace"))
    except Exception as exc:
        log(f"  AS{asn}: 拉取失败 {exc}")
        return []

    prefixes = []
    for item in (data.get("data") or {}).get("prefixes") or []:
        prefix = item.get("prefix")
        if not prefix or ":" in prefix:          # 只要 IPv4
            continue
        prefixes.append(prefix)
    return prefixes


# --------------------------------------------------------------------------
# 3. 中国大陆地址段
# --------------------------------------------------------------------------
def cn_ranges(timeout: float = 60.0, log: Log = print) -> list[tuple[int, int]]:
    """从 APNIC 官方委派文件解析出中国大陆的 IPv4 地址区间（整数形式）。

    APNIC 的 delegated 文件每行格式：
        apnic|CN|ipv4|1.0.1.0|256|20110414|allocated
    其中 count 不保证是 2 的幂，所以这里直接用 [start, start+count-1] 区间，
    不做 CIDR 转换，既精确又省内存。
    """
    raw = _get(APNIC_DELEGATED, timeout=timeout)
    text = raw.decode("utf-8", "replace")
    ranges: list[tuple[int, int]] = []

    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 5 or parts[2] != "ipv4" or parts[1] != "CN":
            continue
        try:
            start = int(ipaddress.IPv4Address(parts[3]))
            count = int(parts[4])
        except (ValueError, ipaddress.AddressValueError):
            continue
        if count <= 0:
            continue
        ranges.append((start, start + count - 1))

    log(f"  APNIC 委派数据：中国大陆共 {len(ranges)} 段 IPv4 分配记录")
    return ranges


class ChinaFilter:
    """判断某个 IP / 前缀是否属于中国大陆（区间合并 + 二分查找）。"""

    def __init__(self, ranges: Iterable[tuple[int, int]]) -> None:
        ordered = sorted(ranges)
        merged: list[tuple[int, int]] = []
        for start, end in ordered:
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        self.ranges = merged
        self._starts = [start for start, _ in merged]

    def __len__(self) -> int:
        return len(self.ranges)

    def contains_ip(self, value: int) -> bool:
        index = bisect.bisect_right(self._starts, value) - 1
        return index >= 0 and value <= self.ranges[index][1]

    def overlaps(self, first: int, last: int) -> bool:
        """[first, last] 与任一 CN 区间有交集。

        注意不能只看前缀的首尾两个地址：阿里的大段（/12、/13）里常常嵌着
        APNIC 分给大陆的 /24，两端都不在 CN 区间内，但中间重叠 ——
        只判断端点会把这些前缀当成"非大陆"留在网段里。
        """
        index = bisect.bisect_right(self._starts, last) - 1
        return index >= 0 and self.ranges[index][1] >= first

    def is_china(self, prefix: str) -> bool:
        """与 CN 段有任何重叠即视为中国大陆（保守做法，宁可多剔除）。"""
        network = ipaddress.ip_network(prefix, strict=False)
        return self.overlaps(int(network.network_address), int(network.broadcast_address))


def exclude_china(prefixes: Sequence[str], filter_: ChinaFilter,
                  log: Log = print) -> tuple[list[str], list[str]]:
    """返回 (保留, 剔除) 两个前缀列表。

    只处理 IPv4：中国大陆的判定区间本身就是 v4，而 v6 前缀若原样留在网段文件里，
    下游的全量展开会试图展开 2^96 个地址（虽然会被上限拦住，但那是错误路径）。
    因此这里直接把非 v4 前缀丢掉，而不是"保留"。
    """
    import ipaddress as _ip

    kept, dropped, skipped = [], [], 0
    for prefix in prefixes:
        try:
            network = _ip.ip_network(prefix, strict=False)
        except ValueError:
            continue
        if network.version != 4:
            skipped += 1
            continue
        try:
            if filter_.is_china(prefix):
                dropped.append(prefix)
            else:
                kept.append(prefix)
        except ValueError:
            continue
    if skipped:
        log(f"  跳过 {skipped} 个非 IPv4 前缀（本工具只扫 IPv4）")
    if dropped:
        log(f"  剔除中国大陆前缀 {len(dropped)} 个")
    return kept, dropped


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def discover_esa_prefixes(asns: Sequence[int] | None = None,
                          log: Log = print,
                          timeout: float = 30.0) -> dict:
    """发现 ESA 可用（境外）的阿里云网段。"""
    if asns is None:
        log("1) 发现阿里云 ASN …")
        asns = default_asns(log=log, timeout=timeout)
    else:
        log(f"1) 使用指定 ASN：{', '.join('AS' + str(a) for a in asns)}")

    log("2) 拉取各 ASN 宣告的前缀 …")
    all_prefixes: dict[int, list[str]] = {}
    for asn in asns:
        prefixes = asn_prefixes(asn, timeout=timeout, log=log)
        if prefixes:
            all_prefixes[asn] = prefixes
            log(f"  AS{asn}: {len(prefixes)} 个 IPv4 前缀")

    union = sorted({p for prefixes in all_prefixes.values() for p in prefixes})
    log(f"  合计去重后 {len(union)} 个前缀")

    log("3) 拉取中国大陆地址段并排除 …")
    filter_ = ChinaFilter(cn_ranges(timeout=timeout * 2, log=log))
    log(f"  合并后 {len(filter_)} 段中国大陆地址")
    kept, dropped = exclude_china(union, filter_, log=log)

    # 过滤掉过大的网段（扫描成本高）
    oversized = []
    final = []
    for prefix in kept:
        network = ipaddress.ip_network(prefix, strict=False)
        if network.num_addresses > MAX_PREFIX_ADDRESSES:
            oversized.append(prefix)
        else:
            final.append(prefix)
    if oversized:
        log(f"  跳过 {len(oversized)} 个超大网段（>{MAX_PREFIX_ADDRESSES} 地址）")

    total_ips = sum(ipaddress.ip_network(p, strict=False).num_addresses for p in final)
    log(f"4) 结论：可用于 ESA 优选的境外网段 {len(final)} 个，约 {total_ips} 个 IP")

    return {
        "asns": list(asns),
        "prefixes": final,
        "dropped_china": dropped,
        "oversized": oversized,
        "total_ips": total_ips,
    }


def write_targets(result: dict, path: Path, log: Log = print) -> int:
    """把发现结果写成网段文件（带注释头）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# 自动发现：阿里云（Alibaba / ESA）境外网段，已排除中国大陆",
        f"# 更新时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# ASN: {', '.join('AS' + str(a) for a in result.get('asns', []))}",
        f"# 网段数: {len(result.get('prefixes', []))}，"
        f"剔除中国大陆: {len(result.get('dropped_china', []))}",
        "# 重新生成: python run.py esa-ranges --output targets/esa.txt",
        "",
    ]
    # 原子写：扫描可能正在读这个文件，直接覆盖会读到半截网段列表（静默少扫）
    from .core import atomic_write_text
    atomic_write_text(path, "\n".join(header + list(result.get("prefixes", []))) + "\n")
    log(f"已写入 {len(result.get('prefixes', []))} 个网段 -> {path}")
    return len(result.get("prefixes", []))
