"""把优选结果批量下发到 DNS（华为云 DNS）。

对应原来的 updatedns.py，但支持：
  * 一个平台一份配置（zone / 记录名 / AK / 批量大小），互不干扰；
  * 只动目标记录名，绝不碰同 zone 下其它记录；
  * 先删除旧记录集，再按批次重建（华为云单记录集 IP 上限 50）；
  * dry-run 预览要做什么，不会真的改线上解析。

安全提示：执行前请确认 record_name 是你要改的那条，删旧建新之间域名会有一小段
空窗期，属正常现象；若创建失败，脚本会提示你旧记录内容以便回滚。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .core import safe_log

Log = Callable[[str], None]


def _logger(log: Log | None) -> Log:
    """统一日志出口，默认走编码安全的 safe_log。"""
    return log or safe_log


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class DnsTarget:
    """一条待下发的 DNS 记录配置。"""

    zone_name: str
    record_name: str                       # 不带末尾的点，函数内部自动补
    provider: str = "huawei"               # huawei / cloudflare
    region: str = "cn-north-4"
    access_key_id: str = ""
    access_key_secret: str = ""
    api_token: str = ""                    # Cloudflare API Token
    zone_id: str = ""                      # Cloudflare Zone ID（可留空自动查）
    record_type: str = "A"
    ttl: int = 1
    batch_size: int = 50
    platform: str = ""
    cname: str = ""                        # 记录值固定为 CNAME 时使用

    def normalized_name(self) -> str:
        return self.record_name.rstrip(".") + "."

    @classmethod
    def from_dict(cls, data: dict, platform: str = "") -> "DnsTarget":
        return cls(
            zone_name=(data.get("zone_name") or os.environ.get("HUAWEI_ZONE", "")).strip(),
            record_name=(data.get("record_name") or os.environ.get("HUAWEI_RECORD", "")).strip(),
            provider=data.get("provider", "huawei"),
            region=data.get("region") or os.environ.get("HUAWEI_REGION", "cn-north-4"),
            access_key_id=data.get("access_key_id") or os.environ.get("HUAWEI_AK", ""),
            access_key_secret=data.get("access_key_secret") or os.environ.get("HUAWEI_SK", ""),
            api_token=data.get("api_token") or os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            zone_id=data.get("zone_id", ""),
            record_type=data.get("record_type", "A"),
            ttl=int(data.get("ttl", 1)),
            batch_size=int(data.get("batch_size", 50)),
            platform=platform,
            cname=data.get("cname", ""),
        )


@dataclass
class DnsUpdateResult:
    ok: bool
    zone_id: str = ""
    deleted: list[str] = field(default_factory=list)      # 被删除记录集里原来的 IP
    created_batches: int = 0
    updated_batches: int = 0
    deleted_batches: int = 0
    total_ips: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return "下发失败: " + "; ".join(self.errors or ["未知错误"])
        return (f"下发成功: 更新 {self.updated_batches} 个 / 新建 {self.created_batches} 个 / "
                f"删除 {self.deleted_batches} 个记录集，共写入 {self.total_ips} 个 IP")


# --------------------------------------------------------------------------
# 华为云 SDK 封装
# --------------------------------------------------------------------------
class _RequestShim:
    """无 SDK 时的请求对象替身：任意属性可赋值，任意关键字参数可构造。

    仅在未安装华为云 SDK 时使用（例如离线自检）。
    真正下发 DNS 必须安装 SDK，见 build_client()。
    """

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<RequestShim {vars(self)}>"


class MissingSdkError(RuntimeError):
    """未安装华为云 SDK 时抛出，附带安装指引。"""


class _FallbackExceptions:
    """SDK 缺失时的异常替身，保证 except 子句语法有效且不会误捕真实异常。"""

    class ClientRequestException(Exception):
        error_msg = ""


_FALLBACK_SDK = {
    "_available": False,
    "BasicCredentials": None,
    "exceptions": _FallbackExceptions,
    "DnsClient": None,
    "DnsRegion": None,
    "ListPublicZonesRequest": _RequestShim,
    "ListRecordSetsRequest": _RequestShim,
    "DeleteRecordSetRequest": _RequestShim,
    "CreateRecordSetWithLineRequest": _RequestShim,
    "CreateRecordSetWithLineRequestBody": _RequestShim,
    "UpdateRecordSetRequest": _RequestShim,
    "UpdateRecordSetReq": _RequestShim,
}


def sdk_available() -> bool:
    """华为云 SDK 是否可用（只有真正下发 DNS 才需要）。"""
    return bool(_sdk().get("_available"))


def _sdk():
    """延迟导入 SDK。

    未安装时返回后备字典（请求类由轻量 shim 代替），
    这样 DNS 业务逻辑仍可离线自检；build_client() 会显式报错提示安装。
    """
    try:
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.exceptions import exceptions
        from huaweicloudsdkdns.v2 import DnsClient
        from huaweicloudsdkdns.v2.model import (
            CreateRecordSetWithLineRequest,
            CreateRecordSetWithLineRequestBody,
            DeleteRecordSetRequest,
            ListPublicZonesRequest,
            ListRecordSetsRequest,
            UpdateRecordSetReq,
            UpdateRecordSetRequest,
        )
        from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
    except ImportError:
        # 未安装 SDK：返回后备实现，保证离线自检可用
        return dict(_FALLBACK_SDK)

    return {
        "_available": True,
        "BasicCredentials": BasicCredentials,
        "exceptions": exceptions,
        "DnsClient": DnsClient,
        "DnsRegion": DnsRegion,
        "CreateRecordSetWithLineRequest": CreateRecordSetWithLineRequest,
        "CreateRecordSetWithLineRequestBody": CreateRecordSetWithLineRequestBody,
        "DeleteRecordSetRequest": DeleteRecordSetRequest,
        "ListPublicZonesRequest": ListPublicZonesRequest,
        "ListRecordSetsRequest": ListRecordSetsRequest,
        "UpdateRecordSetRequest": UpdateRecordSetRequest,
        "UpdateRecordSetReq": UpdateRecordSetReq,
    }


def build_client(target: DnsTarget):
    """按配置创建华为云 DNS 客户端（需要已安装并配置华为云 SDK）。"""
    sdk = _sdk()
    if not sdk.get("_available"):
        raise MissingSdkError(
            "缺少华为云 SDK，无法下发 DNS。请先安装：\n"
            "    pip install huaweicloudsdkcore huaweicloudsdkdns\n"
            "  （Docker 镜像已内置；本地调试可用 --no-dns 跳过，"
            "或只做预览：docker compose run --rm optimizer dns-check -p eo-lt）"
        )
    if not target.access_key_id or not target.access_key_secret:
        raise RuntimeError("缺少 AccessKey，请配置 access_key_id / access_key_secret 或 HUAWEI_AK/HUAWEI_SK")
    credentials = sdk["BasicCredentials"](target.access_key_id, target.access_key_secret)
    return (
        sdk["DnsClient"].new_builder()
        .with_credentials(credentials)
        .with_region(sdk["DnsRegion"].value_of(target.region))
        .build()
    )


def _get_zone_id(client, target: DnsTarget, log: Log) -> str | None:
    sdk = _sdk()
    request = sdk["ListPublicZonesRequest"]()
    request.name = target.zone_name
    try:
        response = client.list_public_zones(request)
    except sdk["exceptions"].ClientRequestException as exc:
        log(f"  ✗ 查询 zone 失败: {exc.error_msg if hasattr(exc, 'error_msg') else exc}")
        return None
    except Exception as exc:  # 网络抖动 / SDK 其它异常，同样不能穿透
        log(f"  ✗ 查询 zone 异常: {type(exc).__name__}: {exc}")
        return None

    zones = getattr(response, "zones", None) or []
    if not zones:
        log(f"  ✗ 未找到域名 {target.zone_name}")
        return None
    return zones[0].id


def _find_records(client, zone_id: str, target: DnsTarget, log: Log) -> list[dict] | None:
    """找出目标记录名下的记录集（只匹配完全相同的主机名，自动翻页）。

    返回值区分两种「没有」：
      * []    —— 查询成功，线上确实没有该记录的记录集（可以放心新建）
      * None  —— 查询失败（网络抖动、权限不足等），**现状未知**

    区分这两者至关重要：若把「查询失败」当成「没有记录」，调用方会走新建分支，
    结果旧的记录集仍然保留、又新增一批 —— 域名上出现重复 A 记录，
    而且旧记录再也不会被清理。所以查询失败必须向上传递、由调用方中止下发。
    """
    sdk = _sdk()
    wanted = target.normalized_name()
    found: list[dict] = []
    page_token = None
    page = 0

    while True:
        page += 1
        if page > 50:                      # 防御性上限，避免异常时死循环
            log("  ⚠ 记录集翻页超过 50 页，提前停止")
            break

        request = sdk["ListRecordSetsRequest"]()
        request.zone_id = zone_id
        request.type = target.record_type
        request.name = wanted
        request.limit = 500
        if page_token:
            request.offset = None
            request.marker = page_token

        try:
            response = client.list_record_sets(request)
        except sdk["exceptions"].ClientRequestException as exc:
            log(f"  ✗ 查询记录失败: {exc.error_msg if hasattr(exc, 'error_msg') else exc}")
            return None
        except Exception as exc:  # 网络抖动 / SDK 其它异常
            log(f"  ✗ 查询记录集异常: {type(exc).__name__}: {exc}")
            return None

        for recordset in getattr(response, "recordsets", None) or []:
            name = (recordset.name or "")
            if name.rstrip(".") + "." == wanted:
                found.append({
                    "id": recordset.id,
                    "name": name,
                    "records": list(recordset.records or []),
                })

        links = getattr(response, "links", None)
        page_token = getattr(links, "next", None) if links else None
        if not page_token:
            break

    return found


def _delete_record(client, zone_id: str, recordset_id: str, log: Log) -> bool:
    sdk = _sdk()
    request = sdk["DeleteRecordSetRequest"]()
    request.zone_id = zone_id
    request.recordset_id = recordset_id
    try:
        client.delete_record_set(request)
        return True
    except sdk["exceptions"].ClientRequestException as exc:
        log(f"  ✗ 删除记录集失败: {exc.error_msg if hasattr(exc, 'error_msg') else exc}")
        return False
    except Exception as exc:  # 网络抖动 / SDK 其它异常，同样不能穿透
        log(f"  ✗ 删除记录集异常: {type(exc).__name__}: {exc}")
        return False


def _create_record(client, zone_id: str, target: DnsTarget, ips: Sequence[str], log: Log) -> bool:
    sdk = _sdk()
    request = sdk["CreateRecordSetWithLineRequest"]()
    request.zone_id = zone_id
    request.body = sdk["CreateRecordSetWithLineRequestBody"](
        records=list(ips),
        ttl=target.ttl,
        type=target.record_type,
        name=target.normalized_name(),
    )
    try:
        client.create_record_set_with_line(request)
        return True
    except sdk["exceptions"].ClientRequestException as exc:
        log(f"  ✗ 创建记录集失败: {exc.error_msg if hasattr(exc, 'error_msg') else exc}")
        return False
    except Exception as exc:  # 网络抖动 / SDK 其它异常，同样不能穿透
        log(f"  ✗ 创建记录集异常: {type(exc).__name__}: {exc}")
        return False


def _update_record(client, zone_id: str, recordset_id: str, target: DnsTarget,
                   ips: Sequence[str], log: Log) -> bool:
    """就地更新已有记录集：不删除、不产生解析空窗期。"""
    sdk = _sdk()
    request = sdk["UpdateRecordSetRequest"]()
    request.zone_id = zone_id
    request.recordset_id = recordset_id
    request.body = sdk["UpdateRecordSetReq"](
        records=list(ips),
        ttl=target.ttl,
        type=target.record_type,
    )
    try:
        client.update_record_set(request)
        return True
    except sdk["exceptions"].ClientRequestException as exc:
        log(f"  ✗ 更新记录集失败: {exc.error_msg if hasattr(exc, 'error_msg') else exc}")
        return False
    except Exception as exc:  # 网络抖动 / SDK 其它异常，同样不能穿透
        log(f"  ✗ 更新记录集异常: {type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _shrunk_too_much(existing_count: int, new_count: int, force: bool) -> str:
    """解析量骤降保护：返回拒绝原因（空字符串表示允许下发）。

    阈值随规模变化：现有 100+ 条时允许降到 1/4，4 条以上降到 1/3，
    极小规模要求至少保留一半 —— 目的是拦住「网段写错/网络抖动」
    导致线上解析被砍掉大半的情况。
    """
    if force or existing_count <= 0:
        return ""
    keep_ratio = 0.25 if existing_count >= 100 else (0.34 if existing_count > 3 else 0.5)
    minimum = max(1, int(existing_count * keep_ratio))
    if new_count >= minimum or (existing_count == 1 and new_count >= 1):
        return ""
    return (f"解析量骤降保护：现有 {existing_count} 条记录，本次只写入 {new_count} 条"
            f"（低于下限 {minimum}）。如确认要缩容，请加 --force")


def sync_records(
    target: DnsTarget,
    ips: Sequence[str],
    log: Log | None = None,
    client=None,
    dry_run: bool = False,
    force: bool = False,
) -> DnsUpdateResult:
    """把 ips 全量同步到 target 指定的 DNS 记录。

    策略（比原来的「先删光再重建」更安全）：
      1. 优先【就地更新】已有的同名记录集，全程域名都有解析，无空窗期；
      2. 记录集不够时才新建；
      3. 最后才删除多余的记录集。
    并带有「解析量骤降保护」：新 IP 数远少于现有记录时会直接中止。
    """
    result = DnsUpdateResult(ok=False)
    log = _logger(log)
    ips = [ip.strip() for ip in ips if ip and ip.strip()]

    if not target.zone_name or not target.record_name:
        result.errors.append("zone_name / record_name 不能为空")
        return result
    if not ips:
        result.errors.append("IP 列表为空，拒绝下发（避免把线上解析清空）")
        return result

    # 服务商分支
    if target.provider == "cloudflare":
        return _sync_cloudflare(target, ips, log, dry_run, force)
    if target.provider not in ("huawei", ""):
        result.errors.append(f"暂不支持的服务商: {target.provider}（可选 huawei / cloudflare）")
        return result

    if target.batch_size < 1 or target.batch_size > 50:
        result.errors.append(f"batch_size 必须在 1~50 之间，当前 {target.batch_size}")
        return result

    if dry_run:
        log(f"[dry-run] zone={target.zone_name} 记录={target.normalized_name()} "
            f"类型={target.record_type} ttl={target.ttl}")
        log(f"[dry-run] 将就地更新/新建 {target.record_type} 记录集，"
            f"写入 {len(ips)} 个 IP（每批 {target.batch_size}），并删除多余记录集")
        result.ok = True
        result.total_ips = len(ips)
        result.created_batches = (len(ips) + target.batch_size - 1) // target.batch_size
        return result

    client = client or build_client(target)

    zone_id = _get_zone_id(client, target, log)
    if not zone_id:
        result.errors.append("获取 zone id 失败")
        return result
    result.zone_id = zone_id

    existing = _find_records(client, zone_id, target, log)
    if existing is None:
        # 查不到现状就绝不动线上：否则会把旧记录当成「没有」而新建，
        # 造成重复解析且旧记录永远清理不掉。
        result.errors.append("查询现有记录集失败，现状未知，已中止下发（避免产生重复解析）")
        log("  ✗ 查询现有记录集失败，已中止（不修改线上解析）")
        return result

    old_ips = [ip for record in existing for ip in record["records"]]
    result.deleted.extend(old_ips)

    # 阈值统一：现有记录较多时（>=100）允许降到 1/4；较少时降到 1/3；
    # 极小规模（<=3）也要求至少保留一半，避免「本来 2 条、结果只留 1 条」这种缩容。
    reason = _shrunk_too_much(len(old_ips), len(ips), force)
    if reason:
        result.errors.append(reason)
        log(f"  ✗ {reason}")
        return result

    batches = [ips[i:i + target.batch_size] for i in range(0, len(ips), target.batch_size)]
    log(f"  现有 {len(existing)} 个记录集/{len(old_ips)} 个 IP，本次需 {len(batches)} 个记录集")

    # 步骤 1：就地更新已有记录集（无空窗期）
    for index, batch in enumerate(batches):
        if index < len(existing):
            record = existing[index]
            if _update_record(client, zone_id, record["id"], target, batch, log):
                result.updated_batches += 1
                result.total_ips += len(batch)
                log(f"  ✓ 更新记录集 {record['id']}：{len(batch)} 个 IP（解析不中断）")
            else:
                result.errors.append(f"更新记录集 {record['id']} 失败")
                return result
        else:
            # 步骤 2：记录集不够，新建
            if _create_record(client, zone_id, target, batch, log):
                result.created_batches += 1
                result.total_ips += len(batch)
                log(f"  ✓ 新建记录集：{len(batch)} 个 IP")
            else:
                result.errors.append(f"新建记录集失败（第 {index + 1} 批）")
                log(f"  ⚠ 回滚提示: 本次开始前的旧记录为 {old_ips[:50]}")
                return result

    # 步骤 3：删除多余的记录集
    for record in existing[len(batches):]:
        if _delete_record(client, zone_id, record["id"], log):
            result.deleted_batches += 1
            log(f"  ✓ 删除多余记录集 {record['id']}（{len(record['records'])} 个 IP）")
        else:
            result.errors.append(f"删除多余记录集 {record['id']} 失败")
            return result

    result.ok = not result.errors
    return result


def inspect(target: DnsTarget, log: Log | None = None, client=None) -> DnsUpdateResult:
    """只读检查：列出目标记录名下现有的记录集，不做任何修改。

    适合在下发前确认「要改的确实是这条记录」以及密钥是否有权限。
    """
    result = DnsUpdateResult(ok=False)
    log = _logger(log)
    if not target.zone_name or not target.record_name:
        result.errors.append("zone_name / record_name 不能为空")
        return result

    if target.provider == "cloudflare":
        return _inspect_cloudflare(target, log)

    client = client or build_client(target)
    zone_id = _get_zone_id(client, target, log)
    if not zone_id:
        result.errors.append("获取 zone id 失败（检查 zone 名与 AK/SK 权限）")
        return result
    result.zone_id = zone_id

    records = _find_records(client, zone_id, target, log)
    if records is None:
        # 查询失败 ≠ 没有记录，不能谎报「暂无记录」
        result.errors.append("查询记录集失败（网络或权限问题）")
        log("  ✗ 查询记录集失败（网络或权限问题）")
        return result
    if not records:
        log(f"  记录 {target.normalized_name()} 下暂无 {target.record_type} 记录")
    for record in records:
        result.deleted.extend(record["records"])
        log(f"  · {record['name']} ({len(record['records'])} 个 IP): "
            f"{', '.join(record['records'][:10])}{' ...' if len(record['records']) > 10 else ''}")
    result.ok = True
    result.created_batches = len(records)
    result.total_ips = len(result.deleted)
    return result


def sync_platform(platform, log: Log | None = None, dry_run: bool = False, client=None,
                  force: bool = False) -> DnsUpdateResult:
    """按平台配置读取结果文件并下发。"""
    log = _logger(log)
    if not platform.dns:
        return DnsUpdateResult(ok=False, errors=[f"{platform.name} 未配置 dns 段"])
    if not platform.output_file or not Path(platform.output_file).exists():
        return DnsUpdateResult(ok=False, errors=[f"结果文件不存在: {platform.output_file}"])

    ips = [line.split("\t")[0].strip()
           for line in Path(platform.output_file).read_text(encoding="utf-8").splitlines()
           if line.strip()]
    target = DnsTarget.from_dict(platform.dns, platform=platform.key)
    log(f"== {platform.name} -> {target.normalized_name()} ({len(ips)} 个 IP) ==")
    return sync_records(target, ips, log=log, client=client, dry_run=dry_run, force=force)


# --------------------------------------------------------------------------
# Cloudflare 服务商分支
# --------------------------------------------------------------------------
def _sync_cloudflare(target: DnsTarget, ips: Sequence[str], log: Log,
                     dry_run: bool, force: bool) -> DnsUpdateResult:
    from . import dns_cloudflare as cf

    result = DnsUpdateResult(ok=False)
    token = target.api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        result.errors.append("缺少 Cloudflare API Token（配置或 CLOUDFLARE_API_TOKEN 环境变量）")
        return result

    fqdn = target.record_name.rstrip(".")

    if dry_run:
        log(f"[dry-run] provider=cloudflare zone={target.zone_name} 记录={fqdn} "
            f"ttl={target.ttl}")
        log(f"[dry-run] 将把该记录的 A 记录同步为 {len(ips)} 个 IP（多余记录会被删除）")
        result.ok = True
        result.total_ips = len(ips)
        result.created_batches = len(ips)
        return result

    try:
        existing = cf.inspect(token, target.zone_name, fqdn, zone_id=target.zone_id, log=log)
    except cf.CloudflareError as exc:
        result.errors.append(f"查询 Cloudflare 记录失败: {exc}")
        return result
    except Exception as exc:      # 网络抖动同样不能穿透出去
        result.errors.append(f"查询 Cloudflare 记录异常: {type(exc).__name__}: {exc}")
        return result

    result.deleted.extend(existing)

    reason = _shrunk_too_much(len(existing), len(ips), force)
    if reason:
        result.errors.append(reason)
        log(f"  ✗ {reason}")
        return result

    try:
        created, deleted = cf.replace(token, target.zone_name, fqdn, ips,
                                     ttl=target.ttl, zone_id=target.zone_id, log=log)
    except cf.CloudflareError as exc:
        result.errors.append(f"Cloudflare 同步失败: {exc}")
        return result
    except Exception as exc:      # 同上
        result.errors.append(f"Cloudflare 同步异常: {type(exc).__name__}: {exc}")
        return result

    result.created_batches = created
    result.deleted_batches = deleted
    result.total_ips = len(ips)
    result.ok = not result.errors
    return result


def _inspect_cloudflare(target: DnsTarget, log: Log) -> DnsUpdateResult:
    from . import dns_cloudflare as cf

    result = DnsUpdateResult(ok=False)
    token = target.api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        result.errors.append("缺少 Cloudflare API Token")
        return result
    fqdn = target.record_name.rstrip(".")
    try:
        values = cf.inspect(token, target.zone_name, fqdn, zone_id=target.zone_id, log=log)
    except cf.CloudflareError as exc:
        result.errors.append(f"查询失败: {exc}")
        return result
    except Exception as exc:      # 网络抖动同样不能穿透
        result.errors.append(f"查询异常: {type(exc).__name__}: {exc}")
        return result
    result.deleted.extend(values)
    result.total_ips = len(values)
    result.ok = True
    for value in values[:20]:
        log(f"  · {fqdn} -> {value}")
    if len(values) > 20:
        log(f"  · ... 其余 {len(values) - 20} 条省略")
    return result


if __name__ == "__main__":  # 兼容 python -m eo.dns（只读检查，不修改解析）
    from .cli import main

    sys.exit(main(["dns-check", *sys.argv[1:]]))
