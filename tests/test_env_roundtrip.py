#!/usr/bin/env python3
"""`.env` 读写往返测试：写进去再读出来，必须一模一样。

背景：env_set() 会用 env_quote() 给含空格/#/引号的值加引号，其中 `"` 会被
转义成 `\\"`；但读取侧只剥掉外层引号，不做反转义 —— 于是「值里带双引号」
的配置（比如自己生成的网页令牌）写进去再读出来就多了个反斜杠。

这类问题的危害是"看起来成功了"：deploy.sh env_set 报告写入成功，
下次启动读到的却是被改过的值，表现为"令牌不对/密钥无效"。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo.deploytool import env_parse, env_quote, env_set             # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-envrt-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


CASES = [
    ("简单值", "abc123"),
    ("带空格", "hello world"),
    ("带井号", "pass#word"),
    ("带空格和井号", "token # not a comment"),
    ("前导井号", "#starts-with-hash"),
    ("带双引号", 'a"b'),
    ("全是双引号", '"""'),
    ("双引号加空格", 'say "hi" now'),
    ("带单引号", "it's"),
    ("单双引号都有", 'a"b\'c'),
    ("带反斜杠", "C:\\path\\to\\file"),
    ("反斜杠加引号", 'a\\"b'),
    ("带等号", "k=v"),
    ("带美元符", "$HOME/${VAR}"),
    ("带百分号", "100%"),
    ("带中文", "密钥值"),
    ("带制表符", "a\tb"),
    ("很长", "x" * 500),
    ("末尾空格", "trailing   "),
    ("前导空格", "   leading"),
]

print("=" * 70)
print(".env 读写往返")
print("=" * 70)

path = WORK / ".env"
for label, value in CASES:
    path.unlink(missing_ok=True)
    env_set(path, "TEST_KEY", value)
    got = env_parse(path).get("TEST_KEY", "<缺失>")
    ok = got == value
    check(f"{label}", ok, f"写入 {value!r} -> 读回 {got!r}" if not ok else repr(value)[:32])

# --------------------------------------------------------------------------
print()
print("空值：写入空串应读回空串（不是缺失）")
# --------------------------------------------------------------------------
path.unlink(missing_ok=True)
env_set(path, "EMPTY", "")
got = env_parse(path).get("EMPTY", "<缺失>")
check("空值往返", got == "", repr(got))

# --------------------------------------------------------------------------
print()
print("重复设置同一个键：只保留一行，且值是最新的")
# --------------------------------------------------------------------------
path.write_text("# 注释\nEO_RATE=100\nOTHER=1\n", encoding="utf-8")
env_set(path, "EO_RATE", "50")
env_set(path, "EO_RATE", "75")
text = path.read_text(encoding="utf-8")
check("注释被保留", "# 注释" in text, text.replace("\n", " | ")[:60])
check("其它键被保留", "OTHER=1" in text)
check("EO_RATE 只出现一次", text.count("EO_RATE=") == 1, str(text.count("EO_RATE=")))
check("值是最新的", env_parse(path)["EO_RATE"] == "75", env_parse(path)["EO_RATE"])

# --------------------------------------------------------------------------
print()
print("新增键：追加到末尾，不破坏原有内容")
# --------------------------------------------------------------------------
env_set(path, "NEW_KEY", "new value")
data = env_parse(path)
check("原有键仍在", data.get("EO_RATE") == "75" and data.get("OTHER") == "1", str(data))
check("新键写入成功", data.get("NEW_KEY") == "new value", repr(data.get("NEW_KEY")))

# --------------------------------------------------------------------------
print()
print("BOM 容忍：Windows 上编辑过的 .env 常带 BOM")
# --------------------------------------------------------------------------
bom = WORK / ".env.bom"
bom.write_bytes("\ufeffEO_RATE=42\nEO_NETWORK=lt\n".encode("utf-8"))
data = env_parse(bom)
check("BOM 不影响第一个键", data.get("EO_RATE") == "42", str(data))
check("后续键正常", data.get("EO_NETWORK") == "lt", str(data))

# --------------------------------------------------------------------------
print()
print("行尾注释：不加引号时应被剥掉，加引号时应保留")
# --------------------------------------------------------------------------
manual = WORK / ".env.manual"
manual.write_text('A=1  # 说明\nB="2  # 也是值"\nC=   # 只是说明\n', encoding="utf-8")
data = env_parse(manual)
check("未加引号的行尾注释被剥掉", data.get("A") == "1", repr(data.get("A")))
check("加引号时 # 属于值", data.get("B") == "2  # 也是值", repr(data.get("B")))
check("空值 + 行尾说明 -> 空值", data.get("C") == "", repr(data.get("C")))

print()
print("=" * 70)
if _failures:
    print(f".env 往返测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print(".env 往返测试全部通过")
sys.exit(0)
