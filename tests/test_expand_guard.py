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
print("=" * 70)
if _failures:
    print(f"超大网段保护测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("超大网段保护测试全部通过")
sys.exit(0)
