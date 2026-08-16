"""process_runner.py - ProcessRunner: UAC verification/elevation and command execution.

Single execution contract for the whole DanTech Studio suite. The GUI layer
MUST never run system commands directly: every process execution is routed
through this module, keeping presentation and system access decoupled.

Design rules:
- Windows-only (UAC, CREATE_NO_WINDOW). Non-Windows platforms raise clearly.
- Type hints, explicit exception handling and docstrings on every public method.
- Long-running commands can be launched in background threads through
  ``run_command_async`` so the UI never freezes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, Union

#: ``CREATE_NO_WINDOW`` flag: prevents any console window from flashing on screen.
_CREATE_NO_WINDOW = 0x08000000

#: Commands that request elevation are launched without waiting; the UAC dialog
#: is handled by Windows itself.
_SW_HIDE = 0
_SW_SHOW = 1


class ProcessRunnerError(RuntimeError):
    """Raised for programming errors or unsupported platforms.

    Operational failures (non-zero exit codes, timeouts) are NOT raised:
    they are reported through :class:`CommandResult`.
    """


@dataclass
class CommandResult:
    """Normalized outcome of a finished process execution."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    success: bool = False
    elapsed: float = 0.0
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def combined(self) -> str:
        """Both streams concatenated, useful for log panels."""
        return f"{self.stdout}\n{self.stderr}".strip()


def ensure_windows() -> None:
    """Raise on non-Windows platforms to fail fast with a clear message."""
    if os.name != "nt" or sys.platform != "win32":
        raise ProcessRunnerError(
            "DanTech Studio solo se ejecuta en Windows: "
            "UAC, Defender, winget y netsh no estan disponibles fuera de la plataforma."
        )


def is_admin() -> bool:
    """Return True when the current process has Administrator privileges.

    Uses ``shell32.IsUserAnAdmin`` through ctypes; no subprocess overhead.
    """
    ensure_windows()
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError) as exc:  # pragma: no cover - defensive
        return False


def run_as_admin(operation: str, show_window: bool = False) -> bool:
    """Request UAC elevation for an executable or document.

    Launches ``operation`` (a path, or a command line when it contains spaces
    in its first token) with the ``runas`` verb so Windows shows the consent
    dialog. The call returns immediately; it does NOT wait for the elevated
    process to finish.

    Args:
        operation: Executable/verb target (path or command line).
        show_window: Whether the elevated process should show a console window.

    Returns:
        True when the elevation was accepted/launched, False when the user
        cancelled the UAC prompt or the request failed.
    """
    ensure_windows()
    import ctypes

    verb = "runas"
    params: Optional[str] = None
    file_path = operation

    # ShellExecuteW splits on the first space; when the target carries
    # arguments, pass them explicitly so the path is not truncated.
    if " " in operation and not operation.startswith('"'):
        head, _, tail = operation.partition(" ")
        if head.endswith(".exe") or os.path.exists(head):
            file_path = head
            params = tail

    show = _SW_SHOW if show_window else _SW_HIDE
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, verb, file_path, params, None, show)
    except (AttributeError, OSError) as exc:
        return False

    # ShellExecuteW returns a value > 32 on success.
    return result > 32


def find_executable(names: Iterable[str]) -> Optional[Path]:
    """Locate an executable by name across PATH and common system folders.

    Used to discover tools such as ``winget.exe`` or ``MpCmdRun.exe`` whose
    location varies between Windows versions and installs.

    Args:
        names: Candidate executable names, e.g. ``("winget.exe",)``.

    Returns:
        Absolute path of the first match, or None when not found.
    """
    search_dirs = [
        Path(os.environ.get("WINDIR", r"C:\Windows")),
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps",
        Path(r"C:\Program Files\Windows Defender"),
        Path(r"C:\Program Files (x86)\Windows Defender"),
    ]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
        for directory in search_dirs:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def run_command(
    command: Sequence[str],
    timeout: Optional[float] = None,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CommandResult:
    """Execute a command synchronously and capture its output.

    The command is never shown in a console window (CREATE_NO_WINDOW). Output
    is decoded as UTF-8 with lossy replacement so localized tools never crash
    the parser. Operational errors never raise: they are encoded in the result.

    Args:
        command: Tokens of the command line, e.g. ``("sfc", "/scannow")``.
        timeout: Optional wall-clock limit in seconds.
        cwd: Working directory for the child process.
        env: Optional environment overrides merged over the current one.

    Returns:
        A :class:`CommandResult` with streams, exit code and timing.
    """
    ensure_windows()
    if not command:
        raise ProcessRunnerError("run_command requiere al menos un token de comando.")

    resolved_env = None
    if env:
        resolved_env = {**os.environ, **env}

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=resolved_env,
            creationflags=_CREATE_NO_WINDOW,
            shell=False,
        )
        elapsed = time.monotonic() - started
        return CommandResult(
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
            success=completed.returncode == 0,
            elapsed=elapsed,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        return CommandResult(
            stderr="Comando cancelado por timeout.",
            returncode=-1,
            success=False,
            elapsed=elapsed,
            timed_out=True,
            error=f"Timeout tras {timeout} segundos: {exc}",
        )
    except (OSError, ValueError) as exc:
        elapsed = time.monotonic() - started
        return CommandResult(
            stderr=str(exc),
            returncode=-1,
            success=False,
            elapsed=elapsed,
            error=f"No se pudo lanzar el proceso: {exc}",
        )


def run_command_async(
    command: Sequence[str],
    on_complete: Callable[[CommandResult], None],
    on_error: Optional[Callable[[CommandResult], None]] = None,
    timeout: Optional[float] = None,
    cwd: Optional[Union[str, Path]] = None,
) -> threading.Thread:
    """Run a command in a daemon thread and report back through callbacks.

    Keeps the UI responsive while SFC/DISM/Defender work in the background.
    Callbacks are invoked from the worker thread; GUI code MUST marshal UI
    updates back to the main thread (e.g. ``widget.after``) to stay safe.

    Args:
        command: Tokens of the command line.
        on_complete: Called with the result when the process finishes.
        on_error: Called instead of ``on_complete`` when execution could not
            even start (result.success is False and result.error is set).
        timeout: Optional wall-clock limit in seconds.
        cwd: Working directory for the child process.

    Returns:
        The started daemon thread (joinable for tests/sync callers).
    """

    def _worker() -> None:
        result = run_command(command, timeout=timeout, cwd=cwd)
        if not result.success and result.error and on_error is not None:
            on_error(result)
        else:
            on_complete(result)

    thread = threading.Thread(target=_worker, name=f"run-{command[0] if command else 'cmd'}", daemon=True)
    thread.start()
    return thread


def run_powershell_async(
    script: str,
    on_complete: Callable[[CommandResult], None],
    on_error: Optional[Callable[[CommandResult], None]] = None,
    timeout: Optional[float] = None,
) -> threading.Thread:
    """Execute a PowerShell snippet/script file in a background thread.

    Prefer a script FILE over inline code: quoting is delegated to
    ``-ExecutionPolicy Bypass -File`` and the payload travels as a literal
    argument, avoiding command-line injection surprises.

    Args:
        script: Path to a ``.ps1`` file to execute.
        on_complete: Called with the result when PowerShell finishes.
        on_error: Called when the process could not be launched.
        timeout: Optional wall-clock limit in seconds.

    Returns:
        The started daemon thread.
    """
    powershell = find_executable(("powershell.exe",))
    if powershell is None:
        raise ProcessRunnerError("No se encontro powershell.exe en el sistema.")

    command = [
        str(powershell),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(Path(script).resolve()),
    ]
    return run_command_async(command, on_complete=on_complete, on_error=on_error, timeout=timeout)