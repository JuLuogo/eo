#!/usr/bin/env python3
"""Cloudflare DNS 分支测试（全部使用假 HTTP 层，不会发出任何真实请求）。

为什么单独测这个分支：它和华为云分支是完全独立的两套代码 ——
华为是「一个记录集装多个 IP」，Cloudflare 是「一个 IP 一条 A 记录」，
删改顺序、分页、去重的逻辑都不一样，华为分支的测试覆盖不到这里。

重点验证「宁可少改也不能把解析清空」：
  新建失败时绝不能删旧记录 / 分页漏读会产生重复解析 /
  重复 IP 会变成多条同名 A 记录 / 不能开启代理（proxied）否则优选失效。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo import dns_cloudflare as cf                                    # noqa: E402
from eo.dns import DnsTarget, inspect, sync_records                     # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


print("=" * 70)
print("Cloudflare DNS 分支测试（假 HTTP 层，不碰线上）")
print("=" * 70)


# --------------------------------------------------------------------------
# 假 Cloudflare API：一个有状态的区域，支持分页与故障注入
# --------------------------------------------------------------------------
class FakeCloudflare:
    """模拟 Cloudflare v4 API。records 是 {content: record_id} 的有序映射。"""

    def __init__(self, records=None, zone_exists=True, page_size=100,
                 fail_create_for=(), fail_delete_for=(), total_pages_hint=None):
        self.records: dict[str, str] = dict(records or {})
        self.zone_exists = zone_exists
        self.page_size = page_size
        self.fail_create_for = set(fail_create_for)
        self.fail_delete_for = set(fail_delete_for)
        self.total_pages_hint = total_pages_hint
        self.calls: list[tuple[str, str]] = []          # (method, path)
        self.bodies: list[dict] = []                    # 所有请求体（GET 为空 dict）
        self.writes: list[tuple[str, dict]] = []        # 只有写操作（POST/PUT/DELETE）
        self._next_id = 1000

    # -- 供测试统计 ------------------------------------------------------
    @property
    def created(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for m, p in self.calls if m == "POST"]

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    # -- 假的 HTTP 层 ----------------------------------------------------
    def request(self, method, path, token, payload=None, timeout=20.0):
        self.calls.append((method, path))
        self.bodies.append(payload or {})
        if method in ("POST", "PUT", "DELETE") and payload is not None:
            self.writes.append((method, payload))
        elif method == "POST":
            self.writes.append((method, {}))
        if token != "cf-token":
            raise cf.CloudflareError("HTTP 403: Invalid token")

        if path.startswith("/zones?"):
            result = [{"id": "zone-1", "name": "example.com"}] if self.zone_exists else []
            return {"success": True, "result": result}

        # /zones/<id>/dns_records...
        if method == "GET":
            query = path.split("?", 1)[1] if "?" in path else ""
            params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
            if params.get("type") not in (None, "A"):
                return {"success": True, "result": [], "result_info": {"total_pages": 1}}
            if params.get("name") and params["name"] != "eo.1.060730.xyz":
                return {"success": True, "result": [], "result_info": {"total_pages": 1}}
            page = int(params.get("page", 1))
            per_page = int(params.get("per_page", self.page_size))
            items = list(self.records.items())
            total = len(items)
            start = (page - 1) * per_page
            chunk = items[start:start + per_page]
            total_pages = self.total_pages_hint or max(1, -(-total // per_page))
            return {
                "success": True,
                "result": [{"id": rid, "type": "A", "name": "eo.1.060730.xyz",
                            "content": content, "ttl": 1, "proxied": False}
                           for content, rid in chunk],
                "result_info": {"page": page, "per_page": per_page, "total_pages": total_pages},
            }

        if method == "POST":
            content = payload["content"]
            if content in self.fail_create_for:
                raise cf.CloudflareError(f"HTTP 400: record already exists ({content})")
            self._next_id += 1
            self.records[content] = f"rec-{self._next_id}"
            return {"success": True, "result": {"id": self.records[content]}}

        if method == "PUT":
            rid = path.rsplit("/", 1)[-1]
            self.records[payload["content"]] = rid
            return {"success": True, "result": {"id": rid}}

        if method == "DELETE":
            rid = path.rsplit("/", 1)[-1]
            for content, existing in list(self.records.items()):
                if existing == rid:
                    if content in self.fail_delete_for:
                        raise cf.CloudflareError(f"HTTP 400: cannot delete {content}")
                    del self.records[content]
                    return {"success": True, "result": {"id": rid}}
            raise cf.CloudflareError("HTTP 404: record not found")

        raise cf.CloudflareError(f"未预期的请求 {method} {path}")


def install_fake(api: FakeCloudflare):
    """把 cf._request 换成假实现；返回还原函数。"""
    original = cf._request
    cf._request = api.request
    return lambda: setattr(cf, "_request", original)


def target(**over) -> DnsTarget:
    base = dict(zone_name="example.com", record_name="eo.1.060730.xyz",
                provider="cloudflare", api_token="cf-token", ttl=1)
    base.update(over)
    return DnsTarget(**base)


# --------------------------------------------------------------------------
print()
print("1) _request 的 HTTP / 业务错误处理")
# --------------------------------------------------------------------------
class FakeHTTP:
    def __init__(self, body, code=200):
        self._body = body
        self.code = code

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


original_urlopen = urllib.request.urlopen


def stub_urlopen(exc=None, body=None):
    def fake(req, timeout=None):
        if exc is not None:
            raise exc
        return FakeHTTP(json.dumps(body).encode())
    urllib.request.urlopen = fake


try:
    stub_urlopen(body={"success": True, "result": [{"id": "z1"}]})
    body = cf._request("GET", "/zones?name=x", "tok")
    check("成功响应正常解析", body["result"][0]["id"] == "z1", str(body))

    stub_urlopen(body={"success": False, "errors": [{"message": "Invalid API Token"}]})
    try:
        cf._request("GET", "/zones", "tok")
        check("success=false 必须抛 CloudflareError", False, "没有抛异常")
    except cf.CloudflareError as exc:
        check("success=false 必须抛 CloudflareError", "Invalid API Token" in str(exc), str(exc))

    err = urllib.error.HTTPError("https://api.cloudflare.com/x", 403, "Forbidden", None, None)
    err.read = lambda: b'{"success":false}'                       # type: ignore[method-assign]
    stub_urlopen(exc=err)
    try:
        cf._request("GET", "/zones", "tok")
        check("HTTPError 转成 CloudflareError", False, "没有抛异常")
    except cf.CloudflareError as exc:
        check("HTTPError 转成 CloudflareError（含状态码）", "403" in str(exc), str(exc))

    stub_urlopen(exc=urllib.error.URLError("connection refused"))
    try:
        cf._request("GET", "/zones", "tok")
        check("URLError 转成 CloudflareError", False, "没有抛异常")
    except cf.CloudflareError as exc:
        check("URLError 转成 CloudflareError（含网络错误）", "网络错误" in str(exc), str(exc))

    # 请求头必须带 Bearer token
    captured = {}

    def capture(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeHTTP(json.dumps({"success": True, "result": []}).encode())

    urllib.request.urlopen = capture
    cf._request("GET", "/zones", "my-token", timeout=7.5)
    auth = {k.lower(): v for k, v in captured["headers"].items()}.get("authorization", "")
    check("请求头带 Bearer token", auth == "Bearer my-token", auth)
    check("超时参数透传", captured["timeout"] == 7.5, str(captured["timeout"]))
finally:
    urllib.request.urlopen = original_urlopen

# --------------------------------------------------------------------------
print()
print("2) get_zone_id")
# --------------------------------------------------------------------------
api = FakeCloudflare()
restore = install_fake(api)
try:
    check("能查到 zone id", cf.get_zone_id("cf-token", "example.com") == "zone-1")
    missing = FakeCloudflare(zone_exists=False)
    restore()
    restore = install_fake(missing)
    try:
        cf.get_zone_id("cf-token", "nope.com")
        check("zone 不存在时报错", False, "没有抛异常")
    except cf.CloudflareError as exc:
        check("zone 不存在时报错", "未找到 zone" in str(exc), str(exc))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("3) replace：新增缺失 / 保留已存在 / 删除多余")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={"1.1.1.1": "rec-1", "2.2.2.2": "rec-2"})
restore = install_fake(api)
try:
    created, deleted = cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                                  ["2.2.2.2", "3.3.3.3"], zone_id="zone-1")
    check("只新建缺失的那条", created == 1 and "3.3.3.3" in api.records, str(api.records))
    check("只删除多余的那条", deleted == 1 and "1.1.1.1" not in api.records, str(api.records))
    check("已存在的记录未被重写（不产生无谓改动）",
          api.count("PUT") == 0, f"PUT={api.count('PUT')}")
    check("最终解析值与预期一致",
          sorted(api.records) == ["2.2.2.2", "3.3.3.3"], str(sorted(api.records)))

    # 顺序：必须先建后删，否则中间有一段时间解析是空的
    order = [m for m, _ in api.calls if m in ("POST", "DELETE")]
    check("先新建后删除（避免解析空窗）",
          order.index("POST") < order.index("DELETE"), str(order))

    # 内容完全一致时不应有任何写操作
    api2 = FakeCloudflare(records={"1.1.1.1": "rec-1", "2.2.2.2": "rec-2"})
    restore()
    restore = install_fake(api2)
    cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
               ["1.1.1.1", "2.2.2.2"], zone_id="zone-1")
    check("内容一致时零写操作",
          api2.count("POST") == 0 and api2.count("DELETE") == 0 and api2.count("PUT") == 0,
          f"POST={api2.count('POST')} DELETE={api2.count('DELETE')} PUT={api2.count('PUT')}")
    # 代理必须关闭：proxied=True 时返回的是 Cloudflare 的 IP，优选出来的边缘 IP 白测了
    check("proxied 必须为 False（否则优选出来的 IP 会被代理覆盖）",
          bool(api.writes) and all(b.get("proxied") is False for m, b in api.writes if m in ("POST", "PUT")),
          str([b.get("proxied") for m, b in api.writes]))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("4) replace：新建失败时绝不能删旧记录（失手就会清空解析）")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={"1.1.1.1": "rec-1", "2.2.2.2": "rec-2"},
                     fail_create_for={"3.3.3.3", "4.4.4.4"})
restore = install_fake(api)
try:
    try:
        cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                   ["3.3.3.3", "4.4.4.4"], zone_id="zone-1")
        raised = False
    except cf.CloudflareError as exc:
        raised = True
        check("新建失败必须上报错误", True, str(exc)[:70])
    if not raised:
        check("新建失败必须上报错误", False, "静默返回了")
    check("新建全失败时旧解析必须原样保留",
          sorted(api.records) == ["1.1.1.1", "2.2.2.2"], str(sorted(api.records)))
    check("新建失败时不得执行删除", api.count("DELETE") == 0, f"DELETE={api.count('DELETE')}")

    # 部分成功也要保留旧记录（宁可多几条，也不能清空）
    api3 = FakeCloudflare(records={"1.1.1.1": "rec-1"}, fail_create_for={"4.4.4.4"})
    restore()
    restore = install_fake(api3)
    try:
        cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                   ["3.3.3.3", "4.4.4.4"], zone_id="zone-1")
    except cf.CloudflareError:
        pass
    check("部分新建失败时旧解析仍保留",
          "1.1.1.1" in api3.records, str(sorted(api3.records)))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("5) replace：分页（单页 100 条，漏翻页会产生重复解析）")
# --------------------------------------------------------------------------
many = {f"10.0.{i // 250}.{i % 250}": f"rec-{i}" for i in range(250)}
api = FakeCloudflare(records=many, page_size=100)
restore = install_fake(api)
try:
    seen = cf.list_records("cf-token", "zone-1", "eo.1.060730.xyz")
    check("列记录能翻页取全 250 条", len(seen) == 250, f"取到 {len(seen)} 条")
    check("确实发生了翻页", api.count("GET") >= 3, f"GET={api.count('GET')}")

    # 只把最后一个 IP 换掉：若不翻页会看不到它 → 不删旧的、反而新建同名记录
    wanted = sorted(many)[:-1] + ["203.0.113.9"]
    api2 = FakeCloudflare(records=many, page_size=100)
    restore()
    restore = install_fake(api2)
    created, deleted = cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                                 wanted, zone_id="zone-1")
    check("大记录数下仍是 1 建 1 删（无重复解析）",
          (created, deleted) == (1, 1), f"created={created} deleted={deleted}")
    check("最终记录数没有膨胀", len(api2.records) == 250, str(len(api2.records)))
    check("被替换掉的旧 IP 已删除",
          sorted(many)[-1] not in api2.records, sorted(many)[-1])
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("6) replace：重复 IP 不得产生多条同名 A 记录")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={})
restore = install_fake(api)
try:
    created, deleted = cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                                  ["3.3.3.3", "3.3.3.3", "", "  ", "4.4.4.4"],
                                  zone_id="zone-1")
    check("重复/空 IP 被去重",
          sorted(api.records) == ["3.3.3.3", "4.4.4.4"], str(sorted(api.records)))
    check("新建数量等于去重后的数量", created == 2, f"created={created}")
    check("空字符串不会被下发", "" not in api.records and "  " not in api.records)
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("7) replace：删除失败要上报（不能静默留脏记录）")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={"1.1.1.1": "rec-1"}, fail_delete_for={"1.1.1.1"})
restore = install_fake(api)
try:
    try:
        cf.replace("cf-token", "example.com", "eo.1.060730.xyz",
                   ["3.3.3.3"], zone_id="zone-1")
        check("删除失败必须上报", False, "静默返回了")
    except cf.CloudflareError as exc:
        check("删除失败必须上报", "删除" in str(exc) or "delete" in str(exc).lower(), str(exc)[:70])
    check("新建的记录仍然生效", "3.3.3.3" in api.records, str(sorted(api.records)))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("8) sync_records 的 provider 分支（走公共入口）")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={"1.1.1.1": "rec-1"})
restore = install_fake(api)
try:
    # dry-run 绝不能发请求
    result = sync_records(target(), ["2.2.2.2"], dry_run=True)
    check("provider=cloudflare 的 dry-run 成功", result.ok, result.summary())
    check("dry-run 零 API 调用", api.calls == [], str(api.calls))
    check("dry-run 报告将要写入的 IP 数", result.total_ips == 1, str(result.total_ips))

    result = sync_records(target(), ["2.2.2.2"])
    check("真实下发成功", result.ok, result.summary())
    check("确实调用过 Cloudflare（不是华为分支）",
          any("/dns_records" in p for _, p in api.calls), str(api.calls[:2]))
    check("下发后解析只剩新 IP", sorted(api.records) == ["2.2.2.2"], str(sorted(api.records)))

    # 缺少 token：必须明确报错且不发请求
    api.calls.clear()
    result = sync_records(target(api_token=""), ["2.2.2.2"])
    check("缺 token 时失败", not result.ok, result.summary())
    check("缺 token 时提示明确", any("Token" in e for e in result.errors), str(result.errors))
    check("缺 token 时不发任何请求", api.calls == [], str(api.calls))

    # 骤降保护同样要在 Cloudflare 分支生效
    api.records = {f"10.1.0.{i}": f"rec-{i}" for i in range(40)}
    result = sync_records(target(), ["203.0.113.1"])
    check("骤降保护在 Cloudflare 分支生效（40 -> 1）", not result.ok, result.summary())
    check("骤降时未删除任何记录", len(api.records) == 40, str(len(api.records)))
    result = sync_records(target(), ["203.0.113.1"], force=True)
    check("force=True 时允许骤降", result.ok, result.summary())

    # 查询异常不能穿透
    def boom(*a, **kw):
        raise RuntimeError("SSL handshake failed")
    original_req = cf._request
    cf._request = boom
    result = sync_records(target(), ["2.2.2.2"])
    cf._request = original_req
    check("查询异常被兜住（不穿透）", not result.ok and result.errors, result.summary())

    # 不支持的服务商
    result = sync_records(target(provider="dnspod"), ["2.2.2.2"])
    check("未支持的服务商明确报错",
          not result.ok and any("dnspod" in e for e in result.errors), str(result.errors))

    # IP 为空
    api.calls.clear()
    result = sync_records(target(), [])
    check("IP 列表为空时拒绝下发", not result.ok and api.calls == [], str(result.errors))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("9) inspect 只读检查")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={"1.1.1.1": "rec-1", "2.2.2.2": "rec-2"})
restore = install_fake(api)
try:
    result = inspect(target())
    check("inspect 成功", result.ok, result.summary())
    check("inspect 列出全部解析值",
          sorted(result.deleted) == ["1.1.1.1", "2.2.2.2"], str(sorted(result.deleted)))
    check("inspect 不产生任何写操作",
          api.count("POST") == 0 and api.count("PUT") == 0 and api.count("DELETE") == 0,
          f"POST={api.count('POST')} PUT={api.count('PUT')} DELETE={api.count('DELETE')}")

    result = inspect(target(api_token=""))
    check("inspect 缺 token 时报错", not result.ok and "Token" in result.errors[0], str(result.errors))

    result = inspect(target(zone_name="", record_name=""))
    check("inspect 参数缺失时报错", not result.ok, str(result.errors))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("10) TTL 透传（Cloudflare 免费版 1 = Auto）")
# --------------------------------------------------------------------------
api = FakeCloudflare(records={})
restore = install_fake(api)
try:
    cf.replace("cf-token", "example.com", "eo.1.060730.xyz", ["3.3.3.3"],
               ttl=1, zone_id="zone-1")
    check("ttl=1 原样下发", api.writes[-1][1]["ttl"] == 1, str(api.writes[-1][1]))
    cf.replace("cf-token", "example.com", "eo.1.060730.xyz", ["4.4.4.4"],
               ttl=300, zone_id="zone-1")
    check("ttl=300 原样下发", api.writes[-1][1]["ttl"] == 300, str(api.writes[-1][1]))
    cf.create_record("cf-token", "zone-1", "x.example.com", "5.5.5.5", ttl=0)
    check("ttl=0 被兜底成 1（避免 API 报错）", api.writes[-1][1]["ttl"] == 1, str(api.writes[-1][1]))
    check("记录类型为 A", api.writes[-1][1]["type"] == "A", str(api.writes[-1][1]))
    check("create_record 默认不开启代理", api.writes[-1][1]["proxied"] is False,
          str(api.writes[-1][1]))
finally:
    restore()

# --------------------------------------------------------------------------
print()
print("=" * 70)
if _failures:
    print(f"Cloudflare 分支测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("Cloudflare 分支测试全部通过")
sys.exit(0)
