# -*- coding: utf-8 -*-
"""多平台优选配置（按「平台 × 运营商」组织）。

原来的 eo.py 只能盯一个站点（pcb.060730.xyz），现在把「平台」抽出来，
每个平台一份配置，一次运行即可同时为多个平台筛选可用节点。

★ 关于运营商（联通 / 电信 / 移动）
--------------------------------
同一个平台（如 EdgeOne）在不同运营商网络里的最优节点是不同的，
所以 DNS 记录也按运营商分开：`eo-lt`(联通) / `eo-dx`(电信) / `eo-yd`(移动)。

**关键：运营商不是靠代码区分的，而是靠「在哪台机器上跑」。**
延迟必须从该运营商网络内部测才有意义 —— 所以：

    联通服务器上跑  python run.py scan --network lt   -> 下发到 eo-lt
    电信服务器上跑  python run.py scan --network dx   -> 下发到 eo-dx
    移动服务器上跑  python run.py scan --network yd   -> 下发到 eo-yd

三个运营商的平台共用同一套网段文件和校验域名，只有 network 与 DNS 记录名不同。

常见写法对照：lt = 联通、dx = 电信、yd = 移动（可以在 core.NETWORK_LABELS 里改）。

怎么加一个新平台？复制一段下面的 dict，改 5 个地方即可：
    key / name        唯一标识与显示名
    host              该平台的校验域名（CNAME 指向该平台边缘节点）
    targets_file      该平台要扫描的网段文件（一行一个 IP / CIDR / 区间）
    output_file       优选结果写到哪里（一行一个 IP，可直接喂给 updatedns.py）
    network + dns     运营商代号与对应的 DNS 记录名

判断 host 是否正确的标准：本机用 【默认解析】 访问 https://<host> 返回 200。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from .core import Platform

ROOT = Path(__file__).resolve().parent.parent


def _path(relative: str) -> str:
    return str((ROOT / relative).resolve())


def _huawei_dns(zone: str, record: str) -> dict:
    """华为云 DNS 下发配置（现在三个平台统一走华为云）。"""
    return {
        "provider": "huawei",
        "zone_name": zone,
        "record_name": record,
        "region": HUAWEI_REGION,
        "access_key_id": HUAWEI_AK,
        "access_key_secret": HUAWEI_SK,
        "ttl": DNS_TTL,
        "batch_size": DNS_BATCH_SIZE,
    }


def _carrier_variants(base_key: str, name: str, host: str, targets: str,
                      outputs: dict[str, str], records: dict[str, str],
                      enabled: dict[str, bool], note: str,
                      scheme: str = "https", port: int = 443,
                      sample_per_24: int = 0,
                      extra: dict | None = None) -> dict[str, Platform]:
    """为一个平台生成 lt / dx / yd 三个运营商变体。

    outputs/records/enabled 三个 dict 都以运营商代号为键。
    sample_per_24 > 0 表示该平台网段很稀疏/很大，默认按每 /24 取 N 个代表 IP 抽样。
    """
    variants: dict[str, Platform] = {}
    for net in ("lt", "dx", "yd"):
        settings = dict(
            key=f"{base_key}-{net}",
            name=f"{name}·{ {'lt': '联通', 'dx': '电信', 'yd': '移动'}[net] }",
            host=host,
            network=net,
            port=port,
            scheme=scheme,
            path="/",
            status_codes=("200",),
            timeout=3.0,
            ping_timeout_ms=300,
            precheck_ping=False,
            retries=1,
            workers=80,
            sample_per_24=sample_per_24,
            targets_file=targets,
            output_file=outputs[net],
            history_file=str(Path(outputs[net]).parent / "history"
                             / (Path(outputs[net]).stem + ".jsonl")),
            enabled=enabled.get(net, False),
            dns=_huawei_dns(records["zone"], records[net]),
            note=note,
        )
        if extra:
            settings.update(extra)
        variants[settings["key"]] = Platform(**settings)
    return variants


# ==========================================================================
# DNS 下发配置（华为云 DNS）
# --------------------------------------------------------------------------
# ★ 密钥绝不写进代码（这个仓库是公开的）。按下面顺序读取：
#     1. 环境变量 HUAWEI_AK / HUAWEI_SK（推荐，写在 .env 或 compose 里）
#     2. Web 控制台「DNS 下发」里填的密钥（存到 config/config.json，已 gitignore）
#   两者都没有时，DNS 下发会报错提示，但优选功能不受影响。
#
# 建议用只有 DNS 权限的子账号密钥，别用主账号 AK。
# ==========================================================================
HUAWEI_AK = os.environ.get("HUAWEI_AK", "").strip()
HUAWEI_SK = os.environ.get("HUAWEI_SK", "").strip()
HUAWEI_REGION = os.environ.get("HUAWEI_REGION", "cn-north-4").strip() or "cn-north-4"

# 每个 A 记录集最多放多少个 IP（华为云单记录集上限 50，保持 20~50 较稳）
DNS_BATCH_SIZE = 50
DNS_TTL = 1

# 默认只启用联通那一套（在联通机器上跑）；电信/移动机器上把对应 enabled 打开
DEFAULT_CARRIER = "lt"

# CloudFront 没有「每个分配域名不同」的限制：边缘节点对所有分配域名一视同仁，
# 所以随便用一个真实存在的 CloudFront 域名就能当校验目标，这里默认用 AWS 自己的
# 静态站点（走 CloudFront，长期稳定）。有自己的分配域名时替换掉即可。
CFT_PROBE_HOST = "d1.awsstatic.com"


# ==========================================================================
# 平台定义（每个平台 × 3 个运营商）
# ==========================================================================
PLATFORMS: dict[str, Platform] = {}

# ---------------- 腾讯云 EdgeOne (EO) ----------------
PLATFORMS.update(_carrier_variants(
    base_key="eo",
    name="EdgeOne",
    host="pcb.060730.xyz",
    targets=_path("targets/eo.txt"),
    outputs={
        "lt": _path("results/eo-lt.txt"),
        "dx": _path("results/eo-dx.txt"),
        "yd": _path("results/eo-yd.txt"),
    },
    records={
        "zone": "1.060730.xyz",
        "lt": "eo-lt.1.060730.xyz",
        "dx": "eo-dx.1.060730.xyz",
        "yd": "eo-yd.1.060730.xyz",
    },
    enabled={"lt": True, "dx": False, "yd": False},
    note="腾讯云 EdgeOne；沿用原 eo.py 的网段与校验域名",
))

# ---------------- 阿里云 ESA (边缘安全加速) ----------------
# ⚠ 该域名目前【正在申请 HTTPS 证书】：实测 163.181.78.0/24 全部 40 个节点都回
#   "tlsv1 alert internal error"（SSL alert 80），80 端口则正常 200。
#   证书下来后把 scheme/port 改成 https/443 即可（下面已用常量标注）。
ESA_SCHEME = "http"
ESA_PORT = 80

PLATFORMS.update(_carrier_variants(
    base_key="esa",
    name="ESA",
    host="blog.157658.xyz",
    targets=_path("targets/esa.txt"),
    outputs={
        "lt": _path("results/esa-lt.txt"),
        "dx": _path("results/esa-dx.txt"),
        "yd": _path("results/esa-yd.txt"),
    },
    records={
        "zone": "157658.xyz",
        "lt": "esa-lt.157658.xyz",
        "dx": "esa-dx.157658.xyz",
        "yd": "esa-yd.157658.xyz",
    },
    enabled={"lt": True, "dx": False, "yd": False},
    note="阿里云 ESA；网段由 esa-ranges 从 BGP 推导；当前用 http(80) 优选",
    scheme=ESA_SCHEME,
    port=ESA_PORT,
    sample_per_24=1,          # 2191 个网段 / 1419 万地址，必须抽样
))

# ---------------- 亚马逊 CloudFront (CFT) ----------------
PLATFORMS.update(_carrier_variants(
    base_key="cft",
    name="CloudFront",
    host=CFT_PROBE_HOST,
    targets=_path("targets/cft.txt"),
    outputs={
        "lt": _path("results/cft-lt.txt"),
        "dx": _path("results/cft-dx.txt"),
        "yd": _path("results/cft-yd.txt"),
    },
    records={
        "zone": "1.060730.xyz",
        "lt": "cft-lt.1.060730.xyz",
        "dx": "cft-dx.1.060730.xyz",
        "yd": "cft-yd.1.060730.xyz",
    },
    enabled={"lt": True, "dx": False, "yd": False},
    note="Amazon CloudFront；网段用 python run.py ranges --source cloudfront 自动拉取",
    sample_per_24=1,          # 官方网段含 /15 这种大段，必须抽样
))


NETWORKS = ("lt", "dx", "yd")           # 联通 / 电信 / 移动
SERVICES = ("eo", "esa", "cft")         # 各平台的基础 key


def env_platform_keys() -> list[str]:
    """按环境变量决定「这台机器该跑哪些平台」。

    优先级：
      1. EO_PLATFORMS —— 显式列表（deploy.sh / onekey.sh 会写入），
         例如 ``eo-lt,esa-lt,cft-lt``；
      2. EO_NETWORK   —— 本机运营商（lt/dx/yd），推导出该运营商的 3 个变体；
      3. 都没有       —— 返回空列表，由调用方回退到代码里的默认启用项。

    为什么需要第 2 条：EO_NETWORK 是「这台机器是谁」的声明。若只认
    EO_PLATFORMS，那么有人在 .env 里只改了 EO_NETWORK=dx（很自然的做法），
    程序仍会去跑默认的联通平台 —— 在电信机器上测联通节点毫无意义。
    """
    raw = (os.environ.get("EO_PLATFORMS") or "").strip()
    if raw:
        return [k.strip() for k in raw.replace(",", " ").split() if k.strip()]
    carrier = (os.environ.get("EO_NETWORK") or "").strip().lower()
    if carrier in NETWORKS:
        return [f"{svc}-{carrier}" for svc in SERVICES]
    return []


def get_platforms(keys: list[str] | None = None, include_disabled: bool = False,
                  networks: list[str] | None = None) -> list[Platform]:
    """按 key / 运营商取平台。

    keys 为空表示全部（默认只取启用的）；networks 用于按运营商筛选，
    例如 networks=["lt"] 只取联通那一套。
    两者都未指定时会参考 EO_PLATFORMS / EO_NETWORK（见 env_platform_keys）。

    被明确点名的平台一律视为「启用」：调用方（-p / -n / EO_PLATFORMS /
    EO_NETWORK）已经表达了「我就要跑这些」的意图，此时若还用代码里的
    enabled 默认值把它们过滤掉，扫描就会静默什么都不做 ——
    而代码里默认只有 lt 三件套是启用的。
    """
    explicit = bool(keys or networks)
    if not keys and not networks and not include_disabled:
        keys = env_platform_keys() or None
        explicit = bool(keys)

    if keys:
        # 允许写基础 key（eo / esa / cft）——CLI 帮助与文档里一直是这么举例的
        # （`run.py scan -p eo esa`、`update_dns.py --platform eo esa cft`），
        # 但平台字典里只有 eo-lt / eo-dx 这类变体，直接用基础 key 会报「未知平台」。
        # 这里按运营商展开：
        #   * 指定了 -n/--network  -> 用那几个运营商（-p eo -n dx => eo-dx）
        #   * 否则                  -> 用本机运营商（EO_NETWORK，缺省 lt）
        if networks:
            carriers = [n.strip().lower() for n in networks if str(n).strip()]
        else:
            carriers = [(os.environ.get("EO_NETWORK") or DEFAULT_CARRIER).strip().lower()
                        or DEFAULT_CARRIER]

        expanded: list[str] = []
        unknown: list[str] = []
        for raw_key in keys:
            key = str(raw_key).strip()
            if not key:
                continue
            if key in PLATFORMS:
                expanded.append(key)
            elif key in SERVICES:
                expanded.extend(f"{key}-{c}" for c in carriers)
            else:
                unknown.append(key)
        if unknown:
            raise KeyError(f"未知平台: {', '.join(unknown)}"
                           f"（可选: {', '.join(PLATFORMS)}；也可用基础名 "
                           f"{'/'.join(SERVICES)} 让程序按运营商自动展开）")
        # 去重并保持顺序
        seen: set[str] = set()
        keys = [k for k in expanded if not (k in seen or seen.add(k))]
        selected = [PLATFORMS[k] for k in keys]
    elif networks:
        # 按运营商筛选时以【全部变体】为基础：若先只取"默认启用的平台"
        # （代码里只有 lt 三件套），再筛 dx/yd 就会得到空列表 ——
        # 也就是「电信机上跑 --network dx 什么都不扫」。
        selected = list(PLATFORMS.values())
    else:
        selected = [p for p in PLATFORMS.values() if include_disabled or p.enabled]

    if networks:
        wanted = {n.lower() for n in networks}
        selected = [p for p in selected if (p.network or DEFAULT_CARRIER) in wanted]

    if explicit:
        selected = [replace(p, enabled=True) for p in selected]
    return selected


def carrier_keys(base_key: str) -> list[str]:
    """返回某个平台的全部运营商变体 key，例如 eo -> [eo-lt, eo-dx, eo-yd]。"""
    return [f"{base_key}-{net}" for net in ("lt", "dx", "yd")]
