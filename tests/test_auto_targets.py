#!/usr/bin/env python3
"""「自动拉取网段」功能测试。

背景：网页平台设置里一直有「自动拉取网段（cloudfront 等）」这个输入框，
字段能存、能回显，但**没有任何代码读它** —— 填了完全没效果，
是典型的"看起来配好了其实没接线"。现在接上了，这里验证接线正确且安全：
  * 留空 = 关闭（默认行为完全不变）
  * 文件还新鲜时不重复拉取（否则 300 秒一轮 = 一天 288 次请求）
  * 拉取失败不能影响本轮扫描（继续用旧网段）
  * 不认识的数据源要明确提示
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo import ranges                                              # noqa: E402
from eo.core import Platform                                       # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-autotargets-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def make(targets_file: Path, source: str) -> Platform:
    return Platform(key="t-lt", name="Test", host="test.local", network="lt",
                    targets_file=str(targets_file), auto_targets=source)


def patch_fetch(prefixes: list[str] | None, fail: Exception | None = None):
    """替换 ranges.update_targets_file（它自己会调 fetch 与写文件）。"""
    calls = []
    real_fetch = ranges.fetch

    def fake_fetch(key, timeout=30.0):                             # noqa: ANN001
        calls.append(key)
        if fail is not None:
            raise fail
        return list(prefixes or [])

    ranges.fetch = fake_fetch                                      # type: ignore[assignment]
    return calls, real_fetch


def restore(real) -> None:                                         # noqa: ANN001
    ranges.fetch = real                                            # type: ignore[assignment]


print("=" * 70)
print("自动拉取网段（网页上的 auto_targets）")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 留空 = 关闭，什么都不做（默认行为不变）")
# --------------------------------------------------------------------------
f = WORK / "a.txt"
f.write_text("1.1.1.0/24\n", encoding="utf-8")
calls, real = patch_fetch(["2.2.2.0/24"])
try:
    note = ranges.auto_refresh(make(f, ""), log=lambda *_: None)
finally:
    restore(real)
check("没有提示信息", note == "", repr(note))
check("没有发起拉取", not calls, str(calls))
check("文件没被动过", f.read_text("utf-8") == "1.1.1.0/24\n")

# --------------------------------------------------------------------------
print()
print("2) 首次（文件不存在）会拉取并写入")
# --------------------------------------------------------------------------
missing = WORK / "b.txt"
calls, real = patch_fetch(["2.2.2.0/24", "2.2.3.0/24"])
try:
    note = ranges.auto_refresh(make(missing, "cloudfront"), log=lambda *_: None)
finally:
    restore(real)
check("发起了拉取", calls == ["cloudfront"], str(calls))
check("文件已生成", missing.exists(), str(missing))
check("内容正确", "2.2.2.0/24" in missing.read_text("utf-8"), missing.read_text("utf-8")[:40])
check("提示信息说明了结果", "自动更新" in note and "2" in note, note)

# --------------------------------------------------------------------------
print()
print("3) 文件还新鲜时不重复拉取（否则一天几百次请求）")
# --------------------------------------------------------------------------
fresh = WORK / "c.txt"
fresh.write_text("# 新拉过\n3.3.3.0/24\n", encoding="utf-8")
calls, real = patch_fetch(["9.9.9.0/24"])
try:
    note = ranges.auto_refresh(make(fresh, "cloudfront"), log=lambda *_: None,
                               min_age_hours=24)
finally:
    restore(real)
check("没有重复拉取", not calls, str(calls))
check("没有提示（静默跳过）", note == "", repr(note))
check("文件内容未变", "3.3.3.0/24" in fresh.read_text("utf-8"))

print()
print("3b) 文件过期（mtime 调老）后会重新拉取")
# --------------------------------------------------------------------------
old = WORK / "c2.txt"
old.write_text("3.3.3.0/24\n", encoding="utf-8")
past = time.time() - 48 * 3600
os.utime(old, (past, past))
calls, real = patch_fetch(["4.4.4.0/24"])
try:
    note = ranges.auto_refresh(make(old, "cloudfront"), log=lambda *_: None,
                               min_age_hours=24)
finally:
    restore(real)
check("过期后重新拉取", calls == ["cloudfront"], str(calls))
check("文件被更新", "4.4.4.0/24" in old.read_text("utf-8"), old.read_text("utf-8")[:40])

# --------------------------------------------------------------------------
print()
print("4) 拉取失败：必须保留旧文件，并把原因说清楚")
# --------------------------------------------------------------------------
keep = WORK / "d.txt"
keep.write_text("# 旧网段\n5.5.5.0/24\n", encoding="utf-8")
past = time.time() - 48 * 3600
os.utime(keep, (past, past))
calls, real = patch_fetch(None, fail=OSError("network unreachable"))
try:
    note = ranges.auto_refresh(make(keep, "cloudfront"), log=lambda *_: None)
finally:
    restore(real)
check("返回警告提示", "失败" in note or "⚠" in note, note)
check("原因里有异常信息", "network unreachable" in note, note[:80])
check("旧文件完好（没有被清空）", "5.5.5.0/24" in keep.read_text("utf-8"),
      keep.read_text("utf-8")[:40])

print()
print("4b) 拉到空结果：也不能覆盖旧文件")
# --------------------------------------------------------------------------
keep2 = WORK / "d2.txt"
keep2.write_text("# 旧网段\n6.6.6.0/24\n", encoding="utf-8")
past = time.time() - 48 * 3600
os.utime(keep2, (past, past))
calls, real = patch_fetch([])
try:
    note = ranges.auto_refresh(make(keep2, "cloudfront"), log=lambda *_: None)
finally:
    restore(real)
check("空结果被拒绝（提示失败）", "失败" in note or "没有" in note or "⚠" in note, note)
check("旧文件完好", "6.6.6.0/24" in keep2.read_text("utf-8"),
      keep2.read_text("utf-8")[:40])

# --------------------------------------------------------------------------
print()
print("5) 不认识的数据源：明确提示，不去发请求")
# --------------------------------------------------------------------------
calls, real = patch_fetch(["7.7.7.0/24"])
try:
    note = ranges.auto_refresh(make(WORK / "e.txt", "akamai"), log=lambda *_: None)
finally:
    restore(real)
check("提示不认识", "不认识" in note, note)
check("没有发起请求", not calls, str(calls))
check("提示里列出可选值", "cloudfront" in note, note)

# --------------------------------------------------------------------------
print()
print("6) Platform 上确实有 auto_targets 字段（接线的前提）")
# --------------------------------------------------------------------------
p = make(WORK / "f.txt", "cloudfront")
check("字段存在且可读", getattr(p, "auto_targets", None) == "cloudfront", str(p.auto_targets))
check("默认值是空串（不开启）", Platform(key="x", name="X", host="h").auto_targets == "",
      repr(Platform(key="x", name="X", host="h").auto_targets))

# --------------------------------------------------------------------------
print()
print("7) run_once 里确实调用了它（否则还是死字段）")
# --------------------------------------------------------------------------
src = (ROOT / "eo" / "runner.py").read_text(encoding="utf-8")
check("runner 引用了 auto_refresh", "auto_refresh" in src, "已接线")
check("扫描前调用", src.index("auto_refresh") < src.index("scan_platforms(platforms"),
      "位置正确")
check("失败不影响本轮（有 try/except 兜住）",
      "自动拉取网段异常" in src, "有兜底")

# --------------------------------------------------------------------------
print()
print("8) 网页字段与后端字段对齐（防止再次变成死字段）")
# --------------------------------------------------------------------------
from eo.config_store import PlatformSettings                       # noqa: E402

check("PlatformSettings 有 auto_targets",
      "auto_targets" in {f.name for f in __import__("dataclasses").fields(PlatformSettings)},
      "有")
check("Platform 有 auto_targets",
      "auto_targets" in {f.name for f in __import__("dataclasses").fields(Platform)},
      "有")
webui_src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
check("build_platforms 把它传下去", "auto_targets=(override.auto_targets" in webui_src,
      "已传")

print()
print("=" * 70)
if _failures:
    print(f"自动拉取网段测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("自动拉取网段测试全部通过")
sys.exit(0)
