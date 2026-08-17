"""driver_manager.py - DriverManager: driver info and best-effort update.

Windows-only core module. Driver inventory is read through ``Get-CimInstance
Win32_PnPSignedDriver`` and parsed from JSON (locale-independent). Updates are
performed as a best-effort sequence: Windows exposes no single "update all
drivers" command line, so the flow creates a System Restore point first and
then forces a hardware-rescan with ``pnputil /scan-devices``. All subprocesses
go through ``utils.process_runner`` (never raw subprocess).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

from utils.process_runner import (
    CommandResult,
    ensure_windows,
    is_admin,
    run_command,
    run_command_async,
)

#: User-facing error shown whenever an admin-only operation is blocked.
_ADMIN_REQUIRED_ERROR = "Se requieren permisos de administrador."

#: Timeout for the driver inventory query (CIM enumeration can be slow).
_LIST_DRIVERS_TIMEOUT = 120.0

#: Default label used when creating the pre-update restore point.
_DEFAULT_RESTORE_DESCRIPTION = "DanTech Studio - antes de actualizar drivers"

_T = TypeVar("_T")


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

    thread = threading.Thread(target=_worker, name="driver-immediate", daemon=True)
    thread.start()
    return thread


def _run_in_thread(
    fn: Callable[[], _T],
    on_complete: Callable[[_T], None],
    on_error: Optional[Callable[[Exception], None]],
    name: str,
) -> threading.Thread:
    """Run an in-process callable in a daemon thread, mirroring the async contract.

    Args:
        fn: Zero-argument callable producing the result.
        on_complete: Called with the result when ``fn`` returns.
        on_error: Called with the raised exception when ``fn`` fails.
        name: Thread name for diagnostics.

    Returns:
        The started daemon thread.
    """

    def _worker() -> None:
        try:
            result = fn()
        except Exception as exc:  # pragma: no cover - defensive
            if on_error is not None:
                on_error(exc)
            return
        on_complete(result)

    thread = threading.Thread(target=_worker, name=name, daemon=True)
    thread.start()
    return thread


def _parse_json_output(stdout: str) -> list[dict]:
    """Parse a PowerShell ``ConvertTo-Json`` payload (object or array)."""
    text = stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


@dataclass
class DriverInfo:
    """Basic inventory data of one signed driver."""

    device_name: str
    driver_version: str
    manufacturer: str


@dataclass
class StepResult:
    """One command step of the best-effort update sequence."""

    label: str
    result: CommandResult


@dataclass
class DriverUpdateResult:
    """Outcome of the best-effort driver update sequence."""

    restore_point_created: bool = False
    restore_point_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)
    success: bool = False


class DriverManager:
    """Query driver inventory and run a best-effort driver refresh.

    Sync methods return their dataclass reports; ``*_async`` variants run the
    slowest operations (CIM inventory, update sequence) on daemon threads so
    the GUI stays fluid.
    """

    # ------------------------------------------------------------ public: sync

    def list_drivers(self) -> tuple[list[DriverInfo], list[str]]:
        """List signed drivers via ``Get-CimInstance Win32_PnPSignedDriver``.

        Entries without a ``DeviceName`` are filtered out. The query runs
        through PowerShell with ``ConvertTo-Json`` so parsing is
        locale-independent.

        Returns:
            A tuple of the found :class:`DriverInfo` items and the errors
            accumulated during the query.
        """
        ensure_windows()
        script = (
            "Get-CimInstance Win32_PnPSignedDriver "
            "| Where-Object { $_.DeviceName } "
            "| Select-Object -Property DeviceName, DriverVersion, Manufacturer "
            "| ConvertTo-Json -Compress"
        )
        result = run_command(
            ("powershell.exe", "-NoProfile", "-Command", script),
            timeout=_LIST_DRIVERS_TIMEOUT,
        )
        if not result.success:
            return [], [result.combined or result.error or "No se pudo listar los controladores."]

        try:
            rows = _parse_json_output(result.stdout)
        except json.JSONDecodeError as exc:
            return [], [f"No se pudo interpretar la salida de Get-CimInstance: {exc}"]

        infos: list[DriverInfo] = []
        for row in rows:
            device = str(row.get("DeviceName") or "").strip()
            if not device:
                continue
            infos.append(
                DriverInfo(
                    device_name=device,
                    driver_version=str(row.get("DriverVersion") or ""),
                    manufacturer=str(row.get("Manufacturer") or ""),
                )
            )
        return infos, []

    def create_restore_point(
        self,
        description: str = _DEFAULT_RESTORE_DESCRIPTION,
    ) -> CommandResult:
        """Create a System Restore checkpoint via ``Checkpoint-Computer``.

        Requires Administrator privileges. The description is sanitized (single
        quotes removed) before being interpolated into the command line.

        Note: when System Restore is disabled on the machine, PowerShell exits
        with a non-zero return code. That failure is reported through the
        returned :class:`CommandResult`; it does NOT raise.

        Args:
            description: Label stored with the restore point.

        Returns:
            A :class:`CommandResult` with the Checkpoint-Computer output.
        """
        ensure_windows()
        if not is_admin():
            return _admin_blocked_result()

        safe_description = description.replace("'", "").strip()
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Checkpoint-Computer -Description '{safe_description}' "
            "-RestorePointType MODIFY_SETTINGS",
        )
        return run_command(command)

    def scan_hardware_changes(self) -> CommandResult:
        """Force a hardware-rescan via ``pnputil /scan-devices``.

        The scan usually requires Administrator privileges; without them
        pnputil fails, which is reported as a normal failed
        :class:`CommandResult` and never raises.

        Returns:
            A :class:`CommandResult` with the pnputil output.
        """
        ensure_windows()
        return run_command(("pnputil.exe", "/scan-devices"))

    def update_drivers_best_effort(
        self,
        create_restore_point: bool = True,
    ) -> DriverUpdateResult:
        """Best-effort driver refresh: restore point first, then hardware rescan.

        Limitation (documented on purpose): Windows exposes no "update all
        drivers" command through the command line. This sequence therefore (1)
        optionally creates a System Restore point as a rollback safety net and
        (2) forces a hardware rescan so Windows re-evaluates every device
        driver. A failed restore point NEVER aborts the scan: it is recorded
        through ``restore_point_error`` and the sequence continues.

        Args:
            create_restore_point: Create a restore point before the rescan.

        Returns:
            A :class:`DriverUpdateResult`; ``success`` reflects the rescan step.
        """
        ensure_windows()
        result = DriverUpdateResult()

        if create_restore_point:
            rp = self.create_restore_point()
            result.restore_point_created = rp.success
            if not rp.success:
                result.restore_point_error = (
                    rp.error or rp.combined or "No se pudo crear el punto de restauracion."
                )

        scan = self.scan_hardware_changes()
        result.steps.append(StepResult(label="scan_hardware_changes", result=scan))
        result.success = scan.success
        return result

    # ----------------------------------------------------------- public: async

    def list_drivers_async(
        self,
        on_complete: Callable[[tuple[list[DriverInfo], list[str]]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``list_drivers`` in a daemon thread.

        Args:
            on_complete: Called with ``(infos, errors)`` when done.
            on_error: Called with the exception when the query raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.list_drivers, on_complete, on_error, "driver-list")

    def create_restore_point_async(
        self,
        description: str = _DEFAULT_RESTORE_DESCRIPTION,
        on_complete: Callable[[CommandResult], None] | None = None,
        on_error: Optional[Callable[[CommandResult], None]] = None,
    ) -> threading.Thread:
        """Run ``create_restore_point`` in a background thread.

        Args:
            description: Label stored with the restore point.
            on_complete: Called with the :class:`CommandResult` when finished.
            on_error: Called when execution could not even start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        if not is_admin():
            return _spawn_immediate_result(_admin_blocked_result(), on_complete, on_error)

        safe_description = description.replace("'", "").strip()
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Checkpoint-Computer -Description '{safe_description}' "
            "-RestorePointType MODIFY_SETTINGS",
        )
        return run_command_async(command, on_complete=on_complete, on_error=on_error)

    def scan_hardware_changes_async(
        self,
        on_complete: Callable[[CommandResult], None],
        on_error: Optional[Callable[[CommandResult], None]] = None,
    ) -> threading.Thread:
        """Run ``scan_hardware_changes`` in a background thread.

        Args:
            on_complete: Called with the :class:`CommandResult` when finished.
            on_error: Called when execution could not even start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        return run_command_async(
            ("pnputil.exe", "/scan-devices"),
            on_complete=on_complete,
            on_error=on_error,
        )

    def update_drivers_async(
        self,
        create_restore_point: bool = True,
        on_complete: Callable[[DriverUpdateResult], None] | None = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``update_drivers_best_effort`` in a daemon thread.

        Args:
            create_restore_point: Create a restore point before the rescan.
            on_complete: Called with the :class:`DriverUpdateResult` when done.
            on_error: Called with the exception when the sequence raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.update_drivers_best_effort(create_restore_point),
            on_complete,
            on_error,
            "driver-update",
        )