"""运行结果通知（webhook / 飞书机器人）。

服务器上无人值守跑监控时，需要「出问题时能通知到我」。这里只依赖标准库，
把一轮结果 POST 成 JSON，兼容常见的 webhook 形态：

  * 飞书自定义机器人：``https://open.feishu.cn/open-apis/bot/v2/hook/xxx``
    body 用 ``{"msg_type": "text", "content": {"text": "..."}}``
  * 企业微信机器人：``.../cgi-bin/webhook/send?key=xxx``，``{"msgtype":"text","text":{"content":"..."}}``
  * 通用 webhook（Bark / 自建 / Slack 等）：直接发 ``{"text": "..."}``

不配置 webhook 时所有函数都是空操作。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Sequence

Log = Callable[[str], None]


def _render(outcomes: Sequence) -> str:
    lines = ["【边缘节点优选】"]
    for outcome in outcomes:
        platform = outcome.platform
        if outcome.error:
            lines.append(f"⚠ {platform.name}: 扫描失败 - {outcome.error}")
            continue
        top = "、".join(f"{p.ip}({p.latency_ms:.0f}ms)" for p in outcome.passed[:3]) or "无"
        lines.append(f"{platform.name}: 可用 {len(outcome.passed)}/{outcome.total}，最快 {top}")
    return "\n".join(lines)


def build_payload(webhook: str, text: str) -> dict:
    """按 webhook 类型生成对应的请求体。"""
    url = webhook.lower()
    if "feishu" in url or "larksuite" in url:
        return {"msg_type": "text", "content": {"text": text}}
    if "weixin" in url or "qyapi" in url:
        return {"msgtype": "text", "text": {"content": text}}
    return {"text": text}


def send(webhook: str | None, text: str, log: Log | None = None,
         timeout: float = 10.0) -> bool:
    """发送通知；未配置 webhook 时直接返回 True（不影响主流程）。"""
    log = log or (lambda _m: None)
    if not webhook:
        return True

    payload = json.dumps(build_payload(webhook, text), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(200).decode("utf-8", "replace")
        log(f"通知已发送（HTTP {response.status}）: {body[:120]}")
        return True
    except urllib.error.URLError as exc:
        log(f"通知发送失败: {exc}")
        return False
    except Exception as exc:  # pragma: no cover
        log(f"通知发送异常: {exc}")
        return False


def notify_outcomes(webhook: str | None, outcomes: Sequence, log: Log | None = None) -> bool:
    """汇报一轮扫描结果。"""
    return send(webhook, _render(outcomes), log=log)


def notify_alert(webhook: str | None, title: str, detail: str, log: Log | None = None) -> bool:
    """发送告警（可用节点过少、扫描失败、DNS 下发失败等）。"""
    return send(webhook, f"⚠ {title}\n{detail}", log=log)
