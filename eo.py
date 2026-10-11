#!/usr/bin/env python3
"""向后兼容入口：老命令 ``python eo.py`` 依然可用，行为与改造前一致。

等价于：
    python run.py scan -p eo --targets eo.txt --no-write  之后把结果写回 yes.txt
实际实现直接把结果写入 yes.txt，保持原脚本的输入输出约定。
"""

import sys
from pathlib import Path

from eo.core import safe_log, scan_platform, write_result
from eo.platforms import PLATFORMS

ROOT = Path(__file__).resolve().parent
LEGACY_INPUT = ROOT / "eo.txt"
LEGACY_OUTPUT = ROOT / "yes.txt"


def main() -> int:
    platform = PLATFORMS["eo"]
    input_path = LEGACY_INPUT if LEGACY_INPUT.exists() else Path(platform.targets_file)
    output_path = LEGACY_OUTPUT                # 保持原脚本写 yes.txt 的约定
    previous_output = platform.output_file

    if not input_path.exists():
        safe_log(f"找不到网段文件: {input_path}")
        return 2

    # 直接复用平台配置，但把输入输出换回老路径
    platform.targets_file = str(input_path)
    platform.output_file = str(output_path)
    try:
        outcome = scan_platform(platform)
    finally:
        platform.output_file = previous_output

    write_result(output_path, outcome.passed)
    if outcome.error:
        safe_log(f"\n⚠ {outcome.error}")
        return 1
    safe_log(f"\n✅ 检测完成，{len(outcome.passed)} 个IP可用，已写入 {output_path.name}")
    safe_log("   提示：现在支持多平台同时优选，试试 python run.py list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
