#!/usr/bin/env python
"""三运营商（联通/电信/移动）平台配置专项测试。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def fresh():
    for m in [m for m in list(sys.modules) if m.startswith("eo.")]:
        del sys.modules[m]
    import eo.platforms as p
    return p


print("=" * 70)
print("三运营商平台配置专项测试")
print("=" * 70)

print()
print("1) 平台矩阵：3 平台 x 3 运营商 = 9 个变体")
os.environ.pop("EO_PLATFORMS", None)
os.environ.pop("EO_NETWORK", None)
m = fresh()
keys = sorted(m.PLATFORMS)
print(f"     {keys}")
expected = sorted(f"{s}-{c}" for s in ("eo", "esa", "cft") for c in ("lt", "dx", "yd"))
check("9 个变体齐全", keys == expected, f"差异 {set(expected) ^ set(keys)}")
check("carrier_keys('eo') 返回 3 个变体",
      sorted(m.carrier_keys("eo")) == ["eo-dx", "eo-lt", "eo-yd"],
      str(m.carrier_keys("eo")))
check("NETWORKS 常量", tuple(m.NETWORKS) == ("lt", "dx", "yd"), str(m.NETWORKS))

print()
print("2) 运营商标签（core.network_label）")
from eo.core import network_label
for c, want in (("lt", "联通"), ("dx", "电信"), ("yd", "移动")):
    got = network_label(c)
    check(f"{c} -> {want}", got == want, got)

print()
print("3) 只设 EO_NETWORK -> 该机器只跑自己的运营商")
for carrier in ("lt", "dx", "yd"):
    os.environ.pop("EO_PLATFORMS", None)
    os.environ["EO_NETWORK"] = carrier
    mm = fresh()
    keys_env = mm.env_platform_keys()
    want = sorted(f"{s}-{carrier}" for s in ("eo", "esa", "cft"))
    check(f"EO_NETWORK={carrier} -> env_platform_keys", sorted(keys_env) == want,
          str(sorted(keys_env)))
    got = sorted(p.key for p in mm.get_platforms())
    check(f"   CLI get_platforms 一致", got == want, str(got))
    nets = {p.network for p in mm.get_platforms()}
    check(f"   全部属于 {carrier}", nets == {carrier}, str(nets))

print()
print("4) EO_PLATFORMS 优先于 EO_NETWORK")
os.environ["EO_NETWORK"] = "lt"
os.environ["EO_PLATFORMS"] = "eo-dx,cft-yd"
mm = fresh()
got = sorted(p.key for p in mm.get_platforms())
check("显式列表覆盖运营商推导", got == ["cft-yd", "eo-dx"], str(got))

print()
print("5) 都不设 -> 代码默认（lt 系列）")
os.environ.pop("EO_PLATFORMS", None)
os.environ.pop("EO_NETWORK", None)
mm = fresh()
got = sorted(p.key for p in mm.get_platforms())
check("默认 lt 三件套", got == ["cft-lt", "eo-lt", "esa-lt"], str(got))
check("--all-platforms 仍能看到全部 9 个",
      len(mm.get_platforms(include_disabled=True)) == 9,
      str(len(mm.get_platforms(include_disabled=True))))

print()
print("6) -n/--network 过滤（必须走真实路径：不带 include_disabled）")
# 注意：这里刻意不传 include_disabled=True。真实 CLI 走的是默认值，
# 而默认值只启用代码里 enabled 的变体（lt 三件套）。历史上正是「先取默认启用、
# 再按运营商筛」导致 dx/yd 得到空列表 —— 传 include_disabled=True 会掩盖这个 bug。
for c in ("lt", "dx", "yd"):
    real = mm.get_platforms(networks=[c])          # 与 CLI 完全一致的调用
    got = sorted(p.key for p in real)
    check(f"--network {c}", got == sorted(f"{s}-{c}" for s in ("eo", "esa", "cft")), str(got))
    check(f"--network {c} 全部标记为启用", bool(got) and all(p.enabled for p in real),
          str([p.key for p in real if not p.enabled]) or "全部 enabled")
    check(f"--network {c} 结果非空（回归：先取默认启用再筛会得空列表）", bool(got), str(got))

multi = sorted(p.key for p in mm.get_platforms(networks=["lt", "dx"]))
check("--network lt dx 得 6 个变体", multi == sorted(f"{s}-{c}" for s in ("eo", "esa", "cft")
                                                    for c in ("lt", "dx")), str(multi))
up = sorted(p.key for p in mm.get_platforms(networks=["DX"]))
check("--network 大小写不敏感", up == ["cft-dx", "eo-dx", "esa-dx"], str(up))

# -p 与 -n 矛盾时不能静默少跑：结果为 0，CLI 会明确报错并退出 2
clash = mm.get_platforms(["eo-lt"], networks=["dx"])
check("-p eo-lt 配 -n dx 得空列表（由 CLI 报错，不静默扫描）", clash == [], str([p.key for p in clash]))
# 部分矛盾：合法的那部分照跑
partial = sorted(p.key for p in mm.get_platforms(["eo-lt", "eo-dx"], networks=["dx"]))
check("-p eo-lt eo-dx 配 -n dx 只留 eo-dx", partial == ["eo-dx"], str(partial))

print()
print("7) 同一平台三运营商共用目标域名（只有运营商与 DNS 记录不同）")
for svc in ("eo", "esa", "cft"):
    hosts = {mm.PLATFORMS[f"{svc}-{c}"].host for c in ("lt", "dx", "yd")}
    check(f"{svc} 三运营商同 host", len(hosts) == 1, str(hosts))

print()
print("8) DNS 记录名按运营商区分（三台机器各写各的，不会互相覆盖）")
records = {f"{s}-{c}": mm.PLATFORMS[f"{s}-{c}"].dns.get("record_name")
           for s in ("eo", "esa", "cft") for c in ("lt", "dx", "yd")}
check("9 条记录名全局唯一", len(set(records.values())) == 9,
      f"{len(set(records.values()))}/9")
for c in ("lt", "dx", "yd"):
    rs = [records[f"{s}-{c}"] for s in ("eo", "esa", "cft")]
    check(f"{c} 的三条记录名不同", len(set(rs)) == 3, str(rs))
print(f"     样例: {records['eo-lt']} / {records['eo-dx']} / {records['eo-yd']}")

print()
print("9) 平台参数（抽样 / 协议 / 网段）")
for k in ("eo-lt", "esa-lt", "cft-lt"):
    p = mm.PLATFORMS[k]
    print(f"     {k}: sample_per_24={p.sample_per_24} scheme={p.scheme} port={p.port} "
          f"targets={p.targets_file} host={p.host}")
    check(f"{k} 有网段文件", bool(p.targets_file))
    check(f"{k} 有校验域名", bool(p.host))
check("Networks 三平台共用同一套网段文件与域名",
      mm.PLATFORMS["eo-lt"].targets_file == mm.PLATFORMS["eo-dx"].targets_file,
      f'{mm.PLATFORMS["eo-lt"].targets_file}')

print()
print("10) Web 端 build_platforms 与 CLI 一致")
os.environ["EO_NETWORK"] = "yd"
os.environ.pop("EO_PLATFORMS", None)
fresh()
from eo.webui import make_service          # noqa: E402
svc = make_service(Path(tempfile.mkdtemp()) / "c.json")
bp = svc.build_platforms()
check("EO_NETWORK=yd 时 Web 启用移动三件套",
      sorted(p.key for p in bp if p.enabled) == ["cft-yd", "eo-yd", "esa-yd"],
      str(sorted(p.key for p in bp if p.enabled)))
check("Web 只返回这 3 个平台", len(bp) == 3, str(len(bp)))

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项: {', '.join(failures)}")
    sys.exit(1)
print("三运营商配置全部测试通过")
sys.exit(0)
