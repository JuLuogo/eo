#!/usr/bin/env python3
"""网页表单字段 ↔ 后端配置字段的一致性检查。

为什么需要：网页上的输入框是靠 `data-f="字段名"` 把值 POST 给 /api/config 的。
如果字段名和后端不认识的名字对不上（打错一个字母、或者后端改了名没同步前端），
用户填了、点了保存、页面还提示"已更新"，但值根本没存进去 —— 完全无声的失效。

这类问题静态测试本来抓不到（JS 语法对、API 存在、按钮也绑了事件），
所以单独做一次字段名对齐检查。
"""
from __future__ import annotations

import re
import sys
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eo.config_store import AppConfig, DnsSettings, PlatformSettings   # noqa: E402
from eo.pages import PAGE_HTML                                          # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def js_field_names() -> dict[str, set[str]]:
    """从 pages.py 里抽出各面板用到的字段名。

    field(label, key, name, value) -> data-f="name"
    变量面板是 field('标签', 'env_key', 'config_key', ...) 的不同写法，
    所以这里按出现顺序收集所有 data-f。
    """
    source = PAGE_HTML
    names = set(re.findall(r'data-f="([^"]+)"', source))
    # field('标签', key, 'name', val) 这种在模板字符串里拼出来的
    names |= set(re.findall(r"field\([^,]+,[^,]+,\s*'([^']+)'", source))
    return {"all": names}


print("=" * 70)
print("网页表单字段 ↔ 后端配置字段一致性")
print("=" * 70)

platform_fields = {f.name for f in fields(PlatformSettings)}
dns_fields = {f.name for f in fields(DnsSettings)}
app_fields = {f.name for f in fields(AppConfig)}

print()
print(f"后端可配置字段：PlatformSettings {len(platform_fields)} 个、"
      f"DnsSettings {len(dns_fields)} 个、AppConfig {len(app_fields)} 个")

# --------------------------------------------------------------------------
print()
print("1) 平台面板里的字段必须都是 PlatformSettings 真实存在的字段")
# --------------------------------------------------------------------------
source = PAGE_HTML
platform_section = re.search(r"function renderPlatforms.*?\n}\n", source, re.S)
check("找得到平台面板渲染函数", platform_section is not None)
used: list[str] = []
if platform_section:
    body = platform_section.group(0)
    used = re.findall(r"field\([^,]+,\s*key,\s*'([^']+)'", body)
    used += re.findall(r"selStatic\([^,]+,\s*key,\s*'([^']+)'", body)
    used += re.findall(r'data-f="([^"]+)"', body)
    used = sorted(set(used))
    unknown = [name for name in used if name not in platform_fields]
    check("字段名全部能在 PlatformSettings 里找到", not unknown,
          f"未知字段: {unknown}" if unknown else f"{len(used)} 个字段全部匹配")
    print(f"     平台面板字段: {used}")
    # 反向：新加的字段要真的出现在界面上（否则等于没做）
    for must in ("max_latency_ms", "top_n", "workers", "timeout", "retries", "sample_per_24"):
        check(f"  {must} 在界面上有输入框", must in used, "有" if must in used else "缺")

# --------------------------------------------------------------------------
print()
print("2) DNS 面板字段必须都是 DnsSettings 真实存在的字段")
# --------------------------------------------------------------------------
dns_section = re.search(r"function renderDns.*?\n}\n", source, re.S)
check("找得到 DNS 面板渲染函数", dns_section is not None)
if dns_section:
    body = dns_section.group(0)
    dns_used = sorted(set(re.findall(r"field\([^,]+,\s*key,\s*'([^']+)'", body)
                          + re.findall(r"data-df=\"([^\"]+)\"", body)
                          + re.findall(r'data-f="([^"]+)"', body)))
    unknown = [n for n in dns_used if n not in dns_fields]
    check("DNS 字段全部匹配", not unknown,
          f"未知: {unknown}" if unknown else f"{len(dns_used)} 个字段全部匹配")
    print(f"     DNS 面板字段: {dns_used}")

# --------------------------------------------------------------------------
print()
print("3) 保存路径必须真的把值送到后端")
# --------------------------------------------------------------------------
check("平台设置保存走 /api/config", "savePlatform" in source and "/api/config" in source)
check("savePlatform 用的是 data-f 属性名",
      re.search(r"dataset\.f", source) is not None, "已使用 dataset.f")
check("保存成功后有提示（否则用户以为没生效）", "已更新" in source, "有 toast")

# --------------------------------------------------------------------------
print()
print("4) 后端 update() 的字段白名单要覆盖 PlatformSettings 的全部可写字段")
# --------------------------------------------------------------------------
store_src = (ROOT / "eo" / "config_store.py").read_text(encoding="utf-8")
whitelist_match = re.search(
    r'for field_name in \(([^)]*)\):\s*\n\s*if field_name in value', store_src)
check("找得到平台字段白名单", whitelist_match is not None)
if whitelist_match:
    allowed = set(re.findall(r'"([^"]+)"', whitelist_match.group(1)))
    # key 是主键、不能改；enabled 等都应可写
    missing = sorted(platform_fields - allowed - {"key"})
    check("PlatformSettings 的字段都能被网页修改",
          not missing, f"漏掉的字段: {missing}" if missing else f"{len(allowed)} 个可写")
    extra = sorted(allowed - platform_fields)
    check("白名单里没有不存在的字段", not extra, f"多余: {extra}" if extra else "干净")

# --------------------------------------------------------------------------
print()
print("5) 全局设置面板字段要对得上 AppConfig")
# --------------------------------------------------------------------------
vars_section = re.search(r"function renderVars.*?\n}\n", source, re.S)
if vars_section:
    body = vars_section.group(0)
    var_fields = sorted(set(re.findall(r"'([a-z_]+)'\s*,\s*'[^']*'\s*,", body)))
    unknown = [n for n in var_fields if n not in app_fields and n not in platform_fields]
    check("变量面板字段都能对上后端", not unknown,
          f"未知: {unknown}" if unknown else f"{len(var_fields)} 个已对齐")

print()
print("=" * 70)
if _failures:
    print(f"前后端字段一致性：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("前后端字段一致性检查通过")
sys.exit(0)
