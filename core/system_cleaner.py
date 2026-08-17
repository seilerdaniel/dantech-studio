"""system_cleaner.py - SystemCleaner: configurable Windows cache/temp cleanup.

Windows-only core module. It offers a dry-run analysis phase (``analyze_only``)
that measures recoverable bytes WITHOUT touching anything, and a destructive
phase (``clean_now``) that removes the same targets. Every subprocess (only the
Recycle Bin) goes through ``utils.process_runner``; file operations are
in-process so the async variants use their own daemon threads with the same
callback pattern exposed by ``utils.process_runner.run_command_async``.

Safety rules:
  - ``_MEI*`` folders (PyInstaller runtime) are never touched.
  - ``whitelist_paths`` from the :class:`CleanConfig` are never touched.
  - Per-file permission/sharing failures (winerror 5/32) are silent.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, TypeVar

from utils.process_runner import ensure_windows, is_admin, run_command

#: Categories that require Administrator privileges to be processed.
_ADMIN_CATEGORIES = frozenset({"temp_system", "prefetch", "software_distribution"})

#: Optional categories: missing roots are skipped silently instead of reported.
_OPTIONAL_CATEGORIES = frozenset({"browser_cache_chrome", "browser_cache_edge"})

#: Hard cap on the number of FileEntry objects kept in an analysis report.
_MAX_FILE_ENTRIES = 2000

_T = TypeVar("_T")


def _is_silent_delete_error(exc: BaseException) -> bool:
    """True for permission/sharing violations that must not spam the report."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32):
        return True
    return False


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
class CleanConfig:
    """Select which categories are analyzed/cleaned and what is never touched."""

    temp_user: bool = True
    temp_system: bool = True
    prefetch: bool = False
    software_distribution: bool = False
    recycle_bin: bool = False
    browser_cache_chrome: bool = False
    browser_cache_edge: bool = False
    whitelist_paths: tuple[str, ...] = ()


@dataclass
class FileEntry:
    """A single file discovered during the analysis phase."""

    path: str
    size_bytes: int


@dataclass
class CleanAnalysisReport:
    """Result of a read-only scan over the configured cleanup targets."""

    category_totals: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    file_count: int = 0
    files: list[FileEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class CleanResult:
    """Outcome of an actual cleanup run."""

    category_freed: dict[str, int] = field(default_factory=dict)
    total_freed: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


class SystemCleaner:
    """Analyze and clean Windows temporary/cache locations safely.

    ``analyze_only`` never deletes anything; ``clean_now`` removes the same
    targets. Both honor the ``_MEI*`` skip and the ``whitelist_paths`` guard.
    """

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _user_temp() -> Path:
        """Resolve the current user ``%TEMP%`` folder."""
        return Path(
            os.environ.get("TEMP")
            or os.environ.get("TMP")
            or str(Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Temp")
        )

    def _targets(self, config: CleanConfig) -> list[tuple[str, Path]]:
        """Map the enabled config flags to ``(category, root)`` targets.

        Browser profiles are expanded through globbing ("Default" plus every
        ``Profile *`` folder); the Recycle Bin is handled separately and is NOT
        listed here.

        Args:
            config: The :class:`CleanConfig` selecting the categories.

        Returns:
            A list of ``(category, root)`` pairs to walk.
        """
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))

        targets: list[tuple[str, Path]] = []
        if config.temp_user:
            targets.append(("temp_user", self._user_temp()))
        if config.temp_system:
            targets.append(("temp_system", windows / "Temp"))
        if config.prefetch:
            targets.append(("prefetch", windows / "Prefetch"))
        if config.software_distribution:
            targets.append(("software_distribution", windows / "SoftwareDistribution" / "Download"))

        if config.browser_cache_chrome:
            chrome_base = localappdata / "Google" / "Chrome" / "User Data"
            profiles: list[Path] = [chrome_base / "Default"]
            profiles.extend(sorted(chrome_base.glob("Profile *")))
            for profile in profiles:
                targets.append(("browser_cache_chrome", profile / "Cache"))
                targets.append(("browser_cache_chrome", profile / "Code Cache"))

        if config.browser_cache_edge:
            edge_base = localappdata / "Microsoft" / "Edge" / "User Data"
            profiles = [edge_base / "Default"]
            profiles.extend(sorted(edge_base.glob("Profile *")))
            for profile in profiles:
                targets.append(("browser_cache_edge", profile / "Cache"))

        return targets

    def _is_whitelisted(self, path: str, whitelist: Sequence[str]) -> bool:
        """True when ``path`` is inside (or equals) any whitelist entry.

        Paths are normalized and compared case-insensitively (Windows).

        Args:
            path: Absolute path to test.
            whitelist: Configured "never delete" paths.

        Returns:
            True when the path starts with a normalized whitelist entry.
        """
        if not whitelist:
            return False
        normalized = os.path.normpath(path).lower()
        for entry in whitelist:
            candidate = os.path.normpath(str(entry)).lower()
            if normalized == candidate or normalized.startswith(candidate + os.sep):
                return True
        return False

    def _walk_target(
        self,
        root: Path,
        whitelist: Sequence[str],
        on_error: Callable[[str], None],
    ):
        """Walk one target pruning ``_MEI*`` folders and whitelisted subtrees.

        Args:
            root: Directory to walk.
            whitelist: Whitelist forwarded to each directory prune decision.
            on_error: Callback receiving non-silent walk failures.

        Yields:
            ``(dirpath, filenames)`` tuples, mirroring ``os.walk``.
        """

        def _onerror(exc: OSError) -> None:
            if not _is_silent_delete_error(exc):
                on_error(f"No se pudo leer {getattr(exc, 'filename', root)}: {exc}")

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_onerror):
            if self._is_whitelisted(dirpath, whitelist):
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not name.startswith("_MEI")]
            yield dirpath, filenames

    def _run_recycle_bin_clean(self) -> tuple[int, str]:
        """Empty the Recycle Bin via PowerShell and describe the outcome.

        Returns:
            A tuple of ``(bytes_freed, note_or_error)``. The Recycle Bin size
            is unknown beforehand so freed bytes are always reported as 0.
        """
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
        )
        result = run_command(command, timeout=120)
        if result.success:
            return 0, "Papelera de reciclaje vaciada."
        return 0, f"No se pudo vaciar la papelera de reciclaje: {result.combined or result.error}"

    # ------------------------------------------------------------ public: sync

    def analyze_only(self, config: CleanConfig) -> CleanAnalysisReport:
        """Scan the configured targets WITHOUT deleting anything.

        Every candidate file is measured with ``os.path.getsize``; sizes are
        aggregated per category and the total. The ``files`` list is capped at
        2000 entries; further findings only count toward the totals and a note
        is appended to ``errors``.

        Args:
            config: The :class:`CleanConfig` selecting the categories.

        Returns:
            A :class:`CleanAnalysisReport`; nothing on disk is modified.
        """
        ensure_windows()
        report = CleanAnalysisReport()
        started = time.monotonic()
        whitelist = config.whitelist_paths
        capped_note_added = False

        if config.recycle_bin:
            report.category_totals["recycle_bin"] = 0
            report.errors.append(
                "La papelera de reciclaje no se mide en el modo analisis; se reportan 0 bytes."
            )

        for category, root in self._targets(config):
            if category in _ADMIN_CATEGORIES and not is_admin():
                report.errors.append(
                    f"La categoria {category} requiere permisos de administrador."
                )
                continue
            if not root.exists():
                if category not in _OPTIONAL_CATEGORIES:
                    report.errors.append(f"No existe la ruta de {category}: {root}")
                continue

            for dirpath, filenames in self._walk_target(
                root, whitelist, report.errors.append
            ):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if self._is_whitelisted(full, whitelist):
                        continue
                    try:
                        size = os.path.getsize(full)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        continue
                    report.total_bytes += size
                    report.file_count += 1
                    report.category_totals[category] = (
                        report.category_totals.get(category, 0) + size
                    )
                    if len(report.files) < _MAX_FILE_ENTRIES:
                        report.files.append(FileEntry(path=full, size_bytes=size))
                    elif not capped_note_added:
                        report.errors.append(
                            "Se alcanzo el limite de 2000 archivos en el analisis; "
                            "los restantes no se listan."
                        )
                        capped_note_added = True

        report.elapsed = time.monotonic() - started
        return report

    def clean_now(self, config: CleanConfig) -> CleanResult:
        """Delete the configured targets and report freed space.

        Files are measured BEFORE deletion so ``total_freed`` reflects what was
        present. Permission/sharing failures (winerror 5/32) are silent; other
        per-file failures are recorded. Empty directories are removed silently
        after the walk. The Recycle Bin is emptied through PowerShell.

        Args:
            config: The :class:`CleanConfig` selecting the categories.

        Returns:
            A :class:`CleanResult` with per-category freed bytes.
        """
        ensure_windows()
        result = CleanResult()
        started = time.monotonic()
        whitelist = config.whitelist_paths

        if config.recycle_bin:
            freed, note = self._run_recycle_bin_clean()
            result.category_freed["recycle_bin"] = freed
            result.errors.append(note)

        for category, root in self._targets(config):
            if category in _ADMIN_CATEGORIES and not is_admin():
                result.errors.append(
                    f"La categoria {category} requiere permisos de administrador."
                )
                continue
            if not root.exists():
                if category not in _OPTIONAL_CATEGORIES:
                    result.errors.append(f"No existe la ruta de {category}: {root}")
                continue

            dirpaths: list[str] = []
            for dirpath, filenames in self._walk_target(
                root, whitelist, result.errors.append
            ):
                dirpaths.append(dirpath)
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if self._is_whitelisted(full, whitelist):
                        continue
                    try:
                        size = os.path.getsize(full)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if not _is_silent_delete_error(exc):
                            result.errors.append(f"No se pudo medir {full}: {exc}")
                        continue
                    try:
                        os.remove(full)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if not _is_silent_delete_error(exc):
                            result.errors.append(f"No se pudo eliminar {full}: {exc}")
                        continue
                    result.total_freed += size
                    result.files_deleted += 1
                    result.category_freed[category] = (
                        result.category_freed.get(category, 0) + size
                    )

            for dirpath in reversed(dirpaths):
                try:
                    os.rmdir(dirpath)
                except (PermissionError, OSError):
                    continue

        result.elapsed = time.monotonic() - started
        return result

    # ----------------------------------------------------------- public: async

    def analyze_only_async(
        self,
        config: CleanConfig,
        on_complete: Callable[[CleanAnalysisReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``analyze_only`` in a daemon thread.

        Args:
            config: The :class:`CleanConfig` to analyze.
            on_complete: Called with the :class:`CleanAnalysisReport` when done.
            on_error: Called with the exception when the analysis raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.analyze_only(config), on_complete, on_error, "clean-analyze"
        )

    def clean_now_async(
        self,
        config: CleanConfig,
        on_complete: Callable[[CleanResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``clean_now`` in a daemon thread.

        Args:
            config: The :class:`CleanConfig` to apply.
            on_complete: Called with the :class:`CleanResult` when done.
            on_error: Called with the exception when the cleanup raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.clean_now(config), on_complete, on_error, "clean-now"
        )