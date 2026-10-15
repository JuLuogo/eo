"""运行配置的持久化（Web 界面与容器共用的 config.json）。

设计要点：
  * 配置保存在 ``config/config.json``（容器里挂卷持久化），
    与代码里的 platforms.py 分离 —— 用户在网页上改的东西不会污染代码；
  * 密钥（AK/SK、Cloudflare Token、页面访问令牌）以掩码形式返回给前端，
    只有前端提交了「新值」才会覆盖，避免误把 ``******`` 存回去；
  * 所有字段都有默认值，缺字段自动补齐，配置损坏时能自动回退。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

MASK = "******"

# 记录「哪些键是网页上显式设置过的」的元数据字段。
# 关键：不能简单地「config.json 里有这个键就算网页设置过」——因为 save() 会把
# 整个配置（含默认值）落盘，那样第一次保存任何东西之后，.env 里配的值就全被
# 默认值盖住了。所以这里单独维护一个显式设置过的键列表。
EXPLICIT_KEY = "_explicit"

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("EO_CONFIG", str(Path(__file__).resolve().parent.parent / "config" / "config.json"))
)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return MASK
    return f"{value[:4]}{MASK}{value[-4:]}"


@dataclass
class PlatformSettings:
    """单个平台在网页上可调的设置（None 表示沿用 platforms.py 里的值）。"""

    key: str
    enabled: bool = True
    host: str = ""
    scheme: str = ""                   # http / https；空=用 platforms.py 的值
    targets_file: str = ""
    output_file: str = ""
    workers: int | None = None
    timeout: float | None = None
    retries: int | None = None
    sample_per_24: int | None = None    # 粗扫抽样：每 /24 取几个代表 IP
    http_path: str = "/"
    status_codes: str = "200"          # 逗号分隔
    ping: bool = False
    auto_targets: str = ""             # 如 cloudfront / cloudflare，自动拉取网段

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DnsSettings:
    """某个平台下发的 DNS 配置。"""

    platform: str
    provider: str = "huawei"           # huawei / cloudflare / none
    zone_name: str = ""
    record_name: str = ""
    region: str = "cn-north-4"
    access_key_id: str = ""
    access_key_secret: str = ""
    api_token: str = ""                # Cloudflare API Token
    zone_id: str = ""                  # Cloudflare Zone ID（可选，自动查询）
    ttl: int = 1
    batch_size: int = 50
    enabled: bool = False
    # 记录值为固定 CNAME 时可直接指定（部分平台只能 CNAME 接入）
    cname: str = ""

    def to_dict(self, mask_secrets: bool = True) -> dict:
        data = asdict(self)
        if mask_secrets:
            data["access_key_id"] = _mask(self.access_key_id)
            data["access_key_secret"] = _mask(self.access_key_secret)
            data["api_token"] = _mask(self.api_token)
        return data


@dataclass
class AppConfig:
    """整站配置。"""

    rate: float = 100.0                # 全平台合计连接上限（连接/秒）
    interval: int = 300                # 轮询间隔（秒）
    window: str = ""                   # 时间窗口，如 00:00-07:00
    workers: int = 80
    timeout: float = 3.0
    retries: int = 1
    ping: bool = False
    verbose: bool = False
    dns_mode: str = "none"             # none / dry-run / push
    dns_confirm: bool = False          # 与 dns_mode=push 配合，双重确认
    webhook: str = ""
    notify_always: bool = False
    log_buffer: int = 500              # 实时日志保留条数（环形缓冲）
    web_token: str = ""                # 网页访问令牌，空则自动生成
    auto_run: bool = False             # 容器启动后是否自动开始循环
    platforms: dict[str, PlatformSettings] = field(default_factory=dict)
    dns: dict[str, DnsSettings] = field(default_factory=dict)

    # ---- 序列化 ----
    def to_dict(self, mask_secrets: bool = True) -> dict:
        data = {
            "rate": self.rate,
            "interval": self.interval,
            "window": self.window,
            "workers": self.workers,
            "timeout": self.timeout,
            "retries": self.retries,
            "ping": self.ping,
            "verbose": self.verbose,
            "dns_mode": self.dns_mode,
            "dns_confirm": self.dns_confirm,
            "webhook": self.webhook,
            "notify_always": self.notify_always,
            "log_buffer": self.log_buffer,
            "auto_run": self.auto_run,
            "web_token_set": bool(self.web_token),
            "web_token_masked": _mask(self.web_token) if self.web_token else "",
            "platforms": {k: v.to_dict() for k, v in self.platforms.items()},
            "dns": {k: v.to_dict(mask_secrets) for k, v in self.dns.items()},
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        config = cls()
        for key in ("rate", "interval", "window", "workers", "timeout", "retries",
                    "ping", "verbose", "dns_mode", "dns_confirm", "webhook",
                    "notify_always", "log_buffer", "web_token", "auto_run"):
            if key in data and data[key] is not None:
                setattr(config, key, data[key])

        for key, value in (data.get("platforms") or {}).items():
            if isinstance(value, dict):
                config.platforms[key] = PlatformSettings(
                    key=key,
                    enabled=bool(value.get("enabled", True)),
                    host=value.get("host", ""),
                    scheme=value.get("scheme", ""),
                    targets_file=value.get("targets_file", ""),
                    output_file=value.get("output_file", ""),
                    workers=value.get("workers"),
                    timeout=value.get("timeout"),
                    retries=value.get("retries"),
                    sample_per_24=value.get("sample_per_24"),
                    http_path=value.get("http_path", "/"),
                    status_codes=value.get("status_codes", "200"),
                    ping=bool(value.get("ping", False)),
                    auto_targets=value.get("auto_targets", ""),
                )
        for key, value in (data.get("dns") or {}).items():
            if isinstance(value, dict):
                config.dns[key] = DnsSettings(
                    platform=key,
                    provider=value.get("provider", "huawei"),
                    zone_name=value.get("zone_name", ""),
                    record_name=value.get("record_name", ""),
                    region=value.get("region", "cn-north-4"),
                    access_key_id=value.get("access_key_id", ""),
                    access_key_secret=value.get("access_key_secret", ""),
                    api_token=value.get("api_token", ""),
                    zone_id=value.get("zone_id", ""),
                    ttl=int(value.get("ttl", 1)),
                    batch_size=int(value.get("batch_size", 50)),
                    enabled=bool(value.get("enabled", False)),
                    cname=value.get("cname", ""),
                )
        return config


def env_file_path() -> Path:
    """项目根目录下的 .env 路径（可用 EO_ENV_FILE 覆盖）。"""
    override = os.environ.get("EO_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path | None = None) -> list[str]:
    """把项目 .env 加载进进程环境变量，返回被加载的键名列表。

    为什么需要：deploy.sh 用 systemd 的 EnvironmentFile 注入变量，但
    ``./deploy.sh run``（前台调试）、直接 ``python run.py``、以及容器里
    挂载的 .env 都不会自动进环境。这里统一处理，保证「.env 里写的值
    确实生效」，而不是只写了个文件却没人读。

    优先级：**已存在的进程环境变量优先**（staging/CI 用真实 env 覆盖 .env 是常规做法）。
    """
    target = Path(path) if path else env_file_path()
    if not target.exists():
        return []

    from .deploytool import env_parse

    loaded: list[str] = []
    for key, value in env_parse(target).items():
        if not key or value == "":
            continue
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def env_overrides() -> dict:
    """把 EO_* 环境变量翻译成配置项。

    这些环境变量（在 docker-compose.yml / .env 里写的）用于**首次启动或者
    Web 上没有该字段的记录时**提供默认值；一旦网页上改过并存进 config.json，
    就以 config.json 为准 —— 否则每次重启都会被环境变量覆盖回去。
    """
    mapping = {
        "EO_RATE": ("rate", float),
        "EO_INTERVAL": ("interval", int),
        "EO_WINDOW": ("window", str),
        "EO_WORKERS": ("workers", int),
        "EO_TIMEOUT": ("timeout", float),
        "EO_RETRIES": ("retries", int),
        "EO_LOG_BUFFER": ("log_buffer", int),
        "EO_WEBHOOK": ("webhook", str),
        "EO_AUTO_RUN": ("auto_run", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
        "EO_DNS_SYNC": ("dns_mode", str),
        "EO_PING": ("ping", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
    }
    result: dict = {}
    for env_key, (field, caster) in mapping.items():
        raw = os.environ.get(env_key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            result[field] = caster(raw)
        except (TypeError, ValueError):
            continue
    return result


class ConfigStore:
    """线程安全的配置读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DEFAULT_CONFIG_PATH)
        self._lock = threading.RLock()
        self.explicit: set[str] = set()      # 网页上显式设置过的字段
        # 先把 .env 载入环境，否则「.env 里配了值却没生效」会让人很困惑
        self.dotenv_keys = load_dotenv()
        self.config = self.load()

    # ---- 磁盘 ----
    def load(self) -> AppConfig:
        raw_data: dict = {}
        if self.path.exists():
            try:
                raw_data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                raw_data = {}

        try:
            config = AppConfig.from_dict(raw_data) if raw_data else AppConfig()
        except Exception:
            config = AppConfig()

        self.explicit = set(raw_data.get(EXPLICIT_KEY) or [])

        # 环境变量只作用于「网页上没有显式设置过」的字段
        for field, value in env_overrides().items():
            if field not in self.explicit:
                setattr(config, field, value)

        # 访问令牌：环境变量优先，其次文件，最后自动生成
        created_token = False
        env_token = os.environ.get("EO_WEB_TOKEN", "").strip()
        if env_token:
            config.web_token = env_token
        elif not config.web_token:
            config.web_token = secrets.token_urlsafe(18)
            created_token = True

        self.config = config
        if created_token or not self.path.exists():
            self.save(config)
        return config

    def save(self, config: AppConfig | None = None) -> None:
        config = config or self.config
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._save_payload(config)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)     # 里面有密钥
            except OSError:
                pass

    # ---- 访问 ----
    def get(self) -> AppConfig:
        with self._lock:
            return self.config

    def ensure_dns_entries(self, defaults: Mapping[str, Mapping[str, Any]]) -> bool:
        """为网页补齐缺失的 DNS 配置项（值取自代码里的平台定义）。

        为什么需要：网页的「DNS 下发」面板只渲染 config.dns 里**已有**的条目
        （renderDns 遍历 cfg.dns），而 config.json 初始没有 dns 字段，
        于是面板一片空白、用户根本没有入口去配置 DNS。

        只在内存里补（不落盘）：这些值本身可由 platforms.py 推导，
        用户真正在网页上改过之后才随下次 save() 写入文件。
        返回是否补过条目。
        """
        added = False
        for key, dns in (defaults or {}).items():
            if not dns or key in self.config.dns:
                continue
            provider = str(dns.get("provider") or "none")
            self.config.dns[key] = DnsSettings(
                platform=key,
                provider=provider,
                zone_name=str(dns.get("zone_name") or ""),
                record_name=str(dns.get("record_name") or ""),
                region=str(dns.get("region") or ""),
                zone_id=str(dns.get("zone_id") or ""),
                cname=str(dns.get("cname") or ""),
                ttl=int(dns.get("ttl") or 1),
                batch_size=int(dns.get("batch_size") or 50),
                # 代码里已配置 provider 的平台，网页上默认勾选启用
                enabled=bool(provider and provider != "none"),
            )
            added = True
        return added

    def update(self, patch: dict) -> AppConfig:
        """按前端提交的 patch 更新配置；掩码值不会覆盖真实密钥。"""
        with self._lock:
            config = self.config

            for key in ("rate", "interval", "window", "workers", "timeout", "retries",
                        "ping", "verbose", "dns_mode", "dns_confirm", "webhook",
                        "notify_always", "log_buffer", "auto_run"):
                if key in patch and patch[key] is not None:
                    setattr(config, key, patch[key])
                    self.explicit.add(key)

            for key, value in (patch.get("platforms") or {}).items():
                if not isinstance(value, dict):
                    continue
                current = config.platforms.get(key) or PlatformSettings(key=key)
                for field_name in ("enabled", "host", "scheme", "targets_file", "output_file",
                                   "workers", "timeout", "retries", "sample_per_24",
                                   "http_path", "status_codes", "ping", "auto_targets"):
                    if field_name in value and value[field_name] is not None:
                        setattr(current, field_name, value[field_name])
                config.platforms[key] = current

            for key, value in (patch.get("dns") or {}).items():
                if not isinstance(value, dict):
                    continue
                current = config.dns.get(key) or DnsSettings(platform=key)
                for field_name in ("provider", "zone_name", "record_name", "region",
                                   "ttl", "batch_size", "enabled", "cname", "zone_id"):
                    if field_name in value and value[field_name] is not None:
                        setattr(current, field_name, value[field_name])
                # 密钥：只在提交了非掩码的新值时才覆盖
                for secret_field in ("access_key_id", "access_key_secret", "api_token"):
                    submitted = value.get(secret_field)
                    if submitted and MASK not in str(submitted):
                        setattr(current, secret_field, submitted)
                config.dns[key] = current

            # 允许网页重置访问令牌
            if patch.get("rotate_web_token"):
                config.web_token = secrets.token_urlsafe(18)

            self.config = config
            self.save(config)
            return config

    @property
    def web_token(self) -> str:
        return self.config.web_token

    # ------------------------------------------------------------------
    # 落盘 / 读取
    # ------------------------------------------------------------------
    def _save_payload(self, config: AppConfig) -> dict:
        """落盘用的完整配置（含真实密钥与访问令牌，文件权限 600）。

        注意与 to_dict(mask_secrets=True) 的区别：那个是给接口/页面回显用的，
        会把密钥替换成掩码；**如果拿掩码版本落盘，密钥和访问令牌就永久丢了**
        （Web 端重启后令牌会变，前端存下的令牌立即失效）。
        """
        payload = config.to_dict(mask_secrets=False)
        payload["web_token"] = config.web_token
        # 记录哪些字段是网页显式设置过的，供下次 load 判断 env 是否该生效
        payload[EXPLICIT_KEY] = sorted(self.explicit)
        return payload

    def save(self, config: AppConfig | None = None) -> None:
        config = config or self.config
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._save_payload(config)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)     # 里面有密钥
            except OSError:
                pass
