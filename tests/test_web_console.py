#!/usr/bin/env python
"""Web 控制台全功能测试（修正断言版）。

覆盖用户需求：网页管理并发限制 / 进度条 / 实时日志500条 / 轮询间隔 / DNS配置 / 任务状态 / 定时任务
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根（本文件在 tests/ 下）
sys.path.insert(0, str(ROOT))

PORT = 18801
TOKEN = "webtest-token-123456"
BASE = f"http://127.0.0.1:{PORT}"
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def call(path, method="GET", body=None, token=True, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HDR) if token else {"Content-Type": "application/json"}
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def raw_get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def status():
    return call("/api/status")[1]


def wait_until(fn, timeout=60, interval=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(interval)
    return False


print("=" * 70)
print("Web 控制台全功能测试（修正断言版）")
print("=" * 70)

cfgdir = Path(tempfile.mkdtemp(prefix="eo-web2-"))
cfgp = cfgdir / "config.json"
os.environ["EO_WEB_TOKEN"] = TOKEN
os.environ["EO_PLATFORMS"] = "eo-lt,cft-lt"
os.environ["EO_NETWORK"] = "lt"
os.environ["EO_RATE"] = "137"
os.environ["EO_INTERVAL"] = "456"
os.environ["EO_DNS_SYNC"] = "dry-run"

from eo.webui import WebServer, make_service   # noqa: E402

svc = make_service(cfgp)
server = WebServer(svc, host="127.0.0.1", port=PORT)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(1.5)
print(f"\n服务已启动 {BASE}")

try:
    print()
    print("1) 鉴权")
    check("无令牌 401", call("/api/status", token=False)[0] == 401)
    check("healthz 免鉴权", call("/healthz", token=False)[0] == 200)
    check("带令牌 200", call("/api/status")[0] == 200)

    print()
    print("2) 变量来源识别（含 Docker 场景的进程环境变量）")
    st, v = call("/api/vars")
    vm = {x["key"]: x for x in v["vars"]}
    check("rate 生效 137", float(vm["rate"]["effective"]) == 137, str(vm["rate"]["effective"]))
    check("rate 来源=env（关键修复）", vm["rate"]["source"] == "env", vm["rate"]["source"])
    check("rate 标出具体来源=进程环境变量",
          vm["rate"].get("env_origin") == "进程环境变量", str(vm["rate"].get("env_origin")))
    check("interval 生效 456", int(vm["interval"]["effective"]) == 456,
          str(vm["interval"]["effective"]))
    check("interval 来源=env", vm["interval"]["source"] == "env", vm["interval"]["source"])
    check("log_buffer 默认 500", int(vm["log_buffer"]["effective"]) == 500,
          str(vm["log_buffer"]["effective"]))
    check("未设置的项标 default（window）", vm["window"]["source"] == "default",
          vm["window"]["source"])
    print(f"     变量共 {len(vm)} 项: {sorted(vm)}")

    print()
    print("3) DNS 配置（关键修复：面板此前为空）")
    st, cfgp_body = call("/api/config")
    dns = cfgp_body["config"].get("dns", {})
    print(f"     dns 条目: {sorted(dns)}")
    check("dns 至少有一个可配置条目（此前为空）", len(dns) >= 1, str(sorted(dns)))
    one = dns.get("eo-lt", {})
    for f in ("provider", "zone_name", "record_name", "region", "ttl", "batch_size"):
        check(f"dns.eo-lt 含 {f}", f in one, str(one.get(f)))
    check("provider=huawei", one.get("provider") == "huawei", one.get("provider"))
    check("record_name 正确", one.get("record_name") == "eo-lt.1.060730.xyz",
          one.get("record_name"))
    check("密钥不回传明文", one.get("access_key_id", "") in ("", None) or
          "******" in str(one.get("access_key_id")), str(one.get("access_key_id")))

    print()
    print("4) 网页改配置（并发限制 / 间隔 / 日志条数）")
    st, r = call("/api/config", "POST", {"rate": 88, "interval": 123, "log_buffer": 300})
    check("POST /api/config 成功", r.get("ok") is True, str(r)[:80])
    _, v2 = call("/api/vars")
    vm2 = {x["key"]: x for x in v2["vars"]}
    check("rate=88 且来源=web",
          float(vm2["rate"]["effective"]) == 88 and vm2["rate"]["source"] == "web",
          f'{vm2["rate"]["effective"]}/{vm2["rate"]["source"]}')
    check("日志缓冲容量已跟随改为 300", svc.logs.capacity() == 300, str(svc.logs.capacity()))
    check("落盘含 _explicit", "_explicit" in json.loads(cfgp.read_text(encoding="utf-8")))

    print()
    print("5) DNS 配置可保存（网页改 DNS 字段）")
    st, r = call("/api/config", "POST",
                 {"dns": {"eo-lt": {"record_name": "custom-eo.1.060730.xyz", "ttl": 60}}})
    check("提交 dns 补丁成功", r.get("ok") is True, str(r)[:80])
    _, cfg2 = call("/api/config")
    eo_dns = cfg2["config"]["dns"]["eo-lt"]
    check("record_name 已改", eo_dns["record_name"] == "custom-eo.1.060730.xyz",
          eo_dns["record_name"])
    check("ttl 已改 60", int(eo_dns["ttl"]) == 60, str(eo_dns["ttl"]))
    # 恢复
    call("/api/config", "POST",
         {"dns": {"eo-lt": {"record_name": "eo-lt.1.060730.xyz", "ttl": 1}}})

    print()
    print("6) 首页元素")
    page = raw_get("/")
    print(f"     页面 {len(page)} 字节")
    must = {
        "常用变量面板": "常用变量",
        "运行参数面板": "运行参数",
        "平台设置面板": "平台设置",
        "DNS 下发面板": "DNS 下发",
        "任务状态面板": "任务状态",
        "实时日志面板": "实时日志",
        "进度条容器": 'class="bar"',
        "进度条填充元素": 'id="prog-bar"',
        "进度百分比": 'id="prog-pct"',
        "进度文字": 'id="prog-text"',
        "每平台分项进度": "p.platforms",
        "日志自动滚动": "自动滚动",
        "清空日志": "清空",
        "开始循环": "开始循环",
        "停止": "停止",
        "跑一轮": "跑一轮",
        "DNS 预览": "预览",
        "DNS 下发按钮": "下发",
        "常用变量表单 rate": 'id="v-rate"',
        "常用变量表单 interval": 'id="v-interval"',
        "常用变量表单 log_buffer": 'id="v-logbuf"',
        "常用变量表单 window": 'id="v-window"',
        "SSE": "EventSource",
        "变量表：来源列": "来源",
    }
    for label, needle in must.items():
        check(f"含{label}", needle in page)

    print()
    print("7) 任务状态字段")
    s = status()
    rn = s["runner"]
    for f in ("state", "cycle", "running", "manual", "interval", "last_error",
              "next_run_at", "now"):
        check(f"runner.{f}", f in rn, str(rn.get(f)))
    check("progress 结构", all(k in s["progress"] for k in ("phase", "done", "total",
                                                           "platforms")),
          str(sorted(s["progress"])))
    check("logs 容量信息", s["logs"]["capacity"] == 300, str(s["logs"]))

    print()
    print("8) 触发扫描：进度条数据 + 阶段上报")
    st, r = call("/api/control/run-once", "POST", {})
    check("run-once 接受", r.get("ok") is True, str(r)[:60])

    # 应很快进入 checking 阶段（关键修复：不再长时间无提示）
    ok_checking = wait_until(
        lambda: status()["progress"].get("phase") in ("checking", "scanning"), timeout=20)
    ph = status()["progress"].get("phase")
    check("立即进入 checking/scanning 阶段（不再无进度）", ok_checking, str(ph))

    # 等进度里出现平台明细
    ok_plat = wait_until(
        lambda: bool(status()["progress"].get("platforms")), timeout=30)
    pls = status()["progress"].get("platforms", {})
    check("progress.platforms 已填充", ok_plat, str(sorted(pls)))
    active = [k for k, v in pls.items() if v.get("name")]
    check("仅 eo-lt/cft-lt 参与", sorted(active) == ["cft-lt", "eo-lt"], str(active))

    # 等扫描真正开始（total>0）
    wait_until(lambda: (status()["progress"].get("total") or 0) > 0, timeout=60)
    p2 = status()["progress"]
    check("总进度 total > 0", (p2.get("total") or 0) > 0, str(p2.get("total")))
    check("phase=scanning", p2.get("phase") == "scanning", str(p2.get("phase")))
    print(f"     进度: {p2.get('done')}/{p2.get('total')}")

    print()
    print("9) 并发互斥")
    st, r2 = call("/api/control/run-once", "POST", {})
    check("扫描中重复触发被拒", r2.get("ok") is False, str(r2)[:60])

    print()
    print("10) 停止任务（关键修复：此前对手动任务无效）")
    st, r3 = call("/api/control/stop", "POST", {})
    check("stop 被接受", r3.get("ok") is True, str(r3)[:60])
    t0 = time.time()
    stopped = wait_until(lambda: not status()["runner"]["running"], timeout=45, interval=1.5)
    dt = time.time() - t0
    check("停止在 45 秒内生效", stopped, f"耗时 {dt:.1f}s")
    print(f"     停止耗时 {dt:.1f}s")
    _, lg = call("/api/logs")
    lines = [x["line"] for x in lg.get("logs", [])]
    check("日志记录了停止", any("停止" in l for l in lines),
          next((l for l in lines if "停止" in l), "(无)"))
    check("日志记录了限速", any("限速" in l for l in lines),
          next((l for l in lines if "限速" in l), "(无)"))
    print(f"     最后 4 条日志：")
    for l in lines[-4:]:
        print(f"       {l[:88]}")

    print()
    print("11) 停止后可正常启动循环（此前会一直 409）")
    st, r = call("/api/control/start", "POST", {})
    check("start 成功（不再被 409 挡）", r.get("ok") is True, f"{st} {str(r)[:60]}")
    time.sleep(2)
    rn2 = status()["runner"]
    check("running=True", rn2["running"] is True, str(rn2["running"]))
    check("interval 被下限保护到 >=60", int(rn2["interval"]) >= 60, str(rn2["interval"]))
    check("暴露 configured_interval（网页配置值）",
          "configured_interval" in rn2, str(sorted(rn2)))
    check("本轮进行中时无倒计时（UI 显示「本轮进行中」）",
          rn2["next_run_at"] == 0, str(rn2["next_run_at"]))
    print(f"     本轮 interval={rn2['interval']} 配置 configured={rn2['configured_interval']}")

    print()
    print("12) 间隔热更新（直接验证 runner 每轮调用的 settings 回调）")
    call("/api/config", "POST", {"interval": 90})
    s90 = svc._loop_settings()
    check("interval=90 -> settings 返回 90", int(s90["interval"]) == 90, str(s90))
    rn3 = status()["runner"]
    check("status 的 configured_interval 同步为 90",
          int(rn3.get("configured_interval") or 0) == 90, str(rn3.get("configured_interval")))
    print(f"     本轮仍用 {rn3['interval']}s，下轮起用 {rn3['configured_interval']}s")

    call("/api/config", "POST", {"interval": 10})
    s10 = svc._loop_settings()
    check("低于下限 60 时按 60 执行", int(s10["interval"]) == 60, str(s10))
    _, lg2 = call("/api/logs")
    lines2 = [x["line"] for x in lg2.get("logs", [])]
    check("截断有明确日志（不静默）",
          any("低于下限" in l for l in lines2),
          next((l for l in lines2 if "低于下限" in l), "(无)")[:70])

    call("/api/config", "POST", {"interval": 300})

    st, r = call("/api/control/stop", "POST", {})
    wait_until(lambda: not status()["runner"]["running"], timeout=45)

    print()
    print("13) DNS 预览与下发保护")
    time.sleep(2)
    st, r = call("/api/control/dns-preview", "POST", {})
    check("dns-preview 可用", st == 200 and r.get("ok") is True, f"{st} {str(r)[:70]}")
    st, r = call("/api/control/dns-push", "POST", {})
    check("mode=dry-run 时拒绝下发", r.get("ok") is False, str(r)[:90])

finally:
    try:
        svc.runner.stop()
    except Exception:
        pass
    try:
        server.shutdown()
    except Exception:
        pass

print()
print("=" * 70)
if failures:
    print(f"失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("Web 控制台全部测试通过")
sys.exit(0)
