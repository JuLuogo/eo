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
check("允许低到 1（完全串行）", core.max_concurrent_probes(1) == 1, str(core.max_concurrent_probes(1)))
check("0 或负数被抬到 1", core.max_concurrent_probes(0) == 1, str(core.max_concurrent_probes(0)))
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
check("平台特别多时每平台保底 1 个（允许串行）",
      core.plan_workers(80, 40, parallel=True, limit=10) == 1,
      str(core.plan_workers(80, 40, parallel=True, limit=10)))
check("显式要 1 个并发就是 1",
      core.plan_workers(80, 1, parallel=False, limit=1) == 1,
      str(core.plan_workers(80, 1, parallel=False, limit=1)))

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
          auto * core.probe_memory_mb() <= avail * 0.6 + core.probe_memory_mb(),
          f"{auto}×{core.probe_memory_mb()}MB vs {round(avail)}MB")
check("默认实现下每个并发只按几十 KB 估算（不再是 curl 的 12MB）",
      core.probe_memory_mb() <= 1.0, f"{core.probe_memory_mb()}MB")
os.environ["EO_PROBE"] = "curl"
check("切到 curl 实现时按 12MB 估算", core.probe_memory_mb() == core.PER_PROBE_MB,
      f"{core.probe_memory_mb()}MB")
os.environ.pop("EO_PROBE", None)

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
print("7) 总并发小于平台数时，平台之间自动串行")
# --------------------------------------------------------------------------
captured.clear()
logs: list[str] = []


def fake_serial(platform, **kwargs):                     # noqa: ANN001, ANN003
    captured.append((platform.key, platform.workers))
    return core.ScanOutcome(platform, 0, [], 0, 0.0, "")


core.scan_platform = fake_serial                         # type: ignore[assignment]
try:
    os.environ["EO_MAX_CONCURRENT"] = "2"
    three = [core.Platform(key=f"s{i}", name=f"S{i}", host="example.com",
                           workers=80, enabled=True)
             for i in range(3)]
    core.scan_platforms(three, targets=["192.0.2.1"], log=logs.append)
finally:
    core.scan_platform = original                        # type: ignore[assignment]
    os.environ.pop("EO_MAX_CONCURRENT", None)

check("3 个平台 / 上限 2 -> 每平台 2 个（靠串行保证总数不超）",
      sorted(captured) == [("s0", 2), ("s1", 2), ("s2", 2)], str(sorted(captured)))
check("三个平台都真的跑了",
      sorted(k for k, _ in captured) == ["s0", "s1", "s2"], str(sorted(captured)))
check("日志说明了改成串行",
      any("串行" in line for line in logs), str(logs[:3]))

print()
print("8) 平台逐个扫描（默认）：每个平台必须拿满并发，不能按平台数切分")
# --------------------------------------------------------------------------
# 用户要的行为：先 EO 扫完、再 ESA、再 CFT，当前平台独占全部并发额度。
# 并行时额度是"总量÷平台数"固定切分，某个平台先扫完它那份额度就闲置了 ——
# 如果瓶颈是并发数（而不是全局限速），那部分算力就白扔。
from eo.runner import RunConfig                                   # noqa: E402

captured.clear()
core.scan_platform = fake_scan                                    # type: ignore[assignment]
try:
    os.environ["EO_MAX_CONCURRENT"] = "9"
    three = [core.Platform(key=f"q{i}", name=f"Q{i}", host="example.com",
                           workers=80, enabled=True) for i in range(3)]
    logs8: list[str] = []
    core.scan_platforms(three, targets=["192.0.2.1"], log=logs8.append,
                        parallel_platforms=False)          # 逐平台（runner 会这样传）
finally:
    core.scan_platform = original                                  # type: ignore[assignment]
    os.environ.pop("EO_MAX_CONCURRENT", None)
check("逐个扫描时每个平台都拿满 9 个（不是 3 个）",
      sorted(captured) == [("q0", 9), ("q1", 9), ("q2", 9)], str(sorted(captured)))
check("执行顺序就是平台顺序（先 q0 再 q1 再 q2）",
      [k for k, _w in captured] == ["q0", "q1", "q2"], str([k for k, _w in captured]))
check("日志说明了逐个扫描与并发数",
      any("逐个扫描" in l and "9 个并发" in l for l in logs8),
      str([l for l in logs8 if "逐个" in l][:1]))
check("日志里列出了平台顺序",
      any("Q0 -> Q1 -> Q2" in l for l in logs8),
      str([l for l in logs8 if "->" in l][:1]))

print()
print("9) 扫描顺序由开关控制（默认逐个，可切回并行）")
# --------------------------------------------------------------------------
check("RunConfig 默认是 sequential", RunConfig().platform_order == "sequential",
      RunConfig().platform_order)

from eo.config_store import AppConfig                             # noqa: E402

check("AppConfig 默认也是 sequential", AppConfig().platform_order == "sequential",
      AppConfig().platform_order)

# runner 传参的接线（静态断言，避免"改了默认值但没接线"）
runner_src = (ROOT / "eo" / "runner.py").read_text(encoding="utf-8")
check("run_once 会把顺序传成 parallel_platforms",
      "parallel_platforms=not sequential" in runner_src, "已接线")
webui_src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
check("网页端传入平台顺序", "platform_order=cfg.platform_order" in webui_src, "已接线")
pages_src = (ROOT / "eo" / "pages.py").read_text(encoding="utf-8")
check("网页上有这个开关", "c-seqplatforms" in pages_src, "有")

# 环境变量能改
from eo.config_store import env_overrides                          # noqa: E402

os.environ["EO_PLATFORM_ORDER"] = "parallel"
try:
    check("EO_PLATFORM_ORDER=parallel 会被读进配置",
          env_overrides().get("platform_order") == "parallel",
          str(env_overrides().get("platform_order")))
finally:
    os.environ.pop("EO_PLATFORM_ORDER", None)
check("不设环境变量时读到默认值",
      env_overrides().get("platform_order") in (None, "sequential"),
      str(env_overrides().get("platform_order")))

print()
print("=" * 70)
if _failures:
    print(f"并发自适应测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("并发自适应测试全部通过")
sys.exit(0)
