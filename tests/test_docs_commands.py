#!/usr/bin/env python3
"""文档命令可用性：README / INSTALL 里写的 run.py 命令必须真的能被解析。

为什么值得单独测：文档里的命令是用户第一手照抄的东西。
之前 `run.py scan -p eo`（基础名）就是文档里写的写法，实际会报「未知平台」——
这类问题只有把命令真的喂给解析器才能发现。

做法：从两个文档里抓出所有 `python run.py <子命令> [参数]`，
用真实的 argparse 解析一遍；含占位符（<...>、...、{}）的跳过。
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.cli import SUBCOMMANDS, build_parser, normalize_argv    # noqa: E402

_failures: list[str] = []
SKIP_TOKENS = ("<", ">", "...", "{", "}", "你的", "xxx", "……", "|")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)

print("=" * 70)
print("文档命令可用性")
print("=" * 70)

parser = build_parser()
subcommands = set(SUBCOMMANDS)
print(f"  CLI 子命令: {sorted(subcommands)}")

commands: list[tuple[str, str, str]] = []                          # (文件, 行号, 命令)
for name in ("README.md", "INSTALL.md", "docs/USAGE.md"):
    path = ROOT / name
    if not path.exists():
        continue
    for num, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for match in re.finditer(r"(?:python3?|\./run\.py)\s+run\.py\s+([^\n`]*)", line):
            commands.append((name, str(num), match.group(1).strip()))
        for match in re.finditer(r"(?<![\w./])run\.py\s+([a-z][^\n`]*)", line):
            text = match.group(1).strip()
            if text and (name, str(num), text) not in commands:
                commands.append((name, str(num), text))

print(f"  抓到 {len(commands)} 条可能被照抄的命令")

checked = skipped = 0
for name, num, text in commands:
    if any(tok in text for tok in SKIP_TOKENS):
        skipped += 1
        continue
    # 去掉行尾注释。注意「参数为空、后面直接跟注释」这种写法：
    #   python run.py                      # 扫描全部已启用平台
    # 此时捕获到的是 "# 扫描全部已启用平台"，若不处理会被当成参数去解析。
    text = re.sub(r"(?:^|\s+)#.*$", "", text).strip()
    if not text:
        skipped += 1
        continue
    try:
        argv = shlex.split(text)
    except ValueError:
        skipped += 1
        continue
    if not argv:
        skipped += 1
        continue
    # 必须走 main() 用的同一条归一化路径：`run.py -p eo` 会被补成 `scan -p eo`，
    # 直接 parse_args 会把它误判成「子命令 -p 不存在」。
    argv = normalize_argv(argv)
    if argv[0] not in subcommands:
        check(f"{name}:{num} 子命令 {argv[0]!r} 存在", False,
              f"可选: {sorted(subcommands)}")
        continue
    try:
        parser.parse_args(argv)
        checked += 1
    except SystemExit:
        _failures.append(f"{name}:{num}: run.py {text}")
        print(f"  [FAIL] {name}:{num} 命令无法解析 -> run.py {text}")

print(f"  实际校验 {checked} 条，跳过 {skipped} 条（含占位符或注释）")

print()
print("1) 文档里的 run.py 命令都能被解析")
check("没有解析失败的命令",
      not [f for f in _failures if "run.py" in f],
      "; ".join(f for f in _failures if "run.py" in f) or f"{checked} 条全部通过")

print()
print("2) 反向：CLI 的每个子命令都应该在文档里出现过")
docs = "\n".join((ROOT / n).read_text(encoding="utf-8", errors="replace")
                 for n in ("README.md", "INSTALL.md") if (ROOT / n).exists())
undocumented = sorted(s for s in subcommands if s not in docs)
check("每个子命令都有文档", not undocumented,
      f"未提及: {undocumented}" if undocumented else f"{len(subcommands)} 个都有")

print()
print("3) 文档里的关键参数要在帮助里能找到")
for flag, where in (("--max-ms", "README.md"), ("--top", "README.md"),
                    ("--sample-per-24", "README.md"), ("--deep", "README.md"),
                    ("--window", "README.md")):
    check(f"{flag} 在 {where} 有说明", flag in docs, "有" if flag in docs else "缺")

print()
print("4) 环境变量文档与代码一致（抽查几个容易漂的）")
env_doc = (ROOT / ".env.example").read_text(encoding="utf-8")
for var in ("EO_MAX_MS", "EO_TOP", "EO_WEB_AUTH", "EO_TARGETS_REFRESH_HOURS",
            "EO_DEEP_MAX_NETWORKS", "EO_MAX_CONCURRENT", "EO_PROBE"):
    check(f"{var} 在 .env.example 里", f"{var}=" in env_doc, "有" if f"{var}=" in env_doc else "缺")

print()
print("=" * 70)
if _failures:
    print(f"文档命令可用性：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("文档命令可用性检查通过")
sys.exit(0)
