#!/usr/bin/env python3
"""时间窗口（--window）边界测试。

背景：原实现把 ``25:00-07:00`` 这种写错的窗口算成 start=1500 > end=420，
于是永远判定"不在窗口内" —— 监控静默停摆，日志只有一句"不在时间窗口内，等待"。
而 ``abc`` 这类解析失败却返回 True（一直跑），同一种错误两种相反结果。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.runner import in_window, parse_window, window_problem   # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def at(hh: int, mm: int, spec: str) -> bool:
    """把系统时间假装成 hh:mm 再判断窗口。

    用 time.localtime 的补丁：in_window 只取 tm_hour/tm_min，
    所以构造一个最小对象即可。
    """
    real = time.localtime
    time.localtime = lambda *a: time.struct_time(       # type: ignore[assignment]
        (2026, 1, 1, hh, mm, 0, 3, 1, 0))
    try:
        return in_window(spec)
    finally:
        time.localtime = real                            # type: ignore[assignment]


print("=" * 70)
print("时间窗口边界")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 正常窗口（含端点）")
# --------------------------------------------------------------------------
check("00:00-07:00 在 03:00 内", at(3, 0, "00:00-07:00") is True)
check("00:00-07:00 在 07:00（端点）内", at(7, 0, "00:00-07:00") is True)
check("00:00-07:00 在 07:01 外", at(7, 1, "00:00-07:00") is False)
check("00:00-07:00 在 00:00（端点）内", at(0, 0, "00:00-07:00") is True)
check("00:00-07:00 在 23:59 外", at(23, 59, "00:00-07:00") is False)

print()
print("2) 跨零点窗口")
# --------------------------------------------------------------------------
check("23:00-01:00 在 23:30 内", at(23, 30, "23:00-01:00") is True)
check("23:00-01:00 在 00:30 内", at(0, 30, "23:00-01:00") is True)
check("23:00-01:00 在 01:00（端点）内", at(1, 0, "23:00-01:00") is True)
check("23:00-01:00 在 01:01 外", at(1, 1, "23:00-01:00") is False)
check("23:00-01:00 在 12:00 外", at(12, 0, "23:00-01:00") is False)
check("22:00-02:00 在 00:00 内", at(0, 0, "22:00-02:00") is True)

print()
print("3) 非法写法必须「继续跑」，绝不能静默停摆（本轮修的 bug）")
# --------------------------------------------------------------------------
for bad in ("25:00-07:00", "99:99-99:99", "12:70-13:00", "24:00-06:00",
            "-", "abc", "12:00", "", "  ", "12:00-13:00-14:00", "aa:bb-cc:dd"):
    result = at(3, 0, bad)
    check(f"非法写法 {bad!r} 返回 True（继续跑）", result is True, str(result))

check("parse_window 对非法输入返回 None", parse_window("25:00-07:00") is None)
check("parse_window 对空串返回 None", parse_window("") is None)
check("parse_window 正常解析", parse_window("23:00-01:00") == (1380, 60),
      str(parse_window("23:00-01:00")))

print()
print("4) window_problem 给出可操作的提示")
# --------------------------------------------------------------------------
check("合法写法无提示", window_problem("00:00-07:00") == "", repr(window_problem("00:00-07:00")))
check("留空无提示", window_problem("") == "")
check("跨零点无提示", window_problem("23:00-01:00") == "")

p = window_problem("25:00-07:00")
check("非法写法有提示", bool(p), p)
check("提示里说明会被忽略并全天运行", "全天" in p, p)
check("提示里带上正确格式示例", "HH:MM-HH:MM" in p, p)

p2 = window_problem("05:00-05:00")
check("起止相同时有提示", bool(p2), p2)
check("提示里建议留空", "留空" in p2, p2)

print()
print("5) 回归：以前的行为不能被改坏")
# --------------------------------------------------------------------------
check("05:00-05:00 仍然只在那一分钟内有效", at(5, 0, "05:00-05:00") is True
      and at(5, 1, "05:00-05:00") is False)
check("00:00-23:59 全天有效", at(0, 0, "00:00-23:59") is True
      and at(23, 59, "00:00-23:59") is True)

print()
print("6) 循环里会提醒（只提醒一次，不刷屏）")
# --------------------------------------------------------------------------
from eo.runner import CycleRunner    # noqa: E402

events: list[dict] = []
runner = CycleRunner(emit=events.append)
runner._settings = lambda: {"interval": 1, "window": "25:00-07:00"}   # type: ignore[assignment]
runner.start(lambda c, s: None, interval=1, cycles=2, window="25:00-07:00")
deadline = time.time() + 20
while runner.running and time.time() < deadline:
    time.sleep(0.2)
runner.stop()
lines = [e.get("line", "") for e in events if e.get("type") == "log"]
warns = [l for l in lines if "无法识别" in l]
check("循环里发出了窗口写法警告", bool(warns), str(warns[:1])[:90])
check("只提醒一次（不刷屏）", len(warns) == 1, f"{len(warns)} 次")
check("窗口不合法时仍然执行了任务（没有静默停摆）",
      any(e.get("type") == "state" and e.get("state") == "running" for e in events),
      "有 running 事件")

print()
print("7) 窗口外的一轮要说明「仍计入 --cycles」（否则用户以为跑了 N 轮）")
# --------------------------------------------------------------------------
import sys as _sys                                                  # noqa: E402

events7: list[dict] = []
r7 = CycleRunner(emit=events7.append)
r7.start(lambda c, s: None, interval=1, cycles=2, window="03:00-03:59")
deadline7 = time.time() + 25
while r7.running and time.time() < deadline7:
    time.sleep(0.3)
r7.stop()
lines7 = [e.get("line", "") for e in events7 if e.get("type") == "log"]
waits7 = [l for l in lines7 if "不在时间窗口" in l]
check("窗口外产生了等待日志", len(waits7) >= 2, f"{len(waits7)} 条")
check("等待日志说明了仍计入 --cycles", all("计入 --cycles" in l for l in waits7),
      str(waits7[:1])[:90])
check("窗口外也会跑满 cycles（不会干等到窗口开始）", r7.cycle == 2, str(r7.cycle))

# CLI 帮助里也要说清楚
cli_src = (ROOT / "eo" / "cli.py").read_text(encoding="utf-8")
check("--cycles 的帮助文本说明了这一点", "窗口外的等待也算一轮" in cli_src,
      "已说明")
check("--window 的帮助文本说明了非法写法的处理",
      "写法无法识别时会被忽略" in cli_src, "已说明")

print()
print("=" * 70)
if _failures:
    print(f"时间窗口测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("时间窗口测试全部通过")
sys.exit(0)
