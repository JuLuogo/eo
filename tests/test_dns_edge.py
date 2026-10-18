#!/usr/bin/env python3
"""DNS 下发：极端输入与边界（不发真实请求，用假客户端）。

覆盖：去重、非法地址剔除、IPv6 混入 A 记录、超过 50 个 IP 的分批、
空列表保护、batch_size 越界、zone 不存在、CNAME 模式、骤降保护。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.dns import DnsTarget, sync_records   # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


class FakeSet:
    def __init__(self, i, name, records, typ="A"):
        self.id, self.name, self.records, self.type = i, name, records, typ


class FakeLinks:
    def __init__(self, nxt=None):
        self.next = nxt


class FakeResp:
    def __init__(self, sets, nxt=None):
        self.recordsets, self.links = sets, FakeLinks(nxt)


class FakeZone:
    def __init__(self, zones):
        self.zones = zones


class FakeClient:
    """与 tests/test_dns_sync.py 保持同一套调用约定。

    注意方法名是 create_record_set_with_line、delete_record_set(request)，
    更新走 request.recordset_id / request.body.records ——
    这是 dns.py 里华为云分支真实使用的形态，签名写错会得到假失败。
    """

    def __init__(self, existing_sets=None, zones=None, fail_create=False,
                 fail_update=False, zone_missing=False):
        self.existing = list(existing_sets or [])
        self.zones = zones if zones is not None else [type("Z", (), {"id": "zone-1"})()]
        self.updates: list[tuple[str, list[str]]] = []
        self.creates: list[list[str]] = []
        self.deletes: list[str] = []
        self.fail_create = fail_create
        self.fail_update = fail_update
        self.zone_missing = zone_missing

    def list_public_zones(self, request):                   # noqa: ANN001
        return FakeZone([] if self.zone_missing else self.zones)

    def list_record_sets(self, request):                    # noqa: ANN001
        return FakeResp(self.existing)

    def update_record_set(self, request):                   # noqa: ANN001
        if self.fail_update:
            raise RuntimeError("模拟更新失败")
        self.updates.append((request.recordset_id, list(request.body.records)))

    def create_record_set_with_line(self, request):         # noqa: ANN001
        if self.fail_create:
            raise RuntimeError("模拟新建失败")
        self.creates.append(list(request.body.records))

    def delete_record_set(self, request):                   # noqa: ANN001
        self.deletes.append(request.recordset_id)


def sets_of(n: int, ips_per_set: int, name="eo-lt.1.060730.xyz.", start=1):
    out = []
    for i in range(n):
        ips = [f"9.9.{i}.{j}" for j in range(1, ips_per_set + 1)]
        out.append(FakeSet(f"rs{i + start}", name, ips))
    return out


def written_ips(client: FakeClient) -> list[str]:
    """假客户端里最终写进去的全部 IP（新建 + 更新）。"""
    out: list[str] = []
    for batch in client.creates:
        out.extend(batch)
    for _rid, records in client.updates:
        out.extend(records)
    return out


def target(**over) -> DnsTarget:
    base = dict(zone_name="1.060730.xyz", record_name="eo-lt.1.060730.xyz", ttl=1)
    base.update(over)
    return DnsTarget(**base)


print("=" * 70)
print("DNS 下发：极端输入与边界")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) IP 列表清洗：去重 + 剔除非法地址（本轮修的）")
# --------------------------------------------------------------------------
logs: list[str] = []
client = FakeClient()
r = sync_records(target(), ["1.1.1.1", "1.1.1.1", "2.2.2.2", "abc", "", "  ",
                            "3.3.3.3", "1.1.1.1"], log=logs.append, client=client)
check("下发成功（一行脏数据不该让整批失败）", r.ok is True, r.summary()[:80])
check("重复地址只写入一次", r.total_ips == 3, f"{r.total_ips} 个")
check("有清洗提示日志", any("已清洗" in l for l in logs),
      str([l for l in logs if "已清洗" in l][:1]))
check("提示里点出了非法地址", any("非法地址" in l for l in logs))
check("提示里点出了重复地址", any("重复地址" in l for l in logs))

print()
print("2) A 记录里混入 IPv6：要剔除并说明")
# --------------------------------------------------------------------------
logs = []
client = FakeClient()
r = sync_records(target(record_type="A"), ["1.1.1.1", "2001:db8::1"], log=logs.append, client=client)
check("只写入 IPv4", r.ok and r.total_ips == 1, f"{r.total_ips} 个")
check("日志说明了剔除 IPv6", any("非法地址" in l for l in logs), str(logs[:2]))

print()
print("3) 全部是非法地址：必须拒绝下发（不能把线上清空）")
# --------------------------------------------------------------------------
r = sync_records(target(), ["abc", "not-an-ip"], log=logs.append, client=FakeClient())
check("拒绝下发", r.ok is False, r.summary()[:60])
check("错误信息里说明 IP 列表为空", "为空" in r.summary(), r.summary()[:60])

print()
print("4) 超过 50 个 IP：按 batch_size 分批（每批不超过 50）")
# --------------------------------------------------------------------------
client = FakeClient()
ips = [f"10.0.{i // 250}.{i % 250 + 1}" for i in range(137)]
r = sync_records(target(batch_size=50), ips, log=lambda *_: None, client=client)
check("137 个 IP 分 3 批", r.created_batches == 3, f"{r.created_batches} 批")
check("写入总数正确", r.total_ips == 137, str(r.total_ips))
check("每批不超过 50", all(len(batch) <= 50 for batch in client.creates),
      str([len(b) for b in client.creates]))

print()
print("5) batch_size 越界要明确报错")
# --------------------------------------------------------------------------
for bad in (0, 51, -1):
    r = sync_records(target(batch_size=bad), ["1.1.1.1"], log=lambda *_: None)
    check(f"batch_size={bad} 被拒绝", r.ok is False and "batch_size" in r.summary(),
          r.summary()[:50])

print()
print("6) zone 不存在 / 名称为空")
# --------------------------------------------------------------------------
client = FakeClient(zone_missing=True)
r = sync_records(target(), ["1.1.1.1"], log=lambda *_: None, client=client)
check("zone 不存在时失败", r.ok is False, r.summary()[:60])

r = sync_records(target(zone_name=""), ["1.1.1.1"], log=lambda *_: None)
check("zone 为空时失败", r.ok is False, r.summary()[:60])
r = sync_records(target(record_name=""), ["1.1.1.1"], log=lambda *_: None)
check("record 为空时失败", r.ok is False, r.summary()[:60])

print()
print("7) 解析量骤降保护（不能因为一次异常结果把线上删空）")
# --------------------------------------------------------------------------
existing = sets_of(1, 20)          # 现有 1 个记录集、20 个 IP
client = FakeClient(existing_sets=existing)
r = sync_records(target(), ["1.1.1.1"], log=lambda *_: None, client=client)
check("20 个 -> 1 个被拦截", r.ok is False, r.summary()[:70])
check("错误信息提到骤降/缩容", "骤降" in r.summary() or "缩" in r.summary(), r.summary()[:70])
check("没有真的删除记录", not client.deletes, str(client.deletes))

client = FakeClient(existing_sets=existing)
r = sync_records(target(), ["1.1.1.1"], log=lambda *_: None, client=client, force=True)
check("--force 可以越过保护", r.ok is True, r.summary()[:60])

print()
print("8) CNAME 模式（部分平台只能 CNAME 接入）")
# --------------------------------------------------------------------------
t = target(cname="eo-lt.example.cdn.com")
check("DnsTarget 支持 cname 字段", hasattr(t, "cname"), "有")
r = sync_records(t, ["1.1.1.1"], log=lambda *_: None, dry_run=True)
check("CNAME 模式 dry-run 不报错", r.ok is True, r.summary()[:60])

print()
print("9) dry-run 不产生任何写操作")
# --------------------------------------------------------------------------
client = FakeClient()
r = sync_records(target(), ["1.1.1.1", "2.2.2.2"], log=lambda *_: None,
                 client=client, dry_run=True)
check("dry-run 返回成功", r.ok is True, r.summary()[:60])
check("dry-run 没有新建/更新/删除",
      not client.creates and not client.updates and not client.deletes,
      f"created={len(client.creates)} updated={len(client.updates)} deleted={len(client.deletes)}")

print()
print("10) 创建失败时不能留下残局（旧记录必须还在）")
# --------------------------------------------------------------------------
# 要走到「新建」这一步，必须让批次多于现有记录集：现有 1 个记录集（50 个 IP），
# 本次写 100 个 IP -> 2 批 -> 第 1 批就地更新、第 2 批必须新建（这里注入失败）。
client = FakeClient(existing_sets=sets_of(1, 50), fail_create=True)
many = [f"10.0.{i // 250}.{i % 250 + 1}" for i in range(100)]
r = sync_records(target(), many, log=lambda *_: None, client=client)
check("创建失败时整体判失败", r.ok is False, r.summary()[:60])
check("创建失败时不删除旧记录集", not client.deletes, str(client.deletes))
check("第 1 批确实更新了（就地更新不浪费）", len(client.updates) == 1,
      f"{len(client.updates)} 次更新")

print()
print("11) 同一主机名下允许多条记录集（回归：线上实际 11 条 / 530 个 IP）")
# --------------------------------------------------------------------------
# 曾经有人（我）按「华为云同名记录集只能有一条」加了拦截，
# 结果会直接把线上正在用的布局判成非法。README 里记着真实情况：
#   eo-lt.1.060730.xyz 现有 11 个记录集 / 530 个 IP
# 这里用 11 条现有记录集 + 530 个 IP 精确复现那个布局。
client = FakeClient(existing_sets=sets_of(11, 50))
ips_530 = [f"10.{i // 250}.{i % 250}.{j}" for i in range(11) for j in range(1, 49)]
ips_530 = ips_530[:530]
r = sync_records(target(), ips_530, log=lambda *_: None, client=client)
check("530 个 IP / 11 条记录集：全部就地更新成功", r.ok is True, r.summary()[:80])
check("11 条都更新了", r.updated_batches == 11, str(r.updated_batches))
check("没有多余新建", r.created_batches == 0, str(r.created_batches))

# 再验证「比现有更多」时是新建而不是报错
client = FakeClient(existing_sets=sets_of(1, 50))
r = sync_records(target(), [f"10.7.{i // 250}.{i % 250 + 1}" for i in range(120)],
                 log=lambda *_: None, client=client)
check("现有 1 条、本次需 3 条 -> 1 更新 + 2 新建",
      r.ok is True and r.updated_batches == 1 and r.created_batches == 2,
      f"更新 {r.updated_batches} 新建 {r.created_batches}")
check("新建用的确实是同一个主机名（华为云允许）",
      len(client.creates) == 2, str(len(client.creates)))

print()
print("=" * 70)
if _failures:
    print(f"DNS 下发边界测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("DNS 下发边界测试全部通过")
sys.exit(0)
