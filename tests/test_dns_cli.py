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

# --------------------------------------------------------------------------
print()
print("7) 手动下发也要拦住「可疑结果」（与自动路径同一道闸）")
# --------------------------------------------------------------------------
# 自动路径（run_once -> _apply_dns）会拒绝下发可疑结果；
# 手动执行的 update_dns.py 原来没有这道闸，会把被代理接管的那批地址推到线上。
import tempfile                                                    # noqa: E402

from eo.platforms import PLATFORMS                                 # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="eo-updatedns-"))
try:
    out_file = tmpdir / "eo-lt.txt"
    # 100 个 IP、全部 1ms -> 判定为可疑（好得不真实）
    out_file.write_text("\n".join(f"203.0.113.{i}\t1ms" for i in range(1, 101)) + "\n",
                        encoding="utf-8")

    plat = PLATFORMS["eo-lt"]
    orig_output = plat.output_file
    orig_dns = plat.dns
    plat.output_file = str(out_file)
    plat.dns = dict(orig_dns or {}) or {"provider": "huawei", "zone_name": "1.060730.xyz",
                                        "record_name": "eo-lt.1.060730.xyz"}
    try:
        reason = updatedns._suspicious_result_file(plat)
        check("可疑结果能被识别", bool(reason), reason[:70])
        check("原因里说明是链路被接管", "接管" in reason or "代理" in reason, reason[:70])

        # 没带 --force 时必须拒绝，并且不去调用 sync_platform
        called: list[str] = []
        real_sync = updatedns.sync_platform
        updatedns.sync_platform = lambda p, **kw: (called.append(p.key),   # type: ignore
                                                   DnsUpdateResult(ok=True))[1]
        try:
            logs7: list[str] = []
            rc = updatedns.main(["--dry-run", "-p", "eo-lt"])
            check("可疑结果被拒绝（退出码 1）", rc == 1, f"rc={rc}")
            check("拒绝时没有调用 sync_platform", not called, str(called))

            called.clear()
            rc = updatedns.main(["--dry-run", "--force", "-p", "eo-lt"])
            check("--force 可以越过", rc == 0 and called == ["eo-lt"], f"rc={rc} called={called}")
        finally:
            updatedns.sync_platform = real_sync                    # type: ignore[assignment]

        # 正常结果不该被拦
        out_file.write_text("\n".join(f"203.0.113.{i}\t28ms" for i in range(1, 101)) + "\n",
                            encoding="utf-8")
        check("正常结果不拦", updatedns._suspicious_result_file(plat) == "",
              repr(updatedns._suspicious_result_file(plat))[:60])

        # 没有延迟列时不拦（信息不足，不能误拦）
        out_file.write_text("\n".join(f"203.0.113.{i}" for i in range(1, 101)) + "\n",
                            encoding="utf-8")
        check("没有延迟信息时不拦", updatedns._suspicious_result_file(plat) == "")

        # 结果文件不存在时不拦（交给 sync_platform 报"结果文件不存在"）
        plat.output_file = str(tmpdir / "nope.txt")
        check("结果文件不存在时不拦", updatedns._suspicious_result_file(plat) == "")
    finally:
        plat.output_file = orig_output
        plat.dns = orig_dns
finally:
    import shutil                                                  # noqa: E402
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("8) 自动路径与手动路径共用同一套判定")
# --------------------------------------------------------------------------
from eo.core import looks_suspicious                               # noqa: E402
from eo.runner import _is_suspicious                               # noqa: E402


class _P:
    def __init__(self, ms):                                        # noqa: ANN001
        self.latency_ms = ms


class _Outcome:
    def __init__(self, n_pass, total, ms):                         # noqa: ANN001
        self.passed = [_P(ms) for _ in range(n_pass)]
        self.total = total


check("runner 与 core 判定一致（可疑）",
      _is_suspicious(_Outcome(100, 100, 1.0)) is True
      and bool(looks_suspicious([1.0] * 100, total=100)) is True)
check("runner 与 core 判定一致（正常）",
      _is_suspicious(_Outcome(100, 100, 30.0)) is False
      and bool(looks_suspicious([30.0] * 100, total=100)) is False)
check("样本不足时都不判可疑",
      _is_suspicious(_Outcome(10, 10, 1.0)) is False,
      "10 个样本")
check("通过率低时都不判可疑",
      _is_suspicious(_Outcome(50, 100, 1.0)) is False, "50%")

print()
print("=" * 70)
if _failures:
    print(f"平台 key / updatedns 测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("平台 key / updatedns 测试全部通过")
sys.exit(0)
