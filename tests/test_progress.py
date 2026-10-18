#!/usr/bin/env python3
"""进度计数在并发下必须准确。

背景：on_result / on_plan 由同一个平台的多个探测线程并发调用，
而 `entry["done"] += 1` 是「读-改-写」——执行期间被切换就会少算。
表现为进度条永远到不了 100%、事件推送频率不对。

这里用「探测函数故意加一点延迟」来制造线程交替，跑一轮多平台多 IP 的扫描，
断言最后一次 progress 事件的计数与实际完成的探测数完全一致。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eo.core as core                                            # noqa: E402
from eo.core import Platform, Probe                               # noqa: E402
from eo.runner import RunConfig, run_once                         # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


COUNTER = {"calls": 0}
LOCK = threading.Lock()


def fake_probe(ip, **kw):                                         # noqa: ANN001, ANN003
    """假装探测：加一点点延迟并切换线程，制造最坏的交错。"""
    with LOCK:
        COUNTER["calls"] += 1
    time.sleep(0.0005)
    probe = Probe(ip=ip, ok=True, status="200")
    probe.latency_ms = 10.0
    probe.connect_ms = 10.0
    probe.remote_ip = ip
    return probe


print("=" * 70)
print("进度计数并发正确性")
print("=" * 70)

IP_COUNT = 240
PER_PLATFORM = 120
platforms = [
    Platform(key="a-lt", name="甲", host="a.local", network="lt", workers=32),
    Platform(key="b-lt", name="乙", host="b.local", network="lt", workers=32),
]

# 每个平台各自的 IP 列表（用 --targets 传同一个列表也行，这里直接给平台网段）
import tempfile                                                    # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="eo-progress-"))
targets_file = WORK / "t.txt"
targets_file.write_text("\n".join(f"192.0.2.{i % 250 + 1}" for i in range(IP_COUNT)) + "\n",
                        encoding="utf-8")
for p in platforms:
    p.targets_file = str(targets_file)
    p.output_file = str(WORK / f"{p.key}.txt")
    p.history_file = str(WORK / "h.jsonl")

events: list[dict] = []
real_probe = core.http_probe
core.http_probe = fake_probe                                       # type: ignore[assignment]
try:
    outcomes = run_once(platforms, RunConfig(rate=0, quiet=True, no_write=True),
                        emit=events.append)
finally:
    core.http_probe = real_probe                                   # type: ignore[assignment]

progress_events = [e for e in events if e.get("type") == "progress"]
check("产生了 progress 事件", bool(progress_events), f"{len(progress_events)} 个")

# 注意：探测函数被调用的次数会**多于**扫描目标数 —— 除了正式扫描，
# 还有链路可信度自检、以及每个平台「前 3 个 IP 预检」（用于尽早报错）。
# 所以这里对的是「扫描结果数 == done」，不是「探测调用次数 == done」。
scanned = sum(len(o.passed) for o in outcomes)
pre_checks = COUNTER["calls"] - scanned
check("预检调用数在合理范围（每平台 3 个 + 自检若干）",
      0 <= pre_checks <= 3 * len(platforms) + 4,
      f"总调用 {COUNTER['calls']} - 扫描 {scanned} = 预检 {pre_checks}")

if progress_events:
    last = progress_events[-1]
    print(f"     最后一次 progress: done={last.get('done')} total={last.get('total')}")
    check("last.total 等于实际扫描总数",
          last.get("total") == IP_COUNT * len(platforms),
          f"{last.get('total')} vs {IP_COUNT * len(platforms)}")
    check("last.done 与扫描结果数一致（不能少算）",
          last.get("done") == scanned,
          f"done={last.get('done')} 结果数={scanned}")
    check("last.done 等于 total（进度能到 100%）",
          last.get("done") == last.get("total"),
          f"{last.get('done')} / {last.get('total')}")
    for key, entry in (last.get("platforms") or {}).items():
        check(f"平台 {key} 的 done == total",
              entry.get("done") == entry.get("total"),
              f"{entry.get('done')} / {entry.get('total')}")
        check(f"平台 {key} 的 alive == done（全部探测成功）",
              entry.get("alive") == entry.get("done"),
              f"alive={entry.get('alive')} done={entry.get('done')}")

check("扫描结果数量正确", scanned == IP_COUNT * len(platforms),
      f"{scanned} vs {IP_COUNT * len(platforms)}")

print()
print("=" * 70)
if _failures:
    print(f"进度计数测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("进度计数测试全部通过")
sys.exit(0)
