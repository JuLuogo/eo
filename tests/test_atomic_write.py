#!/usr/bin/env python3
"""原子写测试：并发读写时，读方永远看不到半截文件。

背景：网段文件（targets/*.txt）会被「ranges/esa-ranges 更新」和「扫描读取」
同时用到；结果文件会被「扫描写」和「DNS 下发读」同时用到。如果写入不是原子的
（先截断再写），读方就可能读到半截内容 —— 网段半截 = 静默少扫，
结果半截 = 少下发节点。这些都是"看起来一切正常"的错误。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo import esa_ranges, ranges                                  # noqa: E402
from eo.core import atomic_write_text, write_result               # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-atomic-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


print("=" * 70)
print("原子写：并发读写不能看到半截文件")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 基本行为：写完整内容、不留临时文件")
# --------------------------------------------------------------------------
target = WORK / "a.txt"
target.write_text("旧内容\n", encoding="utf-8")
atomic_write_text(target, "新内容\n")
check("内容被完整替换", target.read_text("utf-8") == "新内容\n", repr(target.read_text("utf-8")))
check("不留下 .tmp", not list(WORK.glob("*.tmp")), str([p.name for p in WORK.glob("*.tmp")]))

print()
print("2) 写入中途失败：原文件必须保持完好（不能被截断）")
# --------------------------------------------------------------------------
target.write_text("原始内容\n", encoding="utf-8")
original_replace = Path.replace


def boom(self, other):                                             # noqa: ANN001
    raise KeyboardInterrupt("模拟写入过程被中断")


Path.replace = boom                                                # type: ignore[assignment]
try:
    try:
        atomic_write_text(target, "半截内容")
    except KeyboardInterrupt:
        pass
finally:
    Path.replace = original_replace                                # type: ignore[assignment]
check("中断后旧文件完好", target.read_text("utf-8") == "原始内容\n",
      repr(target.read_text("utf-8")))

# --------------------------------------------------------------------------
print()
print("3) ranges.update_targets_file 用的是原子写")
# --------------------------------------------------------------------------
targets_file = WORK / "cft.txt"
targets_file.write_text("# 旧\n1.1.1.0/24\n", encoding="utf-8")
orig_fetch = ranges.fetch
ranges.fetch = lambda key, timeout=30.0: [f"10.0.{i}.0/24" for i in range(300)]  # type: ignore
orig_replace = Path.replace


def boom2(self, other):                                            # noqa: ANN001
    raise RuntimeError("写入失败")


Path.replace = boom2                                               # type: ignore[assignment]
try:
    try:
        ranges.update_targets_file("cloudfront", targets_file, log=lambda *_: None)
        check("写入失败会抛错", False, "没有抛异常")
    except RuntimeError:
        check("写入失败会抛错", True)
finally:
    Path.replace = orig_replace                                    # type: ignore[assignment]
    ranges.fetch = orig_fetch                                      # type: ignore[assignment]
check("失败后目标文件仍是旧内容（没有被截断成空）",
      targets_file.read_text("utf-8") == "# 旧\n1.1.1.0/24\n",
      repr(targets_file.read_text("utf-8"))[:40])

print()
print("4) esa_ranges.write_targets 同样是原子写")
# --------------------------------------------------------------------------
esa_file = WORK / "esa.txt"
esa_file.write_text("# 旧\n2.2.2.0/24\n", encoding="utf-8")
Path.replace = boom2                                               # type: ignore[assignment]
try:
    try:
        esa_ranges.write_targets({"prefixes": ["3.3.3.0/24"], "dropped_china": []},
                                 esa_file, log=lambda *_: None)
        check("写入失败会抛错", False, "没有抛异常")
    except RuntimeError:
        check("写入失败会抛错", True)
finally:
    Path.replace = orig_replace                                    # type: ignore[assignment]
check("失败后目标文件仍是旧内容", esa_file.read_text("utf-8") == "# 旧\n2.2.2.0/24\n",
      repr(esa_file.read_text("utf-8"))[:40])

# --------------------------------------------------------------------------
print()
print("5) 真并发：一边反复写、一边反复读，读到的永远是完整内容")
# --------------------------------------------------------------------------
shared = WORK / "shared.txt"
# 初始内容也要是「合法完整」的形态，否则读到初始态会被误判成半截
atomic_write_text(shared, "版本-1\n" + "x" * 20000 + "\n")
stop = threading.Event()
bad_reads: list[str] = []
reads = [0]


def writer() -> None:
    for i in range(400):
        # 内容故意写大一点，非原子写的话中途读到半截的概率很高
        atomic_write_text(shared, f"版本{i}\n" + "x" * 20000 + "\n")
    stop.set()


def reader() -> None:
    while not stop.is_set():
        try:
            text = shared.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            bad_reads.append(f"{type(exc).__name__}: {exc}")
            continue
        reads[0] += 1
        if not text.startswith("版本") or not text.endswith("x\n"):
            bad_reads.append(text[:30])


threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)
check("读了足够多次（说明确实在并发）", reads[0] > 50, f"{reads[0]} 次")
check("没有读到半截内容", not bad_reads, str(bad_reads[:3]))

# --------------------------------------------------------------------------
print()
print("6) 结果文件同样是原子写")
# --------------------------------------------------------------------------
from eo.core import Probe                                          # noqa: E402

res = WORK / "results" / "eo-lt.txt"
res.parent.mkdir(parents=True, exist_ok=True)
res.write_text("1.1.1.1\n", encoding="utf-8")
Path.replace = boom2                                               # type: ignore[assignment]
try:
    try:
        write_result(res, [Probe(ip="203.0.113.9")])
        check("写入失败会抛错", False, "没有抛异常")
    except RuntimeError:
        check("写入失败会抛错", True)
finally:
    Path.replace = orig_replace                                    # type: ignore[assignment]
check("失败后结果文件完好", res.read_text("utf-8") == "1.1.1.1\n",
      repr(res.read_text("utf-8")))

# --------------------------------------------------------------------------
print()
print("7) 代码里不应再有「直接覆盖会被并发读的文件」的写法")
# --------------------------------------------------------------------------
import re                                                          # noqa: E402

suspects: list[str] = []
for path in sorted((ROOT / "eo").glob("*.py")):
    for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not re.search(r"\.write_text\(", line):
            continue
        # 写临时文件是允许的：变量名 tmp / 文件名带 .tmp 都算
        if re.search(r"\btmp\w*\.write_text\(|\.tmp[\"']", line):
            continue
        suspects.append(f"{path.name}:{num}: {line.strip()[:60]}")
check("除了临时文件，没有别处直接 write_text", not suspects, "; ".join(suspects) or "干净")

print()
print("=" * 70)
if _failures:
    print(f"原子写测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("原子写测试全部通过")
sys.exit(0)
