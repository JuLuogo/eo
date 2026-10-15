#!/usr/bin/env python3
"""DNS 下发逻辑深度测试（全部使用假客户端，不会碰任何线上解析）。

这是全项目风险最高的路径 —— 写错就会把线上解析清空，所以逐条验证：
  批量拆分 / 就地更新优先（无空窗期）/ 删除多余记录集 / 操作顺序 /
  解析量骤降保护阈值 / 参数校验 / dry-run 不调用 API / 失败时不继续删除 /
  记录集分页 / 名称必须精确匹配（避免改错记录）/ 记录集不足时新建
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.dns import DnsTarget, _shrunk_too_much, inspect, sync_records   # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------
# 假客户端：记录所有调用，可注入故障、可分页
# --------------------------------------------------------------------------
class FakeLinks:
    def __init__(self, nxt=None):
        self.next = nxt


class FakeSet:
    def __init__(self, i, name, records, typ="A"):
        self.id, self.name, self.records, self.type = i, name, records, typ


class FakeResp:
    def __init__(self, sets, nxt=None):
        self.recordsets, self.links = sets, FakeLinks(nxt)


class FakeZone:
    def __init__(self, zones):
        self.zones = zones


class FakeClient:
    """记录调用序列；支持分页、故障注入、返回不同数量的已有记录集。"""

    def __init__(self, existing_sets=None, zones=None, page_size=None,
                 fail_update=False, fail_create=False, fail_delete=False,
                 zone_missing=False):
        self.existing = list(existing_sets or [])
        self.zones = zones if zones is not None else [type("Z", (), {"id": "zone-1"})()]
        self.page_size = page_size
        self.calls: list[str] = []
        self.updates: list[tuple[str, list[str]]] = []
        self.creates: list[list[str]] = []
        self.deletes: list[str] = []
        self.fail_update = fail_update
        self.fail_create = fail_create
        self.fail_delete = fail_delete
        self.zone_missing = zone_missing

    def list_public_zones(self, request):
        self.calls.append("list_zones")
        return FakeZone([] if self.zone_missing else self.zones)

    def list_record_sets(self, request):
        self.calls.append("list_records")
        if not self.page_size:
            return FakeResp(self.existing)
        marker = getattr(request, "marker", None)
        start = 0
        if marker:
            start = int(marker)
        chunk = self.existing[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(self.existing) else None
        return FakeResp(chunk, nxt)

    def update_record_set(self, request):
        self.calls.append("update")
        if self.fail_update:
            raise RuntimeError("模拟更新失败")
        self.updates.append((request.recordset_id, list(request.body.records)))

    def create_record_set_with_line(self, request):
        self.calls.append("create")
        if self.fail_create:
            raise RuntimeError("模拟新建失败")
        self.creates.append(list(request.body.records))

    def delete_record_set(self, request):
        self.calls.append("delete")
        if self.fail_delete:
            raise RuntimeError("模拟删除失败")
        self.deletes.append(request.recordset_id)


def sets_of(n: int, ips_per_set: int, name="t.1.060730.xyz.", start=1):
    """造 n 个记录集，每个含 ips_per_set 个旧 IP。"""
    out = []
    for i in range(n):
        ips = [f"9.9.{i}.{j}" for j in range(1, ips_per_set + 1)]
        out.append(FakeSet(f"rs{i+start}", name, ips))
    return out


def mk_target(**kw) -> DnsTarget:
    base = dict(zone_name="1.060730.xyz", record_name="t.1.060730.xyz",
                record_type="A", ttl=1, batch_size=50, provider="huawei")
    base.update(kw)
    return DnsTarget(**base)


print("=" * 70)
print("DNS 下发逻辑深度测试（假客户端，不碰线上）")
print("=" * 70)

# ------------------------------------------------------------------ 1. 批量拆分
print()
print("1) 批量拆分")
for count, batch, want_batches in ((140, 50, 3), (50, 50, 1), (51, 50, 2), (1, 50, 1),
                                   (140, 30, 5), (50, 25, 2)):
    c = FakeClient()                       # 无已有记录 -> 全部新建
    t = mk_target(batch_size=batch)
    r = sync_records(t, [f"10.0.0.{i}" for i in range(1, count + 1)],
                     log=lambda _: None, client=c)
    total = sum(len(x) for x in c.creates)
    check(f"{count} 个 IP / 每批 {batch} -> {want_batches} 批",
          r.ok and len(c.creates) == want_batches and total == count and r.total_ips == count,
          f"实际 {len(c.creates)} 批，写入 {total}，ok={r.ok}")
    check(f"  每批不超过 batch_size",
          all(len(x) <= batch for x in c.creates), str([len(x) for x in c.creates]))

# ------------------------------------------------------------------ 2. 就地更新优先
print()
print("2) 就地更新优先（避免解析空窗期）")
c = FakeClient(existing_sets=sets_of(2, 50))
t = mk_target()
ips = [f"10.0.0.{i}" for i in range(1, 141)]      # 3 批
r = sync_records(t, ips, log=lambda _: None, client=c)
check("2 个已有记录集 -> 更新 2 个", len(c.updates) == 2 and r.updated_batches == 2,
      f"updated={len(c.updates)}")
check("不足的部分新建 1 个", len(c.creates) == 1 and r.created_batches == 1,
      f"created={len(c.creates)}")
check("没有删除（记录集数量正好够用）", not c.deletes, str(c.deletes))
check("更新的是已有记录集 id",
      [u[0] for u in c.updates] == ["rs1", "rs2"], str([u[0] for u in c.updates]))
check("调用顺序：先 update 后 create",
      c.calls.index("update") < c.calls.index("create"), str(c.calls))
check("结果 ok 且总数正确", r.ok and r.total_ips == 140, r.summary())

# ------------------------------------------------------------------ 3. 删除多余
print()
print("3) 删除多余记录集")
c = FakeClient(existing_sets=sets_of(5, 10))       # 5 个 × 10 = 50 个旧 IP
t = mk_target()
ips = [f"10.0.0.{i}" for i in range(1, 61)]        # 2 批
r = sync_records(t, ips, log=lambda _: None, client=c)
check("更新 2 个 + 删除 3 个",
      len(c.updates) == 2 and len(c.deletes) == 3,
      f"updates={len(c.updates)} deletes={len(c.deletes)}")
check("删除的是多余的那几个", c.deletes == ["rs3", "rs4", "rs5"], str(c.deletes))
check("顺序：先更新、再删除（全程有解析）",
      all(c.calls.index("update") < i for i, x in enumerate(c.calls) if x == "delete"),
      str(c.calls))
check("ok=True", r.ok, r.summary())

# ------------------------------------------------------------------ 4. 骤降保护
print()
print("4) 解析量骤降保护（防止网段写错把线上砍掉大半）")
# 阈值规则（见 _shrunk_too_much 文档）：>=100 条保留 1/4；>3 条保留 1/3；
# <=3 条保留 1/2；下限至少 1 条。
cases = [
    (200, 50, True,  "200 -> 50（恰好 1/4）允许"),
    (200, 49, False, "200 -> 49（低于 1/4）拒绝"),
    (100, 25, True,  "100 -> 25（1/4）允许"),
    (100, 24, False, "100 -> 24 拒绝"),
    (10, 3, True,    "10 -> 3（1/3=3.33 取整 3）允许"),
    (10, 2, False,   "10 -> 2 拒绝"),
    (6, 2, True,     "6 -> 2（1/3=2）允许"),
    (6, 1, False,    "6 -> 1 拒绝"),
    (4, 1, True,     "4 -> 1（4 属 1/3 档，floor 后下限 1）允许"),
    (3, 1, True,     "3 -> 1（1/2=1.5 取整 1）允许"),
    (2, 1, True,     "2 -> 1（保留一半）允许"),
    (1, 1, True,     "1 -> 1 允许"),
]
for existing, new, allowed, desc in cases:
    reason = _shrunk_too_much(existing, new, force=False)
    got = (reason == "")
    check(desc, got == allowed, f"reason={reason[:40] if reason else '(允许)'}")

check("--force 可越过保护", _shrunk_too_much(100, 1, force=True) == "")
check("现有 0 条时不拦截（首次下发）", _shrunk_too_much(0, 1, force=False) == "")

# 端到端：骤降时不得产生任何写操作
c = FakeClient(existing_sets=sets_of(10, 10))      # 100 个旧 IP
t = mk_target()
r = sync_records(t, ["10.0.0.1"], log=lambda _: None, client=c)
check("骤降被拒时没有任何写操作",
      not c.updates and not c.creates and not c.deletes,
      f"u={len(c.updates)} c={len(c.creates)} d={len(c.deletes)}")
check("拒绝原因写进 errors", (not r.ok) and "骤降保护" in r.errors[0],
      r.errors[0][:60] if r.errors else "(无)")
c2 = FakeClient(existing_sets=sets_of(10, 10))
r2 = sync_records(t, ["10.0.0.1"], log=lambda _: None, client=c2, force=True)
check("--force 后确实执行", r2.ok and len(c2.updates) <= 1, r2.summary())

# ------------------------------------------------------------------ 5. 参数校验
print()
print("5) 参数校验（危险操作必须先拦住）")
c = FakeClient()
r = sync_records(mk_target(), [], log=lambda _: None, client=c)
check("空 IP 列表被拒（避免清空线上）",
      (not r.ok) and "为空" in r.errors[0] and not c.calls,
      r.errors[0][:50] if r.errors else "")

r = sync_records(mk_target(zone_name=""), ["1.1.1.1"], log=lambda _: None, client=c)
check("缺少 zone_name 被拒", (not r.ok) and "zone_name" in r.errors[0], r.errors[0][:50])

r = sync_records(mk_target(record_name=""), ["1.1.1.1"], log=lambda _: None, client=c)
check("缺少 record_name 被拒", (not r.ok) and "record_name" in r.errors[0], r.errors[0][:50])

for bad in (0, -1, 51, 100):
    r = sync_records(mk_target(batch_size=bad), ["1.1.1.1"], log=lambda _: None, client=c)
    check(f"batch_size={bad} 被拒", (not r.ok) and "batch_size" in r.errors[0],
          r.errors[0][:50] if r.errors else "")
for good in (1, 25, 50):
    r = sync_records(mk_target(batch_size=good), ["1.1.1.1"], log=lambda _: None,
                     client=FakeClient())
    check(f"batch_size={good} 允许", r.ok, r.errors[0][:40] if r.errors else "")

r = sync_records(mk_target(provider="dnspod"), ["1.1.1.1"], log=lambda _: None, client=c)
check("不支持的服务商被拒", (not r.ok) and "暂不支持" in r.errors[0], r.errors[0][:50])

# ------------------------------------------------------------------ 6. dry-run
print()
print("6) dry-run 只预览、绝不调用 API")
c = FakeClient(existing_sets=sets_of(3, 10))
r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                 log=lambda _: None, client=c, dry_run=True)
check("dry-run 返回 ok", r.ok, r.summary())
check("dry-run 一次 API 都没调", not c.calls, str(c.calls))
check("dry-run 仍报告将写入的数量", r.total_ips == 140 and r.created_batches == 3,
      f"total={r.total_ips} batches={r.created_batches}")

# ------------------------------------------------------------------ 7. 故障处理
print()
print("7) 失败时不得继续破坏线上")
c = FakeClient(existing_sets=sets_of(5, 10), fail_update=True)
r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                 log=lambda _: None, client=c)
check("更新失败 -> 返回失败", not r.ok, r.summary()[:60])
check("更新失败后不再删除任何记录集", not c.deletes, str(c.deletes))

c = FakeClient(existing_sets=sets_of(1, 10), fail_create=True)
r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                 log=lambda _: None, client=c)
check("新建失败 -> 返回失败", not r.ok, r.summary()[:60])
check("新建失败后不再删除", not c.deletes, str(c.deletes))

c = FakeClient(existing_sets=sets_of(5, 10), fail_delete=True)
r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                 log=lambda _: None, client=c)
check("删除失败 -> 返回失败", not r.ok and "删除" in r.errors[-1], r.errors[-1][:50])

c = FakeClient(zone_missing=True)
r = sync_records(mk_target(), ["1.1.1.1"], log=lambda _: None, client=c)
check("域名不存在 -> 明确报错", (not r.ok) and "zone" in r.errors[0].lower(),
      r.errors[0][:50])

# ------------------------------------------------------------------ 7b. 网络异常
print()
print("7b) 网络异常（非 SDK 异常）必须被兜住，不能穿透导致整轮崩溃")


class NetworkErrorClient(FakeClient):
    """让各操作抛出真实世界里常见的网络异常类型。"""

    def __init__(self, where: str, **kw):
        super().__init__(**kw)
        self.where = where

    def list_public_zones(self, request):
        if self.where == "zone":
            raise ConnectionResetError("[Errno 104] 连接被对方重置")
        return super().list_public_zones(request)

    def list_record_sets(self, request):
        if self.where == "list":
            raise TimeoutError("read timed out")
        return super().list_record_sets(request)

    def update_record_set(self, request):
        if self.where == "update":
            raise ConnectionResetError("[Errno 104] 连接被对方重置")
        return super().update_record_set(request)

    def create_record_set_with_line(self, request):
        if self.where == "create":
            raise TimeoutError("read timed out")
        return super().create_record_set_with_line(request)

    def delete_record_set(self, request):
        if self.where == "delete":
            raise OSError("Network is unreachable")
        return super().delete_record_set(request)


for where, desc in (("zone", "查询 zone"), ("list", "查询记录集"),
                    ("update", "就地更新"), ("create", "新建记录集")):
    kw = {"existing_sets": sets_of(1, 10)} if where != "zone" else {}
    c = NetworkErrorClient(where, **kw)
    try:
        r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                         log=lambda _: None, client=c)
        crashed = False
    except Exception as exc:                       # noqa: BLE001
        r, crashed = None, f"{type(exc).__name__}: {exc}"
    check(f"{desc}时网络异常被兜住（不崩溃）", not crashed, crashed or "")
    if not crashed:
        check(f"  {desc}异常 -> 返回失败并说明原因",
              (not r.ok) and r.errors, r.errors[0][:60] if r.errors else "(无错误)")

# 最关键的一条：查不到「现状」时必须中止，绝不能当成「线上没有记录」去新建
c = NetworkErrorClient("list", existing_sets=sets_of(2, 50))
r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                 log=lambda _: None, client=c)
check("查询现状失败 -> 一次写操作都不做",
      not c.updates and not c.creates and not c.deletes,
      f"updates={len(c.updates)} creates={len(c.creates)} deletes={len(c.deletes)}")
check("  明确说明是「现状未知」而非「没有记录」",
      any("现状未知" in e or "中止" in e for e in r.errors),
      r.errors[0][:60] if r.errors else "(无)")

# inspect 只读检查同样不能把查询失败说成「暂无记录」
c = NetworkErrorClient("list")
r = inspect(mk_target(), log=lambda _: None, client=c)
check("inspect 查询失败 -> 报错而非「暂无记录」",
      (not r.ok) and r.errors, r.errors[0][:50] if r.errors else "(无)")

# 删除阶段失败：前面的更新已完成，但不得因此崩溃
c = NetworkErrorClient("delete", existing_sets=sets_of(5, 10))
try:
    r = sync_records(mk_target(), [f"10.0.0.{i}" for i in range(1, 141)],
                     log=lambda _: None, client=c)
    crashed = None
except Exception as exc:                           # noqa: BLE001
    r, crashed = None, f"{type(exc).__name__}: {exc}"
check("删除阶段网络异常被兜住", not crashed, crashed or "")
if not crashed:
    check("  返回失败而非崩溃", not r.ok, r.summary()[:60])

# ------------------------------------------------------------------ 8. 分页与名称匹配
print()
print("8) 记录集分页 + 名称必须精确匹配")
c = FakeClient(existing_sets=sets_of(7, 5), page_size=2)     # 7 个，每页 2 个
t = mk_target()
ips = [f"10.0.0.{i}" for i in range(1, 36)]                  # 1 批
r = sync_records(t, ips, log=lambda _: None, client=c)
check("分页能取到全部 7 个已有记录集",
      c.calls.count("list_records") >= 4, f"list 调用 {c.calls.count('list_records')} 次")
check("更新第一个 + 删除其余 6 个",
      len(c.updates) == 1 and len(c.deletes) == 6,
      f"updates={len(c.updates)} deletes={len(c.deletes)}")

# 名称相近但不能匹配（前缀/后缀不同）
similar = [
    FakeSet("x1", "xt.1.060730.xyz.", ["1.1.1.1"]),
    FakeSet("x2", "t.1.060730.xyz.evil.com.", ["2.2.2.2"]),
    FakeSet("x3", "t.1.060730.xy.", ["3.3.3.3"]),
    FakeSet("ok", "t.1.060730.xyz.", ["4.4.4.4"]),
]
c = FakeClient(existing_sets=similar)
r = sync_records(mk_target(), ["10.0.0.1"], log=lambda _: None, client=c, force=True)
check("只操作名称完全匹配的那一个",
      [u[0] for u in c.updates] == ["ok"] and not c.deletes,
      f"updates={[u[0] for u in c.updates]} deletes={c.deletes}")

# 无尾点的记录名也能匹配（DNS 里两种写法等价）
no_dot = [FakeSet("nd", "t.1.060730.xyz", ["1.1.1.1"])]
c = FakeClient(existing_sets=no_dot)
r = sync_records(mk_target(), ["10.0.0.1"], log=lambda _: None, client=c)
check("记录名无尾点也能匹配", len(c.updates) == 1 and c.updates[0][0] == "nd",
      f"updates={len(c.updates)}")

# ------------------------------------------------------------------ 9. inspect 只读
print()
print("9) inspect 只读检查")
c = FakeClient(existing_sets=sets_of(3, 4))
r = inspect(mk_target(), log=lambda _: None, client=c)
check("inspect 成功", r.ok, r.summary()[:50])
check("inspect 不产生任何写操作",
      not c.updates and not c.creates and not c.deletes, str(c.calls))
check("inspect 报告现有 IP 数", len(r.deleted) == 12, str(len(r.deleted)))

# ------------------------------------------------------------------ 10. ttl / 类型
print()
print("10) TTL / 记录类型 / 记录名的透传")
seen = {}


class ObserveClient(FakeClient):
    """记录下发给 API 的 body 内容（新建与更新分别观察）。"""

    def update_record_set(self, request):
        seen["u_ttl"] = request.body.ttl
        seen["u_type"] = request.body.type
        seen["u_records"] = list(request.body.records)
        super().update_record_set(request)

    def create_record_set_with_line(self, request):
        seen["c_ttl"] = request.body.ttl
        seen["c_type"] = request.body.type
        seen["c_name"] = request.body.name
        seen["c_records"] = list(request.body.records)
        super().create_record_set_with_line(request)


# 更新路径：已有 1 个记录集 + 1 批
c = ObserveClient(existing_sets=[FakeSet("r1", "t.1.060730.xyz.", ["1.1.1.1"])])
r = sync_records(mk_target(ttl=600, record_type="AAAA"), ["2001:db8::1"],
                 log=lambda _: None, client=c)
check("更新路径 TTL 透传", seen.get("u_ttl") == 600, str(seen.get("u_ttl")))
check("更新路径记录类型透传", seen.get("u_type") == "AAAA", str(seen.get("u_type")))
check("更新路径写入正确 IP", seen.get("u_records") == ["2001:db8::1"],
      str(seen.get("u_records")))

# 新建路径：无已有记录集
seen.clear()
c = ObserveClient()
sync_records(mk_target(ttl=120), ["10.0.0.1", "10.0.0.2"], log=lambda _: None, client=c)
check("新建路径 TTL 透传", seen.get("c_ttl") == 120, str(seen.get("c_ttl")))
check("新建路径记录类型透传（默认 A）", seen.get("c_type") == "A", str(seen.get("c_type")))
check("新建路径记录名带尾点（华为云要求）",
      str(seen.get("c_name", "")).endswith("."), str(seen.get("c_name")))
check("新建路径写入全部 IP", len(seen.get("c_records") or []) == 2,
      str(seen.get("c_records")))

print()
print("=" * 70)
if _failures:
    print(f"失败 {len(_failures)} 项：")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("DNS 下发逻辑全部测试通过")
sys.exit(0)
