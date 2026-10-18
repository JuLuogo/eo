#!/usr/bin/env python3
"""深扫（--deep）功能测试。

深扫的用途：第一轮用 --sample-per-24 粗扫（每 /24 只取几个代表 IP）找出
「哪些 /24 里有节点」，第二轮以命中的 /24 为范围做全量扫描。
这条路径平时很少用，之前只有总量上限的函数被测过，整条链路没验过。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo.core import Platform, parent_24s, sample_platform_targets   # noqa: E402
from eo.runner import RunConfig                                     # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-deep-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def make_platform(output: Path, targets: Path, **over) -> Platform:
    base = dict(key="t-lt", name="Test", host="test.local", network="lt",
                targets_file=str(targets), output_file=str(output))
    base.update(over)
    return Platform(**base)


print("=" * 70)
print("深扫（--deep）：以上一轮命中的 /24 为范围做全量")
print("=" * 70)

targets_file = WORK / "targets.txt"
targets_file.write_text("10.1.0.0/24\n10.2.0.0/24\n10.3.0.0/24\n", encoding="utf-8")

# --------------------------------------------------------------------------
print()
print("1) 上一轮命中的 /24 会被全量展开（不再抽样）")
# --------------------------------------------------------------------------
out = WORK / "r1.txt"
out.write_text("10.1.0.5\n10.3.0.200\n", encoding="utf-8")     # 命中两个不同 /24
plat = make_platform(out, targets_file, sample_per_24=1, deep_scan=True)
logs: list[str] = []
got = sample_platform_targets(plat, log=logs.append)
check("返回的是全量主机数（2 个 /24 × 254）", got is not None and len(got) == 508,
      f"{len(got) if got else None} 个")
check("只包含命中的那两个 /24",
      got is not None and set(parent_24s(got)) == {"10.1.0.0/24", "10.3.0.0/24"},
      str(sorted(set(parent_24s(got or [])))))
check("没有命中过的 /24 不会被扫到",
      got is not None and not any(ip.startswith("10.2.") for ip in got),
      "10.2.x 未出现")
check("网络号与广播地址被排除",
      got is not None and "10.1.0.0" not in got and "10.1.0.255" not in got,
      f"{got[:2] if got else None}")
check("日志说明了深扫范围", any("深扫模式" in line for line in logs),
      str([l for l in logs if "深扫" in l][:1]))

# --------------------------------------------------------------------------
print()
print("2) 结果文件带延迟列（--with-latency 的格式）也能正确解析")
# --------------------------------------------------------------------------
out2 = WORK / "r2.txt"
out2.write_text("10.1.0.5\t12ms\n10.3.0.200\t37ms\n", encoding="utf-8")
got = sample_platform_targets(make_platform(out2, targets_file, deep_scan=True), log=None)
check("带了 \\t延迟 仍能取出 IP", got is not None and len(got) == 508,
      f"{len(got) if got else None} 个")
check("范围与不带延迟时一致",
      got is not None and set(parent_24s(got)) == {"10.1.0.0/24", "10.3.0.0/24"},
      str(sorted(set(parent_24s(got or [])))))

# --------------------------------------------------------------------------
print()
print("3) 上一轮没有结果：深扫退化为抽样（不能什么都扫不到）")
# --------------------------------------------------------------------------
missing = WORK / "nope.txt"
plat = make_platform(missing, targets_file, sample_per_24=1, deep_scan=True)
logs = []
got = sample_platform_targets(plat, log=logs.append)
check("退化为抽样而不是返回空", got is not None and len(got) > 0,
      f"{len(got) if got else None} 个")
check("抽样结果按 per_24 控制（3 个 /24 × 1）", got is not None and len(got) == 3,
      f"{len(got) if got else None} 个")
check("日志提示已退化", any("退化为抽样" in line for line in logs),
      str([l for l in logs if "退化" in l][:1]))

# --------------------------------------------------------------------------
print()
print("4) 只开深扫、不开抽样：也走深扫")
# --------------------------------------------------------------------------
out3 = WORK / "r3.txt"
out3.write_text("10.1.0.9\n", encoding="utf-8")
got = sample_platform_targets(make_platform(out3, targets_file, sample_per_24=0, deep_scan=True),
                              log=None)
check("sample_per_24=0 但 deep_scan=True 仍然深扫", got is not None and len(got) == 254,
      f"{len(got) if got else None} 个")

# --------------------------------------------------------------------------
print()
print("5) 命中过多 /24 时被总量上限截断（回归：曾会把几百 MB 全展开）")
# --------------------------------------------------------------------------
out4 = WORK / "r4.txt"
out4.write_text("\n".join(f"10.{i // 256}.{i % 256}.7" for i in range(5000)), encoding="utf-8")
logs = []
got = sample_platform_targets(make_platform(out4, targets_file, deep_scan=True), log=logs.append)
check("被截断到上限内（不超过 1024 个 /24）",
      got is not None and len(got) <= 1024 * 254,
      f"{len(got) if got else None} 个")
check("明确告知被截断", any("深扫范围过大" in line for line in logs),
      str([l for l in logs if "过大" in l][:1]))
check("保留的是前 N 个（结果按延迟排序）",
      got is not None and got[0] == "10.0.0.1", f"{got[0] if got else None}")

# --------------------------------------------------------------------------
print()
print("6) 完全没开抽样也没开深扫：返回 None，交给调用方全量展开")
# --------------------------------------------------------------------------
check("返回 None", sample_platform_targets(
    make_platform(out3, targets_file, sample_per_24=0, deep_scan=False), log=None) is None)

# --------------------------------------------------------------------------
print()
print("7) --deep 参数能传到平台上（RunConfig.apply_to）")
# --------------------------------------------------------------------------
plat = make_platform(out3, targets_file)
RunConfig(deep_scan=True).apply_to([plat])
check("deep_scan 被打开", plat.deep_scan is True, str(plat.deep_scan))
plat2 = make_platform(out3, targets_file)
RunConfig(deep_scan=False).apply_to([plat2])
check("不传时是关闭的", plat2.deep_scan is False, str(plat2.deep_scan))
plat3 = make_platform(out3, targets_file, sample_per_24=1)
RunConfig(sample_per_24=4, deep_scan=True).apply_to([plat3])
check("sample_per_24 与 deep_scan 可同时生效",
      plat3.sample_per_24 == 4 and plat3.deep_scan is True,
      f"{plat3.sample_per_24} {plat3.deep_scan}")

# --------------------------------------------------------------------------
print()
print("8) 深扫 + 延迟门槛/取前N 组合（第二轮深扫后仍会筛选）")
# --------------------------------------------------------------------------
import eo.core as core                                           # noqa: E402

table = {f"10.1.0.{i}": (float(i), True, 0) for i in range(1, 21)}
real = core.http_probe
try:
    def fake(ip, **kw):                                          # noqa: ANN001, ANN003
        lat, ok, _ = table.get(ip, (999.0, False, 0))
        p = core.Probe(ip=ip)
        p.latency_ms = lat
        p.connect_ms = lat
        p.remote_ip = ip
        p.ok = ok
        p.status = "200" if ok else "000"
        return p
    core.http_probe = fake
    plat = make_platform(WORK / "r5.txt", targets_file, deep_scan=True,
                         max_latency_ms=10, top_n=3)
    Path(plat.output_file).write_text("10.1.0.5\n", encoding="utf-8")
    outcome = core.scan_platform(plat, targets=None, quiet=True)
finally:
    core.http_probe = real
check("深扫范围内筛选生效：只留 3 个", len(outcome.passed) == 3, str(len(outcome.passed)))
check("留下的都是最快的（1ms/2ms/3ms）",
      [p.ip for p in outcome.passed] == ["10.1.0.1", "10.1.0.2", "10.1.0.3"],
      str([p.ip for p in outcome.passed]))

print()
print("=" * 70)
if _failures:
    print(f"深扫测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("深扫测试全部通过")
sys.exit(0)
