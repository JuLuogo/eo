"""自动拉取各平台的节点 IP 网段（避免手工维护几个月就过期的列表）。

数据源都是官方公开接口：
  * cloudfront  : https://ip-ranges.amazonaws.com/ip-ranges.json
                  （取 service=CLOUDFRONT 的 IPv4 前缀；CLOUDFRONT 是边缘节点）
  * cloudflare  : https://api.cloudflare.com/client/v4/ips
  * 其它平台     : 请手工维护 targets/<key>.txt

结果写入 targets/<key>.txt，一行一个 CIDR，并记录拉取时间。
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Callable

SOURCES = {
    "cloudfront": "https://ip-ranges.amazonaws.com/ip-ranges.json",
    "cloudflare": "https://api.cloudflare.com/client/v4/ips",
}

# CloudFront 的 service 字段取值：CLOUDFRONT 为边缘节点，CLOUDFRONT_REGIONAL_EDGE 为区域边缘
CLOUDFRONT_SERVICES = ("CLOUDFRONT", "CLOUDFRONT_REGIONAL_EDGE")


def fetch_cloudfront(timeout: float = 30.0) -> list[str]:
    with urllib.request.urlopen(SOURCES["cloudfront"], timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    prefixes = [
        item["ip_prefix"]
        for item in data.get("prefixes", [])
        if item.get("service") in CLOUDFRONT_SERVICES
    ]
    return sorted(set(prefixes))


def fetch_cloudflare(timeout: float = 30.0) -> list[str]:
    with urllib.request.urlopen(SOURCES["cloudflare"], timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    result = data.get("result") or {}
    prefixes = list(result.get("ipv4_cidrs") or []) + list(result.get("ipv6_cidrs") or [])
    return sorted(set(prefixes))


FETCHERS: dict[str, Callable[..., list[str]]] = {
    "cloudfront": fetch_cloudfront,
    "cloudflare": fetch_cloudflare,
}


def fetch(key: str, timeout: float = 30.0) -> list[str]:
    if key not in FETCHERS:
        raise KeyError(f"不支持自动拉取: {key}（可选: {', '.join(FETCHERS)}）")
    return FETCHERS[key](timeout=timeout)


def update_targets_file(key: str, path: Path, log: Callable[[str], None] = print,
                        timeout: float = 30.0) -> int:
    """拉取网段并覆盖写入目标文件，返回写入的网段数量。"""
    prefixes = fetch(key, timeout=timeout)
    if not prefixes:
        raise RuntimeError(f"{key} 未返回任何网段")

    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"# 自动拉取自 {SOURCES.get(key, key)}",
        f"# 更新时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# 共 {len(prefixes)} 个网段；可用 `python run.py ranges --source {key}` 重新拉取",
        "",
    ]
    path.write_text("\n".join(header + prefixes) + "\n", encoding="utf-8")
    log(f"{key}: 已写入 {len(prefixes)} 个网段 -> {path}")
    return len(prefixes)
