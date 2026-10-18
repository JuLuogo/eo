#!/usr/bin/env python3
"""环境变量三方一致性：.env.example ↔ docker-compose.yml ↔ 代码里实际读取的变量。

为什么值得查：这三处各写一份环境变量清单，很容易漂移 ——
  * 文档里写了、代码根本不读  -> 用户照着配了半天，其实没生效
  * 代码里读了、文档没写      -> 用户根本不知道有这个开关（比如 EO_MAX_MS）
  * compose 里有、示例里没有  -> 用 compose 部署的人不知道还能改
先打印集合差异，再对必须成立的部分做断言。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def env_example_vars() -> set[str]:
    out = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if m:
            out.add(m.group(1))
    return out


def compose_vars() -> set[str]:
    """docker-compose.yml 里正文出现的 ${VAR:-...} 与 VAR: 形式。"""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    out = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
    out |= set(re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):", text, re.M))
    out |= set(re.findall(r"^\s{6}-\s*([A-Z][A-Z0-9_]*)[=:]", text, re.M))
    return out


def code_vars() -> set[str]:
    """代码里实际用 os.environ 读取的变量。

    三种读法都要认：
      * os.environ.get("EO_X") / os.environ["EO_X"]
      * config_store.env_overrides 里的字典键（"EO_X": ("field", type)）
        —— 这一层最容易漏，因为它不是 os.environ 调用
      * 入口/健康检查脚本里的 $EO_X 与 ${EO_X:-默认}
    """
    out: set[str] = set()
    for path in list((ROOT / "eo").glob("*.py")) + [ROOT / "run.py", ROOT / "updatedns.py"]:
        text = path.read_text(encoding="utf-8")
        out |= set(re.findall(r'environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', text))
        out |= set(re.findall(r'environ\[\s*"([A-Z][A-Z0-9_]*)"\s*\]', text))
    # env_overrides 映射表（它是 env_overrides() 里的局部字典 mapping = {...}，
    # 所以直接扫 config_store 里出现的所有 "EO_xxx": 键名）
    store = (ROOT / "eo" / "config_store.py").read_text(encoding="utf-8")
    out |= set(re.findall(r'"(EO_[A-Z0-9_]+)"\s*:', store))
    # 脚本：带花括号和不带花括号都算
    for name in ("docker-entrypoint.sh", "healthcheck.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        out |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)[:\-}]", text))
        out |= set(re.findall(r"\$([A-Z][A-Z0-9_]{2,})\b", text))
    return out


# 这些是内部实现细节，不需要写进给用户看的 .env.example
INTERNAL = {
    "EO_CONFIG",              # config.json 路径（测试与部署脚本内部用）
    "EO_CURL",                # curl 可执行文件路径（只在 EO_PROBE=curl 时用到）
    "EO_ENV_FILE",            # deploytool 写 .env 时用
    "EO_WEB_HEALTHCHECK_HOST",  # 健康检查打哪个地址，默认 127.0.0.1
}


example = env_example_vars()
compose = compose_vars()
code = code_vars()

print("=" * 70)
print("环境变量三方一致性")
print("=" * 70)
print()
print(f"  .env.example        {len(example)} 个")
print(f"  docker-compose.yml  {len(compose)} 个")
print(f"  代码/入口脚本实际读取 {len(code)} 个")

# 只比较 EO_ 开头的（业务配置）；镜像/端口/凭据类变量单独看
eo_example = {v for v in example if v.startswith("EO_")}
eo_code = {v for v in code if v.startswith("EO_")}
eo_compose = {v for v in compose if v.startswith("EO_")}

print()
print("1) 代码读取的 EO_* 必须能在 .env.example 里查到（否则等于隐藏开关）")
undocumented = sorted(eo_code - eo_example - INTERNAL)
check("没有未文档化的 EO_* 变量", not undocumented,
      f"缺文档: {undocumented}" if undocumented else
      f"{len(eo_code)} 个（其中 {len(eo_code & INTERNAL)} 个内部变量）都有说明")

print()
print("2) .env.example 里的 EO_* 必须真的被代码读取（否则是无效开关）")
# EO_BIND / EO_IMAGE 只对 compose 插值有意义，不经过代码读取
COMPOSE_ONLY_VARS = {"EO_BIND", "EO_IMAGE"}
dead = sorted(eo_example - eo_code - COMPOSE_ONLY_VARS)
check("没有写了但没人读的 EO_* 变量", not dead,
      f"无效: {dead}" if dead else f"{len(eo_example)} 个都有效")

print()
print("3) docker-compose.yml 里的 EO_* 必须都在 .env.example 中（compose 用户也看得到）")
COMPOSE_ONLY = {"EO_IMAGE"}   # 只对 compose 有意义（选镜像）
missing_in_example = sorted(eo_compose - eo_example - COMPOSE_ONLY)
check("compose 变量都有示例说明", not missing_in_example,
      f"缺: {missing_in_example}" if missing_in_example else f"{len(eo_compose)} 个都有")

print()
print("4) .env.example 里能影响扫描行为的变量，都必须在 compose 里透传")
# 这些是用户最可能去调的；漏了就会"在 .env 里改了没反应"
# —— EO_MAX_MS / EO_TOP / EO_WEB_AUTH 曾经真的漏了。
IMPORTANT = {"EO_MAX_MS", "EO_TOP", "EO_RATE", "EO_MAX_CONCURRENT", "EO_WORKERS",
             "EO_TIMEOUT", "EO_RETRIES", "EO_INTERVAL", "EO_NETWORK", "EO_PLATFORMS",
             "EO_WEB_AUTH", "EO_WEB_TOKEN", "EO_DNS_SYNC", "EO_CONFIRM_DNS"}
not_in_compose = sorted(v for v in IMPORTANT if v in eo_example and v not in eo_compose)
check("关键变量都在 compose 里透传", not not_in_compose,
      f"未透传: {not_in_compose}" if not_in_compose else f"{len(IMPORTANT)} 个关键变量齐全")

print()
print("5) 代码读取的每一个 EO_* 都要有一条进入容器的路径")
# 只写在 .env 里是不够的：compose 必须透传，或者入口脚本/Dockerfile 里带上。
compose_all = compose
unreachable = sorted(
    v for v in eo_code - INTERNAL
    if v not in compose_all and v not in {"EO_MODE", "EO_NETWORK", "EO_AUTO_RUN"}
)
check("没有「写了也进不了容器」的变量", not unreachable,
      f"无法传入: {unreachable}" if unreachable else "全部可达（compose 透传或入口脚本处理）")

print()
print("6) 打印差异明细（供人工核对）")
print(f"     代码读取但示例未写: {sorted(eo_code - eo_example) or '无'}")
print(f"     示例写了但代码未读: {sorted(eo_example - eo_code) or '无'}")
print(f"     compose 未透传的关键变量: {not_in_compose or '无'}")

print()
print("=" * 70)
if _failures:
    print(f"环境变量一致性：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("环境变量一致性检查通过")
sys.exit(0)
