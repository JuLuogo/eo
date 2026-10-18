#!/usr/bin/env python3
"""全局配置字段的类型安全：网页表单提交的是字符串。

上次给「平台级」字段加了类型转换（_coerce_platform_field），
但「全局」字段（rate/interval/workers/timeout/retries/log_buffer…）走的是
另一段代码：直接 setattr，没有任何转换。如果网页也提交字符串，
存进去就是 "50"，之后 make_rate_limiter("50") 之类会直接抛异常。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo.config_store import ConfigStore                            # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-globaltype-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def store() -> ConfigStore:
    d = WORK / f"s{len(list(WORK.glob('s*')))}"
    d.mkdir(parents=True, exist_ok=True)
    return ConfigStore(d / "config.json")


print("=" * 70)
print("全局配置字段的类型安全")
print("=" * 70)

print()
print("1) 网页提交字符串数值 -> 必须存成数字")
# --------------------------------------------------------------------------
s = store()
s.update({"rate": "50", "interval": "600", "workers": "12",
          "timeout": "2.5", "retries": "0", "log_buffer": "300",
          "max_latency_ms": "150", "top_n": "8"})
cfg = s.get()
for field, want_type, want_val in (
    ("rate", float, 50.0),
    ("interval", int, 600),
    ("workers", int, 12),
    ("timeout", float, 2.5),
    ("retries", int, 0),
    ("log_buffer", int, 300),
    ("max_latency_ms", float, 150.0),
    ("top_n", int, 8),
):
    got = getattr(cfg, field)
    check(f"{field} 类型与值正确", isinstance(got, want_type) and got == want_val,
          f"{got!r} ({type(got).__name__})")

print()
print("2) 存成数字后，下游使用不能报错")
# --------------------------------------------------------------------------
from eo.ratelimit import make_rate_limiter                         # noqa: E402
from eo.runner import RunConfig                                    # noqa: E402

try:
    limiter = make_rate_limiter(cfg.rate)
    check("make_rate_limiter 接受这个 rate", True, repr(limiter))
except Exception as exc:                                           # noqa: BLE001
    check("make_rate_limiter 接受这个 rate", False, f"{type(exc).__name__}: {exc}")

try:
    # 注意 RunConfig 没有 interval 字段（间隔是 CycleRunner 的参数）
    rc = RunConfig(rate=cfg.rate, workers=cfg.workers,
                   timeout=cfg.timeout, retries=cfg.retries,
                   max_latency_ms=cfg.max_latency_ms, top_n=cfg.top_n)
    check("RunConfig 接受这些值", True,
          f"rate={rc.rate} workers={rc.workers} max_ms={rc.max_latency_ms} top={rc.top_n}")
except Exception as exc:                                           # noqa: BLE001
    check("RunConfig 接受这些值", False, f"{type(exc).__name__}: {exc}")

print()
print("3) 空字符串 = 回退默认（而不是存成空串）")
# --------------------------------------------------------------------------
s2 = store()
s2.update({"rate": "", "workers": ""})
cfg2 = s2.get()
check("rate 空串回落默认 100", cfg2.rate == 100, repr(cfg2.rate))
check("workers 空串回落默认", isinstance(cfg2.workers, int), repr(cfg2.workers))

print()
print("4) 非法字符串不能把配置改坏")
# --------------------------------------------------------------------------
s3 = store()
before = s3.get().rate
s3.update({"rate": "abc"})
check("非法值被忽略，保持原值", s3.get().rate == before, repr(s3.get().rate))

print()
print("5) 布尔字段：网页复选框提交 true/false 字符串")
# --------------------------------------------------------------------------
s4 = store()
s4.update({"ping": "true", "auto_run": "false", "verbose": "1"})
cfg4 = s4.get()
check("ping=true -> True", cfg4.ping is True, repr(cfg4.ping))
check("auto_run=false -> False", cfg4.auto_run is False, repr(cfg4.auto_run))
check("verbose=1 -> True", cfg4.verbose is True, repr(cfg4.verbose))

print()
print("6) 字符串字段不受影响")
# --------------------------------------------------------------------------
s5 = store()
s5.update({"window": "00:00-07:00", "webhook": "https://example.com/hook",
           "dns_mode": "dry-run"})
cfg5 = s5.get()
check("window 正确", cfg5.window == "00:00-07:00", repr(cfg5.window))
check("webhook 正确", cfg5.webhook == "https://example.com/hook", repr(cfg5.webhook))
check("dns_mode 正确", cfg5.dns_mode == "dry-run", repr(cfg5.dns_mode))

print()
print("=" * 70)
if _failures:
    print(f"全局字段类型测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("全局字段类型测试全部通过")
sys.exit(0)
