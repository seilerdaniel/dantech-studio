"""app_uninstaller.py - AppUninstaller: Win32 and UWP/AppX uninstall + residuals.

Windows-only core module. Win32 programs are discovered through the three
``Uninstall`` registry keys (HKLM 64-bit, HKLM 32-bit/WOW6432Node, HKCU); UWP
packages are listed through ``Get-AppxPackage``. Every subprocess invocation
goes through ``utils.process_runner`` (never raw subprocess).

Uninstall commands are made silent when ``force_silent`` is enabled and, when
``deep_clean`` is requested, common residuals (AppData folders and
``Software\\<name>`` registry keys) are scanned and reported after a
successful uninstall.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TypeVar

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

from utils.process_runner import (
    CommandResult,
    ProcessRunnerError,
    ensure_windows,
    run_command,
)

#: Registry subpaths scanned for installed Win32 programs.
_UNINSTALL_SUBPATHS: tuple[tuple[str, str], ...] = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM64"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
)

#: DisplayName prefixes treated as hidden updates (skipped unless include_hidden).
_HIDDEN_PREFIXES = ("Update for", "Security Update", "Hotfix")

#: Wall-clock limit for a single uninstall command.
_UNINSTALL_TIMEOUT = 600.0

#: Timeout for the Get-AppxPackage enumeration (can be slow on first run).
_APPX_TIMEOUT = 120.0

_T = TypeVar("_T")


def _read_reg_value(key, name: str):
    """Read one registry value, returning None on any read failure."""
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError, PermissionError):
        return None
    return value


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


@dataclass
class UninstallEntry:
    """A single installed program or UWP package that can be uninstalled."""

    display_name: str
    uninstall_string: str
    hive: str  # "HKLM64" | "HKLM32" | "HKCU" | "AppX"
    registry_key: str = ""
    kind: str = "win32"  # "win32" | "uwp"


@dataclass
class ResidualEntry:
    """A leftover folder or registry key detected after uninstalling."""

    path: Optional[str] = None
    registry_key: Optional[str] = None
    description: str = ""


@dataclass
class UninstallResult:
    """Outcome of uninstalling one program, including residual scan."""

    entry: UninstallEntry
    success: bool
    result: CommandResult
    residuals: list[ResidualEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ResidualScanReport:
    """Leftover folders/keys found for one display name."""

    entries: list[ResidualEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class AppUninstaller:
    """Uninstall Win32 programs and UWP packages, cleaning residuals.

    Sync methods return their dataclass reports; ``*_async`` variants run the
    slowest operations (registry/PowerShell enumeration, uninstall) on daemon
    threads so the GUI stays fluid.
    """

    # ------------------------------------------------------------------ helpers

    def _silent_command(
        self, entry: UninstallEntry, force_silent: bool = True
    ) -> list[str]:
        """Build the silent uninstall command tokens for an entry.

        For MSI programs the product code is extracted and invoked through
        ``msiexec /x <code> /qn /norestart``. Other installers are parsed with
        ``shlex.split`` and, when ``force_silent`` is enabled, standard silent
        flags are appended. UWP packages are removed via PowerShell.

        Args:
            entry: The :class:`UninstallEntry` to uninstall.
            force_silent: Append silent flags to non-MSI commands.

        Returns:
            The command tokens to execute.

        Raises:
            ProcessRunnerError: When no command can be derived.
        """
        if entry.kind == "uwp":
            package = entry.uninstall_string.strip()
            if not package:
                raise ProcessRunnerError("El paquete AppX no tiene PackageFullName.")
            return [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Remove-AppxPackage -Package {package} -ErrorAction Stop",
            ]

        raw = entry.uninstall_string
        lower = raw.lower()
        if "msiexec" in lower:
            match = re.search(r"/I\{([^}]+)\}", raw)
            if match:
                return ["msiexec", "/x", match.group(1), "/qn", "/norestart"]
            tokens = shlex.split(raw)
            if force_silent:
                tokens.extend(["/qn", "/norestart"])
            return tokens

        tokens = shlex.split(raw)
        if force_silent:
            tokens.extend(["/quiet", "/norestart"])
        if not tokens:
            raise ProcessRunnerError("No se pudo derivar un comando de desinstalacion valido.")
        return tokens

    def _delete_registry_tree(self, hive_handle, subkey: str) -> None:
        """Recursively delete a registry key and all its subkeys.

        Args:
            hive_handle: winreg hive handle.
            subkey: Full subkey path under the hive.

        Raises:
            OSError: When the key cannot be deleted (access denied, etc.).
        """
        with winreg.OpenKey(hive_handle, subkey, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    child = winreg.EnumKey(key, index)
                except OSError:
                    break
                self._delete_registry_tree(hive_handle, f"{subkey}\\{child}")
                index += 1
        winreg.DeleteKey(hive_handle, subkey)

    # ------------------------------------------------------------ public: sync

    def list_win32(
        self, include_hidden: bool = False
    ) -> tuple[list[UninstallEntry], list[str]]:
        """Enumerate installed Win32 programs from the Uninstall registry keys.

        Args:
            include_hidden: Also include system components and update entries.

        Returns:
            A tuple of the found :class:`UninstallEntry` items and the errors
            accumulated while reading the registry.
        """
        if os.name != "nt" or winreg is None:
            return [], ["Este modulo solo funciona en Windows."]

        entries: list[UninstallEntry] = []
        errors: list[str] = []
        for subpath, hive_name in _UNINSTALL_SUBPATHS:
            hive_handle = (
                winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
            )
            access = winreg.KEY_READ
            if hive_name == "HKLM64":
                access |= winreg.KEY_WOW64_64KEY
            try:
                with winreg.OpenKey(hive_handle, subpath, 0, access) as parent:
                    key_count = winreg.QueryInfoKey(parent)[0]
                    for index in range(key_count):
                        try:
                            subkey_name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(
                                parent, subkey_name, 0, winreg.KEY_READ
                            ) as child:
                                display_raw = _read_reg_value(child, "DisplayName")
                                uninstall_raw = _read_reg_value(child, "UninstallString")
                                system_component = _read_reg_value(child, "SystemComponent")
                        except (PermissionError, OSError):
                            continue
                        display = str(display_raw or "").strip()
                        uninstall = str(uninstall_raw or "").strip()
                        if not display or not uninstall:
                            continue
                        if not include_hidden:
                            if str(system_component or "").strip() == "1":
                                continue
                            if display.lower().startswith(
                                tuple(p.lower() for p in _HIDDEN_PREFIXES)
                            ):
                                continue
                        hive_path = (
                            "HKLM" if hive_name != "HKCU" else "HKCU"
                        )
                        entries.append(
                            UninstallEntry(
                                display_name=display,
                                uninstall_string=uninstall,
                                hive=hive_name,
                                registry_key=f"{hive_path}\\{subpath}\\{subkey_name}",
                                kind="win32",
                            )
                        )
            except (PermissionError, OSError) as exc:
                errors.append(f"No se pudo leer la clave {hive_name}: {exc}")
        return entries, errors

    def list_appx(self) -> tuple[list[UninstallEntry], list[str]]:
        """Enumerate UWP/AppX packages through ``Get-AppxPackage``.

        Uses ``ConvertTo-Json`` so parsing is locale-independent.

        Returns:
            A tuple of the found :class:`UninstallEntry` items (kind ``uwp``)
            and the errors accumulated during the query.
        """
        ensure_windows()
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-AppxPackage | Select-Object -Property Name, PackageFullName "
            "| ConvertTo-Json -Compress",
        )
        result = run_command(command, timeout=_APPX_TIMEOUT)
        if not result.success:
            return [], [result.combined or result.error or "No se pudo listar los paquetes AppX."]

        try:
            rows = _parse_json_output(result.stdout)
        except json.JSONDecodeError as exc:
            return [], [f"No se pudo interpretar la salida de Get-AppxPackage: {exc}"]

        entries: list[UninstallEntry] = []
        for row in rows:
            name = str(row.get("Name") or "").strip()
            full = str(row.get("PackageFullName") or "").strip()
            if not name or not full:
                continue
            entries.append(
                UninstallEntry(
                    display_name=name,
                    uninstall_string=full,
                    hive="AppX",
                    registry_key="",
                    kind="uwp",
                )
            )
        return entries, []

    def scan_residuals(self, display_name: str) -> ResidualScanReport:
        """Search for leftover folders and registry keys of a display name.

        Checks ``%APPDATA%/<name>``, ``%LOCALAPPDATA%/<name>`` and the
        ``Software/<name>`` registry keys (HKLM, HKLM WOW6432Node, HKCU).
        Access failures are silent.

        Args:
            display_name: Display name of the uninstalled program.

        Returns:
            A :class:`ResidualScanReport` with one :class:`ResidualEntry` per
            finding.
        """
        report = ResidualScanReport()
        for base_name, base_value in (
            ("APPDATA", os.environ.get("APPDATA")),
            ("LOCALAPPDATA", os.environ.get("LOCALAPPDATA")),
        ):
            if not base_value:
                continue
            candidate = Path(base_value) / display_name
            try:
                if candidate.exists():
                    report.entries.append(
                        ResidualEntry(
                            path=str(candidate),
                            description=f"Carpeta residual en {candidate}",
                        )
                    )
            except OSError:
                continue

        if os.name != "nt" or winreg is None:
            return report

        for hive_name, hive_handle in (
            ("HKLM", winreg.HKEY_LOCAL_MACHINE),
            ("HKCU", winreg.HKEY_CURRENT_USER),
        ):
            reg_paths = [rf"Software\{display_name}"]
            if hive_name == "HKLM":
                reg_paths.append(rf"Software\WOW6432Node\{display_name}")
            for reg_path in reg_paths:
                try:
                    with winreg.OpenKey(hive_handle, reg_path, 0, winreg.KEY_READ):
                        pass
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                report.entries.append(
                    ResidualEntry(
                        registry_key=f"{hive_name}\\{reg_path}",
                        description=f"Clave de registro residual {hive_name}\\{reg_path}",
                    )
                )
        return report

    def remove_residual(self, entry: ResidualEntry) -> bool:
        """Remove a single residual folder or registry key.

        Folders are deleted recursively; registry keys are deleted together
        with all their subkeys. Access failures return False silently.

        Args:
            entry: The :class:`ResidualEntry` to remove.

        Returns:
            True when the residual was successfully removed.
        """
        removed = False
        if entry.path:
            try:
                if os.path.isdir(entry.path):
                    shutil.rmtree(entry.path)
                elif os.path.isfile(entry.path):
                    os.remove(entry.path)
                removed = not os.path.exists(entry.path)
            except (PermissionError, OSError):
                removed = False

        if entry.registry_key and os.name == "nt" and winreg is not None:
            hive_name, _, subkey = entry.registry_key.partition("\\")
            hive_handle = (
                winreg.HKEY_LOCAL_MACHINE
                if hive_name.upper() == "HKLM"
                else winreg.HKEY_CURRENT_USER
            )
            try:
                self._delete_registry_tree(hive_handle, subkey)
                removed = True
            except (FileNotFoundError, PermissionError, OSError):
                removed = False
        return removed

    def uninstall(
        self,
        entry: UninstallEntry,
        force_silent: bool = True,
        deep_clean: bool = False,
    ) -> UninstallResult:
        """Uninstall one program and optionally scan/remove its residuals.

        Args:
            entry: The :class:`UninstallEntry` to uninstall.
            force_silent: Make the uninstaller run without prompts.
            deep_clean: Scan residuals after a successful uninstall.

        Returns:
            An :class:`UninstallResult` with the command outcome and, when
            ``deep_clean`` is enabled, the detected residuals.
        """
        ensure_windows()
        try:
            command = self._silent_command(entry, force_silent=force_silent)
        except ProcessRunnerError as exc:
            return UninstallResult(
                entry=entry,
                success=False,
                result=CommandResult(stderr=str(exc), returncode=-1, success=False, error=str(exc)),
                errors=[str(exc)],
            )

        result = run_command(command, timeout=_UNINSTALL_TIMEOUT)
        residuals: list[ResidualEntry] = []
        errors: list[str] = []
        if result.success and deep_clean:
            scan = self.scan_residuals(entry.display_name)
            residuals = scan.entries
            errors.extend(scan.errors)
        elif not result.success:
            errors.append(result.combined or result.error or "El desinstalador devolvio un codigo de error.")
        return UninstallResult(
            entry=entry,
            success=result.success,
            result=result,
            residuals=residuals,
            errors=errors,
        )

    # ----------------------------------------------------------- public: async

    def list_win32_async(
        self,
        include_hidden: bool = False,
        on_complete: Callable[[tuple[list[UninstallEntry], list[str]]], None] | None = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``list_win32`` in a daemon thread.

        Args:
            include_hidden: Also include hidden/system entries.
            on_complete: Called with ``(entries, errors)`` when done.
            on_error: Called with the exception when the enumeration raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.list_win32(include_hidden),
            on_complete,
            on_error,
            "uninstaller-list-win32",
        )

    def list_appx_async(
        self,
        on_complete: Callable[[tuple[list[UninstallEntry], list[str]]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``list_appx`` in a daemon thread.

        Args:
            on_complete: Called with ``(entries, errors)`` when done.
            on_error: Called with the exception when the enumeration raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(self.list_appx, on_complete, on_error, "uninstaller-list-appx")

    def uninstall_async(
        self,
        entry: UninstallEntry,
        force_silent: bool = True,
        deep_clean: bool = False,
        on_complete: Callable[[UninstallResult], None] | None = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``uninstall`` in a daemon thread.

        Args:
            entry: The :class:`UninstallEntry` to uninstall.
            force_silent: Make the uninstaller run without prompts.
            deep_clean: Scan residuals after a successful uninstall.
            on_complete: Called with the :class:`UninstallResult` when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.uninstall(entry, force_silent=force_silent, deep_clean=deep_clean),
            on_complete,
            on_error,
            "uninstaller-uninstall",
        )

    def scan_residuals_async(
        self,
        display_name: str,
        on_complete: Callable[[ResidualScanReport], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``scan_residuals`` in a daemon thread.

        Args:
            display_name: Display name of the uninstalled program.
            on_complete: Called with the :class:`ResidualScanReport` when done.
            on_error: Called with the exception when the scan raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.scan_residuals(display_name),
            on_complete,
            on_error,
            "uninstaller-scan-residuals",
        )

    def remove_residual_async(
        self,
        entry: ResidualEntry,
        on_complete: Callable[[bool], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``remove_residual`` in a daemon thread.

        Args:
            entry: The :class:`ResidualEntry` to remove.
            on_complete: Called with the removal success flag when done.
            on_error: Called with the exception when the operation raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.remove_residual(entry),
            on_complete,
            on_error,
            "uninstaller-remove-residual",
        )