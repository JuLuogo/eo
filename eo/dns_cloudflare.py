"""Cloudflare DNS 服务商支持（CloudFront / Cloudflare 场景常用）。

只用标准库的做法比较别扭，这里用 Cloudflare 官方 REST API：
  * 查 Zone ID      : GET  /client/v4/zones?name=<zone>
  * 列记录           : GET  /client/v4/zones/<zone_id>/dns_records?type=A&name=<fqdn>
  * 更新记录         : PUT  /client/v4/zones/<zone_id>/dns_records/<id>
  * 新建记录         : POST /client/v4/zones/<zone_id>/dns_records
  * 删除记录         : DELETE /client/v4/zones/<zone_id>/dns_records/<id>

Cloudflare 只负责把域名解析到 IP（不提供多 IP 轮询的「记录集」概念），
所以多 IP 的写法是：**同名的多条 A 记录**，每条一个 IP。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Sequence

API_BASE = "https://api.cloudflare.com/client/v4"
Log = Callable[[str], None]


class CloudflareError(RuntimeError):
    pass


def _request(method: str, path: str, token: str, payload: dict | None = None,
             timeout: float = 20.0) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise CloudflareError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudflareError(f"网络错误: {exc}") from exc

    if not body.get("success", False):
        errors = body.get("errors") or []
        message = "; ".join(str(e.get("message", e)) for e in errors) or "未知错误"
        raise CloudflareError(message)
    return body


def get_zone_id(token: str, zone_name: str) -> str:
    body = _request("GET", f"/zones?name={zone_name}", token)
    result = body.get("result") or []
    if not result:
        raise CloudflareError(f"未找到 zone: {zone_name}")
    return result[0]["id"]


def list_records(token: str, zone_id: str, fqdn: str, record_type: str = "A") -> list[dict]:
    body = _request(
        "GET",
        f"/zones/{zone_id}/dns_records?type={record_type}&name={fqdn}&per_page=100",
        token,
    )
    return body.get("result") or []


def create_record(token: str, zone_id: str, name: str, content: str,
                  ttl: int = 1, proxied: bool = False, record_type: str = "A") -> dict:
    body = _request("POST", f"/zones/{zone_id}/dns_records", token, {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": max(1, ttl),
        "proxied": proxied,
    })
    return body.get("result") or {}


def update_record(token: str, zone_id: str, record_id: str, name: str, content: str,
                  ttl: int = 1, proxied: bool = False, record_type: str = "A") -> dict:
    body = _request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", token, {
        "type": record_type,
        "name": name,
        "content": content,
        "ttl": max(1, ttl),
        "proxied": proxied,
    })
    return body.get("result") or {}


def delete_record(token: str, zone_id: str, record_id: str) -> None:
    _request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}", token)


def inspect(token: str, zone_name: str, fqdn: str, zone_id: str = "",
            log: Log = print) -> list[str]:
    """返回该记录名下现有的解析值（IP）。"""
    zone_id = zone_id or get_zone_id(token, zone_name)
    records = list_records(token, zone_id, fqdn)
    log(f"  Cloudflare zone={zone_name}({zone_id}) 记录 {fqdn}: {len(records)} 条")
    return [record.get("content", "") for record in records]


def replace(token: str, zone_name: str, fqdn: str, ips: Sequence[str],
            ttl: int = 1, zone_id: str = "", log: Log = print) -> tuple[int, int]:
    """把 fqdn 的 A 记录全量替换为 ips。

    返回 (新建数, 删除数)。已存在但内容相同的记录会被保留（不产生无谓改动）。
    注意 Cloudflare 免费版 TTL=1 表示 Auto。
    """
    zone_id = zone_id or get_zone_id(token, zone_name)
    existing = list_records(token, zone_id, fqdn)
    existing_by_content = {record.get("content"): record for record in existing}

    wanted = [ip for ip in ips if ip]
    wanted_set = set(wanted)

    created = deleted = 0

    # 1) 先补齐需要新增的（同名的多条 A 记录 = 多 IP 轮询）
    new_ips = [ip for ip in wanted if ip not in existing_by_content]
    for ip in new_ips:
        try:
            create_record(token, zone_id, fqdn, ip, ttl=ttl)
            created += 1
        except CloudflareError as exc:
            log(f"  ✗ 创建 {fqdn} -> {ip} 失败: {exc}")

    # 2) 再删除多余的
    for content, record in existing_by_content.items():
        if content not in wanted_set:
            try:
                delete_record(token, zone_id, record["id"])
                deleted += 1
            except CloudflareError as exc:
                log(f"  ✗ 删除 {fqdn} -> {content} 失败: {exc}")

    log(f"  Cloudflare 同步完成: 新建 {created} 条，删除 {deleted} 条，"
        f"共 {len(wanted)} 个 IP")
    return created, deleted
