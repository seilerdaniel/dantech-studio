"""memory_optimizer.py - MemoryOptimizer: temporary-folder cleanup and RAM collection.

Windows-only core module. It never launches system subprocesses directly: every
operation is in-process (``os.scandir`` for files, psutil + ctypes for memory),
so the async variants use their own daemon threads with the same callback
pattern exposed by ``utils.process_runner.run_command_async``.

Two responsibilities kept in ONE class on purpose (single-feature module):
  - Cleaning ``%TEMP%`` and ``C:\\Windows\\Temp`` (the latter needs admin).
  - Trimming the working set of the top RAM consumers via ``EmptyWorkingSet``.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TypeVar

from utils.process_runner import ensure_windows, is_admin

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so reports fail gracefully
    psutil = None  # type: ignore[assignment]

#: Hard limit on recursive directory walking: prevents runaway descent and
#: keeps symlink/hardlink loops contained even if they are ever followed.
_MAX_DEPTH = 8

#: Access rights needed for ``OpenProcess``: QUERY_INFORMATION | QUERY_LIMITED_INFORMATION.
_PROCESS_QUERY_RIGHTS = 0x0100 | 0x0008

#: Process names never trimmed: Windows kernel/idle pseudo-processes.
_SKIP_PROCESS_NAMES = {"system", "system idle process", "idle"}

_T = TypeVar("_T")


@dataclass
class CleanupReport:
    """Outcome of cleaning a single temporary folder tree."""

    path: Path
    bytes_freed: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class RamReport:
    """Outcome of the RAM working-set trim plus memory snapshot."""

    ram_total_mb: float = 0.0
    ram_available_mb: float = 0.0
    ram_used_mb: float = 0.0
    working_set_trimmed_mb: float = 0.0
    processes_trimmed: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class CombinedReport:
    """Aggregated outcome of ``optimize_all`` (temp cleanup + RAM trim)."""

    temp_reports: list[CleanupReport] = field(default_factory=list)
    ram: Optional[RamReport] = None
    errors: list[str] = field(default_factory=list)


def _run_in_thread(
    fn: Callable[[], _T],
    on_complete: Callable[[_T], None],
    on_error: Optional[Callable[[Exception], None]],
    name: str,
) -> threading.Thread:
    """Run an in-process callable in a daemon thread, mirroring the async contract.

    Args:
        fn: Zero-argument callable producing the report.
        on_complete: Called with the report when ``fn`` returns.
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


class MemoryOptimizer:
    """Clean temporary folders and collect RAM without killing any process.

    All operations accumulate partial failures into the ``errors`` lists: one
    locked file or one denied process NEVER aborts the whole run.
    """

    # ------------------------------------------------------------------ helpers

    def _cleanup_path(self, root: Path, max_depth: int = _MAX_DEPTH) -> CleanupReport:
        """Recursively empty one temporary directory, size measured before delete.

        Args:
            root: Directory to empty (its contents are removed, not itself).
            max_depth: Maximum recursion depth for ``os.scandir`` traversal.

        Returns:
            A :class:`CleanupReport` with freed bytes and partial errors.
        """
        report = CleanupReport(path=root)
        started = time.monotonic()
        if not root.is_dir():
            report.errors.append(f"No existe el directorio temporal: {root}")
            report.elapsed = time.monotonic() - started
            return report

        try:
            self._cleanup_dir(root, 0, report, max_depth)
        except OSError as exc:
            report.errors.append(f"Error limpiando {root}: {exc}")

        report.elapsed = time.monotonic() - started
        return report

    def _cleanup_dir(
        self,
        directory: Path,
        depth: int,
        report: CleanupReport,
        max_depth: int,
    ) -> None:
        """Walk one directory level with ``os.scandir`` and dispatch entries."""
        if depth > max_depth:
            report.errors.append(f"Profundidad maxima ({max_depth}) alcanzada en {directory}")
            return

        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    self._cleanup_entry(entry, depth, report, max_depth)
        except (PermissionError, OSError, FileNotFoundError) as exc:
            report.errors.append(f"No se pudo leer {directory}: {exc}")

    def _cleanup_entry(
        self,
        entry: os.DirEntry,
        depth: int,
        report: CleanupReport,
        max_depth: int,
    ) -> None:
        """Delete a single scandir entry, recursing into real directories."""
        try:
            if entry.is_symlink():
                # Remove the link itself; never follow into its target.
                try:
                    os.remove(entry.path)
                    report.files_deleted += 1
                except (PermissionError, OSError) as exc:
                    report.errors.append(f"No se pudo eliminar el enlace {entry.path}: {exc}")
                return

            if entry.is_dir(follow_symlinks=False):
                self._cleanup_dir(Path(entry.path), depth + 1, report, max_depth)
                try:
                    os.rmdir(entry.path)
                    report.files_deleted += 1
                except (PermissionError, OSError) as exc:
                    report.errors.append(f"No se pudo eliminar la carpeta {entry.path}: {exc}")
                return

            if entry.is_file(follow_symlinks=False):
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                    os.remove(entry.path)
                    report.bytes_freed += size
                    report.files_deleted += 1
                except (PermissionError, OSError) as exc:
                    report.errors.append(f"No se pudo eliminar el archivo {entry.path}: {exc}")
        except (PermissionError, OSError, FileNotFoundError) as exc:
            report.errors.append(f"No se pudo acceder a {entry.path}: {exc}")

    def _trim_process(self, pid: int, name: str, reported_rss: int, report: RamReport) -> None:
        """Trim one process working set via EmptyWorkingSet and measure freed RSS.

        Args:
            pid: Process id to trim.
            name: Process display name (for error messages).
            reported_rss: RSS sampled during enumeration, used as a fallback
                estimate when before/after measurement is not possible.
            report: In-progress RamReport mutated with results.
        """
        if psutil is None:
            report.errors.append("psutil no esta disponible; no se puede medir la memoria.")
            return

        before: Optional[int] = None
        try:
            before = psutil.Process(pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            before = None

        trimmed = False
        handle = None
        try:
            handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_RIGHTS, False, pid)
            if not handle:
                raise OSError("OpenProcess devolvio un handle nulo")
            if ctypes.windll.psapi.EmptyWorkingSet(handle):
                trimmed = True
            else:
                raise OSError(ctypes.WinError())
        except (AttributeError, OSError, ctypes.WinError) as exc:
            report.errors.append(f"No se pudo recortar la memoria de {name} (pid {pid}): {exc}")
        finally:
            if handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except (AttributeError, OSError):
                    pass

        if not trimmed:
            return
        report.processes_trimmed += 1

        if before is None:
            # No before/after measurement available: estimate with the sampled RSS.
            freed = reported_rss
        else:
            try:
                after = psutil.Process(pid).memory_info().rss
                freed = max(before - after, 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                freed = reported_rss

        report.working_set_trimmed_mb += freed / (1024.0 ** 2)

    # ------------------------------------------------------------ public: sync

    def clean_temp_folders(self) -> CleanupReport:
        """Empty the user ``%TEMP%`` and (when admin) ``C:\\Windows\\Temp``.

        Sizes are summed BEFORE deletion so freed bytes reflect what was there.
        In-use or protected entries are skipped and recorded in ``errors``.

        Returns:
            An aggregated :class:`CleanupReport`; ``path`` points to the user
            TEMP folder and the counters/errors merge both targets.
        """
        ensure_windows()
        started = time.monotonic()

        user_temp = Path(
            os.environ.get("TEMP")
            or os.environ.get("TMP")
            or str(Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Temp")
        )
        windows_temp = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Temp"

        reports: list[CleanupReport] = [self._cleanup_path(user_temp)]
        if is_admin():
            reports.append(self._cleanup_path(windows_temp))
        else:
            reports.append(
                CleanupReport(
                    path=windows_temp,
                    errors=[
                        f"No se pudo limpiar {windows_temp}: Se requieren permisos de administrador."
                    ],
                )
            )

        combined = CleanupReport(path=user_temp)
        for report in reports:
            combined.bytes_freed += report.bytes_freed
            combined.files_deleted += report.files_deleted
            combined.errors.extend(report.errors)
        combined.elapsed = time.monotonic() - started
        return combined

    def cleanup_ram(self) -> RamReport:
        """Trim the working set of the top-10 RSS processes and snapshot memory.

        Uses psutil for the snapshot/process enumeration and the Win32
        ``EmptyWorkingSet`` API (via ctypes) to release physical memory pages.
        Processes are NEVER killed or suspended.

        Returns:
            A :class:`RamReport` with the memory snapshot and freed estimate.
        """
        ensure_windows()
        report = RamReport()
        started = time.monotonic()

        if psutil is None:
            report.errors.append("psutil no esta disponible; no se puede limpiar la RAM.")
            report.elapsed = time.monotonic() - started
            return report

        try:
            virtual = psutil.virtual_memory()
            report.ram_total_mb = virtual.total / (1024.0 ** 2)
            report.ram_available_mb = virtual.available / (1024.0 ** 2)
            report.ram_used_mb = virtual.used / (1024.0 ** 2)
        except (psutil.Error, OSError) as exc:
            report.errors.append(f"No se pudo leer la memoria virtual: {exc}")

        candidates: list[tuple[int, int, str]] = []
        try:
            for proc in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    if proc.info.get("pid") == os.getpid():
                        continue
                    name = str(proc.info.get("name") or "").lower()
                    if name in _SKIP_PROCESS_NAMES:
                        continue
                    info = proc.info.get("memory_info")
                    rss = int(getattr(info, "rss", 0) or 0)
                    if rss > 0:
                        candidates.append((rss, int(proc.info.get("pid") or 0), name))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except (psutil.Error, OSError) as exc:
            report.errors.append(f"No se pudo enumerar procesos: {exc}")

        candidates.sort(key=lambda item: item[0], reverse=True)
        for reported_rss, pid, name in candidates[:10]:
            self._trim_process(pid, name, reported_rss, report)

        report.elapsed = time.monotonic() - started
        return report

    def optimize_all(self) -> CombinedReport:
        """Run temp cleanup and RAM trim in one pass.

        Returns:
            A :class:`CombinedReport` flattening the errors of both phases.
        """
        ensure_windows()
        report = CombinedReport()
        temp_report = self.clean_temp_folders()
        ram_report = self.cleanup_ram()

        report.temp_reports = [temp_report]
        report.ram = ram_report
        report.errors.extend(temp_report.errors)
        report.errors.extend(ram_report.errors)
        return report

    # ----------------------------------------------------------- public: async

    def clean_temp_folders_async(
        self,
        on_complete: Callable[[CleanupReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``clean_temp_folders`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`CleanupReport` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.clean_temp_folders, on_complete, on_error, "clean-temp")

    def cleanup_ram_async(
        self,
        on_complete: Callable[[RamReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``cleanup_ram`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`RamReport` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.cleanup_ram, on_complete, on_error, "cleanup-ram")

    def optimize_all_async(
        self,
        on_complete: Callable[[CombinedReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``optimize_all`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`CombinedReport` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.optimize_all, on_complete, on_error, "optimize-all")