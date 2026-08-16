"""system_repair.py - SystemRepair: SFC, DISM and network-stack reset.

Windows-only core module. All system commands are executed through
``utils.process_runner`` (never raw subprocess): the GUI and this module only
route tokens, while process_runner handles windows/streams/timeouts.

Every operation here requires Administrator privileges. When elevation is
missing the method returns a failed result WITHOUT executing anything;
auto-elevation is a GUI decision.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from utils.process_runner import (
    CommandResult,
    ProcessRunnerError,
    ensure_windows,
    is_admin,
    run_command,
    run_command_async,
)

#: User-facing error shown whenever an admin-only operation is blocked.
_ADMIN_REQUIRED_ERROR = "Se requieren permisos de administrador."

#: Ordered reset_network_stack command sequence. All run even on partial failure.
_NETWORK_RESET_COMMANDS: Sequence[Sequence[str]] = (
    ("netsh", "winsock", "reset"),
    ("netsh", "int", "ip", "reset"),
    ("ipconfig", "/flushdns"),
)


@dataclass
class RepairReport:
    """Aggregated outcome of a multi-step repair operation."""

    operation: str
    results: list[CommandResult] = field(default_factory=list)
    success: bool = field(init=False)

    def __post_init__(self) -> None:
        self.success = all(result.success for result in self.results)


def _admin_blocked_result() -> CommandResult:
    """Build the canonical failed result used when elevation is missing."""
    return CommandResult(
        stderr=_ADMIN_REQUIRED_ERROR,
        returncode=-1,
        success=False,
        error=_ADMIN_REQUIRED_ERROR,
    )


def _spawn_immediate_result(
    result: CommandResult,
    on_complete: Callable[[CommandResult], None],
    on_error: Optional[Callable[[CommandResult], None]],
) -> threading.Thread:
    """Deliver a precomputed result through the async callback contract.

    Mirrors ``run_command_async``: results that could not even start (error set)
    go to ``on_error`` when provided, otherwise to ``on_complete``.

    Args:
        result: Already-built :class:`CommandResult` (e.g. admin block).
        on_complete: Success/completion callback.
        on_error: Start-failure callback.

    Returns:
        The started daemon thread.
    """

    def _worker() -> None:
        if not result.success and result.error and on_error is not None:
            on_error(result)
        else:
            on_complete(result)

    thread = threading.Thread(target=_worker, name="result-immediate", daemon=True)
    thread.start()
    return thread


class SystemRepair:
    """Run Windows integrity and network-stack repair tools.

    Each public method verifies ``is_admin()`` first and returns a failed
    result with a clear Spanish message instead of executing without rights.
    """

    # ------------------------------------------------------------ public: sync

    def sfc_scan(self) -> CommandResult:
        """Run ``sfc /scannow`` (System File Checker) synchronously.

        Requires Administrator privileges.

        Returns:
            A :class:`CommandResult` with the SFC output and exit code.
        """
        ensure_windows()
        if not is_admin():
            return _admin_blocked_result()
        return run_command(("sfc", "/scannow"))

    def dism_restore_health(self) -> CommandResult:
        """Run DISM RestoreHealth to repair the Windows system image.

        Requires Administrator privileges. May take a long time; no timeout is
        imposed so a slow restore is never cancelled prematurely.

        Returns:
            A :class:`CommandResult` with the DISM output and exit code.
        """
        ensure_windows()
        if not is_admin():
            return _admin_blocked_result()
        return run_command(("DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"))

    def reset_network_stack(self) -> RepairReport:
        """Reset Winsock, the TCP/IP stack and the DNS cache in sequence.

        Requires Administrator privileges. The three commands always run even
        when an earlier one fails; failures accumulate in ``results``.

        Returns:
            A :class:`RepairReport` whose ``success`` is True only when every
            step succeeded.
        """
        ensure_windows()
        if not is_admin():
            return RepairReport(
                operation="reset_network_stack",
                results=[_admin_blocked_result()],
            )

        results: list[CommandResult] = []
        for command in _NETWORK_RESET_COMMANDS:
            results.append(run_command(command))
        return RepairReport(operation="reset_network_stack", results=results)

    # ----------------------------------------------------------- public: async

    def sfc_scan_async(
        self,
        on_complete: Callable[[CommandResult], None],
        on_error: Optional[Callable[[CommandResult], None]] = None,
    ) -> threading.Thread:
        """Run ``sfc_scan`` in a background thread without blocking the GUI.

        Args:
            on_complete: Called with the :class:`CommandResult` when finished.
            on_error: Called when execution could not even start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        if not is_admin():
            return _spawn_immediate_result(_admin_blocked_result(), on_complete, on_error)
        return run_command_async(
            ("sfc", "/scannow"),
            on_complete=on_complete,
            on_error=on_error,
        )

    def dism_restore_health_async(
        self,
        on_complete: Callable[[CommandResult], None],
        on_error: Optional[Callable[[CommandResult], None]] = None,
    ) -> threading.Thread:
        """Run ``dism_restore_health`` in a background thread.

        Args:
            on_complete: Called with the :class:`CommandResult` when finished.
            on_error: Called when execution could not even start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        if not is_admin():
            return _spawn_immediate_result(_admin_blocked_result(), on_complete, on_error)
        return run_command_async(
            ("DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"),
            on_complete=on_complete,
            on_error=on_error,
        )

    def reset_network_stack_async(
        self,
        on_complete: Callable[[RepairReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``reset_network_stack`` (the full sequence) in a daemon thread.

        Args:
            on_complete: Called with the :class:`RepairReport` when finished.
            on_error: Called with the exception when the sequence cannot start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        if not is_admin():
            blocked = RepairReport(
                operation="reset_network_stack",
                results=[_admin_blocked_result()],
            )
            return _spawn_immediate_repair_report(blocked, on_complete, on_error)

        def _worker() -> None:
            try:
                report = self.reset_network_stack()
            except ProcessRunnerError as exc:
                if on_error is not None:
                    on_error(exc)
                return
            on_complete(report)

        thread = threading.Thread(target=_worker, name="reset-network-stack", daemon=True)
        thread.start()
        return thread


def _spawn_immediate_repair_report(
    report: RepairReport,
    on_complete: Callable[[RepairReport], None],
    on_error: Optional[Callable[[Exception], None]],
) -> threading.Thread:
    """Deliver a precomputed RepairReport through the async callback contract.

    Args:
        report: Already-built :class:`RepairReport` (e.g. admin block).
        on_complete: Completion callback.
        on_error: Failure callback (receives the block result's exception).

    Returns:
        The started daemon thread.
    """

    def _worker() -> None:
        if on_error is not None:
            on_error(ProcessRunnerError(_ADMIN_REQUIRED_ERROR))
        else:
            on_complete(report)

    thread = threading.Thread(target=_worker, name="repair-immediate", daemon=True)
    thread.start()
    return thread