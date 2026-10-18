#!/usr/bin/env python3
"""通知 webhook 测试：用本地 HTTP 接收端验证三种格式与失败处理。

不会向任何外部地址发请求 —— 全部打到 127.0.0.1 上的临时服务。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.notify import (build_payload, notify_alert, notify_outcomes,   # noqa: E402
                       send, _business_error)

_failures: list[str] = []
_received: list[dict] = []
logs: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


class Collector(BaseHTTPRequestHandler):
    status = 200
    body = '{"code":0}'

    def do_POST(self):                                   # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        _received.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "raw": raw,
            "json": self._try_json(raw),
        })
        payload = Collector.body.encode()
        self.send_response(Collector.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _try_json(raw: str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def log_message(self, *args):                        # 静音
        pass


print("=" * 70)
print("通知 webhook 测试（本地接收端）")
print("=" * 70)

server = HTTPServer(("127.0.0.1", 0), Collector)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
print(f"\n本地接收端: {base}")

# ------------------------------------------------------------------ 1. 格式识别
print()
print("1) 按 webhook 地址识别消息格式（用真实的机器人域名）")
cases = [
    ("https://open.feishu.cn/open-apis/bot/v2/hook/abc", "飞书",
     lambda p: p.get("msg_type") == "text" and "text" in p.get("content", {})),
    ("https://open.larksuite.com/open-apis/bot/v2/hook/abc", "飞书国际版",
     lambda p: p.get("msg_type") == "text"),
    ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", "企业微信",
     lambda p: p.get("msgtype") == "text" and "content" in p.get("text", {})),
    ("https://example.com/hook", "通用 webhook",
     lambda p: "text" in p),
    ("https://api.day.app/xxxxx", "Bark",
     lambda p: "text" in p),
]
for url, label, ok_fn in cases:
    payload = build_payload(url, "测试消息")
    check(f"{label} payload 结构正确", ok_fn(payload),
          json.dumps(payload, ensure_ascii=False)[:80])

# ------------------------------------------------------------------ 2. 实际发送
print()
print("2) 实际 HTTP 发送")
_received.clear()
ok = send(f"{base}/generic", "优选完成：3 个平台", log=logs.append)
check("send 返回 True", ok is True, str(ok))
check("接收端确实收到 1 个请求", len(_received) == 1, str(len(_received)))
if _received:
    req = _received[0]
    check("Content-Type 是 JSON", "application/json" in req["content_type"],
          req["content_type"])
    check("正文包含消息内容", "优选完成" in req["raw"], req["raw"][:80])

# ------------------------------------------------------------------ 3. 失败识别
print()
print("3) 失败必须被识别出来（飞书/企业微信失败时也返回 HTTP 200）")
Collector.status = 500
ok = send(f"{base}/generic", "失败测试", log=logs.append)
check("HTTP 500 -> False", ok is False, str(ok))
Collector.status = 200

for body, label, want in (
    ('{"code":40001,"msg":"invalid webhook"}', "飞书 code!=0", False),
    ('{"code":0,"msg":"success"}', "飞书 code=0", True),
    ('{"errcode":93000,"errmsg":"invalid webhook url"}', "企业微信 errcode!=0", False),
    ('{"errcode":0,"errmsg":"ok"}', "企业微信 errcode=0", True),
    ('{"StatusCode":0}', "飞书 StatusCode=0", True),
    ('{"StatusCode":1,"message":"forbidden"}', "飞书 StatusCode!=0", False),
    ('not json at all', "非 JSON 响应（按 HTTP 状态判断）", True),
    ('{"other":"field"}', "无错误字段", True),
):
    Collector.body = body
    logs.clear()
    ok = send(f"{base}/generic", "x", log=logs.append)
    check(f"{label} -> {want}", ok is want, f"send={ok} log={(logs[0][:50] if logs else '')}")

Collector.body = '{"code":0}'

print()
print("  ≥ _business_error 单元验证")
for body, want in (('{"code":0}', ""), ('{"code":1,"msg":"bad"}', "code=1 bad"),
                   ('{"errcode":0}', ""), ('{"errcode":5,"errmsg":"err"}', "errcode=5 err"),
                   ("garbage", ""), ('[1,2]', ""), ('{"code":"x"}', "")):
    got = _business_error(body)
    check(f"_business_error({body[:24]})", got == want, f"{got!r} != {want!r}")

# ------------------------------------------------------------------ 4. 边界
print()
print("4) 边界与容错")
check("未配置 webhook 时按文档返回 True（空操作）",
      send("", "x", log=logs.append) is True)
check("None webhook 同样返回 True", send(None, "x", log=logs.append) is True)
logs.clear()
send("", "不该发出去", log=logs.append)
check("未配置时不产生任何日志（确实没发请求）", not logs, str(logs[:2]))
check("非法 URL 返回 False（不抛异常）",
      send("http://127.0.0.1:1/nope", "x", log=logs.append, timeout=2.0) is False)
check("非法 URL 有失败日志", any("失败" in l or "异常" in l for l in logs),
      (logs[0][:60] if logs else "(无)"))

print()
print("4b) 服务端接受连接但永不响应：必须在超时内放弃，不能把整轮卡住")
# webhook 被黑洞、或对方服务挂了但 TCP 还通，是最常见的"告警发不出去"形态。
import socket      # noqa: E402
import time        # noqa: E402

hang = socket.socket()
hang.bind(("127.0.0.1", 0))
hang.listen(1)
hang_port = hang.getsockname()[1]
held: list[socket.socket] = []


def _accept_and_hold() -> None:
    conn, _ = hang.accept()
    held.append(conn)          # 收下连接，什么都不回
    time.sleep(30)


threading.Thread(target=_accept_and_hold, daemon=True).start()

logs.clear()
t0 = time.time()
ok = send(f"http://127.0.0.1:{hang_port}/hook", "测试", log=logs.append, timeout=2.0)
dt = time.time() - t0
check("挂起的服务端被判为失败", ok is False, str(ok))
check("在超时附近放弃（不是一直等）", 1.5 <= dt <= 6.0, f"{dt:.1f}s")
check("日志里有失败原因", any("失败" in l or "异常" in l for l in logs),
      (logs[0][:70] if logs else "(无)"))
for conn in held:
    try:
        conn.close()
    except OSError:
        pass
hang.close()

# ------------------------------------------------------------------ 5. 业务封装
print()
print("5) 业务封装：notify_alert / notify_outcomes")
_received.clear()
ok = notify_alert(f"{base}/generic", "链路不可信", "192.0.2.1 返回了 200", log=logs.append)
check("notify_alert 成功", ok is True, str(ok))
check("  接收端收到 1 个请求", len(_received) == 1, str(len(_received)))
if _received:
    body = _received[0]["raw"]
    check("  正文含标题", "链路不可信" in body, body[:80])
    check("  正文含详情", "192.0.2.1" in body, body[:80])


class FakePlatform:
    key = "eo-lt"
    name = "EdgeOne·联通"


class FakeProbe:
    def __init__(self, ip, ms):
        self.ip, self.latency_ms = ip, ms


class FakeOutcome:
    def __init__(self, passed, total, error=""):
        self.platform = FakePlatform()
        self.passed = [FakeProbe(f"1.1.1.{i}", 10 + i) for i in range(1, passed + 1)]
        self.total = total
        self.error = error
        self.elapsed = 12.5

    def summary_line(self):
        return f"{self.platform.name}: {len(self.passed)}/{self.total}"


_received.clear()
ok = notify_outcomes(f"{base}/generic", [FakeOutcome(5, 100)], log=logs.append)
check("notify_outcomes 成功", ok is True, str(ok))
if _received:
    body = _received[0]["raw"]
    check("  汇总含平台名", "EdgeOne" in body, body[:100])
    check("  汇总含数量", "5/100" in body, body[:120])

_received.clear()
notify_outcomes(f"{base}/generic", [FakeOutcome(0, 100, error="网段文件不存在")],
                log=logs.append)
check("失败结果也会发通知", len(_received) >= 1, str(len(_received)))
if _received:
    check("  汇总里报告了错误", "网段文件不存在" in _received[0]["raw"],
          _received[0]["raw"][:120])

server.shutdown()

print()
print("=" * 70)
if _failures:
    print(f"失败 {len(_failures)} 项：")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("通知 webhook 全部测试通过")
sys.exit(0)
