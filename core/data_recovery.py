"""data_recovery.py - DataRecovery: light recovery by extension + optional winfr.exe.

Windows-only core module. The default ``light`` mode walks a source directory
and copies the matching files to a destination mirroring the relative layout;
it is fast, deterministic and needs no external tools. The optional ``winfr``
mode wraps the Microsoft Store "Windows File Recovery" tool for a deeper scan
of a drive.

File-level permission/sharing errors are SILENT (never crash, never spam the
report); structural failures (missing source, unwritable destination) ARE
reported in ``errors``. All subprocess execution goes through
``utils.process_runner``.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, TypeVar

from utils.process_runner import ensure_windows, find_executable, run_command

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so drives degrade to ["C:\\"]
    psutil = None  # type: ignore[assignment]

#: Well-known extension groups. Keys are ASCII on purpose: the GUI can display
#: localized labels ("Imágenes") on top of these stable identifiers.
EXTENSION_GROUPS: dict[str, tuple[str, ...]] = {
    "Documentos": (".pdf", ".docx", ".xlsx"),
    "Imagenes": (".jpg", ".jpeg", ".png"),
    "Comprimidos": (".zip", ".rar"),
}

#: Winfr may show interactive prompts; give the wrapped run a generous budget.
_WINFR_TIMEOUT = 1800.0

#: Collision suffix added to an existing destination file before its extension.
_COLLISION_SUFFIX = "_1"

_T = TypeVar("_T")


def _is_silent_copy_error(exc: BaseException) -> bool:
    """True for permission/sharing violations that must not spam the report."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32):
        return True
    return False


def _silent_walk_error(_error: OSError) -> None:
    """Swallow per-directory walk errors: inaccessible folders are skipped."""


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


@dataclass
class RecoveryJobResult:
    """Outcome of a single recovery job."""

    source: str
    destination: str
    mode: str  # "light" | "winfr"
    extensions: list[str] = field(default_factory=list)
    files_recovered: int = 0
    bytes_recovered: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class RecoveryProgress:
    """Informative progress snapshot (only useful for the GUI, never blocking)."""

    scanned: int = 0
    found: int = 0
    current: str = ""


class DataRecovery:
    """Recover files by extension and optionally wrap the winfr.exe tool."""

    # ------------------------------------------------------------ public: sync

    def get_drives(self) -> list[str]:
        """List the fixed drive letters of this machine.

        Returns:
            Drive letters such as ``["C:\\", "D:\\"]``. Falls back to ``["C:\\"]``
            when psutil is unavailable or the enumeration fails.
        """
        ensure_windows()
        if psutil is None:
            return ["C:\\"]
        try:
            return [partition.device for partition in psutil.disk_partitions(all=False)]
        except (OSError, psutil.Error):
            return ["C:\\"]

    def find_winfr(self) -> Optional[Path]:
        """Locate the Windows File Recovery executable.

        Returns:
            Absolute path of ``winfr.exe`` or None when not installed.
        """
        ensure_windows()
        return find_executable(("winfr.exe",))

    def _avoid_collision(self, target: Path) -> Path:
        """Return ``target`` or, when it already exists, a ``_1``-suffixed path.

        Args:
            target: Candidate destination file path.

        Returns:
            A non-existing path within the same directory.
        """
        if not target.exists():
            return target
        stem, suffix, parent = target.stem, target.suffix, target.parent
        index = 1
        while True:
            candidate = parent / f"{stem}{_COLLISION_SUFFIX}{index}{suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def recover(
        self,
        source: Path,
        destination: Path,
        extensions: Sequence[str],
        mode: str = "light",
    ) -> RecoveryJobResult:
        """Recover files matching ``extensions`` from ``source``.

        In ``light`` mode the source tree is walked and matching files are
        copied preserving their relative layout. Per-file permission/sharing
        errors are SILENT; missing source or unwritable destination are
        reported in ``errors``. In ``winfr`` mode the scan is delegated to
        winfr.exe with a long timeout (1800s).

        Note: winfr.exe may ask for interactive confirmation and, depending on
        the destination, requires a separate drive for output.

        Args:
            source: Directory (light) or drive root (winfr) to scan.
            destination: Target folder where recovered files are written.
            extensions: Lower-case suffixes, e.g. ``(".pdf", ".docx")``.
            mode: ``"light"`` (default) or ``"winfr"``.

        Returns:
            A :class:`RecoveryJobResult` with counters, errors and elapsed time.
        """
        if mode == "winfr":
            return self.recover_with_winfr(source, destination, extensions)

        result = RecoveryJobResult(
            source=str(source),
            destination=str(destination),
            mode="light",
            extensions=list(extensions),
        )
        started = time.monotonic()

        if not source.is_dir():
            result.errors.append(f"No existe la carpeta de origen: {source}")
            result.elapsed = time.monotonic() - started
            return result

        try:
            destination.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as exc:
            result.errors.append(f"No se pudo crear la carpeta de destino: {exc}")
            result.elapsed = time.monotonic() - started
            return result

        normalized = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions
        }
        if not normalized:
            result.errors.append("No se indicaron extensiones a recuperar.")
            result.elapsed = time.monotonic() - started
            return result

        for root, _dirs, files in os.walk(source, onerror=_silent_walk_error):
            for filename in files:
                if Path(filename).suffix.lower() not in normalized:
                    continue
                source_file = Path(root) / filename
                try:
                    target_file = self._avoid_collision(destination / source_file.relative_to(source))
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, target_file)
                    result.bytes_recovered += source_file.stat().st_size
                    result.files_recovered += 1
                except (PermissionError, OSError) as exc:
                    if not _is_silent_copy_error(exc):
                        result.errors.append(f"No se pudo recuperar {source_file}: {exc}")

        result.elapsed = time.monotonic() - started
        return result

    def recover_with_winfr(
        self,
        source: Path,
        destination: Path,
        extensions: Sequence[str],
    ) -> RecoveryJobResult:
        """Recover files by delegating the scan to winfr.exe.

        Known limitation: winfr.exe may show an interactive confirmation
        prompt that the wrapped non-interactive run cannot answer, so some
        recoveries only complete when driven from a console. A long timeout
        (1800s) is imposed because drive scans are slow.

        Args:
            source: Drive to scan (only its drive letter is used).
            destination: Target folder for recovered files.
            extensions: Lower-case suffixes, e.g. ``(".jpg", ".png")``.

        Returns:
            A :class:`RecoveryJobResult`; recovered files must be inspected
            manually in the destination.
        """
        result = RecoveryJobResult(
            source=str(source),
            destination=str(destination),
            mode="winfr",
            extensions=list(extensions),
        )
        started = time.monotonic()

        winfr = self.find_winfr()
        if winfr is None:
            result.errors.append(
                "No se encontro winfr.exe. Instala Windows File Recovery desde Microsoft Store."
            )
            result.elapsed = time.monotonic() - started
            return result

        command = [str(winfr), f"{source.drive[:2]}", str(destination)]
        command.extend(f"/n *.{ext.lstrip('.')}" for ext in extensions)

        completed = run_command(command, timeout=_WINFR_TIMEOUT)
        if completed.success:
            result.errors.append("winfr completado; revisar destino manualmente.")
        else:
            stderr = completed.stderr.strip()
            result.errors.append(
                f"winfr fallo: {stderr or completed.error or 'codigo de salida no nulo'}"
            )
        result.elapsed = time.monotonic() - started
        return result

    # ----------------------------------------------------------- public: async

    def recover_async(
        self,
        source: Path,
        destination: Path,
        extensions: Sequence[str],
        mode: str,
        on_complete: Callable[[RecoveryJobResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``recover`` in a daemon thread.

        Args:
            source: Directory (light) or drive root (winfr) to scan.
            destination: Target folder for recovered files.
            extensions: Lower-case suffixes to match.
            mode: ``"light"`` or ``"winfr"``.
            on_complete: Called with the :class:`RecoveryJobResult` when done.
            on_error: Called with the exception when the job raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.recover(source, destination, extensions, mode=mode),
            on_complete,
            on_error,
            "recovery-job",
        )

    def recover_with_winfr_async(
        self,
        source: Path,
        destination: Path,
        extensions: Sequence[str],
        on_complete: Callable[[RecoveryJobResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``recover_with_winfr`` in a daemon thread.

        Args:
            source: Drive to scan.
            destination: Target folder for recovered files.
            extensions: Lower-case suffixes to match.
            on_complete: Called with the :class:`RecoveryJobResult` when done.
            on_error: Called with the exception when the job raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.recover_with_winfr(source, destination, extensions),
            on_complete,
            on_error,
            "recovery-winfr",
        )
