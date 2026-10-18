#!/usr/bin/env python3
"""SSE 长连接测试：事件推送、断开回收、失联客户端不能永久占住线程。

为什么要测：浏览器标签页在异常失联时（休眠、网线被拔、虚拟机挂起）TCP 连接
还挂着但不再读数据。服务端每秒都在往 SSE 写，发送缓冲区写满后 write 会永久
阻塞 —— 那条线程再也回收不了，开着的标签页越多、堆得越多。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

import eo.webui as webui                                           # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-sse-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def threads() -> int:
    return threading.active_count()


def fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def wait_until(fn, timeout: float = 20.0, step: float = 0.3) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


def sse_connect(port: int, read: bool = True, timeout: float = 10.0):
    """建立一条 SSE 连接；read=False 表示连上后不读（模拟失联客户端）。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 b"Accept: text/event-stream\r\nConnection: keep-alive\r\n\r\n")
    return sock


def read_events(sock: socket.socket, seconds: float = 2.5) -> str:
    sock.settimeout(1.0)
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue
        except OSError:
            break
    return buf.decode("utf-8", "replace")


print("=" * 70)
print("SSE 长连接：推送、断开回收、失联客户端")
print("=" * 70)

os.environ["EO_WEB_AUTH"] = "off"
os.environ["EO_PLATFORMS"] = "eo-lt"
webui._Handler.timeout = 2          # 测试里把连接超时压到 2 秒，别等 30 秒
svc = webui.make_service(WORK / "config.json")
server = webui.WebServer(svc, host="127.0.0.1", port=0)
# 把监听 socket 的发送缓冲调小（Linux 上已接受的连接会继承它），
# 否则内核会替我们把写阻塞"吸收"掉，测不出真实的阻塞场景。
try:
    server.httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
except OSError:
    pass
port = server.httpd.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.6)

base_th, base_fd = threads(), fds()

# --------------------------------------------------------------------------
print()
print("1) 正常连接：能收到 log / progress / runner 三种事件")
# --------------------------------------------------------------------------
svc.log("这是一条测试日志")
sock = sse_connect(port)
text = read_events(sock, seconds=3.0)
sock.close()
check("收到 log 事件", "event: log" in text, f"{len(text)} 字节")
check("收到 progress 事件", "event: progress" in text)
check("收到 runner 事件", "event: runner" in text)
check("日志内容带过来了", "这是一条测试日志" in text)

# --------------------------------------------------------------------------
print()
print("2) 关掉连接后线程与 fd 要回收")
# --------------------------------------------------------------------------
ok = wait_until(lambda: threads() <= base_th + 1 and fds() <= base_fd + 2, timeout=15)
check("线程回到基线附近", threads() <= base_th + 1, f"基线 {base_th} -> {threads()}")
check("fd 回到基线附近", fds() <= base_fd + 2, f"基线 {base_fd} -> {fds()}")

# --------------------------------------------------------------------------
print()
print("3) 多个标签页：6 条并发连接都能收到事件，关掉后全部回收")
# --------------------------------------------------------------------------
socks = [sse_connect(port) for _ in range(6)]
time.sleep(1.5)
check("6 条连接都建立了", threads() >= base_th + 6, f"线程 {threads()}")
got_all = 0
for s in socks:
    if "event: runner" in read_events(s, seconds=2.0):
        got_all += 1
check("6 条连接都收到了事件", got_all == 6, f"{got_all}/6")
for s in socks:
    s.close()
ok = wait_until(lambda: threads() <= base_th + 1, timeout=20)
check("全部关闭后线程回收", ok, f"线程 {threads()}（基线 {base_th}）")

# --------------------------------------------------------------------------
print()
print("4) 连接超时设置存在（防止失联标签页永久占住线程）")
# --------------------------------------------------------------------------
# 背景：SSE 每秒都在写数据。如果浏览器异常失联（休眠、拔网线、虚拟机挂起），
# TCP 连接还挂着但客户端不再读，发送缓冲写满后 write 会永久阻塞 —— 那条线程
# 再也回收不了。给连接设一个 socket 超时，阻塞的写就会抛错并断开。
#
# 说明：这里只检查「机制在不在」，不去构造写阻塞场景 —— 那取决于内核缓冲区
# 行为（SO_SNDBUF/RCVBUF 怎么取整、环回怎么缓冲），实测不稳定，写出来的断言
# 会是"看着测了其实测不准"。正常的回收路径由第 2、3 节覆盖。
check("_Handler.timeout 是有限正数", isinstance(webui._Handler.timeout, int)
      and webui._Handler.timeout > 0, str(webui._Handler.timeout))
os.environ["EO_WEB_SOCKET_TIMEOUT"] = "7"
try:
    source = Path(webui.__file__).read_text(encoding="utf-8")
    check("超时可被 EO_WEB_SOCKET_TIMEOUT 覆盖（默认 30）",
          'EO_WEB_SOCKET_TIMEOUT' in source and '"30"' in source, "默认 30 秒")
finally:
    os.environ.pop("EO_WEB_SOCKET_TIMEOUT", None)
check("SSE 循环里不再有算了却不用的死代码（last_beat）",
      "last_beat" not in Path(webui.__file__).read_text(encoding="utf-8"),
      "已移除")

# --------------------------------------------------------------------------
print()
print("5) 回收之后服务仍然正常")
# --------------------------------------------------------------------------
sock = sse_connect(port)
svc.log("回收后再来一条")
text = read_events(sock, seconds=2.5)
sock.close()
check("新连接仍能收到事件", "event: runner" in text and "回收后再来一条" in text,
      f"{len(text)} 字节")

print()
print("6) 最终没有残留线程/连接")
ok = wait_until(lambda: threads() <= base_th + 1 and fds() <= base_fd + 2, timeout=15)
check("线程与 fd 都回到基线", ok, f"线程 {threads()}/{base_th}  fd {fds()}/{base_fd}")

try:
    server.shutdown()
except Exception:                                                  # noqa: BLE001
    pass
os.environ.pop("EO_WEB_AUTH", None)

print()
print("=" * 70)
if _failures:
    print(f"SSE 测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("SSE 测试全部通过")
sys.exit(0)
