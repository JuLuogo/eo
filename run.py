#!/usr/bin/env python3
"""多平台边缘节点优选工具入口。

    python run.py                # 扫描全部已启用平台
    python run.py list           # 查看平台清单
    python run.py -p eo esa      # 只跑指定平台
    python run.py scan --dns --dry-run

详细用法见 README.md 或 python run.py --help
"""

import sys

from eo.cli import main

if __name__ == "__main__":
    sys.exit(main())
