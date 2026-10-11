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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.dry_run and not args.yes:
        safe_log("⚠ 该操作会修改线上 DNS 解析。确认请追加 --yes，或先用 --dry-run 预览。")
        return 2

    if args.platform:
        platforms = get_platforms(args.platform)
        failures = 0
        for platform in platforms:
            result = sync_platform(platform, dry_run=args.dry_run, force=args.force)
            safe_log(f"  -> {result.summary()}")
            if not result.ok:
                failures += 1
        return 1 if failures else 0

    # 单记录模式（老 updatedns.py 的用法）
    ips_file = Path(args.file) if args.file else ROOT / "yes.txt"
    eo_dns = PLATFORMS["eo"].dns
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
