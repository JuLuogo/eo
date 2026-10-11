"""多平台边缘节点优选工具（原 eo.py 的通用化版本）。"""

from .core import (
    Platform,
    Probe,
    ScanOutcome,
    append_history,
    expand_targets,
    http_probe,
    load_targets,
    ping,
    safe_log,
    scan_platform,
    scan_platforms,
    write_result,
)
from .platforms import PLATFORMS, get_platforms

__all__ = [
    "Platform",
    "Probe",
    "ScanOutcome",
    "append_history",
    "expand_targets",
    "http_probe",
    "load_targets",
    "ping",
    "safe_log",
    "scan_platform",
    "scan_platforms",
    "write_result",
    "PLATFORMS",
    "get_platforms",
]

__version__ = "2.0.0"
