"""audit.py - Simple and robust audit logging for DanTech Studio operations.

This module NEVER raises: every I/O failure is swallowed silently so the GUI
and the other core modules can call :func:`audit` without try/except blocks.
Audit lines are appended to a single UTF-8 log file; when running frozen the
file lives under ``%LOCALAPPDATA%\\DanTechStudio\\logs`` (a writable location,
never the read-only ``_MEIPASS`` temp dir), otherwise under the project
``logs`` folder.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from utils import process_runner

#: Line format used for every audit record.
_LINE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _log_dir() -> Path:
    """Resolve the directory that holds the audit log file.

    Frozen builds (PyInstaller) point at ``%LOCALAPPDATA%\\DanTechStudio\\logs``
    because ``sys._MEIPASS`` is a temporary, read-only extraction folder. A
    source checkout resolves to the project ``logs`` folder via
    ``process_runner.get_resource_path``.

    Returns:
        The directory path where audit logs are stored.
    """
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return Path(os.environ.get("LOCALAPPDATA", ".")) / "DanTechStudio" / "logs"
    return Path(process_runner.get_resource_path("logs"))


def log_file() -> Path:
    """Return the audit log file path, creating its parent directory if needed.

    Returns:
        Absolute path of ``audit.log``.
    """
    directory = _log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass
    return directory / "audit.log"


def audit(action: str, detail: str = "") -> None:
    """Append one audit line, failing silently on any I/O error.

    Args:
        action: Short verb describing the operation (e.g. ``"cleanup"``).
        detail: Optional contextual text stored after the action.
    """
    try:
        timestamp = datetime.now().strftime(_LINE_FORMAT)
        line = f"{timestamp} | {action} | {detail}\n"
        target = log_file()
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except (PermissionError, OSError):
        pass


def read_audit_log(limit: int = 200) -> List[str]:
    """Read the last ``limit`` lines of the audit log.

    Args:
        limit: Maximum number of lines to return (newest last).

    Returns:
        The trailing log lines, or an empty list when the file is missing or
        cannot be read.
    """
    try:
        target = log_file()
        if not target.is_file():
            return []
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        if limit > 0:
            return lines[-limit:]
        return lines
    except (PermissionError, OSError):
        return []
