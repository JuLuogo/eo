#!/usr/bin/env python3
"""网段拉取与解析测试（离线，不访问任何外部地址）。

用假的 HTTP 响应替换 urllib，验证解析逻辑：
  * CloudFront：从 AWS ip-ranges.json 里按 service 过滤，去重排序
  * Cloudflare：从 API 的 result.ipv4_cidrs/ipv6_cidrs 取
  * ESA：从 BGP 公告前缀里剔除中国大陆网段
另外验证写文件、异常处理。
"""
from __future__ import annotations

import atexit
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo import esa_ranges, ranges   # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


class FakeResponse(io.BytesIO):
    """最小可用的 urlopen 返回值替身。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def status(self):
        return 200


def patch_urlopen(module, payloads):
    """把模块里的 urllib.request.urlopen 换成按 URL 返回预设内容。"""
    calls: list[str] = []

    def fake(url, timeout=None, **kw):
        target = url if isinstance(url, str) else getattr(url, "full_url", str(url))
        calls.append(target)
        for key, value in payloads.items():
            if key in target:
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                return FakeResponse(body)
        raise OSError(f"未预设的地址: {target}")

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake
    return calls, original


print("=" * 70)
print("网段拉取与解析测试（离线）")
print("=" * 70)

# ------------------------------------------------------------------ 1. CloudFront
print()
print("1) CloudFront：按 service 过滤 AWS ip-ranges.json")
aws_data = {
    "syncToken": "1",
    "createDate": "2026-01-01-00-00-00",
    "prefixes": [
        {"ip_prefix": "13.32.0.0/15", "region": "GLOBAL", "service": "CLOUDFRONT"},
        {"ip_prefix": "13.224.0.0/14", "region": "GLOBAL", "service": "CLOUDFRONT"},
        {"ip_prefix": "52.84.0.0/15", "region": "GLOBAL", "service": "CLOUDFRONT_REGIONAL_EDGE"},
        {"ip_prefix": "3.5.0.0/16", "region": "us-east-1", "service": "S3"},
        {"ip_prefix": "99.86.0.0/16", "region": "GLOBAL", "service": "CLOUDFRONT"},
        {"ip_prefix": "99.86.0.0/16", "region": "GLOBAL", "service": "CLOUDFRONT"},   # 重复
        {"ip_prefix": "13.32.0.0/15", "region": "GLOBAL", "service": "AMAZON"},        # 非 CF
    ],
}
calls, orig = patch_urlopen(ranges, {"ip-ranges.amazonaws.com": aws_data})
try:
    got = ranges.fetch_cloudfront()
finally:
    ranges.urllib.request.urlopen = orig
print(f"     解析结果: {got}")
check("只保留 CLOUDFRONT 系列", all("3.5.0.0/16" not in x for x in got), str(got))
check("包含 CLOUDFRONT 与 REGIONAL_EDGE",
      "13.32.0.0/15" in got and "52.84.0.0/15" in got, str(got))
check("排除了 S3 / AMAZON 的网段",
      "3.5.0.0/16" not in got, str(got))
check("去重（99.86.0.0/16 只出现一次）", got.count("99.86.0.0/16") == 1, str(got))
check("结果已排序", got == sorted(got), str(got))
check("确实请求了正确的地址",
      any("ip-ranges.amazonaws.com" in c for c in calls), str(calls))

# ------------------------------------------------------------------ 2. Cloudflare
print()
print("2) Cloudflare：从 API result 里取 v4/v6")
cf_data = {"success": True, "result": {
    "ipv4_cidrs": ["173.245.48.0/20", "103.21.244.0/22", "173.245.48.0/20"],
    "ipv6_cidrs": ["2400:cb00::/32", "2606:4700::/32"],
}}
calls, orig = patch_urlopen(ranges, {"api.cloudflare.com": cf_data})
try:
    got = ranges.fetch_cloudflare()
finally:
    ranges.urllib.request.urlopen = orig
print(f"     解析结果: {got}")
check("包含 ipv4 网段", "173.245.48.0/20" in got and "103.21.244.0/22" in got, str(got))
check("包含 ipv6 网段", "2400:cb00::/32" in got, str(got))
check("去重", got.count("173.245.48.0/20") == 1, str(got))
check("数量正确（2 v4 + 2 v6）", len(got) == 4, str(len(got)))

# ------------------------------------------------------------------ 3. fetch 分发
print()
print("3) fetch() 分发与错误处理")
check("SOURCES 覆盖 cloudfront/cloudflare",
      {"cloudfront", "cloudflare"} <= set(ranges.SOURCES), str(sorted(ranges.SOURCES)))
try:
    ranges.fetch("not-a-source")
    check("未知 source 抛 KeyError", False)
except KeyError as exc:
    check("未知 source 抛 KeyError", "不支持自动拉取" in str(exc), str(exc)[:50])

# ------------------------------------------------------------------ 4. 写文件
print()
print("4) update_targets_file 写文件")
tmp = Path(tempfile.mkdtemp(prefix="eo-ranges-"))
atexit.register(shutil.rmtree, tmp, ignore_errors=True)
calls, orig = patch_urlopen(ranges, {"ip-ranges.amazonaws.com": aws_data})
try:
    n = ranges.update_targets_file("cloudfront", tmp / "cft.txt", log=lambda _: None)
finally:
    ranges.urllib.request.urlopen = orig
content = (tmp / "cft.txt").read_text(encoding="utf-8")
lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
check("返回写入条数", n == len(lines), f"n={n} lines={len(lines)}")
check("文件里每个网段一行", all("/" in l for l in lines), str(lines[:3]))
check("带注释头", any(l.startswith("#") for l in content.splitlines()),
      content.splitlines()[0][:60] if content else "")

# ------------------------------------------------------------------ 5. ESA：剔除中国大陆
print()
print("5) ESA：从 BGP 前缀里剔除中国大陆网段")
import ipaddress   # noqa: E402


def to_int_ranges(cidrs):
    out = []
    for c in cidrs:
        net = ipaddress.ip_network(c, strict=False)
        out.append((int(net.network_address), int(net.broadcast_address)))
    return out


all_prefixes = [
    "1.1.1.0/24",        # 海外 -> 保留
    "47.74.0.0/16",      # 被 47.0.0.0/8 覆盖 -> 剔除
    "39.96.0.0/13",      # 中国 -> 剔除
    "163.181.0.0/16",    # 海外 -> 保留
    "116.128.0.0/10",    # 中国 -> 剔除
    "2001:db8::/32",     # IPv6，过滤器只处理 v4（不应崩溃）
]
cn_cidrs = ["39.96.0.0/13", "116.128.0.0/10", "47.0.0.0/8"]
cf_filter = esa_ranges.ChinaFilter(to_int_ranges(cn_cidrs))
print(f"     过滤器区间数: {len(cf_filter)}")

kept, dropped = esa_ranges.exclude_china(all_prefixes, cf_filter, log=lambda _: None)
print(f"     保留 {len(kept)}: {kept}")
print(f"     剔除 {len(dropped)}: {dropped}")
check("剔除了明确的国内网段",
      "39.96.0.0/13" in dropped and "116.128.0.0/10" in dropped, str(dropped))
check("被国内大段覆盖的也剔除（47.74/16 ⊂ 47.0/8）",
      "47.74.0.0/16" in dropped, str(dropped))
check("保留了海外网段",
      "1.1.1.0/24" in kept and "163.181.0.0/16" in kept, str(kept))
check("保留与剔除不重叠", not (set(kept) & set(dropped)), str(set(kept) & set(dropped)))
check("IPv6 前缀被丢弃（本工具只扫 IPv4）",
      "2001:db8::/32" not in kept, f"kept={kept}")

print()
print("  ≥ ChinaFilter 边界行为")
single = esa_ranges.ChinaFilter(to_int_ranges(["10.0.0.0/8"]))
check("区间内部 IP 判定为中国", single.contains_ip(int(ipaddress.ip_address("10.1.2.3"))))
check("区间外 IP 判定为非中国",
      not single.contains_ip(int(ipaddress.ip_address("11.1.2.3"))))
check("相邻区间会合并（10.0.0.0/8 + 11.0.0.0/8 -> 1 段）",
      len(esa_ranges.ChinaFilter(to_int_ranges(["10.0.0.0/8", "11.0.0.0/8"]))) == 1,
      str(len(esa_ranges.ChinaFilter(to_int_ranges(["10.0.0.0/8", "11.0.0.0/8"])))))
check("完全重叠中国大陆的前缀 -> is_china=True",
      single.is_china("10.1.0.0/16"))
check("与中国大陆无交集的前缀 -> is_china=False",
      not single.is_china("12.0.0.0/16"))
check("部分重叠也视为中国（保守）", single.is_china("10.0.0.0/7"),
      "10.0.0.0/7 覆盖 10/8 与 11/8")

# ------------------------------------------------------------------ 6. ESA：ASN 搜索解析
print()
print("6) ESA：ASN 搜索解析（从 HE 页面 HTML 里抽 ASN 号）")
he_html = """
<tr><td><a href="/asn/AS45102">AS45102</a></td><td>Alibaba (US) Technology Co., Ltd.</td></tr>
<tr><td><a href="/asn/AS24429">AS24429</a></td><td>Alibaba Cloud</td></tr>
<tr><td><a href="/asn/AS37963">AS37963</a></td><td>Hangzhou Alibaba Advertising Co.,Ltd.</td></tr>
<tr><td><a href="/asn/AS99999">AS99999</a></td><td>Unrelated Network</td></tr>
"""
orig_he = esa_ranges.urllib.request.urlopen
esa_ranges.urllib.request.urlopen = lambda url, timeout=None, **kw: FakeResponse(
    he_html.encode())
try:
    asns = esa_ranges.search_asns("alibaba")
finally:
    esa_ranges.urllib.request.urlopen = orig_he
print(f"     解析出的 ASN: {asns}")
check("返回整数列表", all(isinstance(a, int) for a in asns),
      str([type(a).__name__ for a in asns]))
check("抽出 AS45102", 45102 in asns, str(asns))
check("抽出多个 ASN", len(asns) >= 3, str(len(asns)))
check("按搜索结果原样抓取（不自行按关键词过滤，过滤由 HE 完成）",
      99999 in asns, "页面上出现的 ASN 都会被解析")

# ------------------------------------------------------------------ 7. ESA：RIPEstat 前缀解析
print()
print("7) ESA：RIPEstat 前缀解析")
stat_data = {"data": {"prefixes": [
    {"prefix": "163.181.0.0/16"},
    {"prefix": "47.74.0.0/16"},
    {"prefix": "2001:db8::/32"},        # IPv6 应被忽略（扫的是 v4）
]}}
esa_ranges.urllib.request.urlopen = lambda url, timeout=None, **kw: FakeResponse(
    json.dumps(stat_data).encode())
try:
    prefixes = esa_ranges.asn_prefixes(45102)
finally:
    esa_ranges.urllib.request.urlopen = orig_he
print(f"     解析结果: {prefixes}")
check("解析出 v4 前缀", "163.181.0.0/16" in prefixes, str(prefixes))
check("忽略 IPv6 前缀", not any(":" in p for p in prefixes), str(prefixes))
check("去重且有序", prefixes == sorted(set(prefixes)), str(prefixes))

# ------------------------------------------------------------------ 8. 默认 ASN 列表
print()
print("8) 默认 ASN 列表（无网络时也能用）")
check("KNOWN_ALIBABA_ASNS 非空", bool(esa_ranges.KNOWN_ALIBABA_ASNS),
      f"{len(esa_ranges.KNOWN_ALIBABA_ASNS)} 个")
check("默认列表是整数 ASN 号",
      all(isinstance(a, int) for a in esa_ranges.KNOWN_ALIBABA_ASNS),
      str([type(a).__name__ for a in list(esa_ranges.KNOWN_ALIBABA_ASNS)[:3]]))
check("默认列表含已知的阿里云 ASN（45102 / 24429）",
      {45102, 24429} <= set(esa_ranges.KNOWN_ALIBABA_ASNS),
      str(sorted(esa_ranges.KNOWN_ALIBABA_ASNS)[:6]))
print(f"     {sorted(esa_ranges.KNOWN_ALIBABA_ASNS)}")

print()
print("=" * 70)
if _failures:
    print(f"失败 {len(_failures)} 项：")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("网段拉取解析全部测试通过")
sys.exit(0)
