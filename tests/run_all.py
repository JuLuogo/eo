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
    "probe": "探测实现：进程内 socket+ssl 与 curl 行为一致（状态码/超时/TLS/对端 IP）",
    "filter": "结果筛选：延迟门槛 / 只取最快 N 个 / 稳定性排序",
    "expand_guard": "超大网段保护：必须先判数量再展开（否则 /8 直接 OOM）",
    "leaks": "长跑稳定性：多轮之后线程/fd 不增长、日志缓冲不膨胀",
    "deep": "深扫：命中 /24 全量展开、退化抽样、总量截断、与筛选组合",
    "dns_cli": "平台 key 展开（-p eo 基础名）+ updatedns.py 独立下发入口",
    "sse": "SSE 长连接：事件推送、多标签页、断开回收、连接超时",
    "atomic_write": "原子写：并发读写看不到半截文件、写入失败不破坏原文件",
    "env_consistency": "环境变量三方一致性：.env.example ↔ compose ↔ 代码实际读取",
    "field_alignment": "网页表单字段 ↔ 后端配置字段一致性（防静默保存失败）",
    "window": "时间窗口：跨零点、端点、非法写法不能静默停摆",
    "dns_edge": "DNS 下发极端输入：去重、非法地址、分批、骤降保护、失败不留残局",
    "env_roundtrip": ".env 读写往返：含引号/井号/空格/反斜杠/BOM 的值不能写坏",
    "varview": "网页「常用设置」的来源标注：说谁生效就得是谁生效",
    "global_types": "全局配置字段的类型安全：字符串数值/布尔串/空串/非法值都不能改坏配置",
    "isp_detect": "运营商识别（deploy.sh isp）：中英文归属名、ASN 兜底、异常不崩",
    "auto_targets": "自动拉取网段：新鲜度冷却、失败保留旧文件、接线正确不复活死字段",
    "deploy_scripts": "部署脚本静态审计：语法、--help 不触发安装、可执行位、危险 rm、文档引用",
    "docs_commands": "文档里的 run.py 命令必须真能被解析（含省略子命令的写法）",
    "progress": "多线程下的进度计数：不能少算，进度要能到 100%",
    "web_routes": "网页路由覆盖 + 真实行为：页面/健康检查/404/鉴权",
    "dns_selection": "结果文件保留全部、DNS 只下发筛选后的一批（门槛 + 取前 N）",
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
