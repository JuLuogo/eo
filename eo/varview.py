"""常用运行变量的集中视图：把 .env 与 config.json 的取值摊开，说明谁生效。

用途是给 Web 控制台的「常用设置」面板用：

  * 有些变量既能在 .env 里配，也能在网页上改。实际生效规则是
    「config.json 里有该字段就用网页的值，否则用 .env / 默认值」；
  * 但用户很难记住哪些字段已经写进 config.json，于是容易出现
    「我明明改了 .env 怎么不生效」的困惑。

所以这里把三样东西并排给出来：默认值 / .env 值 / 网页值，并明确标出
实际生效的是哪一个、来源是什么。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .deploytool import env_parse
from .config_store import DEFAULT_CONFIG_PATH

# 面板要展示的常用变量：key -> (中文名, 单位/说明, 是否敏感)
COMMON_VARS = [
    ("rate",        "并发连接上限",   "全平台合计，连接/秒；0 = 不限速"),
    ("interval",    "轮询间隔",       "秒；改完下一轮生效"),
    ("window",      "时间窗口",       "如 00:00-07:00，留空 = 全天"),
    ("workers",     "单平台并发数",   "每个平台同时发起的探测线程数"),
    ("timeout",     "单次探测超时",   "秒"),
    ("retries",     "失败重试次数",   "抗瞬时丢包"),
    ("log_buffer",  "日志保留条数",   "内存环形缓冲，不落盘"),
    ("dns_mode",    "DNS 下发模式",   "none / dry-run / push"),
    ("webhook",     "异常通知地址",   "飞书/企业微信/通用 webhook"),
    ("auto_run",    "启动即开始循环", "true / false"),
]

ENV_MAP = {
    "rate": "EO_RATE",
    "interval": "EO_INTERVAL",
    "window": "EO_WINDOW",
    "workers": "EO_WORKERS",
    "timeout": "EO_TIMEOUT",
    "retries": "EO_RETRIES",
    "log_buffer": "EO_LOG_BUFFER",
    "dns_mode": "EO_DNS_SYNC",
    "webhook": "EO_WEBHOOK",
    "auto_run": "EO_AUTO_RUN",
}

# 平台/部署相关（只读展示）
INFO_VARS = [
    ("EO_NETWORK", "本机运营商", "lt=联通 / dx=电信 / yd=移动"),
    ("EO_WEB_PORT", "Web 端口", ""),
    ("EO_PLATFORMS", "启用的平台", "平台+运营商组合"),
    ("EO_MODE", "运行模式", "web / monitor / scan"),
    ("TZ", "时区", "影响时间窗口判断"),
    ("EO_WEB_TOKEN", "访问令牌", "敏感，仅显示是否已设置"),
]


@dataclass
class VarView:
    key: str
    label: str
    note: str
    default: object
    env_value: object
    web_value: object
    effective: object
    source: str          # default / env / web

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "note": self.note,
            "default": self.default,
            "env_value": self.env_value,
            "web_value": self.web_value,
            "effective": self.effective,
            "source": self.source,
            "env_key": ENV_MAP.get(self.key, ""),
        }


def _defaults() -> dict:
    from .config_store import AppConfig

    base = AppConfig()
    return {key: getattr(base, key) for key, _, _ in COMMON_VARS}


def vars_view(config, env_path: Path | None = None,
              config_path: Path | None = None,
              explicit: set[str] | None = None) -> list[dict]:
    """返回常用变量的三方对照视图。

    config      : AppConfig（网页上的配置，已按优先级合并好）
    env_path    : .env 路径
    config_path : config.json 路径（用于读出网页上保存的值）
    explicit    : 网页上「显式设置过」的字段集合。必须传 ConfigStore.explicit ——
                  不能靠「键是否存在于 config.json」判断，因为 save() 会把整个
                  配置连同默认值一起落盘，那样所有键看起来都像网页设置过。
    """
    defaults = _defaults()
    env_values: dict = {}
    if env_path and Path(env_path).exists():
        env_values = {k: v for k, v in env_parse(Path(env_path)).items() if v != ""}

    web_raw: dict = {}
    try:
        import json

        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if path.exists():
            web_raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        web_raw = {}

    explicit = set(explicit or [])
    result: list[VarView] = []
    for key, label, note in COMMON_VARS:
        env_key = ENV_MAP.get(key, "")
        env_value = env_values.get(env_key, "")
        web_value = web_raw.get(key, "") if key in explicit else ""
        effective = getattr(config, key, None)
        if key in explicit:
            source = "web"
        elif env_value != "":
            source = "env"
        else:
            source = "default"
        result.append(VarView(
            key=key,
            label=label,
            note=note,
            default=defaults.get(key),
            env_value=env_value,
            web_value=web_value,
            effective=effective,
            source=source,
        ))
    return [view.to_dict() for view in result]


def info_view(env_path: Path | None = None) -> list[dict]:
    """部署相关变量的只读视图（敏感值只显示是否已设置）。"""
    env_values = env_parse(Path(env_path)) if env_path and Path(env_path).exists() else {}
    rows = []
    for env_key, label, note in INFO_VARS:
        value = env_values.get(env_key, os.environ.get(env_key, ""))
        if env_key == "EO_WEB_TOKEN":
            value = "已设置" if value else "未设置"
        rows.append({"key": env_key, "label": label, "note": note, "value": value})
    return rows
