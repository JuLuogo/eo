#!/usr/bin/env python3
"""网页前端静态一致性测试（不需要浏览器）。

检查三件事 —— 这三类问题都会让界面「看起来正常但点了没反应」：
  1. 内嵌 JS 的语法是否正确（交给 node --check；没有 node 时跳过）
  2. JS 里 getElementById('x') 引用的 id 是否真的存在于 HTML
  3. JS 里调用的 /api/... 路径是否真的有对应的服务端路由
另外反向检查：HTML 里的按钮有没有绑定事件（避免「死按钮」）。
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.pages import PAGE_HTML            # noqa: E402
import eo.webui as webui                  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


print("=" * 70)
print("网页前端静态一致性测试")
print("=" * 70)

# ---------------------------------------------------------------- 提取
html_part = re.sub(r"<script[^>]*>.*?</script>", "", PAGE_HTML, flags=re.S)
scripts = re.findall(r"<script[^>]*>(.*?)</script>", PAGE_HTML, flags=re.S)
js = "\n".join(scripts)
print(f"\nHTML {len(html_part)} 字节，JS {len(js)} 字节（{len(scripts)} 个 script 块）")
check("页面含内联脚本", bool(scripts), f"{len(scripts)} 块")

# ---------------------------------------------------------------- 1. 语法


def find_node() -> str | None:
    """定位 node：NODE_BIN > PATH > 常见安装位置（含 WSL 下调用 Windows 版）。"""
    node = os.environ.get("NODE_BIN") or shutil.which("node") or shutil.which("nodejs")
    if node:
        return node
    cands = ["/usr/local/bin/node", "/opt/node/bin/node", "/snap/bin/node"]
    for drive in "cdefg":
        cands += [f"/mnt/{drive}/Program Files/nodejs/node.exe",
                  f"/mnt/{drive}/nodejs/node.exe",
                  f"/mnt/{drive}/app/ndejs/node.exe"]
    for cand in cands:
        if Path(cand).exists():
            return cand
    return None


def js_syntax(node: str, source: str) -> tuple[bool, str]:
    """用 node --check 校验 JS 语法，返回 (是否通过, 错误说明)。"""
    # 优先用 stdin：node --check - 不落盘，也不受「WSL 调用 Windows 版 node.exe
    # 时 Linux 路径无法解析」的影响（Windows node 会把 /tmp/x.js 当成 C:\tmp\x.js）
    proc = subprocess.run([node, "--check", "-"], input=source,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        # 兼容不支持 `-` 的旧版 node：退回临时文件方式
        tmpdir = Path(tempfile.mkdtemp(prefix="eo-jscheck-"))
        atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
        tmp = tmpdir / "page.js"
        tmp.write_text(source, encoding="utf-8")
        target = str(tmp)
        wslpath = shutil.which("wslpath")
        if node.endswith(".exe") and wslpath:
            conv = subprocess.run([wslpath, "-w", target], capture_output=True, text=True)
            if conv.returncode == 0 and conv.stdout.strip():
                target = conv.stdout.strip()
        alt = subprocess.run([node, "--check", target], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        if alt.returncode == 0:
            return True, "node --check 通过（文件模式）"

    if proc.returncode == 0:
        return True, "node --check 通过"

    # node 的 stderr 末行往往是版本号，优先摘出真正的错误行
    lines = [l.rstrip() for l in (proc.stderr or "").splitlines() if l.strip()]
    detail = next((l for l in lines if "Error" in l), None) \
        or (lines[-1] if lines else "node --check 失败")
    idx = next((i for i, l in enumerate(lines) if "Error" in l), None)
    if idx is not None and idx > 0 and not lines[idx - 1].lstrip().startswith("^"):
        detail = f"{lines[idx - 1].strip()} -> {detail}"
    return False, detail


print()
print("1) JS 语法（node --check）")
node = find_node()
if node:
    print(f"     使用 node: {node}")
    ok_js, detail = js_syntax(node, js)
    check("JS 语法正确", ok_js, detail)
    # 反向自测：校验器必须能抓到故意写错的 JS，否则上面的 OK 没有意义
    bad_ok, _ = js_syntax(node, "function broken( { return 1; }")
    check("校验器能发现语法错误（自测）", not bad_ok, "故意写错的 JS 被正确判为不通过")
else:
    print("  [SKIP] 未找到 node，跳过语法校验（可用 NODE_BIN=/path/to/node 指定，或安装 node）")


# ---------------------------------------------------------------- 2. 元素 id
print()
print("2) JS 引用的元素 id 是否存在于 HTML")
ids_in_html = set(re.findall(r'id="([^"]+)"', html_part))
ids_in_js = set(re.findall(r'getElementById\(\s*[\'"]([^\'"]+)[\'"]\s*\)', js))
print(f"     HTML 定义 {len(ids_in_html)} 个 id，JS 引用 {len(ids_in_js)} 个")
missing = sorted(ids_in_js - ids_in_html)
# 少数 id 由 JS 动态生成（例如日志行），已知可接受
DYNAMIC_OK: set[str] = set()
really_missing = [i for i in missing if i not in DYNAMIC_OK]
check("所有 getElementById 都有对应元素", not really_missing,
      f"缺失: {really_missing}" if really_missing else f"{len(ids_in_js)} 个全部匹配")
if missing:
    print(f"     （未匹配: {missing}）")

# ---------------------------------------------------------------- 3. API 路径
print()
print("3) JS 调用的 API 是否都有服务端路由")
apis_in_js = set(re.findall(r'[\'"`](/api/[A-Za-z0-9_\-/]*)[\'"`?]', js))
# 服务端路由：直接从 webui 源码里抽 path == "..." 比较可靠
src = (ROOT / "eo" / "webui.py").read_text(encoding="utf-8")
routes = set(re.findall(r'path == "(/api/[^"]+)"', src))
routes |= {r.rstrip("/") for r in routes}
print(f"     服务端路由: {sorted(routes)}")
print(f"     JS 调用: {sorted(apis_in_js)}")
unknown = sorted(a for a in apis_in_js if a.rstrip("/") not in {r.rstrip("/") for r in routes})
check("JS 调用的 API 都存在", not unknown,
      f"未知: {unknown}" if unknown else f"{len(apis_in_js)} 个全部存在")

# 反向：服务端有路由但前端没用到（提示性，不算失败）
unused = sorted(r for r in routes
                if r not in apis_in_js and r not in ("/healthz",))
if unused:
    print(f"     提示：前端未使用 {unused}")

# ---------------------------------------------------------------- 4. 死按钮
print()
print("4) 按钮是否绑定了事件")
buttons = re.findall(r'<button[^>]*id="([^"]+)"', PAGE_HTML)
print(f"     HTML 按钮: {buttons}")
dead = []
for b in buttons:
    # 绑定方式可能是 getElementById('x').onclick / addEventListener / onclick 属性
    if (f"getElementById('{b}')" in js or f'getElementById("{b}")' in js
            or f'"{b}"' in js or f"'{b}'" in js):
        continue
    dead.append(b)
check("按钮都绑定了事件", not dead, f"无绑定: {dead}" if dead else f"{len(buttons)} 个已绑定")

# ---------------------------------------------------------------- 5. 关键交互
print()
print("5) 关键交互链路完整")
must_have = {
    "启动循环": "control/start",
    "停止": "control/stop",
    "跑一轮": "control/run-once",
    "DNS 预览": "control/dns-preview",
    "DNS 下发": "control/dns-push",
    "清空日志": "logs/clear",
    "保存变量": "/api/config",
    "SSE 实时推送": "EventSource",
    "日志增量拉取": "after=",
    "进度百分比": "prog-pct",
    "每平台进度": "platforms",
    "倒计时": "fmtCountdown",
}
for label, needle in must_have.items():
    check(f"{label} 已接线", needle in js, needle)

# ---------------------------------------------------------------- 6. 容错
print()
print("6) 容错：首屏失败要能自愈、日志不能重复显示")
# 回归：原来启动函数里是 catch(e){ return; }，一次首屏失败就再也不会设置
# 轮询，页面永久空白，只能手动刷新。
check("首屏加载失败后仍然设置了轮询",
      "setInterval(pollLogs" in js and "setInterval(refresh" in js,
      "有轮询")
check("首屏失败不再是直接 return 跳过初始化",
      "await loadAll();\n  }catch(e){ return; }" not in js,
      "已改为继续初始化并提示重试")
check("refresh() 会在配置缺失时补拉配置",
      "if (!CONFIG || !STATE)" in js, "有自愈逻辑")
check("超时/失败有提示",
      "正在自动重试" in js, "有 toast 提示")

# 日志去重：SSE 与轮询两条来源共用同一个去重入口
check("日志写入统一走去重函数 addLogOnce", "function addLogOnce" in js, "已定义")
check("SSE 的日志事件走 addLogOnce",
      re.search(r"addEventListener\('log',\s*ev=>\{\s*addLogOnce\(", js) is not None,
      "已接入")
check("轮询的日志也走 addLogOnce",
      "data.logs||[]).forEach(it=>{ addLogOnce(it);" in js.replace("\n", "") or
      "addLogOnce(it)" in js, "已接入")
check("没有其他地方直接调用 addLog（绕过去重）",
      len(re.findall(r"(?<!function )addLog\(", js)) <= 2,
      f"{len(re.findall(r'(?<!function )addLog\(', js))} 处直接调用")

print()
print("=" * 70)
if _failures:
    print(f"失败 {len(_failures)} 项：")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("前端静态一致性检查通过")
sys.exit(0)
