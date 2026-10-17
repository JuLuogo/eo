#!/usr/bin/env python3
"""结果筛选测试：延迟门槛 + 只取最快 N 个 + 稳定性排序。

对应需求：「超过一定延迟就丢弃剩下的 IP，按延迟（和丢包）排序取前多少」。
这里用假的探测函数喂数据，不访问网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eo.core as core                        # noqa: E402
from eo.runner import RunConfig               # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def make_platform(**over) -> core.Platform:
    base = dict(key="t", name="Test", host="test.local", network="lt",
                workers=4, retries=0, timeout=1.0)
    base.update(over)
    return core.Platform(**base)


def patch_probes(table: dict[str, tuple[float, bool, int]]):
    """table: ip -> (延迟ms, 是否成功, 需要失败几次才成功)"""
    calls: dict[str, int] = {}

    def fake(ip, **kwargs):                   # noqa: ANN001, ANN003
        latency, ok, fail_times = table[ip]
        n = calls.get(ip, 0)
        calls[ip] = n + 1
        probe = core.Probe(ip=ip)
        probe.latency_ms = latency
        probe.connect_ms = latency
        probe.remote_ip = ip
        if not ok or n < fail_times:
            probe.ok = False
            probe.status = "000"
            probe.error = "timeout"
        else:
            probe.ok = True
            probe.status = "200"
        return probe

    return fake, calls


print("=" * 70)
print("结果筛选：延迟门槛 / 只取前 N / 稳定性排序")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 不设门槛、不限数量：全部保留（默认行为不变）")
# --------------------------------------------------------------------------
table = {f"192.0.2.{i}": (float(i * 10), True, 0) for i in range(1, 11)}
real_probe = core.http_probe
try:
    fake, _ = patch_probes(table)
    core.http_probe = fake
    out = core.scan_platform(make_platform(top_n=0, max_latency_ms=0),
                             targets=list(table), quiet=True)
finally:
    core.http_probe = real_probe
check("10 个全通过就保留 10 个", len(out.passed) == 10, str(len(out.passed)))
check("按延迟升序", [p.latency_ms for p in out.passed] == sorted(p.latency_ms for p in out.passed),
      str([p.latency_ms for p in out.passed]))

# --------------------------------------------------------------------------
print()
print("2) 延迟门槛：超过 60ms 的丢弃")
# --------------------------------------------------------------------------
try:
    fake, _ = patch_probes(table)
    core.http_probe = fake
    logs: list[str] = []
    out = core.scan_platform(make_platform(max_latency_ms=60),
                             targets=list(table), quiet=False, log=logs.append)
finally:
    core.http_probe = real_probe
check("只剩 <=60ms 的 6 个", len(out.passed) == 6, str(len(out.passed)))
check("留下来的都不超过门槛", all(p.latency_ms <= 60 for p in out.passed),
      str([p.latency_ms for p in out.passed]))
check("日志说明了丢弃数量", any("丢弃延迟" in line for line in logs),
      str([l for l in logs if "丢弃延迟" in l]))

# --------------------------------------------------------------------------
print()
print("3) 只保留最快的 N 个")
# --------------------------------------------------------------------------
try:
    fake, _ = patch_probes(table)
    core.http_probe = fake
    logs = []
    out = core.scan_platform(make_platform(top_n=3),
                             targets=list(table), quiet=False, log=logs.append)
finally:
    core.http_probe = real_probe
check("只留 3 个", len(out.passed) == 3, str(len(out.passed)))
check("留下的确实是最快的三个",
      [p.ip for p in out.passed] == ["192.0.2.1", "192.0.2.2", "192.0.2.3"],
      str([p.ip for p in out.passed]))
check("日志说明了只保留 N 个", any("只保留最快 3 个" in line for line in logs),
      str([l for l in logs if "只保留" in l]))

# --------------------------------------------------------------------------
print()
print("4) 门槛 + 前 N 同时生效（先按门槛丢，再取前 N）")
# --------------------------------------------------------------------------
try:
    fake, _ = patch_probes(table)
    core.http_probe = fake
    out = core.scan_platform(make_platform(max_latency_ms=80, top_n=2),
                             targets=list(table), quiet=True)
finally:
    core.http_probe = real_probe
check("先丢弃 >80ms 的，再取最快 2 个",
      [p.ip for p in out.passed] == ["192.0.2.1", "192.0.2.2"],
      str([p.ip for p in out.passed]))

# --------------------------------------------------------------------------
print()
print("5) 稳定性排序：同样延迟时，重试过的排后面")
# --------------------------------------------------------------------------
# 三个 IP 延迟相同；.11 第一次失败重试才成功（attempts=2）
table2 = {"192.0.2.11": (20.0, True, 1),
          "192.0.2.12": (20.0, True, 0),
          "192.0.2.13": (20.0, True, 0)}
try:
    fake, calls = patch_probes(table2)
    core.http_probe = fake
    out = core.scan_platform(make_platform(retries=1),
                             targets=list(table2), quiet=True)
finally:
    core.http_probe = real_probe
check("三个都通过", len(out.passed) == 3, str([p.ip for p in out.passed]))
check("需要重试的那个排在最后", out.passed[-1].ip == "192.0.2.11",
      str([(p.ip, p.attempts) for p in out.passed]))
check("attempts 被如实记录",
      [p.attempts for p in out.passed if p.ip == "192.0.2.11"] == [2],
      str([(p.ip, p.attempts) for p in out.passed]))
check("一次就成功的 attempts=1",
      all(p.attempts == 1 for p in out.passed if p.ip != "192.0.2.11"),
      str([(p.ip, p.attempts) for p in out.passed]))

# --------------------------------------------------------------------------
print()
print("6) RunConfig.apply_to 能把参数传到平台")
# --------------------------------------------------------------------------
plat = make_platform(top_n=0, max_latency_ms=0.0)
cfg = RunConfig(max_latency_ms=150.0, top_n=5)
cfg.apply_to([plat])
check("max_latency_ms 已应用", plat.max_latency_ms == 150.0, str(plat.max_latency_ms))
check("top_n 已应用", plat.top_n == 5, str(plat.top_n))

# --------------------------------------------------------------------------
print()
print("7) 环境变量 EO_MAX_MS / EO_TOP 生效（命令行参数优先）")
# --------------------------------------------------------------------------
import os                                                    # noqa: E402

orig_env = {k: os.environ.get(k) for k in ("EO_MAX_MS", "EO_TOP")}
try:
    os.environ["EO_MAX_MS"] = "250"
    os.environ["EO_TOP"] = "8"
    from eo.cli import _opt_number                            # noqa: E402
    check("EO_MAX_MS 被读取", _opt_number(None, "EO_MAX_MS", float) == 250.0,
          str(_opt_number(None, "EO_MAX_MS", float)))
    check("EO_TOP 被读取", _opt_number(None, "EO_TOP", int) == 8,
          str(_opt_number(None, "EO_TOP", int)))
    check("命令行优先于环境变量",
          _opt_number(30, "EO_MAX_MS", float) == 30.0,
          str(_opt_number(30, "EO_MAX_MS", float)))
    os.environ["EO_MAX_MS"] = "abc"
    check("非法值忽略、返回 None（不覆盖平台配置）",
          _opt_number(None, "EO_MAX_MS", float) is None,
          str(_opt_number(None, "EO_MAX_MS", float)))
finally:
    for k, v in orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print()
print("=" * 70)
if _failures:
    print(f"结果筛选测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("结果筛选测试全部通过")
sys.exit(0)
