#!/usr/bin/env python
"""调度器（定时任务）专项测试：用假 job，不依赖网络。

覆盖用户需求：通过 web 页面管理「间隔多长时间跑一轮」+ 定时任务。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eo.runner import CycleRunner, in_window

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


print("=" * 70)
print("调度器专项测试")
print("=" * 70)

print()
print("1) 基本循环：3 轮后停止")
runs: list[int] = []
rn = CycleRunner(emit=lambda e: None)
rn.start(lambda cyc, stop: runs.append(cyc), interval=1, cycles=3)
ok = rn.wait(timeout=30) if hasattr(rn, "wait") else None
t0 = time.time()
while rn.running and time.time() - t0 < 30:
    time.sleep(0.2)
print(f"     执行轮次: {runs}")
check("按 cycles=3 执行", runs == [1, 2, 3], str(runs))
check("结束后 running=False", not rn.running, str(rn.running))
check("结束后 state=idle", rn.state == "idle", rn.state)

print()
print("2) 间隔热更新：每轮开始前重新读取设置")
seen: list[int] = []
rn2 = CycleRunner(emit=lambda e: None)
settings_calls: list[int] = []
seq = [5, 1, 1]


def settings() -> dict:
    # 每次被调用就切换到下一个间隔，模拟网页上改间隔
    idx = min(len(settings_calls), len(seq) - 1)
    settings_calls.append(idx)
    seen.append(seq[idx])
    return {"interval": seq[idx], "window": ""}


rn2.start(lambda cyc, stop: None, interval=99, cycles=4, window="")
rn2._settings = settings              # 直接注入（等价于 web 端的 _loop_settings）
rn2.stop()
# 重新起一个，这次带上 settings
rn2 = CycleRunner(emit=lambda e: None)
rn2._settings = settings
settings_calls.clear()
seen.clear()
rn2.start(lambda cyc, stop: None, interval=99, cycles=4, window="")
t0 = time.time()
while rn2.running and time.time() - t0 < 40:
    time.sleep(0.2)
print(f"     settings 调用次数: {len(settings_calls)}，间隔序列: {seen}")
check("每轮开始前都调用了 settings", len(settings_calls) >= 4, str(len(settings_calls)))
check("间隔按 settings 生效（5->1->1...）", seen[:3] == [5, 1, 1], str(seen[:4]))

print()
print("3) next_run_at：轮次之间给出下次执行时间")
samples: list[tuple[str, float]] = []
rn3 = CycleRunner(emit=lambda e: None, settings=lambda: {"interval": 2, "window": ""})
rn3.start(lambda cyc, stop: time.sleep(0.2), interval=2, cycles=3)
t0 = time.time()
while rn3.running and time.time() - t0 < 30:
    samples.append((rn3.state, rn3.next_run_at))
    time.sleep(0.1)
sleeping = [n for s, n in samples if s == "sleeping"]
running = [n for s, n in samples if s == "running"]
check("等待期间有 next_run_at（倒计时可用）",
      any(n > 0 for n in sleeping), f"sleeping 样本 {len(sleeping)}，非零 {sum(1 for n in sleeping if n>0)}")
check("执行期间 next_run_at 归零（UI 显示本轮进行中）",
      all(n == 0 for n in running), f"running 样本 {len(running)}")
check("结束后 next_run_at 归零", rn3.next_run_at == 0.0, str(rn3.next_run_at))

print()
print("4) 停止响应：等待期间也能立即叫醒")
rn4 = CycleRunner(emit=lambda e: None)
rn4.start(lambda cyc, stop: None, interval=3600, cycles=0)   # 间隔 1 小时
time.sleep(1.5)
check("已进入 sleeping（长间隔）", rn4.state == "sleeping", rn4.state)
t0 = time.time()
rn4.stop()
while rn4.running and time.time() - t0 < 10:
    time.sleep(0.1)
dt = time.time() - t0
check("sleep 期间停止立即生效（<5s）", dt < 5, f"{dt:.2f}s")
check("停止后 state=idle", rn4.state == "idle", rn4.state)

print()
print("5) 停止响应：执行期间通过 stop_event 中断")
stop_seen: list[bool] = []


def slow_job(cyc: int, stop: threading.Event) -> None:
    for _ in range(100):
        if stop.is_set():
            stop_seen.append(True)
            return
        time.sleep(0.1)
    stop_seen.append(False)


rn5 = CycleRunner(emit=lambda e: None)
rn5.start(slow_job, interval=1, cycles=0)
time.sleep(1.0)
rn5.stop()
t0 = time.time()
while rn5.running and time.time() - t0 < 15:
    time.sleep(0.1)
dt = time.time() - t0
check("执行期间 stop_event 被置位（job 能感知）", any(stop_seen), str(stop_seen[:3]))
check("停止在 15s 内生效", dt < 15, f"{dt:.2f}s")

print()
print("6) 时间窗口判断")
check("全天（空窗口）始终在窗口内", in_window(""))
check("当前时刻所在的一分钟窗口", in_window(time.strftime("%H:%M") + "-23:59")
      or in_window("00:00-" + time.strftime("%H:%M")))
check("跨零点窗口可解析", in_window("23:00-01:00") in (True, False))

print()
print("7) 并发启动保护")
rn7 = CycleRunner(emit=lambda e: None)
rn7.start(lambda cyc, stop: time.sleep(3), interval=60, cycles=0)
second = rn7.start(lambda cyc, stop: None, interval=60, cycles=0)
check("运行中重复 start 返回 False", second is False, str(second))
rn7.stop()
t0 = time.time()
while rn7.running and time.time() - t0 < 10:
    time.sleep(0.1)

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项: {', '.join(failures)}")
    sys.exit(1)
print("调度器全部测试通过")
sys.exit(0)
