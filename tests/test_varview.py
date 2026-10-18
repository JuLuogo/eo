#!/usr/bin/env python3
"""变量来源标注测试：面板说「谁生效」，就必须真的是谁生效。

标错的危害：用户看到「环境变量生效」，于是去改 .env，改完还是没变化 ——
因为实际生效的是默认值（环境变量里那个值是非法/被忽略的）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit                                                      # noqa: E402

from eo import config_store                                 # noqa: E402
from eo.config_store import ConfigStore                      # noqa: E402
from eo.varview import vars_view                             # noqa: E402

_failures: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="eo-varview-"))
atexit.register(shutil.rmtree, WORK, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def view(store: ConfigStore, env_path: Path) -> dict[str, dict]:
    rows = vars_view(store.get(), env_path=env_path, config_path=store.path,
                     explicit=store.explicit, from_env=store.from_env)
    return {r["key"]: r for r in rows}


# env_overrides 关心的键 + 令牌。每个用例开始前都要清掉：
# load_dotenv 会把上一个用例的 .env 值注入 os.environ 并一直留着，
# 而「进程环境变量优先于 .env 文件」，不清就会串味（实测踩到过）。
TRACKED_ENV = [
    "EO_RATE", "EO_INTERVAL", "EO_WINDOW", "EO_WORKERS", "EO_TIMEOUT", "EO_RETRIES",
    "EO_LOG_BUFFER", "EO_MAX_MS", "EO_TOP", "EO_WEB_AUTH", "EO_WEBHOOK", "EO_AUTO_RUN",
    "EO_DNS_SYNC", "EO_PING", "EO_WEB_TOKEN",
]


def fresh(tag: str, env_text: str = "") -> tuple[ConfigStore, Path]:
    """建一个隔离的 store。

    顺序很重要：先把上一个用例残留的环境变量清掉，再写本用例的 .env，
    然后才构造 ConfigStore —— 真实启动流程就是先 load_dotenv() 再建 store。
    同时把 env_file_path 指向临时文件，避免读到项目根目录真实的 .env。
    """
    d = WORK / tag
    d.mkdir(parents=True, exist_ok=True)
    envf = d / ".env"
    envf.write_text(env_text, encoding="utf-8")

    for key in TRACKED_ENV:
        os.environ.pop(key, None)

    real_env_file_path = config_store.env_file_path
    config_store.env_file_path = lambda *a, **kw: envf        # type: ignore[assignment]
    try:
        store = ConfigStore(d / "config.json")
    finally:
        config_store.env_file_path = real_env_file_path       # type: ignore[assignment]
    return store, envf


def cleanup_env(keys: list[str]) -> None:
    for k in keys:
        os.environ.pop(k, None)


print("=" * 70)
print("变量来源标注：说谁生效就得是谁生效")
print("=" * 70)

for key in ("EO_RATE", "EO_WORKERS", "EO_INTERVAL", "EO_TIMEOUT"):
    os.environ.pop(key, None)

# --------------------------------------------------------------------------
print()
print("1) 没有 .env、没有网页设置 -> 标为默认值")
# --------------------------------------------------------------------------
store, envf = fresh("a")
rows = view(store, envf)
check("rate 来源=default", rows["rate"]["source"] == "default", rows["rate"]["source"])
check("rate 生效值=默认 100", rows["rate"]["effective"] == 100, str(rows["rate"]["effective"]))
check("from_env 为空", store.from_env == set(), str(store.from_env))

# --------------------------------------------------------------------------
print()
print("2) .env 里写了合法值 -> 标为环境变量，且生效值等于它")
# --------------------------------------------------------------------------
store, envf = fresh("b", env_text="EO_RATE=42\n")
rows = view(store, envf)
check("rate 来源=env", rows["rate"]["source"] == "env", rows["rate"]["source"])
check("rate 生效值=42", rows["rate"]["effective"] == 42, str(rows["rate"]["effective"]))
check("from_env 记录了 rate", "rate" in store.from_env, str(store.from_env))

# --------------------------------------------------------------------------
print()
print("3) .env 里写了【非法】值 -> 不能被标成「环境变量生效」（本轮重点）")
# --------------------------------------------------------------------------
# 非法值会被 env_overrides 的 caster 丢掉，实际生效的是默认值。
# 如果面板还标「环境变量」，用户就会一直去改那个根本没生效的变量。
store, envf = fresh("c", env_text="EO_RATE=abc\n")
rows = view(store, envf)
r = rows["rate"]
check("非法环境变量：生效值回落到默认 100", r["effective"] == 100, str(r["effective"]))
check("非法环境变量：来源不能标成 env", r["source"] != "env",
      f"source={r['source']} effective={r['effective']}")
check("非法环境变量不进入 from_env", "rate" not in store.from_env, str(store.from_env))
check("面板仍能看到那个非法值（便于排查）", r["env_value"] == "abc", str(r["env_value"]))

# --------------------------------------------------------------------------
print()
print("3b) 部分非法：合法的照常生效，非法的回落默认")
# --------------------------------------------------------------------------
store, envf = fresh("c2", env_text="EO_RATE=55\nEO_WORKERS=xyz\n")
rows = view(store, envf)
check("合法的 rate 生效", rows["rate"]["effective"] == 55
      and rows["rate"]["source"] == "env", f"{rows['rate']['effective']} {rows['rate']['source']}")
check("非法的 workers 回落默认并标 default",
      rows["workers"]["source"] == "default", rows["workers"]["source"])

# --------------------------------------------------------------------------
print()
print("4) 网页上设置过 -> 网页值优先，来源标 web")
# --------------------------------------------------------------------------
store, envf = fresh("d", env_text="EO_RATE=42\n")
store.update({"rate": 77})
rows = view(store, envf)
r = rows["rate"]
check("来源=web", r["source"] == "web", r["source"])
check("生效值=网页的 77", r["effective"] == 77, str(r["effective"]))
check("网页值也展示出来", r["web_value"] == 77, str(r["web_value"]))
check("环境变量值仍能看到（便于对照）", r["env_value"] == "42", str(r["env_value"]))

# --------------------------------------------------------------------------
print()
print("5) 面板列出的键必须都是 AppConfig 真实字段")
# --------------------------------------------------------------------------
from dataclasses import fields                                # noqa: E402

from eo.config_store import AppConfig                          # noqa: E402
from eo.varview import COMMON_VARS                             # noqa: E402

app_fields = {f.name for f in fields(AppConfig)}
bad = [k for k, _, _ in COMMON_VARS if k not in app_fields]
check("面板键都是真实字段", not bad, f"未知: {bad}" if bad else f"{len(COMMON_VARS)} 个")

from eo.varview import ENV_MAP                                 # noqa: E402

missing_map = [k for k, _, _ in COMMON_VARS if k not in ENV_MAP]
check("每个键都映射了环境变量名", not missing_map, str(missing_map))

# --------------------------------------------------------------------------
print()
print("6) 敏感信息不能在面板里泄露")
# --------------------------------------------------------------------------
store, envf = fresh("e")
envf.write_text("EO_WEB_TOKEN=super-secret-token`nHUAWEI_SK=secret-sk`n", encoding="utf-8")
rows = view(store, envf)
leaked = [k for k, r in rows.items() if "secret" in json.dumps(r, ensure_ascii=False)]
check("常用变量面板不含密钥内容", not leaked, str(leaked))

from eo.varview import info_view                               # noqa: E402

info = {r["key"]: r["value"] for r in info_view(envf)}
check("令牌只显示是否已设置", info.get("EO_WEB_TOKEN") == "已设置", str(info.get("EO_WEB_TOKEN")))
check("信息面板不含华为密钥", "HUAWEI_SK" not in info, str(list(info)))

print()
print("=" * 70)
if _failures:
    print(f"变量来源标注测试：失败 {len(_failures)} 项")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("变量来源标注测试全部通过")
sys.exit(0)
