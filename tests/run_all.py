#!/usr/bin/env python3
"""统一测试入口：跑完 tests/ 下的全部套件并汇总。

用法（在仓库根目录）：
    python tests/run_all.py            # 全部
    python tests/run_all.py ratelimit  # 只跑名字含 ratelimit 的
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = sorted((ROOT / "tests").glob("test_*.py"))

SUITES = {
    "ratelimit": "全局限速器：任意 1 秒窗口内连接数不超上限",
    "ratelimit_e2e": "端到端限速：真实探测路径确实受限速约束",
    "logbuffer": "实时日志环形缓冲：500 条上限 + 动态删除",
    "scheduler": "调度器：间隔热更新 / 停止响应 / 时间窗口",
    "carriers": "三运营商平台配置（联通/电信/移动）",
    "web_console": "Web 控制台：鉴权 / 配置 / 进度 / 日志 / DNS / 任务状态",
    "frontend_static": "前端静态一致性：JS 语法 / 元素 id / API 路径 / 按钮绑定",
    "dns_sync": "DNS 下发安全性：批量/就地更新/骤降保护/网络异常不误改线上",
    "dns_cloudflare": "Cloudflare DNS 分支：分页/去重/新建失败不清空解析（假 HTTP 层）",
    "notify": "通知 webhook：飞书 / 企业微信 / 通用 JSON / 业务错误码",
    "ranges": "网段拉取解析：CloudFront / Cloudflare / ESA BGP（离线解析）",
    "cli_e2e": "CLI 端到端：scan 落盘 / monitor 多轮 / 参数校验 / 运营商选择",
    "shutdown": "优雅退出：SIGTERM/SIGINT 停止循环并退出 0 + 结果文件原子替换",
    "resources": "并发自适应：按可用内存/核数限制探测进程数（防小内存机器卡死）",
}

# 需要 Docker 的套件不放进 run_all（它单独跑）：
#   tests/docker_integration.sh


def main(argv: list[str]) -> int:
    picked = argv[1:]
    targets = [t for t in TESTS
               if not picked or any(p in t.stem for p in picked)]
    if not targets:
        print("没有匹配的测试套件。可用：")
        for t in TESTS:
            print(f"  {t.stem}")
        return 1

    print(f"仓库根目录：{ROOT}")
    print(f"将运行 {len(targets)} 个测试套件\n")

    results: list[tuple[str, str, float]] = []
    for t in targets:
        desc = next((v for k, v in SUITES.items() if k in t.stem), "")
        print("=" * 70)
        print(f"▶ {t.stem}" + (f" —— {desc}" if desc else ""))
        print("=" * 70)
        import time
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(t)], cwd=ROOT)
        dt = time.time() - t0
        ok = proc.returncode == 0
        results.append((t.stem, "PASS" if ok else "FAIL", dt))
        print(f"  → {'通过' if ok else '失败'}（{dt:.1f}s）\n")

    print("#" * 70)
    print("#  汇总")
    print("#" * 70)
    failed = 0
    for name, status, dt in results:
        print(f"  {status:<5} {name:<20} {dt:6.1f}s")
        if status == "FAIL":
            failed += 1
    print()
    if failed:
        print(f"{failed} 个套件失败")
    else:
        print(f"全部 {len(results)} 个套件通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
