#!/usr/bin/env python3
"""运营商识别（deploy.sh isp）测试。

判错的后果不小：整台机器会去扫错运营商的网段，或者干脆"无法判定"让人手填。
全部用假的 HTTP 响应，不访问网络。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo import deploytool                                        # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):                                    # noqa: ANN002
        return False


def fake_urlopen(payload, recorder: list | None = None):
    def _open(url, timeout=None, **kw):                           # noqa: ANN001, ANN003
        if recorder is not None:
            recorder.append(url)
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return FakeResponse(body.encode("utf-8") if isinstance(body, str) else body)
    return _open


def with_ipapi(payload, func):                                    # noqa: ANN001
    """把 urlopen 换成返回指定 ip-api 响应，并固定公网 IP 查询结果。"""
    real = deploytool.urlopen
    deploytool.urlopen = fake_urlopen(payload)                     # type: ignore[assignment]
    try:
        return func()
    finally:
        deploytool.urlopen = real                                  # type: ignore[assignment]


print("=" * 70)
print("运营商识别")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 英文归属名称")
# --------------------------------------------------------------------------
for isp, want in (("China Unicom", "lt"), ("CHINA UNICOM (Beijing)", "lt"),
                  ("CHINANET Guangdong", "dx"), ("China Telecom", "dx"),
                  ("China Mobile Communications", "yd"), ("CMNET", "yd")):
    got = with_ipapi({"status": "success", "isp": isp, "org": "", "as": "AS99999"},
                     lambda: deploytool.detect("1.2.3.4"))
    check(f"{isp} -> {want}", got["carrier"] == want,
          f"{got['carrier']} ({got['reason']})")

# --------------------------------------------------------------------------
print()
print("2) 中文归属名称（查询带 lang=zh-CN，ip-api 可能直接返回中文）")
# --------------------------------------------------------------------------
for isp, want in (("中国联通", "lt"), ("中国联通北京市分公司", "lt"),
                  ("中国电信", "dx"), ("中国电信集团公司", "dx"),
                  ("中国移动", "yd"), ("中国移动通信集团", "yd"),
                  ("中国铁通", "yd")):
    got = with_ipapi({"status": "success", "isp": isp, "org": "", "as": "AS99999"},
                     lambda: deploytool.detect("1.2.3.4"))
    check(f"{isp} -> {want}", got["carrier"] == want,
          f"{got['carrier']} ({got['reason']})")

# --------------------------------------------------------------------------
print()
print("3) 名称认不出时退回 ASN")
# --------------------------------------------------------------------------
for asn, want in (("AS4837", "lt"), ("AS4134", "dx"), ("AS9808", "yd"),
                  ("as4837", "lt")):
    got = with_ipapi({"status": "success", "isp": "Some Cloud Ltd", "org": "",
                      "as": asn},
                     lambda: deploytool.detect("1.2.3.4"))
    check(f"{asn} -> {want}", got["carrier"] == want, f"{got['carrier']} ({got['reason']})")

print()
print("4) 名称优先于 ASN（名称更具体）")
# --------------------------------------------------------------------------
got = with_ipapi({"status": "success", "isp": "China Unicom", "org": "", "as": "AS4134"},
                 lambda: deploytool.detect("1.2.3.4"))
check("名称说联通就用联通", got["carrier"] == "lt", f"{got['carrier']} ({got['reason']})")

print()
print("5) 都认不出 -> unknown 且给出原因")
# --------------------------------------------------------------------------
got = with_ipapi({"status": "success", "isp": "Amazon.com Inc.", "org": "AWS",
                  "as": "AS16509"},
                 lambda: deploytool.detect("1.2.3.4"))
check("carrier=unknown", got["carrier"] == "unknown", got["carrier"])
check("原因里带上 ISP 与 ASN", "AWS" in got["reason"] or "16509" in got["reason"],
      got["reason"])
check("label 是「未知」", got["label"] == "未知", got["label"])

print()
print("6) ip-api 返回失败状态")
# --------------------------------------------------------------------------
got = with_ipapi({"status": "fail", "message": "reserved range"},
                 lambda: deploytool.detect("1.2.3.4"))
check("返回 unknown", got["carrier"] == "unknown", got["carrier"])
check("原因带上 API 的消息", "reserved" in got["reason"], got["reason"])

print()
print("7) 查询异常（网络问题）不能崩")
# --------------------------------------------------------------------------
real = deploytool.urlopen


def boom(url, timeout=None, **kw):                                # noqa: ANN001, ANN003
    raise OSError("network down")


deploytool.urlopen = boom                                         # type: ignore[assignment]
try:
    got = deploytool.detect("1.2.3.4")
    check("异常被兜住", got["carrier"] == "unknown", got["carrier"])
    check("原因说明是异常", "异常" in got["reason"], got["reason"])
finally:
    deploytool.urlopen = real                                     # type: ignore[assignment]

# --------------------------------------------------------------------------
print()
print("8) 取不到公网 IP")
# --------------------------------------------------------------------------
deploytool.urlopen = boom                                         # type: ignore[assignment]
try:
    got = deploytool.detect("")
    check("返回 unknown", got["carrier"] == "unknown", got["carrier"])
    check("原因说明取不到公网 IP", "公网 IP" in got["reason"], got["reason"])
finally:
    deploytool.urlopen = real                                     # type: ignore[assignment]

# --------------------------------------------------------------------------
print()
print("9) public_ip 解析")
# --------------------------------------------------------------------------
got = with_ipapi("203.0.113.9\n", lambda: deploytool.public_ip())
check("纯文本 IP", got == "203.0.113.9", got)
got = with_ipapi("Your IP is 203.0.113.9\n", lambda: deploytool.public_ip())
check("带前后文的 IP", got == "203.0.113.9", got)
got = with_ipapi("<html>no ip here</html>", lambda: deploytool.public_ip())
check("没有 IP 时返回空串（不误抓）", got == "", repr(got))

# --------------------------------------------------------------------------
print()
print("10) 判定表本身")
# --------------------------------------------------------------------------
check("三个运营商都有中文与英文关键词",
      all(any(ord(ch) > 127 for kw in ws for ch in kw)
          and any(ord(ch) < 127 for kw in ws for ch in kw)
          for ws in deploytool.NAME_KEYWORDS.values()),
      str({k: len(v) for k, v in deploytool.NAME_KEYWORDS.items()}))
check("关键词表覆盖三个运营商",
      set(deploytool.NAME_KEYWORDS) == {"lt", "dx", "yd"}, str(set(deploytool.NAME_KEYWORDS)))
check("ASN 表里每个值都是合法运营商",
      set(deploytool.ASN_CARRIER.values()) <= {"lt", "dx", "yd"},
      str(set(deploytool.ASN_CARRIER.values())))
check("注释里不再引用不存在的文件",
      "isptool" not in (ROOT / "eo" / "deploytool.py").read_text(encoding="utf-8"),
      "已改为说明这里是唯一一份判定表")

print()
print("=" * 70)
if _failures:
    print(f"运营商识别测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("运营商识别测试全部通过")
sys.exit(0)
