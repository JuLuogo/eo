"""Web 管理界面（仅用标准库，容器里不用装额外依赖）。

功能：
  * 配置管理：并发限速、轮询间隔、时间窗口、逐平台网段/域名、DNS 服务商与令牌
  * 运行控制：开始/停止循环、立即跑一轮、手动预览 DNS 下发
  * 实时日志：环形缓冲（默认 500 条，超出自动丢弃最旧的），支持增量拉取与 SSE 推送
  * 进度：进度条 + 每平台明细（已测/可用/总量）

安全：默认绑定 0.0.0.0，但所有接口都要 Bearer Token（首次启动自动生成，
写在 config/config.json，也可用环境变量 EO_WEB_TOKEN 指定）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config_store import AppConfig, ConfigStore, PlatformSettings
from .core import Platform, network_label, safe_log
from .platforms import PLATFORMS
from .runner import CycleRunner, RunConfig, run_once

DEFAULT_LOG_LINES = 500


# --------------------------------------------------------------------------
# 日志环形缓冲
# --------------------------------------------------------------------------
class LogBuffer:
    """固定容量的日志环形缓冲：只保留最近 N 条，不落盘、不占空间。"""

    def __init__(self, capacity: int = DEFAULT_LOG_LINES) -> None:
        self._lock = threading.Lock()
        self._items: deque[dict] = deque(maxlen=max(50, capacity))
        self._seq = 0

    def set_capacity(self, capacity: int) -> None:
        with self._lock:
            capacity = max(50, int(capacity))
            if capacity == self._items.maxlen:
                return
            self._items = deque(self._items, maxlen=capacity)

    def append(self, line: str, level: str = "info") -> dict:
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "line": line,
            }
            self._items.append(item)
            return item

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            items = [item for item in self._items if item["seq"] > seq]
        return items[-limit:]

    def tail(self, count: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._items)
        return items[-count:]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def capacity(self) -> int:
        return self._items.maxlen or 0


# --------------------------------------------------------------------------
# 服务主体
# --------------------------------------------------------------------------
class OptimizerService:
    """把配置、运行状态、日志、控制接口聚合在一起。"""

    def __init__(self, store: ConfigStore | None = None) -> None:
        self.store = store or ConfigStore()
        self.logs = LogBuffer(self.store.get().log_buffer)
        self.lock = threading.RLock()

        self.runner = CycleRunner(emit=self._on_event, settings=self._loop_settings)
        self.progress: dict = {"phase": "idle", "done": 0, "total": 0, "platforms": {}}
        self.platforms_cache: list[dict] = []
        self.last_results: dict[str, Any] = {}
        self.last_finished_at: int = 0
        self.started_at: float | None = None

    # ---- 事件 ----
    def _on_event(self, event: dict) -> None:
        if event.get("type") == "log":
            self.logs.append(event.get("line", ""))
        elif event.get("type") == "progress":
            self.progress = {k: v for k, v in event.items() if k != "type"}
        elif event.get("type") == "state":
            state = event.get("state", "idle")
            if state != "idle":
                self.progress = {**self.progress, "phase": state}
            if event.get("finished_at"):
                self.last_finished_at = event["finished_at"]

    def log(self, line: str, level: str = "info") -> None:
        self.logs.append(line, level=level)

    # ---- 平台构造 ----
    def build_platforms(self, keys: list[str] | None = None) -> list[Platform]:
        """把代码里的平台定义与网页上的覆盖配置合并。"""
        cfg = self.store.get()
        result: list[Platform] = []

        for key, base in PLATFORMS.items():
            override = cfg.platforms.get(key)
            if keys and key not in keys:
                continue

            platform = Platform(
                key=base.key,
                name=base.name,
                network=base.network,
                host=(override.host if override and override.host else base.host),
                port=base.port,
                scheme=(override.scheme if override and override.scheme else base.scheme),
                path=(override.http_path if override and override.http_path else base.path),
                status_codes=tuple(
                    s.strip() for s in (override.status_codes if override and override.status_codes
                                        else ",".join(base.status_codes)).split(",") if s.strip()
                ),
                timeout=(override.timeout if override and override.timeout else
                         (cfg.timeout or base.timeout)),
                ping_timeout_ms=base.ping_timeout_ms,
                precheck_ping=bool(override.ping) if override else base.precheck_ping,
                retries=(override.retries if override and override.retries is not None else
                         (cfg.retries if cfg.retries is not None else base.retries)),
                workers=(override.workers if override and override.workers else
                         (cfg.workers or base.workers)),
                sample_per_24=(override.sample_per_24 if override and override.sample_per_24
                               else base.sample_per_24),
                targets_file=(override.targets_file if override and override.targets_file
                              else base.targets_file),
                output_file=(override.output_file if override and override.output_file
                             else base.output_file),
                history_file=base.history_file,
                enabled=(override.enabled if override else base.enabled),
                dns=self._build_dns(key, base),
                note=base.note,
            )
            result.append(platform)

        # 允许网页上新增代码里没有的平台条目
        for key, override in cfg.platforms.items():
            if key in PLATFORMS or (keys and key not in keys):
                continue
            if not override.host:
                continue
            result.append(Platform(
                key=key,
                name=key.upper(),
                host=override.host,
                path=override.http_path or "/",
                status_codes=tuple(s.strip() for s in (override.status_codes or "200").split(",")
                                   if s.strip()),
                timeout=override.timeout or cfg.timeout or 3.0,
                precheck_ping=bool(override.ping),
                retries=override.retries if override.retries is not None else 1,
                workers=override.workers or cfg.workers or 80,
                targets_file=override.targets_file,
                output_file=override.output_file,
                history_file="",
                enabled=override.enabled,
                dns=self._build_dns(key, None),
                note="网页上新增的平台",
            ))
        return result

    def _build_dns(self, key: str, base: Platform | None) -> dict:
        cfg = self.store.get()
        setting = cfg.dns.get(key)
        if not setting or not setting.enabled or setting.provider == "none":
            return base.dns if base else {}
        return {
            "provider": setting.provider,
            "zone_name": setting.zone_name,
            "record_name": setting.record_name,
            "region": setting.region,
            "access_key_id": setting.access_key_id or os.environ.get("HUAWEI_AK", ""),
            "access_key_secret": setting.access_key_secret or os.environ.get("HUAWEI_SK", ""),
            "api_token": setting.api_token or os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            "zone_id": setting.zone_id,
            "ttl": setting.ttl,
            "batch_size": setting.batch_size,
            "cname": setting.cname,
        }

    def build_run_config(self, extra: dict | None = None) -> RunConfig:
        cfg = self.store.get()
        extra = extra or {}
        dns_mode = extra.get("dns_mode", cfg.dns_mode)
        if dns_mode == "push" and not (cfg.dns_confirm or extra.get("dns_confirm")):
            # 双重确认：网页没勾「确认修改」就降级为预览，绝不误改线上
            self.log("⚠ 未勾选「确认修改线上解析」，本次 DNS 操作降级为预览", level="warn")
            dns_mode = "dry-run"
        return RunConfig(
            rate=cfg.rate,
            timeout=cfg.timeout,
            workers=cfg.workers,
            retries=cfg.retries,
            ping=cfg.ping,
            no_write=bool(extra.get("no_write", False)),
            webhook=cfg.webhook or None,
            notify_always=cfg.notify_always,
            dns_mode=dns_mode,
            dns_yes=True,          # 确认逻辑已在上面处理
            dns_force=bool(extra.get("dns_force", False)),
            quiet=True,            # Web 端日志走事件，不重复打印
            verbose=cfg.verbose,
        )

    # ---- 运行控制 ----
    def current_interval(self) -> int:
        return max(60, int(self.store.get().interval or 300))

    def _loop_settings(self) -> dict:
        """CycleRunner 每轮开始前会调用它，因此网页上改间隔/窗口无需重启。"""
        cfg = self.store.get()
        return {"interval": max(60, int(cfg.interval or 300)), "window": cfg.window or ""}

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.runner.running:
                return False, "循环已在运行"
            platforms = self.build_platforms()
            enabled = [p for p in platforms if p.enabled]
            if not enabled:
                return False, "没有启用任何平台"
            self.log(f"启动循环：{len(enabled)} 个平台，间隔 {self.current_interval()}s，"
                     f"限速 {self.store.get().rate:g} 连接/秒")

            def job(cycle_no: int, stop_event) -> None:
                # 每轮都重新读取配置，网页上改的参数下一轮即生效
                plats = self.build_platforms()
                config = self.build_run_config()
                outcomes = run_once(plats, config, emit=self._on_event, stop_event=stop_event)
                self.last_results = {
                    "cycle": cycle_no,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "platforms": {o.platform.key: {"alive": len(o.passed),
                                                   "total": o.total,
                                                   "error": o.error} for o in outcomes},
                }

            started = self.runner.start(job, interval=self.current_interval(),
                                        cycles=0, window=self.store.get().window or "")
            return (True, "已启动") if started else (False, "启动失败")

    def stop(self) -> tuple[bool, str]:
        if not self.runner.running:
            return False, "循环未在运行"
        self.runner.stop()
        self.log("已请求停止，当前轮收尾后退出")
        return True, "正在停止"

    def run_single(self, extra: dict | None = None) -> tuple[bool, str]:
        """立即跑一轮（不进入循环）。"""
        with self.lock:
            if self.runner.running:
                busy = "手动任务" if self.runner.manual else "循环任务"
                return False, f"已有{busy}在执行，请等它结束或先停止循环"
            platforms = self.build_platforms()
            if not [p for p in platforms if p.enabled]:
                return False, "没有启用任何平台"
            config = self.build_run_config(extra)
            self.log("手动触发：立即跑一轮")

            def worker() -> None:
                try:
                    outcomes = run_once(platforms, config, emit=self._on_event)
                    self.last_results = {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "platforms": {o.platform.key: {"alive": len(o.passed),
                                                       "total": o.total,
                                                       "error": o.error} for o in outcomes},
                    }
                except Exception as exc:
                    self.log(f"⚠ 执行失败: {exc}", level="error")
                    self._on_event({"type": "state", "state": "idle"})

            # 借用 runner 线程机制保证「同时只有一个任务」
            self.runner.manual = True
            self.runner.start(lambda cycle, stop: worker(), interval=60, cycles=1)
            return True, "已开始"

    def dns_preview(self) -> tuple[bool, str]:
        return self.run_single({"dns_mode": "dry-run", "no_write": True})

    def dns_push(self) -> tuple[bool, str]:
        return self.run_single({"dns_mode": "push", "dns_confirm": True, "no_write": True})

    # ---- 状态 ----
    def vars_overview(self) -> dict:
        """常用变量的三方对照（默认值 / .env / 网页），说明谁在生效。"""
        from .config_store import env_file_path
        from .varview import info_view, vars_view

        env_path = env_file_path()
        config = self.store.get()
        return {
            "vars": vars_view(config, env_path, self.store.path, self.store.explicit),
            "info": info_view(env_path),
            "env_file": str(env_path),
            "env_exists": env_path.exists(),
            "env_loaded": getattr(self.store, "dotenv_keys", []),
            "config_file": str(self.store.path),
        }

    def status(self) -> dict:
        cfg = self.store.get()
        platforms = []
        for platform in self.build_platforms():
            override = cfg.platforms.get(platform.key)
            platforms.append({
                "key": platform.key,
                "name": platform.name,
                "network": platform.network,
                "network_label": network_label(platform.network),
                "enabled": platform.enabled,
                "host": platform.host,
                "scheme": platform.scheme,
                "targets_file": platform.targets_file,
                "output_file": platform.output_file,
                "workers": platform.workers,
                "timeout": platform.timeout,
                "retries": platform.retries,
                "sample_per_24": platform.sample_per_24,
                "ping": platform.precheck_ping,
                "status_codes": ",".join(platform.status_codes),
                "dns_enabled": bool(platform.dns),
                "dns_provider": (platform.dns or {}).get("provider", ""),
                "record_name": (platform.dns or {}).get("record_name", ""),
                "auto_targets": override.auto_targets if override else "",
            })
        return {
            "runner": self.runner.snapshot(),
            "progress": self.progress,
            "platforms": platforms,
            "last_results": self.last_results,
            "last_finished_at": self.last_finished_at,
            "logs": {"capacity": self.logs.capacity(), "last_seq": self.logs.last_seq},
            "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "EdgeOptimizer/1.0"
    service: OptimizerService

    # ---- 工具 ----
    def log_message(self, fmt: str, *args) -> None:      # 静默默认访问日志
        return

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        token = self.service.store.web_token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided:
            provided = (query.get("token") or [""])[0]
        return provided == token

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:            # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/healthz":
            self._json({"ok": True})
            return

        if not self._authorized(query):
            if path == "/":
                self._text("需要访问令牌：请在地址后加 ?token=你的令牌", status=HTTPStatus.UNAUTHORIZED)
            else:
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/":
            self._text(PAGE_HTML, content_type="text/html; charset=utf-8")
        elif path == "/api/config":
            self._json({"config": self.service.store.get().to_dict()})
        elif path == "/api/vars":
            self._json(self.service.vars_overview())
        elif path == "/api/status":
            self._json(self.service.status())
        elif path == "/api/logs":
            after = int((query.get("after") or ["0"])[0])
            limit = int((query.get("limit") or ["500"])[0])
            if after:
                items = self.service.logs.since(after, limit)
            else:
                items = self.service.logs.tail(limit)
            self._json({"logs": items, "last_seq": self.service.logs.last_seq,
                        "capacity": self.service.logs.capacity()})
        elif path == "/api/events":
            self._stream()
        elif path == "/api/platforms":
            self._json({"platforms": self.service.status()["platforms"]})
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:           # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorized(query):
            self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        body = self._body()
        service = self.service

        if path == "/api/config":
            config = service.store.update(body)
            service.logs.set_capacity(config.log_buffer)
            service.log("配置已更新")
            self._json({"ok": True, "config": config.to_dict()})
        elif path == "/api/control/start":
            ok, message = service.start()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/control/stop":
            ok, message = service.stop()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/control/run-once":
            ok, message = service.run_single(body or None)
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/control/dns-preview":
            ok, message = service.dns_preview()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/control/dns-push":
            ok, message = service.dns_push()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/logs/clear":
            service.logs = LogBuffer(service.store.get().log_buffer)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # ---- SSE ----
    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        cursor = 0
        last_beat = time.time()
        try:
            for item in self.service.logs.tail(200):
                self._sse("log", item)
                cursor = item["seq"]
            self._sse("progress", self.service.progress)
            while True:
                items = self.service.logs.since(cursor, 200)
                for item in items:
                    self._sse("log", item)
                    cursor = item["seq"]
                self._sse("progress", self.service.progress)
                self._sse("runner", self.service.runner.snapshot())
                if time.time() - last_beat > 15:
                    last_beat = time.time()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _sse(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


class WebServer:
    """把服务跑在一个线程化的 HTTP 服务器上。"""

    def __init__(self, service: OptimizerService, host: str = "0.0.0.0", port: int = 8787) -> None:
        self.service = service
        handler = type("BoundHandler", (_Handler,), {"service": service})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host = host
        self.port = port

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()


def make_service(config_path: Path | None = None) -> OptimizerService:
    store = ConfigStore(config_path)
    return OptimizerService(store)


# 页面在 pages.py 里（单独文件便于前端改动，不影响后端逻辑）
from .pages import PAGE_HTML  # noqa: E402  (放在末尾避免循环导入)
