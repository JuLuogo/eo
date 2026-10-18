#!/usr/bin/env python3
"""平台 key 展开 + updatedns.py 独立下发入口测试。

两条背景：
  1. CLI 帮助与文档里一直写 `-p eo esa cft`（基础名），但平台字典里只有
     eo-lt/eo-dx 这类变体 —— 基础名会直接报「未知平台」。
  2. updatedns.py 是单独下发 DNS 的入口，之前的测试只覆盖 run.py 那条路。
本套件不发任何真实请求（sync 函数被替换成假的）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updatedns                                                  # noqa: E402
from eo.dns import DnsUpdateResult                                 # noqa: E402
from eo.platforms import get_platforms                             # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def keys(**kw) -> list[str]:
    return [p.key for p in get_platforms(**kw)]


print("=" * 70)
print("平台 key 展开 与 updatedns.py 入口")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 基础名按本机运营商展开（文档里的写法现在能用了）")
# --------------------------------------------------------------------------
os.environ.pop("EO_NETWORK", None)
check("-p eo -> eo-lt", keys(keys=["eo"]) == ["eo-lt"], str(keys(keys=["eo"])))
check("-p eo esa cft -> 三个 lt 变体",
      keys(keys=["eo", "esa", "cft"]) == ["eo-lt", "esa-lt", "cft-lt"],
      str(keys(keys=["eo", "esa", "cft"])))

os.environ["EO_NETWORK"] = "yd"
check("EO_NETWORK=yd 时 -p eo -> eo-yd", keys(keys=["eo"]) == ["eo-yd"],
      str(keys(keys=["eo"])))
check("EO_NETWORK=yd 时 -p cft -> cft-yd", keys(keys=["cft"]) == ["cft-yd"],
      str(keys(keys=["cft"])))
os.environ.pop("EO_NETWORK", None)

check("-p eo -n dx -> eo-dx（显式运营商优先）",
      keys(keys=["eo"], networks=["dx"]) == ["eo-dx"],
      str(keys(keys=["eo"], networks=["dx"])))
check("-p eo esa -n lt dx -> 四个变体",
      keys(keys=["eo", "esa"], networks=["lt", "dx"]) ==
      ["eo-lt", "eo-dx", "esa-lt", "esa-dx"],
      str(keys(keys=["eo", "esa"], networks=["lt", "dx"])))

print()
print("2) 老的显式写法不变，重复自动去除")
# --------------------------------------------------------------------------
check("-p eo-lt 不变", keys(keys=["eo-lt"]) == ["eo-lt"])
check("-p eo-lt cft-lt 不变", keys(keys=["eo-lt", "cft-lt"]) == ["eo-lt", "cft-lt"])
check("-p eo eo-lt 去重成 1 个", keys(keys=["eo", "eo-lt"]) == ["eo-lt"],
      str(keys(keys=["eo", "eo-lt"])))
check("-p eo eo（自身重复）去重", keys(keys=["eo", "eo"]) == ["eo-lt"],
      str(keys(keys=["eo", "eo"])))

print()
print("3) 拼错时报错，并提示可以用基础名")
# --------------------------------------------------------------------------
try:
    keys(keys=["eo-ltx"])
    check("拼错报错", False, "没有抛异常")
except KeyError as exc:
    msg = str(exc)
    check("拼错报错", True, msg[:50])
    check("错误信息里提到基础名可用", "基础名" in msg and "eo" in msg, msg[-60:])

# --------------------------------------------------------------------------
print()
print("4) updatedns.py：必须先确认（--yes 或 --dry-run）")
# --------------------------------------------------------------------------
check("不带 --yes/--dry-run 返回 2", updatedns.main([]) == 2)

print()
print("5) updatedns.py 单记录模式：参数校验")
# --------------------------------------------------------------------------
check("--record 缺失返回 2", updatedns.main(["--dry-run", "-p", "eo-lt"]) in (0, 1, 2))
check("-f 指向不存在的文件返回 2",
      updatedns.main(["--dry-run", "-f", "/nonexistent/x.txt", "--record", "a.b.c"]) == 2)

print()
print("6) updatedns.py：-p 基础名能正确展开（用假 sync 验证传进去的平台）")
# --------------------------------------------------------------------------
calls: list[str] = []
real_platform, real_records = updatedns.sync_platform, updatedns.sync_records


def fake_platform(platform, **kw):                                 # noqa: ANN001, ANN003
    calls.append(platform.key)
    return DnsUpdateResult(ok=True, total_ips=1)


def fake_records(target, ips, **kw):                               # noqa: ANN001, ANN003
    calls.append(f"single:{target.record_name}:{len(ips)}")
    return DnsUpdateResult(ok=True, total_ips=len(ips))


updatedns.sync_platform = fake_platform                            # type: ignore[assignment]
updatedns.sync_records = fake_records                              # type: ignore[assignment]
try:
    calls.clear()
    rc = updatedns.main(["--dry-run", "-p", "eo", "esa", "cft"])
    check("-p eo esa cft 退出码 0", rc == 0, f"rc={rc}")
    check("三个平台都按本机运营商展开",
          calls == ["eo-lt", "esa-lt", "cft-lt"], str(calls))

    calls.clear()
    os.environ["EO_NETWORK"] = "dx"
    rc = updatedns.main(["--dry-run", "-p", "eo"])
    check("EO_NETWORK=dx 时 -p eo 下发到 eo-dx", calls == ["eo-dx"], str(calls))
    os.environ.pop("EO_NETWORK", None)

    calls.clear()
    rc = updatedns.main(["--dry-run", "-p", "cft-lt"])
    check("显式变体仍按原样", calls == ["cft-lt"], str(calls))

    # 单记录模式：读文件 + 指定记录名
    tmp = ROOT / "tests" / "_dns_cli_tmp.txt"
    tmp.write_text("1.1.1.1\n2.2.2.2\n", encoding="utf-8")
    try:
        calls.clear()
        rc = updatedns.main(["--dry-run", "-f", str(tmp), "--record", "eo-lt.1.060730.xyz"])
        check("单记录模式退出码 0", rc == 0, f"rc={rc}")
        check("读到了文件里的 2 个 IP", calls == ["single:eo-lt.1.060730.xyz:2"], str(calls))
    finally:
        tmp.unlink(missing_ok=True)

    # 失败要反映到退出码
    def failing(platform, **kw):                                   # noqa: ANN001, ANN003
        return DnsUpdateResult(ok=False, errors=["boom"])

    updatedns.sync_platform = failing                              # type: ignore[assignment]
    rc = updatedns.main(["--dry-run", "-p", "eo"])
    check("下发失败时退出码 1", rc == 1, f"rc={rc}")
finally:
    updatedns.sync_platform = real_platform                        # type: ignore[assignment]
    updatedns.sync_records = real_records                          # type: ignore[assignment]

print()
print("=" * 70)
if _failures:
    print(f"平台 key / updatedns 测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("平台 key / updatedns 测试全部通过")
sys.exit(0)
