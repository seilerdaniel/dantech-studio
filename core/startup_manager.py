"""startup_manager.py - StartupManager: read and toggle Windows startup programs.

Windows-only core module. Startup entries are read from the ``Run`` registry
keys (HKCU + HKLM); the enabled state is read and written through the
``StartupApproved`` binary blob that Windows Explorer uses. Every registry
access is guarded with try/except so missing keys or missing admin rights
degrade to reported errors instead of raising.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so boot info degrades gracefully
    psutil = None  # type: ignore[assignment]

#: Registry path of the autorun entry list.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: Registry path of the StartupApproved enabled/disabled blob.
_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

#: Byte 0 of the StartupApproved blob: 2 = enabled, 3 = disabled.
_APPROVED_ENABLED = 2
_APPROVED_DISABLED = 3

#: StartupApproved blobs are 12 bytes long.
_APPROVED_BLOB_LENGTH = 12

#: Non-Windows fallback message.
_NON_WINDOWS_ERROR = "Este modulo solo funciona en Windows."

_T = TypeVar("_T")


def _make_approved_blob(enabled: bool) -> bytes:
    """Build the 12-byte StartupApproved blob for a given state."""
    return bytes([_APPROVED_ENABLED if enabled else _APPROVED_DISABLED]) + b"\x00" * (
        _APPROVED_BLOB_LENGTH - 1
    )


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
class StartupEntry:
    """A single autorun program found in the registry."""

    name: str
    command: str
    hive: str  # "HKCU" | "HKLM"
    enabled: bool = True
    state_known: bool = False  # True when a StartupApproved blob exists


@dataclass
class StartupActionResult:
    """Outcome of enabling/disabling one startup entry."""

    name: str
    hive: str
    enabled: bool
    success: bool
    error: Optional[str] = None


@dataclass
class BootInfo:
    """System boot time and uptime derived from psutil."""

    boot_time: str = "Desconocido"
    uptime_hours: float = 0.0
    errors: list[str] = field(default_factory=list)


class StartupManager:
    """Read and toggle startup programs through the Windows registry."""

    def _hive_handle(self, hive: str):
        """Map a hive name to its winreg handle."""
        if hive == "HKCU":
            return winreg.HKEY_CURRENT_USER
        return winreg.HKEY_LOCAL_MACHINE

    def _apply_approved_state(self, entry: StartupEntry, hive_handle) -> None:
        """Overwrite ``enabled``/``state_known`` from the StartupApproved blob.

        Missing blob means "not tracked": the entry stays enabled with
        ``state_known=False``. Any registry read failure is ignored.
        """
        try:
            with winreg.OpenKey(hive_handle, _APPROVED_KEY, 0, winreg.KEY_READ) as key:
                blob, _ = winreg.QueryValueEx(key, entry.name)
        except (PermissionError, OSError):
            return
        if isinstance(blob, (bytes, bytearray)) and len(blob) > 0:
            entry.state_known = True
            if blob[0] == _APPROVED_ENABLED:
                entry.enabled = True
            elif blob[0] == _APPROVED_DISABLED:
                entry.enabled = False

    # ------------------------------------------------------------ public: sync

    def list_entries(self) -> tuple[list[StartupEntry], list[str]]:
        """List autorun entries from the HKCU and HKLM ``Run`` keys.

        HKLM can fail without administrator rights; that failure is recorded in
        the returned errors list instead of aborting the enumeration.

        Returns:
            A tuple of the found :class:`StartupEntry` items and the errors
            accumulated while reading the registry.
        """
        if os.name != "nt" or winreg is None:
            return [], [_NON_WINDOWS_ERROR]

        entries: list[StartupEntry] = []
        errors: list[str] = []
        for hive_name, hive_handle in (
            ("HKCU", winreg.HKEY_CURRENT_USER),
            ("HKLM", winreg.HKEY_LOCAL_MACHINE),
        ):
            try:
                with winreg.OpenKey(hive_handle, _RUN_KEY, 0, winreg.KEY_READ) as key:
                    value_count = winreg.QueryInfoKey(key)[1]
                    for index in range(value_count):
                        try:
                            name, command, _value_type = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        entry = StartupEntry(
                            name=name,
                            command=str(command),
                            hive=hive_name,
                        )
                        self._apply_approved_state(entry, hive_handle)
                        entries.append(entry)
            except (PermissionError, OSError) as exc:
                errors.append(f"No se pudo leer la clave {hive_name}: {exc}")
        return entries, errors

    def set_enabled(self, entry: StartupEntry, enabled: bool) -> StartupActionResult:
        """Enable or disable one startup entry via the StartupApproved blob.

        The entry must still exist under its ``Run`` key. HKLM writes without
        administrator rights return ``success=False`` with a clear message; this
        method never raises.

        Args:
            entry: The :class:`StartupEntry` to toggle.
            enabled: Desired state (True = run at startup).

        Returns:
            A :class:`StartupActionResult` describing the outcome.
        """
        result = StartupActionResult(
            name=entry.name,
            hive=entry.hive,
            enabled=enabled,
            success=False,
        )
        if os.name != "nt" or winreg is None:
            result.error = _NON_WINDOWS_ERROR
            return result

        hive_handle = self._hive_handle(entry.hive)
        try:
            with winreg.OpenKey(hive_handle, _RUN_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, entry.name)
        except FileNotFoundError:
            result.error = "Entrada no encontrada"
            return result
        except (PermissionError, OSError) as exc:
            result.error = f"No se pudo verificar la entrada: {exc}"
            return result

        try:
            with winreg.CreateKeyEx(hive_handle, _APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(
                    key,
                    entry.name,
                    0,
                    winreg.REG_BINARY,
                    _make_approved_blob(enabled),
                )
            result.success = True
        except PermissionError:
            result.error = "Se requieren permisos de administrador."
        except OSError as exc:
            result.error = f"No se pudo actualizar el estado: {exc}"
        return result

    def get_boot_info(self) -> BootInfo:
        """Read the system boot time and current uptime.

        Returns:
            A :class:`BootInfo`; when psutil is unavailable the boot time is
            "Desconocido", uptime is 0.0 and the cause is recorded in errors.
        """
        if psutil is None:
            return BootInfo(
                boot_time="Desconocido",
                uptime_hours=0.0,
                errors=["psutil no esta disponible; no se puede leer el boot time."],
            )
        try:
            boot_timestamp = psutil.boot_time()
            boot_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot_timestamp))
            uptime_hours = max((time.time() - boot_timestamp) / 3600.0, 0.0)
            return BootInfo(boot_time=boot_time, uptime_hours=uptime_hours)
        except (OSError, psutil.Error) as exc:
            return BootInfo(
                boot_time="Desconocido",
                uptime_hours=0.0,
                errors=[f"No se pudo leer el boot time: {exc}"],
            )

    # ----------------------------------------------------------- public: async

    def list_entries_async(
        self,
        on_complete: Callable[[tuple[list[StartupEntry], list[str]]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``list_entries`` in a daemon thread.

        Args:
            on_complete: Called with ``(entries, errors)`` when done.
            on_error: Called with the exception when the enumeration raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.list_entries, on_complete, on_error, "startup-list")

    def set_enabled_async(
        self,
        entry: StartupEntry,
        enabled: bool,
        on_complete: Callable[[StartupActionResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``set_enabled`` in a daemon thread.

        Args:
            entry: The :class:`StartupEntry` to toggle.
            enabled: Desired state.
            on_complete: Called with the :class:`StartupActionResult` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.set_enabled(entry, enabled),
            on_complete,
            on_error,
            "startup-set",
        )

    def get_boot_info_async(
        self,
        on_complete: Callable[[BootInfo], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_boot_info`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`BootInfo` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.get_boot_info, on_complete, on_error, "startup-boot")
