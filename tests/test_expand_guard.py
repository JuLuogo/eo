#!/usr/bin/env python3
"""验证超大网段必须在「分配之前」就被拦住。

背景：expand_targets("10.0.0.0/8") 原来先把 1677 万个地址展开成 list，
再判断是否超上限 —— 保护等于没有，直接 OOM。自检 cmd_selftest 里正好有这一句，
而它被 HEALTHCHECK 每 2 分钟调用一次，于是小机器一启动容器就内存打满、CPU 100%。
"""
from __future__ import annotations

import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.core import MAX_EXPAND_HOSTS, expand_targets, load_targets   # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


print("=" * 70)
print("超大网段保护：必须先判数量、再展开")
print("=" * 70)

print()
print("1) /8 这种超大网段要立刻抛错，且不能吃内存")
before = rss_mb()
t0 = time.perf_counter()
raised = False
try:
    expand_targets("10.0.0.0/8")
except ValueError as exc:
    raised = True
    msg = str(exc)
dt = time.perf_counter() - t0
after = rss_mb()
check("抛出 ValueError", raised, msg if raised else "没有抛异常")
check("耗时很短（<0.5s，说明没去展开）", dt < 0.5, f"{dt*1000:.1f}ms")
check("内存增长可忽略（<50MB）", after - before < 50, f"+{after - before:.1f}MB（峰值 {after:.1f}MB）")
check("错误信息里带上限与数量", "上限" in msg and "16777214" in msg, msg)

print()
print("2) 刚过上限的网段也要拦住（/12 = 1048574 个，恰好在 1_048_576 以内，应放行）")
try:
    got = expand_targets("10.0.0.0/12")
    check("/12 放行", len(got) == 1048574, f"{len(got)} 个")
except ValueError as exc:
    check("/12 放行", False, f"被拒绝：{exc}")

try:
    expand_targets("10.0.0.0/11")
    check("/11 被拒绝", False, "没有抛异常")
except ValueError:
    check("/11 被拒绝", True, "2097150 > 1048576")

print()
print("3) 起止区间同样先判后展开")
raised = False
try:
    expand_targets("10.0.0.0-10.255.255.255")
except ValueError:
    raised = True
check("超大区间抛错", raised)

print()
print("4) 正常用法不受影响")
check("/24", len(expand_targets("192.0.2.0/24")) == 254)
check("单 IP", expand_targets("1.2.3.4") == ["1.2.3.4"])
check("小范围区间", expand_targets("1.2.3.1-1.2.3.4") ==
      ["1.2.3.1", "1.2.3.2", "1.2.3.3", "1.2.3.4"])
check("注释", expand_targets("# x") == [])
check("非法输入", expand_targets("not-an-ip") == [])

print()
print("5) 网段文件里混进超大网段：load_targets 要报错而不是 OOM")
import tempfile                                                  # noqa: E402

with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
    fh.write("192.0.2.0/24\n10.0.0.0/8\n")
    path = Path(fh.name)
before = rss_mb()
raised = False
try:
    load_targets(path, sample_per_24=0)
except ValueError as exc:
    raised = True
    msg2 = str(exc)
after = rss_mb()
check("load_targets 抛 ValueError", raised, msg2 if raised else "没有抛")
check("同样没吃内存", after - before < 50, f"+{after - before:.1f}MB")
path.unlink(missing_ok=True)

print()
print("6) 自检本身现在能在限内存下跑完（模拟健康检查）")
import subprocess                                                # noqa: E402
t0 = time.perf_counter()
proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "selftest"],
                      capture_output=True, text=True, timeout=120,
                      env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
dt = time.perf_counter() - t0
check("run.py selftest 退出码 0", proc.returncode == 0,
      f"rc={proc.returncode} 最后一行：{(proc.stdout or '').strip().splitlines()[-1:]}")
check("自检跑得很快（<20s）", dt < 20, f"{dt:.1f}s")

print()
print("7) 深扫总量上限：命中很多 /24 时不能无限累积")
from eo.core import (DEEP_MAX_NETWORKS as DNET, _expand_networks_capped,   # noqa: E402
                     parent_24s, sample_network)
import ipaddress                                                          # noqa: E402

nets = [f"10.{i // 256}.{i % 256}.0/24" for i in range(5000)]
logs: list[str] = []
before = rss_mb()
t0 = time.perf_counter()
got = _expand_networks_capped(nets, log=logs.append, platform_name="Test")
dt = time.perf_counter() - t0
grow = rss_mb() - before
check("网段数被截断到 DEEP_MAX_NETWORKS",
      len(got) == DNET * 254, f"{len(got)} 个地址（{DNET} 个 /24 × 254）")
check("总量不超过 MAX_EXPAND_HOSTS", len(got) <= MAX_EXPAND_HOSTS, f"{len(got)}")
check("内存增长可控（<120MB）", grow < 120, f"+{grow:.1f}MB  {dt:.2f}s")
check("日志说明了截断", any("深扫范围过大" in line for line in logs),
      str([l for l in logs if "深扫" in l][:1]))
check("截断后取的是前 N 个（结果按延迟排序，前面的更优）",
      got[:1] == [str(ipaddress.ip_address(int(ipaddress.ip_network(nets[0]).network_address) + 1))],
      f"{got[:1]}")

small = _expand_networks_capped(nets[:3], log=None, platform_name="Test")
check("网段数在上限内时全量展开", len(small) == 3 * 254, f"{len(small)}")

print()
print("8) parent_24s 不会因为大量命中而失控")
seeds = [f"10.{i // 256}.{i % 256}.{j}" for i in range(2000) for j in (1, 5)]
t0 = time.perf_counter()
ns = parent_24s(seeds)
check("4000 个 IP 归并成 2000 个 /24", len(ns) == 2000, f"{len(ns)}  {time.perf_counter()-t0:.2f}s")

print()
print("9) sample_network 在大网段上有界（按 /24 取代表，不物化全部）")
t0 = time.perf_counter()
picks = sample_network(ipaddress.ip_network("10.0.0.0/8"), 4)
dt = time.perf_counter() - t0
check("65536 个 /24 × 4 = 262144 个代表地址", len(picks) == 262144, f"{len(picks)}  {dt:.2f}s")
check("抽样结果数量由 per_24 决定，与网段总大小无关", len(picks) == 65536 * 4, str(len(picks)))

print()
print("10) 全模块顶层定义不得重复（重复定义会静默覆盖，改动白做）")
import ast                                                                # noqa: E402

dup_found: list[str] = []
for py in sorted((ROOT / "eo").glob("*.py")):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        dup_found.append(f"{py.name}: {sorted(dups)}")
check("eo/*.py 没有重复的顶层函数/类", not dup_found, "; ".join(dup_found) or "干净")

print()
print("=" * 70)
if _failures:
    print(f"超大网段保护测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("超大网段保护测试全部通过")
sys.exit(0)
