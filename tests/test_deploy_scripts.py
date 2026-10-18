#!/usr/bin/env python3
"""部署脚本审计（纯静态，绝不执行会改系统的子命令）。

为什么需要：install.sh / onekey.sh 会装系统包、写配置、起服务。
上一轮我写审计脚本时曾让它们执行 `deploy.sh install`，真的开始安装了 ——
所以这里只做静态检查，唯一执行的命令是 `sh -n`（语法）和 `--help`（必须只打印用法）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


SCRIPTS = ["install.sh", "deploy.sh", "onekey.sh", "docker-setup.sh",
           "docker-entrypoint.sh", "healthcheck.sh", "rebuild.sh",
           "tests/docker_integration.sh"]

print("=" * 70)
print("部署脚本审计（静态）")
print("=" * 70)

# --------------------------------------------------------------------------
print()
print("1) 语法检查（sh -n，只解析不执行）")
# --------------------------------------------------------------------------
for name in SCRIPTS:
    path = ROOT / name
    if not path.exists():
        check(f"{name} 存在", False, "文件缺失")
        continue
    proc = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    check(f"{name} 语法正确", proc.returncode == 0,
          proc.stderr.strip()[:80] if proc.returncode else "ok")

# --------------------------------------------------------------------------
print()
print("2) --help 只打印用法，绝不触发安装")
# --------------------------------------------------------------------------
# 回归：install.sh / onekey.sh 原来忽略 --help，直接开始装依赖、写系统配置。
for name in ("install.sh", "onekey.sh"):
    path = ROOT / name
    try:
        proc = subprocess.run(["bash", str(path), "--help"], capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        check(f"{name} --help 立即返回", False, "超时（可能开始执行安装了）")
        continue
    check(f"{name} --help 打印用法", bool(re.search(r"用法|USAGE", out)),
          out.strip().splitlines()[0][:60] if out.strip() else "(无输出)")
    danger = re.search(r"\[install\]|\[onekey\]|apt-get install|get\.docker\.com|git clone", out)
    check(f"{name} --help 没有触发安装动作", danger is None,
          f"发现: {danger.group(0)}" if danger else "干净")

# --------------------------------------------------------------------------
print()
print("3) git 里的可执行位")
# --------------------------------------------------------------------------
proc = subprocess.run(["git", "ls-files", "-s", *SCRIPTS], capture_output=True,
                      text=True, cwd=ROOT)
modes = {}
for line in proc.stdout.splitlines():
    parts = line.split()
    if len(parts) >= 4:
        modes[parts[3]] = parts[0]
for name in SCRIPTS:
    if name == "healthcheck.sh":
        continue                      # 由 Dockerfile chmod，不进仓库可执行位要求
    mode = modes.get(name)
    check(f"{name} 可执行位正确", mode == "100755", f"{mode or '未跟踪'}")

# --------------------------------------------------------------------------
print()
print("4) 危险模式：rm -rf 后的变量必须加引号（命令位置才算）")
# --------------------------------------------------------------------------
# 只看命令位置（行首或 ; && 之后），且该行不能含引号 ——
# 否则会把提示文本里的 `rm -rf $DIR 后重新执行本命令"` 误判成危险命令。
DANGEROUS = re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+-rf\s+\$[A-Za-z_][A-Za-z0-9_]*"
                       r"(?![\"'])")
risky: list[str] = []
for name in SCRIPTS:
    path = ROOT / name
    if not path.exists():
        continue
    for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or '"' in stripped or "'" in stripped:
            continue                      # 注释或字符串里的说明文字，不是命令
        if DANGEROUS.search(stripped):
            risky.append(f"{name}:{num}: {stripped[:60]}")
check("没有未加引号的 rm -rf $VAR", not risky, "; ".join(risky) or "干净")

# --------------------------------------------------------------------------
print()
print("5) 文档引用一致性")
# --------------------------------------------------------------------------
docs = "\n".join((ROOT / n).read_text(encoding="utf-8", errors="replace")
                 for n in ("README.md", "INSTALL.md") if (ROOT / n).exists())
missing_scripts = [ref for ref in set(re.findall(r"\./(\w[\w.-]*\.sh)", docs))
                   if not (ROOT / ref).exists()]
check("文档引用的脚本都存在", not missing_scripts, str(missing_scripts))

missing_mods = [m for m in set(re.findall(r"eo/([a-z_]+\.py)", docs))
                if not (ROOT / "eo" / m).exists()]
check("文档引用的 eo/*.py 都存在", not missing_mods, str(missing_mods))

# 代码注释里引用的模块也要存在（曾经注释里写着"与 eo/isptool.py 保持一致"，
# 那个文件根本不存在）
ghost: list[str] = []
for path in (ROOT / "eo").glob("*.py"):
    text = path.read_text(encoding="utf-8")
    for ref in set(re.findall(r"eo/([a-z_]+\.py)", text)):
        if not (ROOT / "eo" / ref).exists():
            ghost.append(f"{path.name} -> eo/{ref}")
check("代码注释没有引用不存在的模块", not ghost, "; ".join(ghost) or "干净")

# --------------------------------------------------------------------------
print()
print("6) 入口脚本的降权与信号处理")
# --------------------------------------------------------------------------
entry = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
check("入口脚本会降权到非 root", "setpriv" in entry, "有 setpriv")

# 比较【调用】的先后，而不是全文里第一次出现的位置 ——
# 注释里先提到 setpriv，会让朴素的 index() 比较得出相反结论。
def _call_line(text: str, name: str) -> int:
    for num, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(rf"^{re.escape(name)}\b", stripped) or re.search(
                rf"(?:^|[;&|]\s*)(?:exec\s+)?{re.escape(name)}\b", stripped):
            return num
    return -1


own_line = _call_line(entry, "fix_ownership")
priv_line = _call_line(entry, "setpriv")
check("找到 fix_ownership 的调用", own_line > 0, str(own_line))
check("找到 setpriv 降权那一行", priv_line > 0, str(priv_line))
check("降权（setpriv）发生在修正属主之后",
      0 < own_line < priv_line, f"fix_ownership@{own_line} setpriv@{priv_line}")

dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
# 只看有效指令行：注释里我自己写了「老写法 python run.py selftest」做说明
docker_code = "\n".join(l for l in dockerfile.splitlines() if not l.strip().startswith("#"))
check("Dockerfile 声明 STOPSIGNAL SIGTERM", "STOPSIGNAL SIGTERM" in docker_code, "有")
check("健康检查不再起 Python（指令行里没有 run.py selftest）",
      "healthcheck.sh" in docker_code and "run.py selftest" not in docker_code,
      "用 curl 打 /healthz")

print()
print("=" * 70)
if _failures:
    print(f"部署脚本审计：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("部署脚本审计通过")
sys.exit(0)
