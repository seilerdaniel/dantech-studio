"""task_scheduler.py - TaskScheduler: unattended monthly maintenance via schtasks.

Windows-only core module. Wraps ``schtasks.exe`` (routed through
``utils.process_runner``, never raw subprocess) to create, query and delete
the scheduled task that runs DanTech Studio's EXPRESS maintenance profile
silently once a month with highest privileges.

The task action points to this same executable with the
``--mantenimiento-expreso`` flag; ``main.py`` detects the flag and runs the
headless maintenance sequence instead of opening the GUI.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils.process_runner import (
    CommandResult,
    ensure_windows,
    is_admin,
    run_command,
)

#: Fixed Windows task name for the monthly maintenance job.
TASK_NAME = "DanTechStudio_MantenimientoMensual"

#: CLI flag consumed by ``main.py`` to run the headless express profile.
EXPRESS_FLAG = "--mantenimiento-expreso"

#: User-facing error shown when elevation is missing.
_ADMIN_REQUIRED_ERROR = "Se requieren permisos de administrador."

#: Localized "task/file not found" markers returned by schtasks.
_TASK_MISSING_MARKERS = re.compile(
    r"no existe|does not exist|0x80070002|"
    r"no puede encontrar el archivo|cannot find the file"
)

_TIMEOUT = 60.0


@dataclass
class ScheduledTaskStatus:
    """Outcome of querying the maintenance task."""

    installed: bool = False
    detail: str = ""
    errors: list[str] = field(default_factory=list)


def _run_in_thread(
    fn: Callable[[], object],
    on_complete: Callable[[object], None],
    on_error: Optional[Callable[[Exception], None]],
    name: str,
) -> threading.Thread:
    """Run an in-process callable in a daemon thread, mirroring the async contract."""

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


class TaskScheduler:
    """Create/query/delete the monthly silent-maintenance scheduled task.

    Sync methods return :class:`CommandResult`/:class:`ScheduledTaskStatus`;
    ``*_async`` variants run on daemon threads so the GUI never blocks on
    schtasks round-trips.
    """

    def __init__(self, task_name: str = TASK_NAME) -> None:
        self._task_name = task_name

    # ------------------------------------------------------------------ helpers

    def _action(self) -> tuple[str, str]:
        """Resolve the ``(executable, arguments)`` pair stored in the task."""
        if getattr(sys, "frozen", False):
            executable = sys.executable
            arguments = EXPRESS_FLAG
        else:
            main_py = Path(__file__).resolve().parent.parent / "main.py"
            executable = sys.executable
            arguments = f'"{main_py}" {EXPRESS_FLAG}'
        return executable, arguments

    def _tr_value(self) -> str:
        """Build the quoted ``/TR`` payload for schtasks."""
        executable, arguments = self._action()
        return f'"{executable}" {arguments}'.strip()

    def _blocked_result(self, operation: str) -> CommandResult:
        """Canonical failed result used when elevation is missing."""
        return CommandResult(
            stderr=_ADMIN_REQUIRED_ERROR,
            returncode=-1,
            success=False,
            error=f"{operation}: {_ADMIN_REQUIRED_ERROR}",
        )

    # --------------------------------------------------------------- public sync

    def get_status(self) -> ScheduledTaskStatus:
        """Query whether the maintenance task exists right now.

        Returns:
            A :class:`ScheduledTaskStatus`; ``installed`` is True only when
            ``schtasks /Query`` succeeds for the exact task name.
        """
        ensure_windows()
        result = run_command(
            ("schtasks", "/Query", "/TN", self._task_name, "/FO", "LIST"),
            timeout=_TIMEOUT,
        )
        if result.success:
            first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            return ScheduledTaskStatus(
                installed=True,
                detail=first_line or f"Tarea {self._task_name} instalada.",
            )
        combined = result.combined.lower()
        if _TASK_MISSING_MARKERS.search(combined):
            return ScheduledTaskStatus(
                installed=False, detail="Sin tarea programada de mantenimiento."
            )
        return ScheduledTaskStatus(
            installed=False,
            detail="No se pudo consultar la tarea programada.",
            errors=[result.combined or "Error desconocido de schtasks."],
        )

    def enable_monthly(
        self,
        day_of_month: int = 1,
        hour: int = 12,
        minute: int = 0,
    ) -> CommandResult:
        """Create/overwrite the monthly maintenance task (Administrator needed).

        The task runs with ``/RL HIGHEST`` so the headless express profile can
        clean system temp folders and trim RAM without UAC prompts.

        Args:
            day_of_month: Execution day, clamped to 1-28 (safe for February).
            hour: Execution hour 0-23.
            minute: Execution minute 0-59.

        Returns:
            A :class:`CommandResult` describing the schtasks outcome.
        """
        ensure_windows()
        if not is_admin():
            return self._blocked_result("Programar mantenimiento")
        day = min(max(int(day_of_month), 1), 28)
        hour_c = min(max(int(hour), 0), 23)
        minute_c = min(max(int(minute), 0), 59)
        command = (
            "schtasks",
            "/Create",
            "/TN", self._task_name,
            "/TR", self._tr_value(),
            "/SC", "MONTHLY",
            "/D", str(day),
            "/ST", f"{hour_c:02d}:{minute_c:02d}",
            "/RL", "HIGHEST",
            "/F",
        )
        return run_command(command, timeout=_TIMEOUT)

    def disable(self) -> CommandResult:
        """Delete the maintenance task (Administrator needed).

        Deleting a non-existent task is reported as SUCCESS so toggling the
        GUI switch off twice never surfaces a false error.

        Returns:
            A :class:`CommandResult` describing the schtasks outcome.
        """
        ensure_windows()
        if not is_admin():
            return self._blocked_result("Desprogramar mantenimiento")
        result = run_command(
            ("schtasks", "/Delete", "/TN", self._task_name, "/F"),
            timeout=_TIMEOUT,
        )
        if result.success:
            return result
        combined = result.combined.lower()
        if _TASK_MISSING_MARKERS.search(combined):
            return CommandResult(
                stdout="La tarea no existía; nada que eliminar.",
                returncode=0,
                success=True,
            )
        return result

    # --------------------------------------------------------------- public async

    def get_status_async(
        self,
        on_complete: Callable[[ScheduledTaskStatus], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_status`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`ScheduledTaskStatus`.
            on_error: Called with the exception when the query raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.get_status, on_complete, on_error, "task-status")

    def enable_monthly_async(
        self,
        day_of_month: int,
        hour: int,
        minute: int,
        on_complete: Callable[[CommandResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``enable_monthly`` in a daemon thread.

        Args:
            day_of_month: Execution day 1-28.
            hour: Execution hour 0-23.
            minute: Execution minute 0-59.
            on_complete: Called with the :class:`CommandResult` when done.
            on_error: Called with the exception when creation raises.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            on_complete(self.enable_monthly(day_of_month, hour, minute))

        thread = threading.Thread(target=_worker, name="task-enable", daemon=True)
        thread.start()
        return thread

    def disable_async(
        self,
        on_complete: Callable[[CommandResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``disable`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`CommandResult` when done.
            on_error: Called with the exception when deletion raises.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            on_complete(self.disable())

        thread = threading.Thread(target=_worker, name="task-disable", daemon=True)
        thread.start()
        return thread
