"""测试公共工具：把仓库根加入 sys.path，并提供一致的断言输出。

直接 `python tests/test_xxx.py` 或 `python -m pytest tests/` 都能跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def banner(title: str) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70)


def finish(title: str) -> int:
    print()
    print("=" * 70)
    if _failures:
        print(f"{title}：失败 {len(_failures)} 项")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"{title}：全部通过")
    return 0


def require_root() -> Path:
    """确认当前工作目录是仓库根（测试里的相对路径依赖它）。"""
    if not (ROOT / "run.py").exists():
        raise SystemExit(f"找不到 {ROOT / 'run.py'}，请在仓库根目录运行")
    return ROOT
