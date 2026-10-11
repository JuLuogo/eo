"""部署辅助小工具（供 deploy.sh 调用，避免在 bash 里写脆弱的 sed/grep）。

两个功能：
  1. ``isp``    识别本机（或指定 IP）的运营商：联通 lt / 电信 dx / 移动 yd；
  2. ``env``    .env 文件的可靠读写与注释剥离。

为什么单独写 Python：.env 里经常有行尾注释（``EO_WEB_PORT=8787  # 说明``），
用 grep/sed 取值会把注释一起带出来，导致「端口必须是数字」这类莫名报错。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import urlopen

PUBLIC_IP_APIS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "http://members.3322.org/dyndns/getip",
]

# 运营商 ASN 归属（不含中立交换中心），与 eo/isptool.py 保持一致
ASN_CARRIER = {
    4134: "dx", 4809: "dx", 4812: "dx", 4811: "dx", 4816: "dx", 23724: "dx",
    58466: "dx", 140061: "dx",
    4837: "lt", 4808: "lt", 17621: "lt", 17622: "lt", 17623: "lt",
    9929: "lt", 17816: "lt", 134810: "lt",
    9808: "yd", 56040: "yd", 56041: "yd", 56042: "yd", 56046: "yd",
    56048: "yd", 24400: "yd", 132525: "yd",
}
LABELS = {"lt": "联通", "dx": "电信", "yd": "移动", "unknown": "未知"}


# --------------------------------------------------------------------------
# 运营商识别
# --------------------------------------------------------------------------
def public_ip(timeout: float = 8.0) -> str:
    for url in PUBLIC_IP_APIS:
        try:
            with urlopen(url, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
            for token in text.replace("\n", " ").split():
                if token.count(".") == 3 and all(p.isdigit() for p in token.split(".")):
                    return token
        except Exception:
            continue
    return ""


def detect(ip: str = "", timeout: float = 8.0) -> dict:
    """识别运营商，返回 {carrier, label, ip, asn, isp, reason}。"""
    target = ip or public_ip(timeout)
    info = {"carrier": "unknown", "label": LABELS["unknown"], "ip": target,
            "asn": "", "isp": "", "reason": ""}
    if not target:
        info["reason"] = "取不到公网 IP"
        return info

    try:
        url = (f"http://ip-api.com/json/{target}"
               f"?fields=status,isp,org,as,query&lang=zh-CN")
        with urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        if data.get("status") != "success":
            info["reason"] = f"查询失败: {data.get('message', 'unknown')}"
            return info
    except Exception as exc:
        info["reason"] = f"查询异常: {exc}"
        return info

    holder = f"{data.get('isp', '')} {data.get('org', '')}".strip()
    as_text = data.get("as") or ""
    info["isp"], info["asn"] = holder, as_text
    lowered = f"{holder} {as_text}".lower()

    # 1) 归属名称关键词
    if any(w in lowered for w in ("unicom", "china169", "netcom")):
        info.update(carrier="lt", label=LABELS["lt"], reason="归属名称含联通特征")
    elif any(w in lowered for w in ("chinanet", "chinatelecom", "china telecom")):
        info.update(carrier="dx", label=LABELS["dx"], reason="归属名称含电信特征")
    elif any(w in lowered for w in ("cmnet", "chinamobile", "china mobile")):
        info.update(carrier="yd", label=LABELS["yd"], reason="归属名称含移动特征")
    else:
        # 2) ASN 归属
        match = re.match(r"AS(\d+)", as_text.strip(), flags=re.I)
        if match and int(match.group(1)) in ASN_CARRIER:
            carrier = ASN_CARRIER[int(match.group(1))]
            info.update(carrier=carrier, label=LABELS[carrier],
                        reason=f"AS{match.group(1)} 属于{LABELS[carrier]}")
        else:
            info["reason"] = f"无法判定（ISP={holder or '未知'}, AS={as_text or '未知'}）"
    return info


# --------------------------------------------------------------------------
# .env 读写
# --------------------------------------------------------------------------
def env_parse(path: Path) -> dict[str, str]:
    """解析 .env，自动剥掉 BOM、行尾注释与引号。

    用 utf-8-sig 读取：Windows 上用记事本/PowerShell 编辑过的 .env 常带 BOM，
    若不处理，第一行的键会变成 "\\ufeffEO_RATE"，导致该变量静默失效。
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("\ufeff")
        value = value.strip()
        # 先处理 "KEY=   # 只是说明" 这种「空值 + 行尾说明」的写法：
        # 此时 # 并不是注释分隔符，而是紧跟等号，应当视为空值。
        if value.startswith("#"):
            value = ""
        # 再去掉真正的行尾注释：空白 + # 之后的内容（值里真需要 # 请加引号）
        elif not (value.startswith('"') or value.startswith("'")):
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def env_quote(value: str) -> str:
    """包含空格或 # 的值加引号，避免下次解析时被截断。"""
    if value == "":
        return ""
    if re.search(r"[\s#\"']", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def env_set(path: Path, key: str, value: str) -> None:
    """设置/更新 .env 里的键（保留其它行与注释）。"""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new_line = f"{key}={env_quote(value)}"
    replaced = False
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署辅助工具")
    sub = parser.add_subparsers(dest="command")

    isp = sub.add_parser("isp", help="识别本机运营商")
    isp.add_argument("--ip", default="")
    isp.add_argument("--quiet", action="store_true", help="只输出 lt/dx/yd/unknown")
    isp.add_argument("--label", action="store_true", help="只输出中文名")
    isp.add_argument("--json", action="store_true")

    env = sub.add_parser("env", help="读写 .env")
    env.add_argument("action", choices=["get", "set"])
    env.add_argument("key")
    env.add_argument("value", nargs="?")
    env.add_argument("--file", default=".env")

    args = parser.parse_args(argv)
    if args.command == "isp":
        info = detect(args.ip)
        if args.quiet:
            print(info["carrier"])
        elif args.label:
            print(info["label"])
        elif args.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            print(f"公网 IP : {info['ip'] or '未知'}")
            print(f"运营商  : {info['label']}（{info['carrier']}）")
            print(f"ISP/AS  : {info['isp'] or '未知'} / {info['asn'] or '未知'}")
            if info["reason"]:
                print(f"判据    : {info['reason']}")
        return 0 if info["carrier"] != "unknown" else 1

    if args.command == "env":
        path = Path(args.file)
        if args.action == "get":
            print(env_parse(path).get(args.key, ""))
            return 0
        if args.value is None:
            print("set 需要提供 value", file=sys.stderr)
            return 2
        env_set(path, args.key, args.value)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
