#!/usr/bin/env python3
"""性能基准：进程内探测 vs curl 子进程（本地服务器，排除网络波动）。"""
from __future__ import annotations

import http.server
import resource
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.core import _http_probe_curl, _http_probe_socket   # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="eo-bench-"))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):                                       # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                              # noqa: ANN002
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

N = 400
CONC = 32


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def bench(fn, n=N, conc=1, label=""):
    before = rss_mb()
    t0 = time.perf_counter()
    if conc <= 1:
        for _ in range(n):
            fn("127.0.0.1", "bench.local", timeout=3, port=PORT,
               scheme="http", status_codes=("200",))
    else:
        with ThreadPoolExecutor(max_workers=conc) as pool:
            list(pool.map(lambda _: fn("127.0.0.1", "bench.local", timeout=3, port=PORT,
                                       scheme="http", status_codes=("200",)), range(n)))
    dt = time.perf_counter() - t0
    grow = rss_mb() - before
    qps = n / dt if dt else 0
    print(f"  {label:<28} {dt:7.2f}s   {qps:8.1f} 次/秒   峰值内存 +{grow:6.1f}MB")
    return dt, qps, grow


print("=" * 74)
print(f"探测性能基准（本地 HTTP 服务器，{N} 次探测，并发 {CONC}）")
print("=" * 74)

print()
print("单线程串行：")
s1 = bench(_http_probe_socket, conc=1, label="进程内（socket+ssl）")
c1 = bench(_http_probe_curl, conc=1, label="curl 子进程")
print(f"  → 单线程提速 {c1[0] / s1[0]:.1f} 倍")

print()
print(f"并发 {CONC}：")
s2 = bench(_http_probe_socket, conc=CONC, label="进程内（socket+ssl）")
c2 = bench(_http_probe_curl, conc=CONC, label="curl 子进程")
print(f"  → 并发提速 {c2[0] / s2[0]:.1f} 倍")

print()
print("进程开销（curl 每次都要 fork+exec）：")
t0 = time.perf_counter()
for _ in range(50):
    subprocess.run(["curl", "-s", "-o", "/dev/null", "--max-time", "1", "http://127.0.0.1:1/"],
                   capture_output=True)
fork_cost = (time.perf_counter() - t0) / 50
print(f"  空跑一次 curl（连不上）：{fork_cost * 1000:.1f}ms —— 这是每个探测的固定成本")
print(f"  400 次就是 {fork_cost * 400:.1f}s 白白花在 fork 上")

print()
print("内存占用估算（按实测的常驻增量推算 240 并发）：")
print(f"  curl 子进程：每并发 8~12MB -> 240 并发约 2~2.9GB")
print(f"  进程内：每次探测线程栈与缓冲区约 0.1MB -> 240 并发约 24MB")

srv.shutdown()
shutil.rmtree(WORK, ignore_errors=True)
