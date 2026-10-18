#!/usr/bin/env python3
"""结果文件 vs DNS 下发：两份口径必须分开。

需求（用户明确要的）：
  * 结果文件保留**全部**可用 IP（按延迟升序、带延迟列）—— 方便人工核对与回溯
  * DNS 只下发筛选后的那一小批：先按延迟门槛丢，再取最快的 N 个

这里验证：
  1) 扫描结果不再被门槛/N 截断（文件是完整的）
  2) DNS 侧按门槛 + N 筛选，且两条下发路径（自动/手动）行为一致
  3) 延迟列始终存在（DNS 侧要靠它排序）
  4) 边界：筛完为空必须拒绝下发；旧格式文件（无延迟列）要说明而不是静默照发
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shutil                                                      # noqa: E402

import atexit                                                      # noqa: E402

import eo.core as core                                             # noqa: E402
from eo.core import Platform, Probe, write_result                  # noqa: E402
from eo.dns import DnsTarget, _select_for_dns, sync_platform       # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-split-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def make_platform(**over) -> Platform:
    base = dict(key="t-lt", name="Test", host="t.local", network="lt", workers=4,
                targets_file=str(WORK / "t.txt"), output_file=str(WORK / "out.txt"))
    base.update(over)
    return Platform(**base)


def fake_probe_table(table: dict[str, tuple[float, bool]]):
    def fake(ip, **kw):                                            # noqa: ANN001, ANN003
        latency, ok = table.get(ip, (999.0, False))
        p = Probe(ip=ip, ok=ok, status="200" if ok else "000")
        p.latency_ms = latency
        p.connect_ms = latency * 0.1
        p.remote_ip = ip
        return p
    return fake


print("=" * 70)
print("结果文件（全部）与 DNS 下发（筛选后）分开")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 扫描结果不再被门槛/N 截断：结果文件是完整的")
# --------------------------------------------------------------------------
table = {f"10.0.0.{i}": (float(i * 10), True) for i in range(1, 21)}   # 10~200ms
real = core.http_probe
try:
    core.http_probe = fake_probe_table(table)                      # type: ignore[assignment]
    plat = make_platform(max_latency_ms=100, top_n=3)
    out = core.scan_platform(plat, targets=list(table), quiet=True)
finally:
    core.http_probe = real                                         # type: ignore[assignment]
check("20 个全部保留（不再被 top_n=3 截断）", len(out.passed) == 20, str(len(out.passed)))
check("仍然按延迟升序", [p.latency_ms for p in out.passed] == sorted(p.latency_ms for p in out.passed),
      str([p.latency_ms for p in out.passed][:5]))
check("门槛不再影响扫描结果", all(p.latency_ms > 100 for p in out.passed[-3:]),
      str([p.latency_ms for p in out.passed[-3:]]))

print()
print("2) 结果文件格式：始终带延迟列")
# --------------------------------------------------------------------------
result_file = WORK / "out.txt"
write_result(result_file, out.passed)
lines = result_file.read_text("utf-8").splitlines()
check("每行都是 IP<TAB>延迟", all("\t" in line and line.endswith("ms") for line in lines),
      lines[0] if lines else "(空)")
check("行数等于可用 IP 数", len(lines) == 20, str(len(lines)))
check("第一行是最快的", lines[0].startswith("10.0.0.1"), lines[0])

print()
print("3) DNS 侧筛选：先门槛、后取前 N")
# --------------------------------------------------------------------------
entries = [(line.split("\t")[0], float(line.split("\t")[1].rstrip("ms")))
           for line in lines]
logs: list[str] = []
kept = _select_for_dns(make_platform(max_latency_ms=100, top_n=3), entries, logs.append)
check("门槛 100ms + 前 3 个 -> 只剩 3 个", len(kept) == 3, str(kept))
check("留下的正是最快的三个", kept == ["10.0.0.1", "10.0.0.2", "10.0.0.3"], str(kept))
check("日志说明了门槛筛掉多少", any("按门槛筛选" in l for l in logs), str(logs[:1]))

logs = []
kept = _select_for_dns(make_platform(max_latency_ms=50, top_n=0), entries, logs.append)
check("只设门槛：留下 <=50ms 的 5 个", len(kept) == 5, str(kept))

logs = []
kept = _select_for_dns(make_platform(max_latency_ms=0, top_n=7), entries, logs.append)
check("只设数量：留下最快的 7 个", len(kept) == 7, str(kept))

logs = []
kept = _select_for_dns(make_platform(max_latency_ms=0, top_n=0), entries, logs.append)
check("都不设：全部下发（20 个）", len(kept) == 20, str(len(kept)))

print()
print("4) 边界：门槛太严筛空 -> sync_platform 拒绝下发")
# --------------------------------------------------------------------------
plat = make_platform(max_latency_ms=1, output_file=str(result_file),
                     dns={"provider": "huawei", "zone_name": "z.com",
                          "record_name": "t.z.com"})
r = sync_platform(plat, log=logs.append, dry_run=True)
check("筛空后拒绝下发", r.ok is False, r.summary()[:70])
check("原因说明是筛完没剩下", "没有剩下" in r.summary(), r.summary()[:70])

print()
print("5) 旧格式结果文件（没有延迟列）：要说明，不能静默照发全部")
# --------------------------------------------------------------------------
plain = WORK / "plain.txt"
plain.write_text("\n".join(f"10.1.0.{i}" for i in range(1, 6)) + "\n", encoding="utf-8")
plain_entries = [(l.strip(), None) for l in plain.read_text("utf-8").splitlines() if l.strip()]
logs = []
kept = _select_for_dns(make_platform(max_latency_ms=50, top_n=2), plain_entries, logs.append)
check("全部原样下发（无法筛选）", len(kept) == 5, str(len(kept)))
check("明确提示了原因", any("没有延迟列" in l for l in logs), str(logs[:1]))

print()
print("6) 手动下发路径同样生效（updatedns -> sync_platform）")
# --------------------------------------------------------------------------
import updatedns                                                   # noqa: E402
from eo.platforms import PLATFORMS                                 # noqa: E402

plat = PLATFORMS["eo-lt"]
saved = (plat.output_file, plat.max_latency_ms, plat.top_n, plat.dns)
plat.output_file = str(result_file)
plat.max_latency_ms = 100
plat.top_n = 4
plat.dns = dict(plat.dns or {}) or {"provider": "huawei", "zone_name": "1.060730.xyz",
                                    "record_name": "eo-lt.1.060730.xyz"}
captured: list[list[str]] = []
real_sync = updatedns.sync_platform
updatedns.sync_platform = lambda p, **kw: (captured.append(          # type: ignore[assignment]
    _select_for_dns(p, entries, lambda *_: None)), type("R", (), {
        "ok": True, "summary": lambda self=None: "ok"})())[1]
try:
    updatedns.main(["--dry-run", "-p", "eo-lt"])
finally:
    updatedns.sync_platform = real_sync                            # type: ignore[assignment]
    plat.output_file, plat.max_latency_ms, plat.top_n, plat.dns = saved
check("手动下发也按门槛+数量筛选", captured and len(captured[0]) == 4,
      str([len(c) for c in captured]))

print()
print("7) 延迟口径：TCP + TLS（不是纯 TCP 握手）")
# --------------------------------------------------------------------------
src = (ROOT / "eo" / "core.py").read_text(encoding="utf-8")
check("计算式是 connect_ms + tls_ms",
      "result.latency_ms = result.connect_ms + result.tls_ms" in src, "有")
check("Probe 有 tls_ms 字段",
      "tls_ms: float = 0.0" in src, "有")
check("curl 回退路径也用同一口径",
      "time_appconnect" in src, "有")
check("注释里写明了为什么不用纯 TCP 握手",
      "物理上不可能" in src, "有")

print()
print("8) 骤降保护与「允许大幅缩容」入口")
# --------------------------------------------------------------------------
# 用户实际会遇到：线上 530 条记录，把「DNS 下发数量」设成 10，
# 第一次下发会被骤降保护拦住（下限是现有的 1/4 = 132）。
# 所以网页上必须有个明确的开关，否则用户只会看到一直失败。
#
# 这里直接测判定函数（不引入别的测试模块 —— 那些文件是脚本式的，
# import 就会把它们整份跑一遍）。
from eo.dns import _shrunk_too_much                                # noqa: E402

check("530 -> 10 被拦住", _shrunk_too_much(530, 10, False) != "",
      _shrunk_too_much(530, 10, False)[:60])
check("拦下来的提示里说明可以加 --force",
      "--force" in _shrunk_too_much(530, 10, False), _shrunk_too_much(530, 10, False)[:70])
check("force=True 时放行", _shrunk_too_much(530, 10, True) == "", "放行")
check("530 -> 200 不拦（在 1/4 之上）", _shrunk_too_much(530, 200, False) == "", "放行")
check("现有 0 条时不拦（首次创建）", _shrunk_too_much(0, 10, False) == "", "放行")

# 网页入口
webui_src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
pages_src = (ROOT / "eo" / "pages.py").read_text(encoding="utf-8")
check("dns_push 支持 force 参数", "def dns_push(self, force: bool = False)" in webui_src,
      "已支持")
check("路由会读取 body 里的 force", 'body.get("force", False)' in webui_src, "已接线")
check("网页上有「允许大幅缩容」复选框", "d-dnsforce" in pages_src, "有")
check("勾选后带 force 提交", "post('/api/control/dns-push', {force: force})" in pages_src,
      "已接线")
check("勾选时会二次确认", "可能把线上解析砍掉一大半" in pages_src, "有确认")

print()
print("=" * 70)
if _failures:
    print(f"文件/DNS 口径分离测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("文件/DNS 口径分离测试全部通过")
sys.exit(0)
