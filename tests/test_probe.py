#!/usr/bin/env python3
"""探测实现测试：进程内 socket+ssl 必须与 curl 行为一致。

为什么要换实现：curl 每个探测是一个独立进程（8~12MB），240 并发就是约 2GB；
进程内实现只有几十 KB，同样并发内存差两个数量级。但换实现的风险是
行为漂移（状态码、超时、TLS、对端 IP 校验），所以这里用本地服务器逐项对照。

全部请求都打本地回环地址（127.0.0.1），不访问任何外网。
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import atexit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.core import _http_probe_curl, _http_probe_socket, http_probe   # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-probe-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------
# 本地测试服务器：可指定状态码、响应延迟、是否 TLS
# --------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    delay = 0.0
    protocol_version = "HTTP/1.1"

    def do_GET(self):                                        # noqa: N802
        if self.delay:
            time.sleep(self.delay)
        body = b"ok"
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):                            # noqa: D401, ANN002
        pass


class Server:
    def __init__(self, status: int = 200, delay: float = 0.0, tls: bool = False):
        handler = type("H", (Handler,), {"status": status, "delay": delay})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.tls = tls
        if tls:
            cert, key = make_cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:                                    # noqa: BLE001
            pass


def make_cert() -> tuple[str, str]:
    """自签证书（只用于本地测试）。"""
    cert = WORK / "cert.pem"
    key = WORK / "key.pem"
    if not cert.exists():
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "1",
             "-subj", "/CN=test.local"],
            check=True, capture_output=True)
    return str(cert), str(key)


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


print("=" * 70)
print("探测实现：进程内 socket+ssl（默认）与 curl（回退）对照")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) HTTP 200：两种实现都要判定通过")
# --------------------------------------------------------------------------
srv = Server(status=200)
try:
    for label, fn in (("socket", _http_probe_socket), ("curl", _http_probe_curl)):
        p = fn("127.0.0.1", "test.local", timeout=3,
               port=srv.port, scheme="http", status_codes=("200",))
        check(f"{label}: 200 判为可用", p.ok and p.status == "200",
              f"ok={p.ok} status={p.status} err={p.error}")
        check(f"{label}: 记录了连接耗时", p.connect_ms > 0, f"{p.connect_ms:.1f}ms")
        check(f"{label}: 对端 IP 正确", p.remote_ip == "127.0.0.1", p.remote_ip)
finally:
    srv.close()

# --------------------------------------------------------------------------
print()
print("2) HTTP 404：状态码不在白名单时判失败（但状态码要如实带回）")
# --------------------------------------------------------------------------
srv = Server(status=404)
try:
    for label, fn in (("socket", _http_probe_socket), ("curl", _http_probe_curl)):
        p = fn("127.0.0.1", "test.local", timeout=3,
               port=srv.port, scheme="http", status_codes=("200",))
        check(f"{label}: 404 判为不可用", not p.ok and p.status == "404",
              f"ok={p.ok} status={p.status}")
finally:
    srv.close()

# --------------------------------------------------------------------------
print()
print("3) 端口不通：要快速失败并给出原因")
# --------------------------------------------------------------------------
dead = closed_port()
for label, fn in (("socket", _http_probe_socket), ("curl", _http_probe_curl)):
    t0 = time.perf_counter()
    p = fn("127.0.0.1", "test.local", timeout=2, port=dead,
           scheme="http", status_codes=("200",))
    dt = time.perf_counter() - t0
    check(f"{label}: 端口不通判失败", not p.ok, f"ok={p.ok} err={p.error}")
    check(f"{label}: 有可读的错误信息", bool(p.error), p.error)
    check(f"{label}: 没有拖到超时（拒连要立刻返回）", dt < 5.0, f"{dt:.2f}s")

# --------------------------------------------------------------------------
print()
print("4) 服务器不响应：超时控制要看住")
# --------------------------------------------------------------------------
# 服务器故意拖 10 秒不响应；timeout=1 时必须 1 秒左右就放弃。
# 断言放宽到 4 秒是为了抗机器负载抖动，但仍能抓住「超时完全没生效」
# （那样会等到 10 秒服务器才回）。
srv = Server(status=200, delay=10.0)
try:
    for label, fn in (("socket", _http_probe_socket), ("curl", _http_probe_curl)):
        t0 = time.perf_counter()
        p = fn("127.0.0.1", "test.local", timeout=1, port=srv.port,
               scheme="http", status_codes=("200",))
        dt = time.perf_counter() - t0
        check(f"{label}: 超时判失败", not p.ok, f"ok={p.ok} err={p.error}")
        check(f"{label}: 在 timeout 附近放弃（不是等服务器）", dt < 4.0, f"{dt:.2f}s")
finally:
    srv.close()

# --------------------------------------------------------------------------
print()
print("5) HTTPS（自签证书）：必须不校验证书也能成功，且要带 SNI")
# --------------------------------------------------------------------------
srv = Server(status=200, tls=True)
try:
    p = _http_probe_socket("127.0.0.1", "test.local", timeout=3,
                           port=srv.port, scheme="https", status_codes=("200",))
    check("socket: 自签证书也能通过（等价 --insecure）", p.ok,
          f"ok={p.ok} err={p.error}")
    p2 = _http_probe_curl("127.0.0.1", "test.local", timeout=3,
                          port=srv.port, scheme="https", status_codes=("200",))
    check("curl: 同样通过（两种实现一致）", p2.ok, f"ok={p2.ok} err={p2.error}")
finally:
    srv.close()

# --------------------------------------------------------------------------
print()
print("6) 非 HTTP 服务：不能当成 200")
# --------------------------------------------------------------------------
raw = socket.socket()
raw.bind(("127.0.0.1", 0))
raw.listen(5)
garbage_port = raw.getsockname()[1]


def _serve_garbage():
    try:
        conn, _ = raw.accept()
        conn.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
        conn.close()
    except OSError:
        pass


threading.Thread(target=_serve_garbage, daemon=True).start()
try:
    p = _http_probe_socket("127.0.0.1", "test.local", timeout=2,
                           port=garbage_port, scheme="http", status_codes=("200",))
    check("socket: 非 HTTP 响应判失败", not p.ok, f"ok={p.ok} err={p.error}")
    check("socket: 错误里带上了原始内容", "非 HTTP" in p.error, p.error)
finally:
    raw.close()

# --------------------------------------------------------------------------
print()
print("7) http_probe 分发：EO_PROBE 决定用哪个实现")
# --------------------------------------------------------------------------
srv = Server(status=200)
try:
    os.environ.pop("EO_PROBE", None)
    p = http_probe("127.0.0.1", "test.local", timeout=3, port=srv.port,
                   scheme="http", status_codes=("200",))
    check("默认走进程内实现（能成功）", p.ok, f"ok={p.ok} err={p.error}")
    os.environ["EO_PROBE"] = "curl"
    p = http_probe("127.0.0.1", "test.local", timeout=3, port=srv.port,
                   scheme="http", status_codes=("200",))
    check("EO_PROBE=curl 切回 curl 实现（同样成功）", p.ok, f"ok={p.ok} err={p.error}")
finally:
    os.environ.pop("EO_PROBE", None)
    srv.close()

# --------------------------------------------------------------------------
print()
print("8) 并发安全性：50 个线程同时探测不能串味")
# --------------------------------------------------------------------------
srv = Server(status=200)
try:
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        p = _http_probe_socket("127.0.0.1", "test.local", timeout=5,
                               port=srv.port, scheme="http", status_codes=("200",))
        with lock:
            results.append((i, p.ok))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("50 个并发全部成功", len(results) == 50 and all(ok for _, ok in results),
          f"{sum(1 for _, ok in results if ok)}/50 成功")
finally:
    srv.close()

print()
print("=" * 70)
if _failures:
    print(f"探测实现测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("探测实现测试全部通过")
sys.exit(0)
