"""disk_analyzer.py - DiskAnalyzer: SMART health and disk-space analysis.

Windows-only core module. Reads physical-disk SMART data through PowerShell
(via ``utils.process_runner``) and measures the largest folders on C: with
``os.scandir``. No raw subprocess is ever used here.

Design notes:
- SMART queries usually need Administrator rights; every PowerShell call is
  wrapped in try/except and degrades to an ``Unknown`` health entry instead of
  crashing when elevation is missing.
- Directory measurement skips permission errors and never follows symlinks,
  so a denied or cyclic entry can never abort the whole walk.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TypeVar

from utils.process_runner import ensure_windows, run_command

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so analysis fails gracefully
    psutil = None  # type: ignore[assignment]

#: PowerShell query for physical-disk SMART-ish metadata (admin recommended).
_SMART_COMMAND = (
    "powershell.exe",
    "-NoProfile",
    "-Command",
    "Get-PhysicalDisk | Select-Object -Property "
    "FriendlyName,MediaType,HealthStatus,BusType,Temperature,SpindleSpeed "
    "| ConvertTo-Json -Compress",
)

#: Fallback query that works without SMART access (Get-Disk).
_DISK_FALLBACK_COMMAND = (
    "powershell.exe",
    "-NoProfile",
    "-Command",
    "Get-Disk | Select-Object -Property Number,FriendlyName,BusType,OperationalStatus "
    "| ConvertTo-Json -Compress",
)

#: MSFT_PhysicalDisk.BusType enum -> human media label.
_BUSTYPE_MEDIA = {
    "1": "HDD",  # SCSI
    "3": "HDD",  # ATA
    "4": "SSD",  # IEEE1394 (rare SSD bus)
    "10": "HDD",  # SAS
    "11": "HDD",  # SATA
    "17": "NVMe",
    "scsi": "HDD",
    "ata": "HDD",
    "sas": "HDD",
    "sata": "HDD",
    "hdd": "HDD",
    "ssd": "SSD",
    "nvme": "NVMe",
}

#: MSFT_PhysicalDisk.MediaType enum -> human media label (higher precedence).
_MEDIATYPE_MEDIA = {
    "3": "HDD",
    "4": "SSD",
    "5": "SSD",  # SCM / Optane
    "hdd": "HDD",
    "ssd": "SSD",
    "scm": "SSD",
}

#: MSFT_PhysicalDisk.HealthStatus enum -> normalized health label.
_HEALTHSTATUS_MAP = {
    "0": "Unknown",
    "1": "Healthy",
    "2": "Healthy",
    "3": "Warning",
    "4": "Unhealthy",
    "5": "Unhealthy",
    "unknown": "Unknown",
    "healthy": "Healthy",
    "ok": "Healthy",
    "warning": "Warning",
    "unhealthy": "Unhealthy",
}

_T = TypeVar("_T")


@dataclass
class DiskHealth:
    """SMART health snapshot of one physical disk."""

    friendly_name: str
    media_type: str = "Unknown"
    health_status: str = "Unknown"
    temperature_c: Optional[float] = None
    wear_percent: Optional[float] = None
    errors: list[str] = field(default_factory=list)


@dataclass
class FolderSize:
    """Measured size of one folder tree on disk."""

    path: str
    size_bytes: int = 0
    children: int = 0


@dataclass
class DiskUsage:
    """Capacity snapshot of the C: drive."""

    free_gb: float = 0.0
    total_gb: float = 0.0
    percent: float = 0.0


def _enum_to_label(value: object, mapping: dict[str, str]) -> str:
    """Normalize a PowerShell enum (int or string) into a stable label."""
    if value is None:
        return "Unknown"
    key = str(value).strip()
    lowered = key.lower()
    if key in mapping:
        return mapping[key]
    if lowered in mapping:
        return mapping[lowered]
    if lowered in {"unknown", "unspecified", "0"}:
        return "Unknown"
    return "Unknown"


def _map_media_type(bus_type: object, media_type: object) -> str:
    """Resolve a media label from BusType + MediaType (NVMe has precedence)."""
    if _enum_to_label(bus_type, _BUSTYPE_MEDIA) == "NVMe":
        return "NVMe"
    media = _enum_to_label(media_type, _MEDIATYPE_MEDIA)
    if media != "Unknown":
        return media
    return _enum_to_label(bus_type, _BUSTYPE_MEDIA)


def _map_health_status(value: object) -> str:
    """Normalize a HealthStatus/OperationalStatus value into Healthy/Warning/..."""
    if value is None:
        return "Unknown"
    key = str(value).strip().lower()
    if key in {"healthy", "ok", "online"}:
        return "Healthy"
    if key in {"warning", "degraded"}:
        return "Warning"
    if key in {"unhealthy", "failed", "offline"}:
        return "Unhealthy"
    return _enum_to_label(value, _HEALTHSTATUS_MAP)


def _map_operational_status(value: object) -> str:
    """Normalize a Get-Disk OperationalStatus into the health vocabulary."""
    key = str(value).strip().lower()
    if key in {"online", "ok", "1"}:
        return "Healthy"
    if key in {"offline", "failed", "2"}:
        return "Unhealthy"
    if key in {"degraded", "warning"}:
        return "Warning"
    return "Unknown"


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


def _run_in_thread(
    fn: Callable[[], _T],
    on_complete: Callable[[_T], None],
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


class DiskAnalyzer:
    """Query disk SMART health and locate the largest folders on C:.

    Sync methods return reports; ``*_async`` variants run the slowest queries
    (SMART and folder measurement) on daemon threads so the GUI stays fluid.
    """

    # ------------------------------------------------------------------ SMART

    def get_smart_health(self) -> list[DiskHealth]:
        """Query physical-disk health through PowerShell.

        Tries ``Get-PhysicalDisk`` (richer SMART data, often needs admin) and
        falls back to ``Get-Disk`` when the first query fails or is empty. When
        both fail, a single ``Unknown`` entry carries the error messages.

        Returns:
            A list of :class:`DiskHealth`, one per physical disk.
        """
        ensure_windows()
        disks, errors = self._query_physical_disks()
        fallback_used = False
        if not disks:
            disks, errors = self._query_disk_fallback(errors)
            fallback_used = True
        if not disks:
            errors.append("No se pudieron consultar los discos físicos del sistema.")
            return [
                DiskHealth(
                    friendly_name="Desconocido",
                    media_type="Unknown",
                    health_status="Unknown",
                    errors=errors,
                )
            ]
        if fallback_used:
            return [self._build_fallback_disk_health(disk) for disk in disks]
        return [self._build_disk_health(disk) for disk in disks]

    def _query_physical_disks(self) -> tuple[list[dict], list[str]]:
        """Run the ``Get-PhysicalDisk`` query and parse its JSON output."""
        errors: list[str] = []
        try:
            result = run_command(_SMART_COMMAND)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"No se pudo lanzar Get-PhysicalDisk: {exc}")
            return [], errors
        if not result.success or not result.stdout.strip():
            message = result.combined or "Get-PhysicalDisk no devolvió datos."
            errors.append(f"Consulta SMART fallida: {message}")
            return [], errors
        try:
            return _parse_json_output(result.stdout), errors
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"No se pudo interpretar la salida SMART: {exc}")
            return [], errors

    def _query_disk_fallback(self, errors: list[str]) -> tuple[list[dict], list[str]]:
        """Run the ``Get-Disk`` fallback query when SMART data is unavailable."""
        try:
            result = run_command(_DISK_FALLBACK_COMMAND)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"No se pudo lanzar Get-Disk: {exc}")
            return [], errors
        if not result.success or not result.stdout.strip():
            message = result.combined or "Get-Disk no devolvió datos."
            errors.append(f"Consulta fallback fallida: {message}")
            return [], errors
        try:
            return _parse_json_output(result.stdout), errors
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"No se pudo interpretar la salida de Get-Disk: {exc}")
            return [], errors

    def _build_disk_health(self, disk: dict) -> DiskHealth:
        """Build a :class:`DiskHealth` from a parsed physical-disk dict."""
        friendly_name = str(disk.get("FriendlyName") or "Disco").strip()
        health = DiskHealth(
            friendly_name=friendly_name,
            media_type=_map_media_type(disk.get("BusType"), disk.get("MediaType")),
            health_status=_map_health_status(disk.get("HealthStatus")),
        )
        temperature = disk.get("Temperature")
        if temperature is not None:
            try:
                health.temperature_c = float(temperature)
            except (TypeError, ValueError):
                health.temperature_c = None
        health.wear_percent = self._query_wear_percent(friendly_name)
        return health

    def _build_fallback_disk_health(self, disk: dict) -> DiskHealth:
        """Build a :class:`DiskHealth` from a parsed Get-Disk dict.

        Get-Disk exposes ``OperationalStatus`` instead of ``HealthStatus`` and
        no SMART temperature; those fields stay ``Unknown``/None.
        """
        return DiskHealth(
            friendly_name=str(disk.get("FriendlyName") or "Disco").strip(),
            media_type=_enum_to_label(disk.get("BusType"), _BUSTYPE_MEDIA),
            health_status=_map_operational_status(disk.get("OperationalStatus")),
        )

    def _query_wear_percent(self, friendly_name: str) -> Optional[float]:
        """Read the storage wear percentage via StorageReliabilityCounter.

        Returns None (silently) when the query is unavailable or the disk does
        not report wear, e.g. most NVMe drives or a non-admin session.
        """
        safe_name = friendly_name.replace("'", "")
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Get-StorageReliabilityCounter -PhysicalDisk '{safe_name}' "
            "| Select-Object -Property Wear | ConvertTo-Json -Compress",
        )
        try:
            result = run_command(command)
        except Exception:  # pragma: no cover - defensive
            return None
        if not result.success or not result.stdout.strip():
            return None
        try:
            entries = _parse_json_output(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        if not entries:
            return None
        wear = entries[0].get("Wear")
        if wear is None:
            return None
        try:
            return float(wear)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------ folder sizes

    def get_top_folders(
        self,
        root: Path = Path("C:/"),
        limit: int = 10,
        max_depth: int = 3,
    ) -> list[FolderSize]:
        """Return the ``limit`` largest direct subfolders of ``root``.

        Only first-level subfolders are reported; each one is measured
        recursively up to ``max_depth`` levels. Permission errors and symlinks
        are skipped so the walk never aborts. The root itself is not included.

        Args:
            root: Drive/directory whose direct children are measured.
            limit: How many folders to return.
            max_depth: Recursion depth for aggregated sizes.

        Returns:
            A list of :class:`FolderSize`, largest first.
        """
        ensure_windows()
        root = Path(root)
        try:
            with os.scandir(root) as iterator:
                entries = list(iterator)
        except (PermissionError, OSError, FileNotFoundError):
            return []

        folders: list[FolderSize] = []
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            size, children = self._measure_folder(path, 0, max_depth)
            folders.append(
                FolderSize(path=str(path), size_bytes=size, children=children)
            )

        folders.sort(key=lambda folder: folder.size_bytes, reverse=True)
        return folders[:limit]

    def _measure_folder(self, path: Path, depth: int, max_depth: int) -> tuple[int, int]:
        """Recursively sum file bytes and file count under ``path``.

        Returns:
            A ``(size_bytes, children)`` tuple; partial results on permission
            errors are returned instead of raising.
        """
        size = 0
        children = 0
        if depth >= max_depth:
            return size, children
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            sub_size, sub_children = self._measure_folder(
                                Path(entry.path), depth + 1, max_depth
                            )
                            size += sub_size
                            children += sub_children
                        elif entry.is_file(follow_symlinks=False):
                            size += entry.stat(follow_symlinks=False).st_size
                            children += 1
                    except (PermissionError, OSError, FileNotFoundError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            pass
        return size, children

    # ------------------------------------------------------------ disk usage

    def get_disk_usage(self) -> DiskUsage:
        """Read the C: drive capacity through psutil.

        Returns:
            A :class:`DiskUsage`; zeroed values when psutil is unavailable.
        """
        ensure_windows()
        if psutil is None:
            return DiskUsage()
        try:
            usage = psutil.disk_usage("C:/")
        except (psutil.Error, OSError):
            return DiskUsage()
        return DiskUsage(
            free_gb=usage.free / (1024.0 ** 3),
            total_gb=usage.total / (1024.0 ** 3),
            percent=float(usage.percent),
        )

    # ------------------------------------------------------------ async

    def get_smart_health_async(
        self,
        on_complete: Callable[[list[DiskHealth]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_smart_health`` in a daemon thread (PowerShell round-trip).

        Args:
            on_complete: Called with the :class:`DiskHealth` list when done.
            on_error: Called with the exception when the query raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.get_smart_health, on_complete, on_error, "smart-health")

    def get_top_folders_async(
        self,
        root: Path = Path("C:/"),
        limit: int = 10,
        on_complete: Optional[Callable[[list[FolderSize]], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_top_folders`` in a daemon thread (slow disk walk).

        Args:
            root: Drive/directory whose direct children are measured.
            limit: How many folders to return.
            on_complete: Called with the :class:`FolderSize` list when done.
            on_error: Called with the exception when the walk raises.

        Returns:
            The started daemon thread.
        """
        if on_complete is None:

            def _noop(_result: list[FolderSize]) -> None:
                return

            on_complete = _noop

        return _run_in_thread(
            lambda: self.get_top_folders(root=root, limit=limit),
            on_complete,
            on_error,
            "top-folders",
        )
