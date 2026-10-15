#!/usr/bin/env python
"""端到端限速验证：真实扫描是否受 --rate 约束（而不只是限速器类本身）。

做法：准备一小批**必定快速失败**的目标（保留地址 + 极短超时），
用很低的 rate 跑扫描，统计「探测完成的时刻」，再校验任意 1 秒窗口内的
连接次数是否 <= rate。这样能同时验证限速器确实接在探测路径上。
"""
from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

from eo.core import Platform, http_probe, scan_platform
from eo.platforms import PLATFORMS
from eo.ratelimit import make_rate_limiter

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def max_in_window(ts: list[float], window: float = 1.0) -> int:
    ts = sorted(ts)
    best = 0
    j = 0
    for i in range(len(ts)):
        while j < len(ts) and ts[j] < ts[i] + window:
            j += 1
        best = max(best, j - i)
        if j <= i:
            j = i + 1
    return best


print("=" * 70)
print("端到端限速验证（真实探测路径）")
print("=" * 70)

# 取一个真实平台配置，但把目标换成保留地址（RFC 5737，必定不可达）
base = PLATFORMS["eo-lt"]
TARGETS = [f"192.0.2.{i}" for i in range(1, 31)]      # 30 个保留地址

print()
print("1) rate=5 时跑 30 个必定失败的目标（超时 1s，避免拖太久）")
plat = dataclasses.replace(
    base,
    targets_file="",                 # 用显式 targets
    output_file="",
    history_file="",
    sample_per_24=0,
    deep_scan=False,
    timeout=1.0,
    retries=0,                       # 不重试，便于精确计数
    workers=10,                      # 并发足够大，瓶颈只应是限速器
)

RATE = 5
limiter = make_rate_limiter(RATE)
stamps: list[float] = []
t0 = time.monotonic()

outcome = scan_platform(
    plat, targets=TARGETS, log=lambda *_: None, quiet=True, limiter=limiter,
)
elapsed = time.monotonic() - t0
# 用限速器自身的计数（每次真实连接取一个令牌）
acquired = limiter.acquired
print(f"     目标 {len(TARGETS)} 个，用时 {elapsed:.2f}s，限速器放行 {acquired} 次")
print(f"     平均 {acquired / max(elapsed, 1e-6):.2f} 连接/秒（上限 {RATE}）")

check("限速器确实被调用（接入探测路径）", acquired > 0, str(acquired))
check(f"总连接数不超过 rate*时长+余量",
      acquired <= RATE * elapsed + 2, f"{acquired} vs {RATE * elapsed:.1f}+2")
# 30 个目标、rate=5 -> 至少需要约 (30-1)/5 ≈ 5.8 秒
check("扫描时长符合限速预期（>= (n-1)/rate）",
      elapsed >= (len(TARGETS) - 1) / RATE * 0.8,
      f"{elapsed:.2f}s >= {(len(TARGETS)-1)/RATE*0.8:.2f}s")
rate_avg = acquired / max(elapsed, 1e-6)
check(f"实测平均速率 <= rate", rate_avg <= RATE * 1.15,
      f"{rate_avg:.2f} vs {RATE}")

print()
print("2) 用真实的探测时刻做滑动窗口校验")
# 重新跑一次，记录每次探测开始的时间（http_probe 由 scan 内部调用，
# 这里直接用 limiter.acquire 的时刻近似真实连接时刻）
lim2 = make_rate_limiter(RATE)
plat2 = dataclasses.replace(plat, workers=8)
marks: list[float] = []
orig_acquire = lim2.acquire


def timed_acquire() -> None:
    orig_acquire()
    marks.append(time.monotonic())


lim2.acquire = timed_acquire          # type: ignore[method-assign]
scan_platform(plat2, targets=TARGETS, log=lambda *_: None, quiet=True, limiter=lim2)
mw = max_in_window(marks)
print(f"     连接时刻 {len(marks)} 个，任意 1 秒窗口最大 {mw} 次")
check(f"任意 1 秒窗口 <= {RATE}", mw <= RATE, f"实测 {mw}")

print()
print("3) 不限速时应明显更快（证明限速确实在起作用）")
plat3 = dataclasses.replace(plat, workers=10)
t0 = time.monotonic()
nil = make_rate_limiter(0)
scan_platform(plat3, targets=TARGETS, log=lambda *_: None, quiet=True, limiter=nil)
fast = time.monotonic() - t0
print(f"     不限速用时 {fast:.2f}s；限速 {RATE}/s 用时 {elapsed:.2f}s")
check("不限速比限速快", fast < elapsed, f"{fast:.2f}s < {elapsed:.2f}s")

print()
print("4) 默认上限就是 100（用户的硬要求）")
import eo.cli as cli
check("CLI 默认 rate = 100", float(cli.DEFAULT_RATE) == 100.0, str(cli.DEFAULT_RATE))
# 不依赖 pyyaml：直接文本匹配 compose 里的默认值
compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
check("compose 默认 EO_RATE=100", 'EO_RATE: "${EO_RATE:-100}"' in compose_text,
      next((l.strip() for l in compose_text.splitlines() if "EO_RATE" in l), "(未找到)"))
check("compose 挂载了 config/results/targets",
      all(p in compose_text for p in ("./config:/app/config", "./results:/app/results",
                                      "./targets:/app/targets")))

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项: {', '.join(failures)}")
    sys.exit(1)
print("端到端限速验证通过")
sys.exit(0)
