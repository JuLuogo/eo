#!/usr/bin/env python3
"""CLI 端到端测试：scan 落盘、monitor 多轮、参数校验、退出码。

用保留地址（192.0.2.0/24，必定不可达）与极短超时，几秒内跑完，
不会真的去扫线上节点。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def run(args, env_extra=None, timeout=180):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for k in [k for k in env if k.startswith("EO_")]:
        env.pop(k)                      # 清掉残留的 EO_*，避免影响平台选择
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, str(ROOT / "run.py"), *args],
                          cwd=str(ROOT), env=env, timeout=timeout, capture_output=True)
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


print("=" * 70)
print("CLI 端到端测试")
print("=" * 70)

work = Path(tempfile.mkdtemp(prefix="eo-cli-"))
tiny = work / "tiny.txt"
tiny.write_text("\n".join(f"192.0.2.{i}" for i in range(1, 9)) + "\n", encoding="utf-8")
print(f"\n临时目录: {work}")

BASE = ["-p", "eo-lt", "-q", "--targets", str(tiny),
        "--timeout", "1", "--retries", "0", "--rate", "0",
        "--skip-check", "-w", "4"]

RESULTS = ROOT / "results" / "eo-lt.txt"

# ------------------------------------------------------------------ 1. scan 基本
print()
print("1) scan：正常一轮 + JSON 汇总")
summary = work / "summary.json"
before = RESULTS.stat().st_mtime if RESULTS.exists() else None
rc, text = run(["scan", *BASE, "--no-write", "--json", str(summary)])
check("退出码 0", rc == 0, f"rc={rc}")
check("JSON 汇总已生成", summary.exists(), str(summary))
if summary.exists():
    payload = json.loads(summary.read_text(encoding="utf-8"))
    check("是列表且含 1 个平台", isinstance(payload, list) and len(payload) == 1,
          f"{type(payload).__name__} len={len(payload) if isinstance(payload, list) else '-'}")
    if payload:
        item = payload[0]
        for field in ("platform", "total", "alive", "ips", "error"):
            check(f"  含字段 {field}", field in item, str(sorted(item)))
        check("  平台正确", item["platform"] == "eo-lt", str(item["platform"]))
        check("  探测了 8 个目标", item["total"] == 8, str(item["total"]))
        check("  保留地址全部不可用（alive=0）", item["alive"] == 0, str(item["alive"]))
        check("  ips 为空", item["ips"] == [], str(item["ips"]))
check("--no-write 时未改结果文件",
      (RESULTS.stat().st_mtime if RESULTS.exists() else None) == before,
      "mtime 未变")

# ------------------------------------------------------------------ 2. 落盘
print()
print("2) scan：写结果文件与历史")
rc, text = run(["scan", *BASE])
check("退出码 0", rc == 0, f"rc={rc}")
check("结果文件已生成", RESULTS.exists(), str(RESULTS))
if RESULTS.exists():
    ips = [l.split("\t")[0].strip() for l in RESULTS.read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.startswith("#")]
    print(f"     结果文件里 {len(ips)} 个 IP（保留地址不可达，预期 0）")
    check("不可达地址不写入结果", len(ips) == 0, str(ips[:3]))
hist = ROOT / "results" / "history" / "eo-lt.jsonl"
if hist.exists():
    entries = [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("历史文件已追加记录", len(entries) >= 1, f"{len(entries)} 条")
    if entries:
        e = entries[-1]
        check("  记录含平台与总数",
              e.get("platform") == "eo-lt" and int(e.get("total") or 0) == 8,
              f"{e.get('platform')} total={e.get('total')}")

# ------------------------------------------------------------------ 3. 网段文件校验
print()
print("3) --targets 指向不存在的文件：立刻中止（关键修复）")
t0 = time.time()
rc, text = run(["scan", "-p", "eo-lt", "-q",
                "--targets", str(work / "nope.txt"),
                "--timeout", "1", "--skip-check"], timeout=60)
dt = time.time() - t0
check("退出码 2", rc == 2, f"rc={rc}")
check("耗时很短（未去扫全量网段）", dt < 10, f"{dt:.2f}s")
check("提示文件不存在", "网段文件不存在" in text,
      next((l for l in text.splitlines() if "不存在" in l), "(无)")[:70])
check("明确说明不会改用默认网段", "默认网段" in text,
      next((l for l in text.splitlines() if "默认网段" in l), "(无)")[:70])

# ------------------------------------------------------------------ 4. monitor 多轮
print()
print("4) monitor：按 --cycles 跑指定轮数")
rc, text = run(["monitor", *BASE, "--no-write", "--interval", "2", "--cycles", "2"],
               timeout=240)
check("退出码 0", rc == 0, f"rc={rc}")
rounds = text.count("轮结果")
print(f"     输出里出现 {rounds} 次「轮结果」")
check("确实跑了 2 轮", rounds == 2, str(rounds))

# ------------------------------------------------------------------ 5. 时间窗口
print()
print("5) monitor：时间窗口（当前不在窗口内）")
rc, text = run(["monitor", *BASE, "--no-write", "--interval", "1", "--cycles", "1",
                "--window", "03:00-03:01"], timeout=120)
check("退出码 0", rc == 0, f"rc={rc}")
check("提示不在时间窗口内", "不在时间窗口" in text,
      next((l for l in text.splitlines() if "窗口" in l), "(无)")[:70])

# ------------------------------------------------------------------ 6. 参数校验
print()
print("6) 参数校验与退出码")
rc, text = run(["scan", "-p", "not-a-platform", "-q", "--skip-check"], timeout=60)
check("未知平台退出码非 0", rc != 0, f"rc={rc}")
check("列出可用平台", "未知平台" in text and "eo-lt" in text,
      next((l for l in text.splitlines() if "未知平台" in l), "(无)")[:70])

rc, text = run(["scan", "-p", "eo-lt", "-q", "--rate", "abc", "--skip-check"], timeout=60)
check("非法 --rate 被 argparse 拒绝（rc=2）", rc == 2, f"rc={rc}")

# ------------------------------------------------------------------ 7. 其它子命令
print()
print("7) 其它子命令正常结束")
for args, label in ((["selftest"], "selftest"),
                    (["list", "--all-platforms"], "list"),
                    (["ranges", "--source", "list"], "ranges --source list")):
    rc, text = run(args, timeout=120)
    check(f"{label} 退出码 0", rc == 0, f"rc={rc}")
    check(f"{label} 有输出", bool(text.strip()), f"{len(text)} 字节")

# ------------------------------------------------------------------ 8. 运营商筛选
print()
print("8) 运营商选择：EO_NETWORK / -n / -p 都应真的跑起来（关键修复）")


def platforms_of(args, env_extra=None):
    """跑一轮并把 JSON 汇总里的平台 key 读出来（-q 模式下不打印平台名）。"""
    js = work / f"sel-{abs(hash(tuple(args) + tuple((env_extra or {}).items()))) % 10**8}.json"
    rc, text = run([*args, "--json", str(js)], env_extra=env_extra, timeout=120)
    keys = []
    if js.exists():
        try:
            keys = sorted(item["platform"] for item in json.loads(js.read_text("utf-8")))
        except Exception:                       # noqa: BLE001
            keys = []
    return rc, keys, text


rc, keys, text = platforms_of(["scan", "-q", "--targets", str(tiny),
                               "--timeout", "1", "--retries", "0",
                               "--skip-check", "-w", "2", "--no-write"],
                              env_extra={"EO_NETWORK": "dx"})
check("EO_NETWORK=dx 退出码 0", rc == 0, f"rc={rc}")
check("EO_NETWORK=dx 选中电信三件套",
      keys == ["cft-dx", "eo-dx", "esa-dx"], str(keys))
check("  未扫联通平台", not any(k.endswith("-lt") for k in keys), str(keys))

rc, keys, _ = platforms_of(["scan", "-n", "dx", "-q", "--targets", str(tiny),
                            "--timeout", "1", "--retries", "0", "--skip-check",
                            "-w", "2", "--no-write"])
check("-n dx 退出码 0", rc == 0, f"rc={rc}")
check("  -n dx 真的选中电信（此前返回空、什么都不扫）",
      keys == ["cft-dx", "eo-dx", "esa-dx"], str(keys))

rc, keys, _ = platforms_of(["scan", "-n", "yd", "-q", "--targets", str(tiny),
                            "--timeout", "1", "--retries", "0", "--skip-check",
                            "-w", "2", "--no-write"])
check("-n yd 选中移动三件套", keys == ["cft-yd", "eo-yd", "esa-yd"], str(keys))

rc, keys, _ = platforms_of(["scan", "-p", "esa-dx", "-q", "--targets", str(tiny),
                            "--timeout", "1", "--retries", "0", "--skip-check",
                            "-w", "2", "--no-write"])
check("-p esa-dx 只扫该平台", keys == ["esa-dx"], str(keys))

rc, keys, _ = platforms_of(["scan", "-n", "lt", "dx", "-q", "--targets", str(tiny),
                            "--timeout", "1", "--retries", "0", "--skip-check",
                            "-w", "2", "--no-write"])
check("-n lt dx 多选可用", len(keys) == 6, str(keys))

print()
print("=" * 70)
if _failures:
    print(f"失败 {len(_failures)} 项：")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("CLI 端到端测试全部通过")
sys.exit(0)
