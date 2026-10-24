#!/usr/bin/env python3
"""下载测速：按域名 SNI 钉 IP 下载、限速、阈值丢弃，以及与 DNS 下发的联动。

为什么要单独测：
  * 测速是本项目里唯一"按字节数计费"的功能 —— 限速算错会把 VPS 带宽打满，
    阈值算错会把好节点全丢掉。两条都不报错，只会"悄悄出错"。
  * 测速结果要写进结果文件第三列，然后 DNS 侧再按阈值筛；任何一环对不上，
    用户看到的就是"设置了阈值但没生效"。

覆盖：
  1) speed_probe 行为：大文件、小文件（样本不足）、404、服务端不说话、限速是否真的限住
  2) run_speed_tests：只测前 N 个、结果写回 Probe、失败不计入「不达标」
  3) 与 scan_platform / write_result 的接线：结果文件第三列
  4) DNS 侧：阈值丢弃 + 未测速视为未达标 + 没有测速列时拒绝下发
  5) 配置层：新字段的类型转换（网页传字符串不能改坏配置）
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eo.core as core                                             # noqa: E402
import eo.speed as speed_mod                                       # noqa: E402
from eo.config_store import ConfigStore                           # noqa: E402
from eo.core import Platform, Probe, write_result                  # noqa: E402
from eo.dns import _parse_mbps, _parse_ms, _select_for_dns         # noqa: E402
from eo.speed import (SpeedResult, run_speed_tests, speed_probe,          # noqa: E402
                      normalize_speed_path)

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-speed-"))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------
# 本地 HTTP 服务器：可指定响应体积，用来验证"测速"本身
# --------------------------------------------------------------------------
BIG = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:      # 安静
        pass

    def do_GET(self) -> None:                  # noqa: N802
        if self.path.startswith("/small"):
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/missing"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/silent"):
            time.sleep(3.0)
            return
        if self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Length", str(BIG))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            chunk = b"0" * (64 * 1024)
            sent = 0
            try:
                while sent < BIG:
                    self.wfile.write(chunk)
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
SERVER.daemon_threads = True
PORT = SERVER.server_address[1]
threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def probe(path: str, **kw) -> SpeedResult:
    kw.setdefault("seconds", 1.5)
    kw.setdefault("max_bytes", 8 * 1024 * 1024)
    kw.setdefault("timeout", 5.0)
    return speed_probe("127.0.0.1", host="127.0.0.1", port=PORT, scheme="http",
                       path=path, **kw)


print("=" * 70)
print("下载测速 + 阈值筛选")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) speed_probe：正常下载 / 样本太小 / 404 / 服务端不说话")
# --------------------------------------------------------------------------
t0 = time.time()
r = probe("/big.bin", seconds=1.0)
check("大文件测速成功", r.ok, r.line())
check("读到足够字节", r.bytes >= speed_mod.MIN_SAMPLE_BYTES, f"{r.bytes} 字节")
check("速度是正数（回环必然很快）", r.mbps > 20, f"{r.mbps:.1f}Mbps")
check("耗时不失控", time.time() - t0 < 5, f"{time.time() - t0:.2f}s")

r_small = probe("/small.bin")
check("小响应判为测不出（而不是算出 200Mbps）", r_small.ok is False, r_small.line())
check("原因说明了样本太小", "样本太小" in r_small.error or "KB" in r_small.error, r_small.error)

r_404 = probe("/missing")
check("404 判失败", r_404.ok is False, r_404.line())
check("原因里带状态码", "404" in r_404.error, r_404.error)

t0 = time.time()
r_silent = probe("/silent", seconds=0.4, timeout=1.0)
check("服务端不说话 -> 超时失败", r_silent.ok is False, r_silent.line())
check("超时能及时返回", time.time() - t0 < 4.0, f"{time.time() - t0:.2f}s")

# --------------------------------------------------------------------------
print()
print("2) 限速：真的按 Mbps 限住了（这是最容易被算错的一处）")
# --------------------------------------------------------------------------
r_lim = probe("/big.bin", seconds=1.5, limit_mbps=8.0)
check("限速测速成功", r_lim.ok, r_lim.line())
in_tolerance = 6.0 <= r_lim.mbps <= 11.0
check("实测速率贴着 8Mbps（±40%）", in_tolerance, f"{r_lim.mbps:.1f}Mbps")
check("1.5 秒内只读了约 1.5MB（不是 4MB）", r_lim.bytes <= 2 * 1024 * 1024,
      f"{r_lim.bytes / 1024 / 1024:.2f}MB")
check("耗时接近设定时长（说明是主动放慢，不是下载完了）", r_lim.seconds >= 1.2,
      f"{r_lim.seconds:.2f}s")

r_cap = probe("/big.bin", seconds=10, max_bytes=1024 * 1024)
check("max_bytes 上限生效（读满就停）", 1024 * 1024 <= r_cap.bytes <= 1024 * 1024 + 128 * 1024,
      f"{r_cap.bytes} 字节")
check("不限速时明显快于 8Mbps", r_cap.mbps > 20, f"{r_cap.mbps:.1f}Mbps")

# --------------------------------------------------------------------------
print()
print("3) run_speed_tests：只测延迟最好的前 N 个，结果写回 Probe")
# --------------------------------------------------------------------------
def make_probes(n: int) -> list[Probe]:
    out = []
    for i in range(n):
        p = Probe(ip=f"10.9.0.{i + 1}", ok=True, status="200")
        p.latency_ms = 10.0 + i
        out.append(p)
    return out


def _fake_ok(ip: str, table: dict[str, tuple[float, bool]]) -> Probe:
    """假探测：按表返回延迟，让扫描逻辑在没有真实网络时也能跑。"""
    latency, ok = table.get(ip, (999.0, False))
    p = Probe(ip=ip, ok=ok, status="200" if ok else "000")
    p.latency_ms = latency
    p.connect_ms = latency * 0.1
    p.remote_ip = ip
    return p


speeds = {}
real_speed_probe = speed_mod.speed_probe


def fake_speed_probe(ip, **kw):                                    # noqa: ANN001, ANN003
    table = speeds[ip]
    ok, mbps = table
    res = SpeedResult(ip=ip, ok=ok, mbps=mbps, bytes=2 * 1024 * 1024, seconds=1.0)
    if not ok:
        res.error = "模拟失败"
    return res


plat = Platform(key="t-lt", name="测试平台", host="t.local", workers=4,
                speed_test=True, speed_top=3, min_speed_mbps=10.0,
                speed_seconds=1.0, speed_limit_mbps=50.0, speed_concurrency=2)
probes = make_probes(6)
for p in probes:
    speeds[p.ip] = (True, 100.0)          # 默认都很快
speeds["10.9.0.2"] = (True, 3.0)          # 慢：低于阈值
speeds["10.9.0.3"] = (False, 0.0)         # 测速失败
speed_mod.speed_probe = fake_speed_probe
try:
    stats = run_speed_tests(plat, probes, log=lambda *_: None, quiet=True)
finally:
    speed_mod.speed_probe = real_speed_probe
check("只测了前 3 个", stats["tested"] == 3, str(stats))
check("有 2 个成功了", stats["ok"] == 2, str(stats))
check("1 个低于阈值", stats["below"] == 1, str(stats))
check("1 个测速失败", stats["failed"] == 1, str(stats))
check("后面的 IP 没有被测", probes[3].speed_mbps is None and probes[5].speed_mbps is None,
      f"{probes[3].speed_mbps}/{probes[5].speed_mbps}")
check("测到的速度写回了 Probe", probes[0].speed_mbps == 100.0, str(probes[0].speed_mbps))
check("失败的原因写进了 speed_error", probes[2].speed_error == "模拟失败",
      probes[2].speed_error)

# ---- 硬上限：防止手误把流量/带宽打爆 ----
cap_logs: list[str] = []
real_max = speed_mod.MAX_CANDIDATES
speed_mod.MAX_CANDIDATES = 2
try:
    plat_cap = Platform(key="cap-lt", name="Cap", host="t.local", workers=2,
                        speed_test=True, speed_top=0, speed_seconds=1.0)
    got_kw: dict = {}


    def capture_probe(ip, **kw):                                   # noqa: ANN001, ANN003
        got_kw.update(kw)
        return SpeedResult(ip=ip, ok=True, mbps=10.0, bytes=200_000, seconds=1.0)


    stats_cap = run_speed_tests(plat_cap, make_probes(6), log=cap_logs.append,
                                quiet=True, prober=capture_probe)
finally:
    speed_mod.MAX_CANDIDATES = real_max
check("speed_top=0 也会被硬上限截住", stats_cap["tested"] == 2, str(stats_cap))
check("超上限时日志有提示", any("硬上限" in l for l in cap_logs), str(cap_logs))

time_logs: list[str] = []
plat_time = Platform(key="time-lt", name="T", host="t.local", workers=2,
                     speed_test=True, speed_top=1, speed_seconds=999.0)
run_speed_tests(plat_time, make_probes(1), log=time_logs.append, quiet=True,
                prober=lambda ip, **kw: (got_kw.update(kw) or
                                         SpeedResult(ip=ip, ok=True, mbps=10.0,
                                                     bytes=200_000, seconds=1.0)))
check("测速时长超过上限按上限执行", abs(got_kw["seconds"] - speed_mod.MAX_SECONDS) < 1e-6,
      f"{got_kw['seconds']}")
check("时长被截断时有日志", any("超过上限" in l for l in time_logs), str(time_logs))

# ---- 只在延迟门槛内测速：门槛外的反正不会下发，别白烧流量 ----
lat_logs: list[str] = []
plat_lat = Platform(key="lat-lt", name="Lat", host="t.local", workers=2,
                    speed_test=True, speed_top=10, max_latency_ms=12.0,
                    speed_seconds=1.0, speed_concurrency=2)
stats_lat = run_speed_tests(plat_lat, make_probes(6), log=lat_logs.append,
                            quiet=False,
                            prober=lambda ip, **kw: SpeedResult(ip=ip, ok=True, mbps=10.0,
                                                                bytes=200_000, seconds=1.0))
check("延迟门槛外的 IP 不测速", stats_lat["tested"] == 3, str(stats_lat))
check("日志说明了为什么少测", any("不会下发" in l for l in lat_logs), str(lat_logs[:2]))

# --------------------------------------------------------------------------
print()
print("4) 与扫描/落盘接线：结果文件第三列 = 速度（没测到写 -）")
# --------------------------------------------------------------------------
table = {f"10.8.0.{i}": (float(i * 5), True) for i in range(1, 5)}
real_http = core.http_probe
real_run = speed_mod.run_speed_tests


def fake_run(platform, passed, **kw):                              # noqa: ANN001, ANN003
    for p in passed:
        p.speed_mbps = 50.0 if p.ip.endswith(".1") else (5.0 if p.ip.endswith(".2") else None)
    return {"tested": len(passed), "ok": 2, "below": 1, "failed": len(passed) - 2, "skipped": 0}


try:
    core.http_probe = lambda ip, **kw: _fake_ok(ip, table)          # type: ignore[assignment]
    speed_mod.run_speed_tests = fake_run                            # type: ignore[assignment]
    scan_plat = Platform(key="t2-lt", name="T2", host="t.local", workers=2,
                         speed_test=True, speed_top=2,
                         targets_file=str(WORK / "t.txt"))
    out = core.scan_platform(scan_plat, targets=list(table), quiet=True)
finally:
    core.http_probe = real_http                                     # type: ignore[assignment]
    speed_mod.run_speed_tests = real_run                            # type: ignore[assignment]

check("扫描产出了测速列", any(p.speed_mbps is not None for p in out.passed),
      str([p.speed_mbps for p in out.passed]))
result_file = WORK / "speed-out.txt"
write_result(result_file, out.passed, with_speed=True)
lines = result_file.read_text("utf-8").splitlines()
check("每行三列", all(len(line.split("\t")) == 3 for line in lines), lines[0] if lines else "")
check("没测到的写 '-' 而不是 0", any(line.split("\t")[2] == "-" for line in lines),
      str([line.split("\t")[2] for line in lines]))
check("速度列带 Mbps 单位", any(line.split("\t")[2].endswith("Mbps") for line in lines),
      lines[0] if lines else "")

print()
print("4b) 测速路径归一化：填完整 URL / 缺前导斜杠也能用")
# --------------------------------------------------------------------------
check("完整 URL -> 只取路径", normalize_speed_path("https://pcb.example.com/10mb.bin")
      == "/10mb.bin", normalize_speed_path("https://pcb.example.com/10mb.bin"))
check("URL 带查询串 -> 保留查询串",
      normalize_speed_path("http://x.com/a/b.bin?x=1") == "/a/b.bin?x=1",
      normalize_speed_path("http://x.com/a/b.bin?x=1"))
check("只有域名 -> /", normalize_speed_path("https://x.com") == "/",
      normalize_speed_path("https://x.com"))
check("缺前导斜杠 -> 补上", normalize_speed_path("10mb.bin") == "/10mb.bin",
      normalize_speed_path("10mb.bin"))
check("空 -> /", normalize_speed_path("") == "/", normalize_speed_path(""))

print()
print("5) 结果文件解析：- / 空 / 12.3Mbps 都不能解析错")
check("12.3Mbps -> 12.3", _parse_mbps("12.3Mbps") == 12.3, str(_parse_mbps("12.3Mbps")))
check("'-' -> None（没测 ≠ 0）", _parse_mbps("-") is None, str(_parse_mbps("-")))
check("空 -> None", _parse_mbps("") is None, str(_parse_mbps("")))
check("0.0Mbps -> 0.0（测得 0 要保留）", _parse_mbps("0.0Mbps") == 0.0,
      str(_parse_mbps("0.0Mbps")))
check("延迟 35ms -> 35", _parse_ms("35ms") == 35.0, str(_parse_ms("35ms")))
check("延迟 '-' -> None", _parse_ms("-") is None, str(_parse_ms("-")))

# --------------------------------------------------------------------------
print()
print("6) DNS 侧：阈值丢弃 + 未测速视为未达标")
# --------------------------------------------------------------------------
entries = [
    ("1.1.1.1", 10.0, 100.0),
    ("1.1.1.2", 11.0, 5.0),        # 低于 10Mbps -> 丢
    ("1.1.1.3", 12.0, None),       # 没测过 -> 视为未达标
    ("1.1.1.4", 13.0, 20.0),
]
logs: list[str] = []
sel_plat = Platform(key="t3-lt", name="T3", host="t.local",
                    speed_test=True, min_speed_mbps=10.0, top_n=10)
kept = _select_for_dns(sel_plat, entries, logs.append)
check("只留下达标且有测速数据的 2 个", kept == ["1.1.1.1", "1.1.1.4"], str(kept))
check("日志说明了阈值筛掉几个", any("低于 10Mbps 丢弃 1 个" in l for l in logs), str(logs))
check("日志说明了未测速的不下发", any("没有测速数据" in l for l in logs), str(logs))

logs = []
kept = _select_for_dns(sel_plat, [("2.2.2.2", 5.0, None), ("2.2.2.3", 6.0, None)], logs.append)
check("整份文件都没有测速数据时拒绝下发（返回空）", kept == [], str(kept))
check("原因写清楚了", any("拒绝下发" in l for l in logs), str(logs))

logs = []
no_speed_plat = Platform(key="t4-lt", name="T4", host="t.local", top_n=2)
kept = _select_for_dns(no_speed_plat, entries, logs.append)
check("没开测速时阈值不生效（按数量取前 2）", kept == ["1.1.1.1", "1.1.1.2"], str(kept))

logs = []
speed_only_threshold = Platform(key="t5-lt", name="T5", host="t.local",
                                speed_test=True, min_speed_mbps=10.0)
kept = _select_for_dns(speed_only_threshold, entries, logs.append)
check("只设阈值不设数量：剩下达标的 2 个", kept == ["1.1.1.1", "1.1.1.4"], str(kept))

# 阈值 + 延迟门槛 + 数量 三者叠加
logs = []
combo = Platform(key="t6-lt", name="T6", host="t.local", speed_test=True,
                 min_speed_mbps=10.0, max_latency_ms=12.5, top_n=1)
entries2 = [
    ("3.3.3.1", 10.0, 100.0),
    ("3.3.3.2", 11.0, 90.0),
    ("3.3.3.3", 20.0, 90.0),      # 延迟超门槛
    ("3.3.3.4", 12.0, 2.0),       # 速度不达标
]
kept = _select_for_dns(combo, entries2, logs.append)
check("阈值→门槛→前 N 顺序正确", kept == ["3.3.3.1"], str(kept))
check("三步都有日志", sum(1 for l in logs if "筛选" in l or "只下发" in l) >= 2, str(logs))

# --------------------------------------------------------------------------
print()
print("7) 配置层：网页传来的字符串不能把新字段改坏")
# --------------------------------------------------------------------------
store = ConfigStore(WORK / "config.json")
store.update({"platforms": {"eo-lt": {"speed_test": "true", "min_speed_mbps": "12.5",
                                      "speed_top": "20", "speed_path": "/10mb.bin",
                                      "speed_limit_mbps": "", "speed_seconds": "abc"}}})
saved = store.get().platforms["eo-lt"]
check("speed_test 字符串 true -> True", saved.speed_test is True, repr(saved.speed_test))
check("min_speed_mbps -> 12.5", saved.min_speed_mbps == 12.5, repr(saved.min_speed_mbps))
check("speed_top -> 20（int）", saved.speed_top == 20 and isinstance(saved.speed_top, int),
      repr(saved.speed_top))
check("speed_path 原样保存", saved.speed_path == "/10mb.bin", saved.speed_path)
check("空字符串 -> None（回落到默认）", saved.speed_limit_mbps is None,
      repr(saved.speed_limit_mbps))
check("非法数字不写入（保持 None）", saved.speed_seconds is None, repr(saved.speed_seconds))

print()
print("8) 平台面板补齐：config.platforms 为空时也不能是空白面板")
from eo.webui import OptimizerService                              # noqa: E402

WORK2 = Path(tempfile.mkdtemp(prefix="eo-speed-ui-"))
store2 = ConfigStore(WORK2 / "config.json")
service = OptimizerService(store2)
saved_env = {k: os.environ.get(k) for k in ("EO_NETWORK", "EO_PLATFORMS")}
try:
    os.environ["EO_NETWORK"] = "lt"
    payload = service.config_payload()
    keys = list((payload.get("platforms") or {}).keys())
    check("空配置下平台面板也有条目", bool(keys), str(keys))
    one = (payload.get("platforms") or {}).get("eo-lt") or {}
    check("补出来的条目带生效域名", bool(one.get("host")), str(one.get("host")))
    check("补出来的条目带结果路径", bool(one.get("output_file")), str(one.get("output_file")))
    check("补出来的条目带测速字段", "speed_test" in one and "min_speed_mbps" in one,
          str(sorted(k for k in one if k.startswith("speed"))))
    plat = service.build_platforms(keys=["eo-lt"])[0]
    check("build_platforms 能读到测速默认值（默认关闭）", plat.speed_test is False,
          str(plat.speed_test))
finally:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

# 补出来的条目不能被覆盖成"另一个值"
service.store.update({"platforms": {"eo-lt": {"speed_test": True, "min_speed_mbps": 15}}})
try:
    os.environ["EO_NETWORK"] = "lt"
    service.config_payload()
finally:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
saved = service.store.get().platforms.get("eo-lt")
check("已有条目不会被补齐逻辑覆盖", saved.speed_test is True and saved.min_speed_mbps == 15.0,
      f"{saved.speed_test}/{saved.min_speed_mbps}")
plat = service.build_platforms(keys=["eo-lt"])[0]
check("生效值来自网页设置", plat.speed_test is True and plat.min_speed_mbps == 15.0,
      f"{plat.speed_test}/{plat.min_speed_mbps}")

# 测速自检：没有启用平台/没有结果文件时要给出可操作的提示（不能静默成功）
print()
print("8b) 测速自检入口")
store3 = ConfigStore(Path(tempfile.mkdtemp(prefix="eo-speed-chk-")) / "config.json")
service3 = OptimizerService(store3)
try:
    os.environ["EO_NETWORK"] = "lt"
    ok, msg = service3.speed_check()
    check("没启用测速时给出指引", ok is False and "启用下载测速" in msg, msg)
    store3.update({"platforms": {"eo-lt": {"speed_test": True, "speed_path": "/10mb.bin"}}})
    for key in ("esa-lt", "cft-lt"):
        store3.update({"platforms": {key: {"speed_test": False}}})
    ok, msg = service3.speed_check()
    check("没有结果文件时提示先跑一轮", ok is False and "先跑一轮" in msg, msg)
    routes_src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
    check("有 /api/control/speed-check 路由", '"/api/control/speed-check"' in routes_src, "有")

    # 成功路径：真的把结果文件里第一个 IP 拿去测（这里用假 prober，不联网）
    res_file = Path(tempfile.mkdtemp(prefix="eo-speed-chk2-")) / "out.txt"
    res_file.write_text("203.0.113.7\t42ms\t-\n", encoding="utf-8")
    store3.update({"platforms": {"eo-lt": {"output_file": str(res_file),
                                           "min_speed_mbps": 5.0}}})
    real_probe = speed_mod.speed_probe
    speed_mod.speed_probe = lambda ip, **kw: SpeedResult(ip=ip, ok=True, mbps=50.0,
                                                        bytes=2 * 1024 * 1024, seconds=1.0)
    try:
        ok, msg = service3.speed_check()
        check("自检能启动", ok is True, msg)
        deadline = time.time() + 6
        while service3.runner.running and time.time() < deadline:
            time.sleep(0.05)
        text = "\n".join(str(item.get("line") if isinstance(item, dict) else item)
                         for item in service3.logs.tail(50))
        check("自检日志里有实测速度", "50.0Mbps" in text, text[-120:].replace("\n", " | "))
        check("自检给出了是否达标的结论", "达标" in text or "低于阈值" in text,
              text[-120:].replace("\n", " | "))
    finally:
        speed_mod.speed_probe = real_probe
finally:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print()
print("9) 页面接线：测速字段与开关都在界面上")
pages_src = (ROOT / "eo" / "pages.py").read_text(encoding="utf-8")
for name in ("speed_test", "speed_path", "min_speed_mbps", "speed_limit_mbps",
             "speed_seconds", "speed_bytes_mb", "speed_top", "speed_concurrency"):
    present = (f"'{name}'" in pages_src) or (f'"{name}"' in pages_src) or (name in pages_src)
    check(f"  {name} 在平台面板里", present, "有" if present else "缺")
check("有「启用下载测速」开关文案", "启用下载测速" in pages_src, "有")
check("提示了测速路径要大文件", "大文件" in pages_src, "有")
check("下发筛选里会显示速度阈值", "速度 ≥" in pages_src, "有")

print()
print("=" * 70)
SERVER.shutdown()
if _failures:
    print(f"下载测速测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("下载测速测试全部通过")
sys.exit(0)
