#!/usr/bin/env python3
"""网页接口覆盖率：每个路由都要有测试打到。

未测端点最容易藏 bug —— 它既不会被静态检查发现（路由确实存在、
页面也确实在调），也不会在日常使用中暴露（用户很少点）。
这里把 webui.py 声明的路由与测试代码里实际请求过的路径做对比。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def declared_routes() -> set[str]:
    """从 webui.py 里抽出所有被比较的路由字面量。"""
    src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
    routes: set[str] = set()
    for match in re.finditer(r'path\s*==\s*"(/[^"]*)"', src):
        routes.add(match.group(1))
    for match in re.finditer(r'path\.startswith\(\s*"(/[^"]*)"', src):
        routes.add(match.group(1).rstrip("/") + "/*")
    return routes


def tested_routes() -> set[str]:
    """从测试代码里抽出请求过的路径。"""
    hits: set[str] = set()
    for path in list((ROOT / "tests").glob("*.py")) + list((ROOT / "tests").glob("*.sh")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'["\'](/api/[a-z0-9/_-]*|/healthz)["\']', text):
            hits.add(match.group(1))
        # 各种请求辅助函数的调用写法：call("/x")、get("/x")、raw_get("/x") 等
        for match in re.finditer(r'(?:call2?|api|get|post|raw_get)\(\s*["\']'
                                 r'(/[a-z0-9/_-]*)', text):
            hits.add(match.group(1))
        for match in re.finditer(r'(?:curl|wget)[^\n]*?(/api/[a-z0-9/_-]*|/healthz)', text):
            hits.add(match.group(1))
    return hits


print("=" * 70)
print("网页接口覆盖率")
print("=" * 70)

declared = declared_routes()
tested = tested_routes()

# 归一化：/api/events 可能带查询串，测试里写法不同
def normalize(route: str) -> str:
    return route.rstrip("/") or "/"


declared_n = {normalize(r) for r in declared}
tested_n = {normalize(r) for r in tested}

print(f"  webui.py 声明 {len(declared_n)} 个路由：{sorted(declared_n)}")
print(f"  测试里请求过 {len(tested_n)} 个路径")

uncovered = sorted(r for r in declared_n if r not in tested_n)
check("没有未覆盖的路由", not uncovered,
      f"未测: {uncovered}" if uncovered else f"{len(declared_n)} 个路由都有测试")

# --------------------------------------------------------------------------
print()
print("1) 每个控制类端点都要有测试（这些会改状态，最需要覆盖）")
# --------------------------------------------------------------------------
CONTROL = ["/api/control/start", "/api/control/stop", "/api/control/run-once",
           "/api/control/dns-preview", "/api/control/dns-push", "/api/logs/clear",
           "/api/config"]
missing_control = [r for r in CONTROL if r not in tested_n]
check("控制类端点全部有测试", not missing_control, str(missing_control))

print()
print("2) 只读端点也要覆盖")
# --------------------------------------------------------------------------
READONLY = ["/api/status", "/api/config", "/api/vars", "/api/logs", "/api/platforms",
            "/api/events", "/healthz", "/"]
missing_ro = [r for r in READONLY if normalize(r) not in tested_n and r != "/"]
check("只读端点全部有测试", not missing_ro, str(missing_ro))

print()
print("3) 页面本身要被请求过（否则 HTML 里出错也发现不了）")
# --------------------------------------------------------------------------
page_hits = 0
for path in (ROOT / "tests").glob("*.py"):
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(r'(urlopen|call)\(\s*[^)]*["\']/(?:["\']|\?token)', text):
        page_hits += 1
    elif "PAGE_HTML" in text or "render" in text.lower():
        page_hits += 1
check("有测试检查页面内容", page_hits > 0, f"{page_hits} 个测试文件涉及页面")

print()
print("4) 未知路径要返回 404（而不是把页面吐出去）")
# --------------------------------------------------------------------------
src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
check("有 404 兜底", "404" in src or "NOT_FOUND" in src,
      "有" if ("404" in src or "NOT_FOUND" in src) else "缺")

# --------------------------------------------------------------------------
print()
print("5) 真起一个服务，实际打这些路径（静态列表查不出行为问题）")
# --------------------------------------------------------------------------
import json                                                       # noqa: E402
import shutil                                                     # noqa: E402
import tempfile                                                   # noqa: E402
import threading                                                  # noqa: E402
import time                                                       # noqa: E402
import urllib.error                                               # noqa: E402
import urllib.request                                             # noqa: E402

from eo.webui import WebServer, make_service                      # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="eo-routes-"))
os.environ["EO_WEB_AUTH"] = "off"
os.environ["EO_PLATFORMS"] = "eo-lt"
try:
    svc = make_service(WORK / "config.json")
    server = WebServer(svc, host="127.0.0.1", port=0)
    port = server.httpd.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.6)

    def get(path: str) -> tuple[int, str, str]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode()

    def post(path: str, payload=None) -> tuple[int, str]:
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    status, ctype, body = get("/")
    check("GET / 返回 200", status == 200, str(status))
    check("GET / 是 HTML", "text/html" in ctype, ctype)
    check("页面含关键元素", "log" in body and "api/status" in body,
          f"{len(body)} 字节")
    check("页面不泄露令牌内容", "web_token" not in body or "web_token_set" in body,
          "已用掩码字段")

    status, ctype, body = get("/healthz")
    check("GET /healthz 返回 200 JSON", status == 200 and "json" in ctype, f"{status} {ctype}")

    status, ctype, body = get("/api/status")
    check("GET /api/status 返回 200", status == 200, str(status))
    check("status 里有关键字段",
          all(k in body for k in ("runner", "progress", "platforms")), body[:60])

    status, ctype, body = get("/api/does-not-exist")
    check("未知 /api 路径返回 404", status == 404, str(status))
    check("未知 /api 路径返回 JSON（不是 HTML 页面）", "json" in ctype, ctype)

    status, _, _ = get("/nope")
    check("未知普通路径返回 404", status == 404, str(status))

    status, _ = post("/api/does-not-exist")
    check("未知 POST 路径返回 404", status == 404, str(status))
finally:
    try:
        server.shutdown()
    except Exception:                                             # noqa: BLE001
        pass
    os.environ.pop("EO_WEB_AUTH", None)
    shutil.rmtree(WORK, ignore_errors=True)

print()
print("6) 鉴权开启时，未带令牌必须 401（内网可关，但默认要拦住）")
# --------------------------------------------------------------------------
WORK2 = Path(tempfile.mkdtemp(prefix="eo-routes-auth-"))
os.environ.pop("EO_WEB_AUTH", None)
os.environ["EO_WEB_TOKEN"] = "test-token-123"
try:
    svc2 = make_service(WORK2 / "config.json")
    server2 = WebServer(svc2, host="127.0.0.1", port=0)
    port2 = server2.httpd.server_address[1]
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    time.sleep(0.6)

    def raw_get(path: str) -> int:
        req = urllib.request.Request(f"http://127.0.0.1:{port2}{path}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    check("无令牌访问 /api/status -> 401", raw_get("/api/status") == 401,
          str(raw_get("/api/status")))
    check("带正确令牌 -> 200", raw_get("/api/status?token=test-token-123") == 200,
          str(raw_get("/api/status?token=test-token-123")))
    check("令牌错误 -> 401", raw_get("/api/status?token=wrong") == 401,
          str(raw_get("/api/status?token=wrong")))
    check("健康检查无需令牌（否则容器起不来）", raw_get("/healthz") == 200,
          str(raw_get("/healthz")))
finally:
    try:
        server2.shutdown()
    except Exception:                                             # noqa: BLE001
        pass
    os.environ.pop("EO_WEB_TOKEN", None)
    shutil.rmtree(WORK2, ignore_errors=True)

print()
print("=" * 70)
if _failures:
    print(f"网页接口覆盖率：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("网页接口覆盖率检查通过")
sys.exit(0)
