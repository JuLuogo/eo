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
import os
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


def auto_refresh(platform, log: Callable[[str], None] = print,
                 min_age_hours: float | None = None) -> str:
    """按平台的「自动拉取网段」设置刷新它的网段文件，返回说明（无需刷新时返回空串）。

    设计上的几个约束，都是为了"不要因为一次网络问题把扫描搞坏"：
      * 只在文件不存在、或旧于 min_age_hours 时才拉取 —— 否则每轮都请求一次，
        间隔 300 秒的话一天就是 288 次；
      * 拉取失败/返回空 -> 保留原文件，只在日志里说一声，继续用旧网段扫；
      * 写入是原子的（update_targets_file 内部），扫描同时读不会看到半截。
    """
    key = (getattr(platform, "auto_targets", "") or "").strip().lower()
    if not key:
        return ""
    if key not in SOURCES:
        return (f"⚠ 自动拉取源 {key!r} 不认识（可选: {', '.join(SOURCES)}），"
                f"本轮继续用现有网段文件")

    path = Path(platform.targets_file or "")
    if not str(path):
        return "⚠ 未配置网段文件路径，无法自动拉取"

    if min_age_hours is None:
        try:
            min_age_hours = float(os.environ.get("EO_TARGETS_REFRESH_HOURS", "24") or 24)
        except ValueError:
            min_age_hours = 24.0

    if path.exists() and min_age_hours > 0:
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours < min_age_hours:
            return ""            # 还新鲜，不用拉

    try:
        count = update_targets_file(key, path, log=lambda _m: None)
    except Exception as exc:                                 # noqa: BLE001
        return (f"⚠ 自动拉取 {key} 失败（{type(exc).__name__}: {exc}），"
                f"本轮继续用现有网段文件")
    return f"网段已自动更新：{key} -> {path}（{count} 个网段）"


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
    # 原子写：扫描可能正在读这个文件，直接覆盖会读到半截网段列表（静默少扫）
    from .core import atomic_write_text
    atomic_write_text(path, "\n".join(header + prefixes) + "\n")
    log(f"{key}: 已写入 {len(prefixes)} 个网段 -> {path}")
    return len(prefixes)
