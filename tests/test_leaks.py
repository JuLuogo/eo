#!/usr/bin/env python3
"""长跑稳定性测试：连续跑多轮，线程与文件描述符不能持续增长。

这类问题短时间看不出来：跑一轮两轮都正常，跑一天之后 fd 用尽或线程堆满，
表现为"连不上、网页打不开"，而且重启就好 —— 很难查。
这里直接数 /proc/self/fd 和线程数。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                     # noqa: E402

from eo.webui import WebServer, make_service                       # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-leak-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def threads() -> int:
    return threading.active_count()


print("=" * 70)
print("长跑稳定性：多轮之后线程 / fd 不应增长")
print("=" * 70)

cfgdir = WORK / "config"
cfgdir.mkdir(parents=True, exist_ok=True)
tiny = WORK / "tiny.txt"
tiny.write_text("\n".join(f"192.0.2.{i}" for i in range(1, 9)) + "\n", encoding="utf-8")

os.environ["EO_WEB_AUTH"] = "off"
os.environ["EO_NETWORK"] = "lt"
os.environ["EO_PLATFORMS"] = "eo-lt"          # 只跑一个平台，轮次快
svc = make_service(cfgdir / "config.json")
svc.store.update({"platforms": {"eo-lt": {"targets_file": str(tiny), "output_file": str(WORK / "r.txt"),
                                          "enabled": True}},
                  "rate": 0, "timeout": 1, "retries": 0})

server = WebServer(svc, host="127.0.0.1", port=0)
port = server.httpd.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

import urllib.request                                              # noqa: E402


def api(path: str, method: str = "GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def wait_idle(timeout: float = 90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not api("/api/status")["runner"]["running"]:
            return True
        time.sleep(0.4)
    return False


print()
print("1) 先空跑 3 轮，让线程池/缓存稳定下来，再取基线")
for _ in range(3):
    api("/api/control/run-once", "POST", {})
    wait_idle()
base_fd, base_th = fds(), threads()
print(f"     基线：fd={base_fd}  线程={base_th}")

print()
print("2) 再跑 10 轮，观察是否增长")
samples = []
for i in range(10):
    api("/api/control/run-once", "POST", {})
    if not wait_idle():
        check(f"第 {i + 1} 轮结束", False, "超时未结束")
        break
    samples.append((fds(), threads()))
    print(f"     第 {i + 1:2d} 轮：fd={samples[-1][0]}  线程={samples[-1][1]}")

if samples:
    max_fd = max(s[0] for s in samples)
    max_th = max(s[1] for s in samples)
    check("fd 没有持续增长", max_fd <= base_fd + 8, f"基线 {base_fd} -> 峰值 {max_fd}")
    check("线程没有持续增长", max_th <= base_th + 2, f"基线 {base_th} -> 峰值 {max_th}")

print()
print("3) 日志缓冲不会无限增长（跑了很多轮之后仍是 500 上限）")
st = api("/api/status")
logs = api("/api/logs")
check("日志条数不超过容量", len(logs["logs"]) <= logs["capacity"],
      f"{len(logs['logs'])} / {logs['capacity']}")
check("容量是默认的 500", logs["capacity"] == 500, str(logs["capacity"]))

print()
print("4) 连续 start/stop 循环不泄漏")
for _ in range(3):
    api("/api/control/start", "POST", {})
    time.sleep(1.2)
    api("/api/control/stop", "POST", {})
    wait_idle()
time.sleep(1)
fd2, th2 = fds(), threads()
check("反复启停后 fd 稳定", fd2 <= base_fd + 8, f"{base_fd} -> {fd2}")
check("反复启停后线程稳定", th2 <= base_th + 2, f"{base_th} -> {th2}")

print()
print("5) 大量 HTTP 请求不泄漏连接")
for _ in range(200):
    api("/api/status")
time.sleep(0.8)
fd3 = fds()
check("200 次请求后 fd 稳定", fd3 <= base_fd + 10, f"{base_fd} -> {fd3}")

# --------------------------------------------------------------------------
print()
print("6) monitor 循环长跑：12 轮后 fd / 线程 / 常驻内存都不增长")
# --------------------------------------------------------------------------
# monitor 模式（run.py monitor）用的就是 CycleRunner，与 web 端同一套；
# 这里直接在进程里跑循环，比等 CLI 的 60 秒最小间隔快得多。
from eo.runner import CycleRunner, run_once as runner_run_once      # noqa: E402


def rss_now_mb() -> float:
    """当前常驻内存（不是峰值 —— 峰值只增不减，看不出泄漏）。"""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return -1.0


mon_platforms = svc.build_platforms()
mon_config = svc.build_run_config()
mon_fd, mon_th, mon_rss = fds(), threads(), rss_now_mb()
print(f"     基线：fd={mon_fd}  线程={mon_th}  常驻内存={mon_rss:.1f}MB")

rounds = [0]


def cycle_job(cycle_no: int, stop_event) -> None:                  # noqa: ANN001
    runner_run_once(mon_platforms, mon_config, emit=lambda _e: None, stop_event=stop_event)
    rounds[0] = cycle_no


runner = CycleRunner()
runner.start(cycle_job, interval=0.2, cycles=12)
deadline = time.time() + 240
while runner.running and time.time() < deadline:
    time.sleep(0.5)
runner.stop()
time.sleep(0.5)

end_fd, end_th, end_rss = fds(), threads(), rss_now_mb()
check("12 轮都跑完了", rounds[0] == 12, f"{rounds[0]} 轮（每轮约 7 秒，含 1 秒超时）")
check("fd 不增长", end_fd <= mon_fd + 8, f"{mon_fd} -> {end_fd}")
check("线程不增长", end_th <= mon_th + 2, f"{mon_th} -> {end_th}")
check("常驻内存不持续增长（<8MB）", end_rss - mon_rss < 8,
      f"{mon_rss:.1f}MB -> {end_rss:.1f}MB（+{end_rss - mon_rss:.1f}MB）")

print()
print("7) 每轮的结果与历史文件都在写（确认上面不是「什么都没干」）")
res_file = WORK / "r.txt"
# 历史文件路径由平台配置决定（build_platforms 会给出解析后的绝对路径）
hist_file = Path(mon_platforms[0].history_file) if mon_platforms else WORK / "none.jsonl"
check("结果文件已生成", res_file.exists(), str(res_file))
check("历史文件已生成", hist_file.exists(), str(hist_file))
if hist_file.exists():
    lines = [l for l in hist_file.read_text("utf-8").splitlines() if l.strip()]
    check("历史文件按轮追加（>= 10 条）", len(lines) >= 10, f"{len(lines)} 条")
    check("历史记录是合法 JSON",
          all(json.loads(l).get("platform") for l in lines[:5]), "前 5 条可解析")

try:
    svc.runner.stop()
    server.shutdown()
except Exception:                                                  # noqa: BLE001
    pass
os.environ.pop("EO_WEB_AUTH", None)

print()
print("=" * 70)
if _failures:
    print(f"长跑稳定性测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("长跑稳定性测试全部通过")
sys.exit(0)
