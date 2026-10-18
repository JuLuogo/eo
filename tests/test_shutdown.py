#!/usr/bin/env python3
"""优雅退出与结果文件原子性测试。

针对容器部署最要紧的两件事：
  1. ``docker stop`` 发的是 SIGTERM。默认行为是进程被立即终止 ——
     正在跑的一轮会停在中间（结果文件写一半、DNS 下发做到一半）。
     这里真的起进程、真的发信号，验证「停止循环 -> 关闭服务 -> 退出 0」。
  2. 结果文件是给 curl --resolve 直接消费的，必须原子替换，
     绝不能因为中断留下半截 IP 列表。

Windows 上信号语义不同（SIGTERM 等于 TerminateProcess），会自动跳过。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.core import Probe, write_result            # noqa: E402
from eo.cli import _shutdown_grace                 # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def spawn(args: list[str], env_extra: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.update(env_extra or {})
    return subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), *args],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )


class Output:
    """后台线程持续收集子进程输出（text 模式不能非阻塞读，会解码出错）。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._buf = ""
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:            # type: ignore[union-attr]
                with self._lock:
                    self._buf += line
        except Exception:                            # noqa: BLE001
            pass

    def text(self) -> str:
        with self._lock:
            return self._buf

    def wait_for(self, needle: str, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.text():
                return True
            if self.proc.poll() is not None:
                time.sleep(0.3)                      # 收一下退出前的残余输出
                return needle in self.text()
            time.sleep(0.1)
        return needle in self.text()


print("=" * 70)
print("优雅退出（SIGTERM / SIGINT）与结果文件原子性")
print("=" * 70)

if os.name == "nt":
    print()
    print("  [SKIP] Windows 上 SIGTERM 等于强制终止，信号测试无意义。")
    print("         请在 Linux / WSL / 容器里运行本套件。")
    print()
    print("=" * 70)
    print("优雅退出测试：跳过（平台不支持）")
    sys.exit(0)

work = Path(tempfile.mkdtemp(prefix="eo-shutdown-"))

# --------------------------------------------------------------------------
print()
print("1) EO_SHUTDOWN_GRACE 解析（必须落在 docker 宽限期内）")
# --------------------------------------------------------------------------
os.environ.pop("EO_SHUTDOWN_GRACE", None)
check("未设置时默认 5 秒", _shutdown_grace() == 5.0, str(_shutdown_grace()))
os.environ["EO_SHUTDOWN_GRACE"] = "3"
check("可被环境变量覆盖", _shutdown_grace() == 3.0, str(_shutdown_grace()))
os.environ["EO_SHUTDOWN_GRACE"] = "abc"
check("非法值退回默认（不让启动失败）", _shutdown_grace() == 5.0, str(_shutdown_grace()))
os.environ["EO_SHUTDOWN_GRACE"] = "999"
check("超大值被夹到 60 秒上限", _shutdown_grace() == 60.0, str(_shutdown_grace()))
os.environ["EO_SHUTDOWN_GRACE"] = "-5"
check("负值被夹到 0（允许立即退出）", _shutdown_grace() == 0.0, str(_shutdown_grace()))
os.environ.pop("EO_SHUTDOWN_GRACE", None)
check("默认值留足余量（< docker 默认 10 秒宽限期）", _shutdown_grace() < 10, str(_shutdown_grace()))

# --------------------------------------------------------------------------
print()
print("2) monitor：SIGTERM 要优雅退出（而不是被直接杀掉）")
# --------------------------------------------------------------------------
proc = spawn(["monitor", "-p", "eo-lt", "-q", "--no-write", "--interval", "60",
              "--cycles", "0", "--skip-check", "--targets", "targets/eo.txt"])
out = Output(proc)
check("monitor 正常启动", out.wait_for("持续监控已启动"),
      out.text().strip().splitlines()[-1:])
# 打断第一次扫描，制造「正在跑一轮时收到信号」的真实场景
time.sleep(1.5)
t0 = time.time()
proc.send_signal(signal.SIGTERM)
try:
    rc = proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
    rc = None
dt = time.time() - t0
out = out.text()
check("SIGTERM 后进程退出（未超时）", rc is not None, f"rc={rc}")
check("退出码为 0（优雅退出，不是被信号打死）", rc == 0, f"rc={rc}")
check("退出很快（远小于超时）", dt < 15, f"{dt:.1f}s")
check("明确打印收到 SIGTERM", "收到 SIGTERM" in out,
      next((l for l in out.splitlines() if "SIGTERM" in l), "(无)")[:70])
check("打印监控结束（走到了收尾逻辑）", "监控结束" in out,
      next((l for l in out.splitlines() if "监控结束" in l), "(无)")[:70])
check("提示会跑完本轮再退出", "本轮结束后退出" in out,
      next((l for l in out.splitlines() if "本轮结束后退出" in l), "(无)")[:70])

# --------------------------------------------------------------------------
print()
print("3) monitor：SIGINT（Ctrl+C）同样优雅退出")
# --------------------------------------------------------------------------
proc = spawn(["monitor", "-p", "eo-lt", "-q", "--no-write", "--interval", "60",
              "--cycles", "0", "--skip-check", "--targets", "targets/eo.txt"])
out = Output(proc)
out.wait_for("持续监控已启动")
time.sleep(1.0)
proc.send_signal(signal.SIGINT)
try:
    rc = proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
    rc = None
out = out.text()
check("SIGINT 后退出码为 0", rc == 0, f"rc={rc}")
check("打印收到 SIGINT", "收到 SIGINT" in out,
      next((l for l in out.splitlines() if "SIGINT" in l), "(无)")[:70])

# --------------------------------------------------------------------------
print()
print("4) web：SIGTERM 要关闭服务并退出（容器里就是这条路径）")
# --------------------------------------------------------------------------
port = free_port()
proc = spawn(["web", "--host-bind", "127.0.0.1", "--port", str(port),
              "--config", str(work / "web-config.json")])
out = Output(proc)
check("web 正常启动", out.wait_for("Web 控制台已启动"),
      out.text().strip().splitlines()[-1:])

# 确认真的在监听
alive = False
import urllib.request                                          # noqa: E402
for _ in range(40):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            alive = resp.status == 200
            if alive:
                break
    except Exception:                                          # noqa: BLE001
        time.sleep(0.25)
check("HTTP 服务确实在监听", alive, f"http://127.0.0.1:{port}/healthz")

t0 = time.time()
proc.send_signal(signal.SIGTERM)
try:
    rc = proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
    rc = None
dt = time.time() - t0
out = out.text()
check("web 收到 SIGTERM 后退出", rc is not None, f"rc={rc}")
check("退出码为 0", rc == 0, f"rc={rc}")
check("退出很快", dt < 15, f"{dt:.1f}s")
check("打印收到 SIGTERM", "收到 SIGTERM" in out,
      next((l for l in out.splitlines() if "SIGTERM" in l), "(无)")[:70])
check("打印服务已关闭", "Web 控制台已关闭" in out,
      next((l for l in out.splitlines() if "已关闭" in l), "(无)")[:70])

# 端口必须释放（否则 docker restart 会撞端口）
released = False
for _ in range(20):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            released = True
            break
        except OSError:
            time.sleep(0.25)
check("退出后端口已释放", released, f"port={port}")

# --------------------------------------------------------------------------
print()
print("5) web：正在跑一轮时收到 SIGTERM（模拟 docker stop）")
# --------------------------------------------------------------------------
port = free_port()
cfg = work / "web-config2.json"
proc = spawn(["web", "--host-bind", "127.0.0.1", "--port", str(port),
              "--config", str(cfg)],
             env_extra={"EO_SHUTDOWN_GRACE": "5"})
out = Output(proc)
out.wait_for("Web 控制台已启动")

# 从配置文件里取令牌，然后触发一轮
token = ""
for _ in range(40):
    if cfg.exists():
        try:
            token = json.loads(cfg.read_text("utf-8")).get("web_token") or ""
        except Exception:                                      # noqa: BLE001
            token = ""
        if token:
            break
    time.sleep(0.25)
check("能从配置里取到访问令牌", bool(token), token[:8] + "…" if token else "(无)")

triggered = False
if token:
    def _post(path: str) -> bool:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method="POST",
            data=b"{}", headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode()).get("ok") is True
        except Exception as exc:                               # noqa: BLE001
            print(f"     {path} 失败: {type(exc).__name__}: {exc}")
            return False

    # 上一次运行如果还没彻底停下，run-once 会返回 409；先确保空闲再触发，
    # 并重试几次（小机器上收尾可能慢一点）
    _post("/api/control/stop")
    for _ in range(3):
        triggered = _post("/api/control/run-once")
        if triggered:
            break
        time.sleep(1.5)
check("成功触发一轮扫描", triggered)

time.sleep(2.0)                 # 让这一轮真正进入扫描
t0 = time.time()
proc.send_signal(signal.SIGTERM)
try:
    rc = proc.wait(timeout=25)
except subprocess.TimeoutExpired:
    proc.kill()
    rc = None
dt = time.time() - t0
check("扫描中收到 SIGTERM 仍能退出", rc is not None, f"rc={rc}")
check("退出码为 0", rc == 0, f"rc={rc}")
check("在宽限期内退出（配置 5s + 余量）", dt < 20, f"{dt:.1f}s")

# --------------------------------------------------------------------------
print()
print("6) 结果文件必须原子替换（中断不得留下半截 IP 列表）")
# --------------------------------------------------------------------------
target = work / "results" / "eo-lt.txt"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("1.1.1.1\n2.2.2.2\n", encoding="utf-8")
probes = [Probe(ip="203.0.113.1", latency_ms=12.0), Probe(ip="203.0.113.2", latency_ms=9.0)]

write_result(target, probes)
check("正常写入内容正确",
      target.read_text("utf-8") == "203.0.113.1\t12ms\n203.0.113.2\t9ms", target.read_text("utf-8"))
check("不留下临时文件", not list(target.parent.glob("*.tmp")),
      str([p.name for p in target.parent.glob("*.tmp")]))

# 模拟「写完临时文件、还没来得及替换」就被中断：原文件必须完好
original_replace = Path.replace


def boom(self, other):
    raise KeyboardInterrupt("模拟中断")


Path.replace = boom                                             # type: ignore[assignment]
try:
    try:
        write_result(target, [Probe(ip="198.51.100.9", latency_ms=5.0)])
    except KeyboardInterrupt:
        pass
finally:
    Path.replace = original_replace                             # type: ignore[assignment]
check("中断后旧结果文件完好（没有被写坏）",
      target.read_text("utf-8") == "203.0.113.1\t12ms\n203.0.113.2\t9ms", target.read_text("utf-8"))

# 带延迟的格式也要原子
write_result(target, probes, with_latency=True)
check("带延迟写入格式正确",
      target.read_text("utf-8") == "203.0.113.1\t12ms\n203.0.113.2\t9ms",
      target.read_text("utf-8"))

# 清理
import shutil                                                   # noqa: E402
shutil.rmtree(work, ignore_errors=True)

print()
print("=" * 70)
if _failures:
    print(f"优雅退出测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("优雅退出与结果原子性测试全部通过")
sys.exit(0)
