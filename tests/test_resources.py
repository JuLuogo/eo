#!/usr/bin/env python3
"""并发自适应测试：防止小内存机器被自己的并发压垮。

背景：每个探测都 fork 一个 curl 进程（约 12MB）。默认 workers=80 × 3 个平台
并行 = 240 个进程 ≈ 2GB —— 实测会把 1.8GB 的小机器内存/swap 打满、负载飙到 50+，
机器直接卡死。所以 scan_platforms 开工前会按「可用内存 + 核数」压一次并发。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eo.core as core                                    # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


print("=" * 70)
print("并发自适应：按可用内存/核数限制同时运行的探测进程数")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) max_concurrent_probes：显式上限与边界")
# --------------------------------------------------------------------------
check("显式 limit 生效", core.max_concurrent_probes(32) == 32, str(core.max_concurrent_probes(32)))
check("太小被抬到 2（至少能跑）", core.max_concurrent_probes(0) == 2, str(core.max_concurrent_probes(0)))
check("太大被夹到 512", core.max_concurrent_probes(99999) == 512, str(core.max_concurrent_probes(99999)))

print()
print("2) 环境变量 EO_MAX_CONCURRENT 可覆盖")
# --------------------------------------------------------------------------
os.environ["EO_MAX_CONCURRENT"] = "16"
check("EO_MAX_CONCURRENT=16 生效", core.max_concurrent_probes() == 16, str(core.max_concurrent_probes()))
os.environ["EO_MAX_CONCURRENT"] = "abc"
v = core.max_concurrent_probes()
check("非法值不崩、回退自动推算", isinstance(v, int) and v >= 2, str(v))
os.environ.pop("EO_MAX_CONCURRENT", None)

print()
print("3) plan_workers：把总上限按平台数摊开")
# --------------------------------------------------------------------------
check("3 平台 / 总上限 30 -> 每平台 10",
      core.plan_workers(80, 3, parallel=True, limit=30) == 10,
      str(core.plan_workers(80, 3, parallel=True, limit=30)))
check("6 平台 / 总上限 30 -> 每平台 5",
      core.plan_workers(80, 6, parallel=True, limit=30) == 5,
      str(core.plan_workers(80, 6, parallel=True, limit=30)))
check("串行跑（不并行平台）时单平台可吃满上限",
      core.plan_workers(80, 3, parallel=False, limit=30) == 30,
      str(core.plan_workers(80, 3, parallel=False, limit=30)))
check("原本就低于上限时不上调",
      core.plan_workers(4, 3, parallel=True, limit=30) == 4,
      str(core.plan_workers(4, 3, parallel=True, limit=30)))
check("平台特别多时每平台保底 2 个（不至于单线程）",
      core.plan_workers(80, 40, parallel=True, limit=10) == 2,
      str(core.plan_workers(80, 40, parallel=True, limit=10)))

print()
print("4) 按本机规格推算的值是否合理")
# --------------------------------------------------------------------------
avail = core.available_memory_mb()
auto = core.max_concurrent_probes()
cores = os.cpu_count() or 4
print(f"     本机：{cores} 核，可用内存 {avail if avail is None else round(avail)} MB，"
      f"自动并发上限 {auto}")
check("自动值在 4~512 之间", 4 <= auto <= 512, str(auto))
if cores:
    check("不超过「每核 8 个」的 CPU 上限", auto <= max(4, cores * 8) or avail is None,
          f"{auto} vs {max(4, cores * 8)}")
if avail:
    check("按内存算不超过可用内存的 60%",
          auto * core.PER_PROBE_MB <= avail * 0.6 + core.PER_PROBE_MB,
          f"{auto}×{core.PER_PROBE_MB}MB vs {round(avail)}MB")

print()
print("5) 端到端接线：scan_platforms 真的把 workers 压下来了")
# --------------------------------------------------------------------------
captured: list[tuple[str, int]] = []
original = core.scan_platform


def fake_scan(platform, **kwargs):                       # noqa: ANN001, ANN003
    captured.append((platform.key, platform.workers))
    return core.ScanOutcome(platform, 0, [], 0, 0.0, "")


core.scan_platform = fake_scan                           # type: ignore[assignment]
try:
    os.environ["EO_MAX_CONCURRENT"] = "9"
    plats = [core.Platform(key=f"p{i}", name=f"P{i}", host="example.com",
                           workers=80, enabled=True)
             for i in range(3)]
    core.scan_platforms(plats, targets=["192.0.2.1"], log=lambda *_: None)
finally:
    core.scan_platform = original                        # type: ignore[assignment]
    os.environ.pop("EO_MAX_CONCURRENT", None)

check("三个平台都被调到 3（9 // 3）",
      sorted(captured) == [("p0", 3), ("p1", 3), ("p2", 3)],
      f"{sorted(captured)}（平台是并行扫描的，顺序不定）")
check("原始配置对象未被就地修改", all(p.workers == 80 for p in plats),
      str([p.workers for p in plats]))

print()
print("6) 只有单平台时不摊分（可以吃满上限）")
# --------------------------------------------------------------------------
captured.clear()
core.scan_platform = fake_scan                           # type: ignore[assignment]
try:
    os.environ["EO_MAX_CONCURRENT"] = "9"
    one = [core.Platform(key="only", name="Only", host="example.com",
                         workers=80, enabled=True)]
    core.scan_platforms(one, targets=["192.0.2.1"], log=lambda *_: None)
finally:
    core.scan_platform = original                        # type: ignore[assignment]
    os.environ.pop("EO_MAX_CONCURRENT", None)
check("单平台保留 9 个并发", captured == [("only", 9)], str(captured))

print()
print("=" * 70)
if _failures:
    print(f"并发自适应测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("并发自适应测试全部通过")
sys.exit(0)
