#!/usr/bin/env python
"""限速器专项测试：验证「任意 1 秒窗口内 TCP 连接数 <= rate」这一硬要求。

对应需求：并发限制最好就是每秒 TCP 连接不超过 100。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eo.ratelimit import NullRateLimiter, RateLimiter, make_rate_limiter

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def max_in_window(times: list[float], window: float = 1.0) -> int:
    """计算任意长度为 window 的滑动窗口内的事件数最大值。"""
    ts = sorted(times)
    best = 0
    j = 0
    for i in range(len(ts)):
        # 窗口 [ts[i], ts[i]+window) 内的事件
        while j < len(ts) and ts[j] < ts[i] + window:
            j += 1
        best = max(best, j - i)
        if j <= i:
            j = i + 1
    return best


print("=" * 70)
print("限速器专项测试")
print("=" * 70)

# ---------------------------------------------------------------- 1. 单线程匀速
print()
print("1) 单线程、rate=100、持续 3 秒")
lim = RateLimiter(100.0)
stamps: list[float] = []
t0 = time.monotonic()
while time.monotonic() - t0 < 3.0:
    lim.acquire()
    stamps.append(time.monotonic())
elapsed = time.monotonic() - t0
rate = len(stamps) / elapsed
print(f"     实际: {len(stamps)} 次 / {elapsed:.2f}s = {rate:.1f} 次/秒")
check("平均速率 ≈ 100/s（±15%）", 85 <= rate <= 115, f"{rate:.1f}/s")
mw = max_in_window(stamps)
check("任意 1 秒窗口 <= 100", mw <= 100, f"实测窗口内最大 {mw} 次")

# ---------------------------------------------------------------- 2. 多线程
print()
print("2) 8 线程并发、rate=100、持续 3 秒（验证线程安全与全局上限）")
lim2 = RateLimiter(100.0)
stamps2: list[float] = []
lock = threading.Lock()
stop_at = time.monotonic() + 3.0


def worker() -> None:
    local: list[float] = []
    while time.monotonic() < stop_at:
        lim2.acquire()
        local.append(time.monotonic())
    with lock:
        stamps2.extend(local)


threads = [threading.Thread(target=worker) for _ in range(8)]
t0 = time.monotonic()
for th in threads:
    th.start()
for th in threads:
    th.join()
elapsed2 = time.monotonic() - t0
rate2 = len(stamps2) / elapsed2
print(f"     实际: {len(stamps2)} 次 / {elapsed2:.2f}s = {rate2:.1f} 次/秒")
check("8 线程下平均速率仍 ≈ 100/s", 85 <= rate2 <= 115, f"{rate2:.1f}/s")
mw2 = max_in_window(stamps2)
check("8 线程下任意 1 秒窗口 <= 100", mw2 <= 100, f"实测窗口内最大 {mw2} 次")

# 逐窗口统计，给出分布，确认不是靠平均掩盖
ts2 = sorted(stamps2)
windows = []
j = 0
for i in range(len(ts2)):
    while j < len(ts2) and ts2[j] < ts2[i] + 1.0:
        j += 1
    windows.append(j - i)
if windows:
    print(f"     滑动窗口样本 {len(windows)} 个，最大 {max(windows)}，"
          f"P99 {sorted(windows)[int(len(windows)*0.99)]}")

# ---------------------------------------------------------------- 3. 其它 rate
print()
print("3) 其它 rate 值")
for r in (10.0, 50.0, 200.0):
    lm = RateLimiter(r)
    ss: list[float] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.5:
        lm.acquire()
        ss.append(time.monotonic())
    el = time.monotonic() - t0
    actual = len(ss) / el
    m = max_in_window(ss)
    okr = abs(actual - r) / r < 0.15 and m <= r
    print(f"     rate={r:6.0f} -> 实测 {actual:7.1f}/s，窗口内最大 {m} 次")
    check(f"rate={r:g} 符合上限", okr, f"实测 {actual:.1f}/s 窗口max={m}")

# ---------------------------------------------------------------- 4. 边界与工厂
print()
print("4) 边界行为")
check("rate<=0 返回不限速实现", isinstance(make_rate_limiter(0), NullRateLimiter))
check("rate=None 返回不限速实现", isinstance(make_rate_limiter(None), NullRateLimiter))
check("rate>0 返回限速实现", isinstance(make_rate_limiter(100), RateLimiter))
try:
    RateLimiter(0)
    check("rate=0 构造应报错", False)
except ValueError:
    check("rate=0 构造报错", True)
nl = NullRateLimiter()
t0 = time.monotonic()
for _ in range(100000):
    nl.acquire()
check("不限速实现无阻塞", time.monotonic() - t0 < 1.0,
      f"10 万次耗时 {time.monotonic()-t0:.3f}s")

# ---------------------------------------------------------------- 5. 突发行为
print()
print("5) 空闲后不应积累令牌（防止突发打爆 NAT 表）")
lim5 = RateLimiter(100.0)
lim5.acquire()
time.sleep(2.0)                      # 空闲 2 秒，理论上"攒"了 200 个令牌
burst_stamps: list[float] = [time.monotonic()]
t0 = time.monotonic()
while time.monotonic() - t0 < 1.0:
    lim5.acquire()
    burst_stamps.append(time.monotonic())
m5 = max_in_window(burst_stamps)
print(f"     空闲 2 秒后 1 秒内发起 {len(burst_stamps)} 次，窗口最大 {m5}")
check("空闲后不突发（1 秒内不超过 rate）", m5 <= 100, f"实测 {m5}")

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项: {', '.join(failures)}")
    sys.exit(1)
print("限速器全部测试通过")
sys.exit(0)
