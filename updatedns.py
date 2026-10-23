#!/usr/bin/env python3
"""DNS 下发入口（兼容原来的 updatedns.py）。

老用法（不带参数）行为与改造前一致：读取 yes.txt，下发到 eo-lt.1.060730.xyz。

新用法支持按平台下发、指定文件、预览：

    python update_dns.py                          # 默认：eo 平台，读 results/eo.txt
    python update_dns.py --platform eo esa cft    # 多平台各自下发到自己的记录
    python update_dns.py -f yes.txt --record eo-lt.1.060730.xyz
    python update_dns.py --platform eo --dry-run  # 只预览
    python update_dns.py --platform eo --yes      # 真正执行

说明：与 run.py 的关系是「run.py scan --dns」会自动调用同一套逻辑，
本文件只是给习惯单独跑 DNS 下发的场景留的入口。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eo.core import safe_log
from eo.dns import DnsTarget, sync_platform, sync_records
from eo.platforms import PLATFORMS, get_platforms

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update_dns.py",
        description="把优选结果批量下发到 DNS（华为云）",
    )
    parser.add_argument("-p", "--platform", nargs="+", metavar="KEY",
                        help="按平台配置下发（推荐），如 -p eo esa cft")
    parser.add_argument("-f", "--file", help="直接指定 IP 列表文件（单记录模式）")
    parser.add_argument("--record", help="记录名，如 eo-lt.1.060730.xyz（单记录模式）")
    parser.add_argument("--zone", default="1.060730.xyz", help="zone，默认 1.060730.xyz")
    parser.add_argument("--ttl", type=int, default=1, help="TTL，默认 1")
    parser.add_argument("--batch-size", type=int, default=50, help="每个记录集 IP 数，1~50")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改线上解析")
    parser.add_argument("--yes", action="store_true", help="确认执行修改")
    parser.add_argument("--force", action="store_true", help="忽略「解析量骤降保护」，允许大幅缩容")
    return parser


def _default_dns_config() -> dict:
    """取一份默认的 DNS 凭据配置（三个平台走同一个华为云账号，取谁都一样）。

    注意：PLATFORMS 的键是 eo-lt / eo-dx 这类变体，没有 "eo" 这个基础键 ——
    老代码写的是 PLATFORMS["eo"]，于是单记录模式（-f 文件 --record 记录名）
    一调用就 KeyError 直接崩掉。这里按变体顺序找一个带 dns 配置的。
    """
    for key in ("eo-lt", "eo-dx", "eo-yd", "esa-lt", "cft-lt"):
        platform = PLATFORMS.get(key)
        if platform is not None and platform.dns:
            return platform.dns
    for platform in PLATFORMS.values():          # 兜底：任何一个带 dns 的
        if platform.dns:
            return platform.dns
    return {}


def _suspicious_result_file(platform) -> str:
    """检查某个平台的结果文件是否「好得不真实」，是则返回原因。

    结果文件可能是 `IP` 或 `IP<TAB>延迟ms` 两种格式（--with-latency）。
    没有延迟列时无法判断延迟，只能靠 IP 数量与通过率 —— 那种情况下
    本函数返回空串（不阻拦），因为误拦会让人无法下发。
    """
    from eo.core import looks_suspicious

    path = Path(platform.output_file or "")
    if not path.exists():
        return ""
    ips: list[str] = []
    latencies: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        ips.append(parts[0].strip())
        if len(parts) > 1:
            digits = "".join(c for c in parts[1] if c.isdigit() or c == ".")
            try:
                latencies.append(float(digits))
            except ValueError:
                pass
    if len(latencies) != len(ips) or not ips:
        return ""            # 没有完整的延迟信息，不据此拦截
    return looks_suspicious(latencies, total=len(ips))


def _apply_saved_filters(platforms: list) -> list:
    """把手动下发也用上网页里保存的筛选设置（延迟门槛 / 数量 / 测速阈值）。

    为什么必须：手动下发以前只读代码里的默认值（门槛=0、数量=0、不测速），
    于是同一个结果文件，网页点「下发」只发 10 个，命令行却发全部 ——
    用户会以为网页上的设置没生效。这里读同一份 config.json，两边口径一致。
    """
    try:
        from eo.config_store import ConfigStore

        cfg = ConfigStore().get()
    except Exception as exc:                                   # noqa: BLE001
        safe_log(f"⚠ 读取 config.json 失败（{type(exc).__name__}: {exc}），按代码默认筛选")
        return platforms
    fields = ("max_latency_ms", "top_n", "speed_test", "min_speed_mbps")
    for platform in platforms:
        saved = cfg.platforms.get(getattr(platform, "key", ""))
        if not saved:
            continue
        for name in fields:
            value = getattr(saved, name, None)
            if value is not None:
                setattr(platform, name, value)
    return platforms


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.dry_run and not args.yes:
        safe_log("⚠ 该操作会修改线上 DNS 解析。确认请追加 --yes，或先用 --dry-run 预览。")
        return 2

    if args.platform:
        platforms = _apply_saved_filters(get_platforms(args.platform))
        failures = 0
        for platform in platforms:
            # 与自动下发路径保持一致：结果可疑就拒绝下发，除非显式 --force。
            # 否则手动执行时会把「链路被代理接管」的一批地址推到线上。
            reason = _suspicious_result_file(platform)
            if reason and not args.force:
                safe_log(f"✗ {platform.name} 拒绝下发：{reason}")
                safe_log("  确认要强行下发请追加 --force；建议先跑 python run.py doctor 检查链路")
                failures += 1
                continue
            if reason:
                safe_log(f"⚠ {platform.name} 结果可疑，但已指定 --force，继续下发")
            result = sync_platform(platform, dry_run=args.dry_run, force=args.force)
            safe_log(f"  -> {result.summary()}")
            if not result.ok:
                failures += 1
        return 1 if failures else 0

    # 单记录模式（老 updatedns.py 的用法）
    ips_file = Path(args.file) if args.file else ROOT / "yes.txt"
    eo_dns = _default_dns_config()
    record_name = args.record or eo_dns.get("record_name", "")
    if not record_name:
        safe_log("请用 --record 指定记录名")
        return 2
    if not ips_file.exists():
        safe_log(f"找不到 IP 列表文件: {ips_file}")
        return 2

    ips = [line.split("\t")[0].strip() for line in ips_file.read_text(encoding="utf-8").splitlines()
           if line.strip()]
    target = DnsTarget(
        zone_name=args.zone,
        record_name=record_name,
        ttl=args.ttl,
        batch_size=args.batch_size,
        access_key_id=eo_dns.get("access_key_id", ""),
        access_key_secret=eo_dns.get("access_key_secret", ""),
        region=eo_dns.get("region", "cn-north-4"),
    )
    safe_log(f"== {ips_file.name} -> {target.normalized_name()}（{len(ips)} 个 IP）==")
    result = sync_records(target, ips, dry_run=args.dry_run, force=args.force)
    safe_log(f"  -> {result.summary()}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
