#!/usr/bin/env python
"""日志环形缓冲专项测试。

对应需求：日志不保留，只实时，差不多保留 500 条左右，动态删除。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eo.webui as webui
from eo.webui import LogBuffer

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


print("=" * 70)
print("日志环形缓冲专项测试")
print("=" * 70)

print()
print("1) 默认容量")
default_cap = getattr(webui, "DEFAULT_LOG_LINES", None)
print(f"     DEFAULT_LOG_LINES = {default_cap}")
check("默认容量为 500（需求值）", default_cap == 500, f"实际 {default_cap}")
lb_default = LogBuffer()
check("无参构造容量 = 500", lb_default.capacity() == 500, f"实际 {lb_default.capacity()}")

print()
print("2) 写入 2000 条 -> 只保留最近 500 条")
lb = LogBuffer(500)
for i in range(1, 2001):
    lb.append(f"日志行 {i}")
items = lb.tail(10000)
print(f"     缓冲内条数: {len(items)}")
check("缓冲条数恒定 500", len(items) == 500, f"实际 {len(items)}")
check("保留的是最新的（最后一条 = 日志行 2000）",
      items[-1]["line"] == "日志行 2000", items[-1]["line"])
check("最旧的是第 1501 条（前 1500 条被动态删除）",
      items[0]["line"] == "日志行 1501", items[0]["line"])
check("seq 单调递增且不因丢弃而重置",
      items[0]["seq"] == 1501 and items[-1]["seq"] == 2000,
      f'seq {items[0]["seq"]}..{items[-1]["seq"]}')
check("last_seq = 2000", lb.last_seq == 2000, str(lb.last_seq))

print()
print("3) 内存不无限增长（持续写入 5 万条）")
lb2 = LogBuffer(500)
for i in range(50000):
    lb2.append(f"x{i}")
check("5 万次写入后仍只保留 500 条", len(lb2.tail(99999)) == 500)
check("last_seq = 50000", lb2.last_seq == 50000, str(lb2.last_seq))

print()
print("4) 运行时调整容量（网页上可改日志保留条数）")
lb3 = LogBuffer(500)
for i in range(1, 601):
    lb3.append(f"L{i}")
lb3.set_capacity(200)
its = lb3.tail(10000)
print(f"     调整为 200 后：条数 {len(its)}，最旧 {its[0]['line']}，最新 {its[-1]['line']}")
check("缩容后立即只保留 200 条", len(its) == 200, str(len(its)))
check("缩容保留最新数据（最旧= L401）", its[0]["line"] == "L401", its[0]["line"])
lb3.set_capacity(800)
for i in range(601, 901):
    lb3.append(f"L{i}")
its2 = lb3.tail(10000)
print(f"     扩容为 800 后写入到 900：条数 {len(its2)}")
check("扩容后按新上限保留", len(its2) == 500 and its2[-1]["line"] == "L900",
      f"{len(its2)} 条，最后 {its2[-1]['line']}")
check("容量下限保护（<50 会被提到 50）", LogBuffer(1).capacity() == 50,
      str(LogBuffer(1).capacity()))

print()
print("5) since(seq) —— SSE 增量推送用")
lb4 = LogBuffer(500)
for i in range(1, 101):
    lb4.append(f"M{i}")
new = lb4.since(90)
print(f"     since(90) 返回 {len(new)} 条，首条 seq={new[0]['seq']}")
check("since 只返回更新的条目", len(new) == 10 and new[0]["seq"] == 91, str(len(new)))
check("since(0) 返回全部", len(lb4.since(0)) == 100, str(len(lb4.since(0))))
check("since(最新) 返回空", len(lb4.since(100)) == 0)
# 客户端落后超过缓冲时，只能拿到还在缓冲区里的部分（这是环形缓冲的预期行为）
lb5 = LogBuffer(500)
for i in range(1, 1001):
    lb5.append(f"N{i}")
stale = lb5.since(0)
print(f"     客户端 seq=0 落后时，since 返回 {len(stale)} 条（受缓冲上限约束）")
check("落后客户端不会拿到已丢弃的历史", len(stale) == 500, str(len(stale)))

print()
print("6) 线程安全（多线程并发写入）")
lb6 = LogBuffer(500)
errs: list[str] = []


def writer(tid: int) -> None:
    try:
        for i in range(2000):
            lb6.append(f"T{tid}-{i}")
    except Exception as e:                       # pragma: no cover
        errs.append(f"{type(e).__name__}: {e}")


ths = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("并发写入无异常", not errs, "; ".join(errs[:3]))
check("并发写入后仍为 500 条", len(lb6.tail(9999)) == 500)
check("并发写入总计数正确（8*2000）", lb6.last_seq == 16000, str(lb6.last_seq))
seqs = [x["seq"] for x in lb6.tail(9999)]
# 8 线程 × 2000 条 = 16000 条，保留最后 500 条 -> seq 应为 15501..16000
check("并发下 seq 严格连续无重复", seqs == list(range(15501, 16001)),
      f"{seqs[0]}..{seqs[-1]}，去重后 {len(set(seqs))}")

print()
print("7) 条目结构（网页渲染需要）")
one = LogBuffer(500).append("测试", level="error")
check("含 seq/time/level/line 字段",
      all(k in one for k in ("seq", "time", "level", "line")), str(sorted(one)))
check("level 可指定", one["level"] == "error", one["level"])
check("time 为 HH:MM:SS", len(one["time"]) == 8 and one["time"].count(":") == 2, one["time"])

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项: {', '.join(failures)}")
    sys.exit(1)
print("日志环形缓冲全部测试通过")
sys.exit(0)
