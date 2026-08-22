"""main_window.py - DanTechStudioApp: main GUI window of the DanTech Studio suite.

A dark-mode desktop suite for Windows built on CustomTkinter. It provides a
sidebar navigation with nine views (Dashboard, Memoria, Reparación, Seguridad,
Apps, Scripts, Recuperación, Red y conectividad, Programas de inicio) that
expose the functionality of the ``core`` modules.

Execution contract:
    - This module NEVER runs system commands directly: every long-running
      operation is delegated to ``utils.process_runner`` or to the async
      variants of the ``core`` classes.
    - Long operations run on daemon threads; UI updates from worker threads
      are marshalled back to the main thread through ``self.after(0, ...)``.
    - ``psutil`` is used ONLY for read-only telemetry snapshots (CPU/RAM),
      never to launch processes.

All user-facing strings are in neutral Spanish; code identifiers and
docstrings are in English.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import customtkinter as ctk
from customtkinter import filedialog
from tkinter import TclError, messagebox

from core.app_uninstaller import (
    AppUninstaller,
    ResidualEntry,
    UninstallEntry,
    UninstallResult,
)
from core.audit import audit
from core.data_recovery import DataRecovery, EXTENSION_GROUPS, RecoveryJobResult
from core.driver_manager import DriverInfo, DriverManager, DriverUpdateResult
from core.disk_analyzer import DiskAnalyzer, DiskHealth, DiskUsage
from core.installer import Installer, WingetResult
from core.malware_cleaner import MalwareCleaner, ScanReport
from core.memory_optimizer import (
    CleanupReport,
    CombinedReport,
    MemoryOptimizer,
    RamReport,
)
from core.network_diagnostic import NetworkDiagnostic, NetworkResetResult, PingResult
from core.report_generator import ActionRecord, ReportGenerator
from core.startup_manager import BootInfo, StartupActionResult, StartupEntry, StartupManager
from core.system_cleaner import (
    CleanAnalysisReport,
    CleanConfig,
    CleanResult,
    SystemCleaner,
)
from core.system_repair import RepairReport, SystemRepair
from utils import process_runner
from utils.process_runner import CommandResult

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - telemetry degrades gracefully
    psutil = None  # type: ignore[assignment]

try:
    import qrcode  # type: ignore[import-untyped]
    from PIL import Image, ImageTk  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - QR codes degrade gracefully
    qrcode = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

#: Absolute path of the PowerShell maintenance script executed by the Scripts view.
_SCRIPT_PATH: Path = Path(process_runner.get_resource_path("scripts/optimize_windows.ps1"))

#: Ordered sidebar navigation labels (view names, Spanish UI strings).
_NAV_ITEMS: tuple[str, ...] = (
    "Dashboard",
    "Memoria",
    "Reparación",
    "Seguridad",
    "Apps",
    "Scripts",
    "Recuperación",
    "Red & Conectividad",
    "Programas de Inicio",
)

#: Highlight colors for the active navigation button.
_NAV_ACTIVE_FG: str = "#1f6aa5"
_NAV_INACTIVE_FG: str = "transparent"

#: Log line colors.
_LOG_COLOR_INFO: str = "#dce4ee"
_LOG_COLOR_ERROR: str = "#ff6b6b"

#: Spanish labels for the SystemCleaner cleanup categories (GUI translation).
_CLEANER_CATEGORY_LABELS: dict[str, str] = {
    "temp_user": "Temporales de usuario",
    "temp_system": "Temporales del sistema",
    "prefetch": "Prefetch",
    "software_distribution": "SoftwareDistribution",
    "recycle_bin": "Papelera",
    "browser_cache_chrome": "Cache Chrome",
    "browser_cache_edge": "Cache Edge",
}

#: Status banner / indicator colors (success, warning, failure).
_COLOR_OK: str = "#4caf50"
_COLOR_WARN: str = "#f39c12"
_COLOR_BAD: str = "#e74c3c"

_MB: float = 1024.0 ** 2
_GB: float = 1024.0 ** 3


class DanTechStudioApp(ctk.CTk):
    """Main window of DanTech Studio: sidebar navigation plus content panel.

    Each sidebar button shows a dedicated view frame; every long operation
    runs on a daemon thread and reports back through ``self.after(0, ...)``
    so the widgets are only ever touched from the main thread.
    """

    def __init__(self, script_path: Optional[str] = None) -> None:
        """Build the window, the sidebar and the nine views, then start telemetry.

        Args:
            script_path: Optional absolute path of the PowerShell maintenance
                script; defaults to the resolved bundled ``_SCRIPT_PATH``.

        Automatic DPI-aware scaling is deactivated BEFORE any widget exists:
        on Windows it can leave the scale factor as None, which crashes
        CTkTextbox creation inside ``_apply_widget_scaling`` with a TypeError
        ("'NoneType' and 'float'"). Forcing a fixed scale keeps the layout
        deterministic across monitors.
        """
        self._script_path: Path = Path(script_path) if script_path else _SCRIPT_PATH

        ctk.deactivate_automatic_dpi_awareness()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)

        super().__init__(fg_color="#1a1a1a")

        self.title("DanTech Studio")
        self.geometry("1100x700")
        self.minsize(900, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._memory_optimizer = MemoryOptimizer()
        self._system_repair = SystemRepair()
        self._malware_cleaner = MalwareCleaner()
        self._installer = Installer()
        self._report_generator = ReportGenerator()
        self._disk_analyzer = DiskAnalyzer()
        self._recovery_manager = DataRecovery()
        self._network_diagnostic = NetworkDiagnostic()
        self._startup_manager = StartupManager()
        self._system_cleaner = SystemCleaner()
        self._app_uninstaller = AppUninstaller()
        self._driver_manager = DriverManager()

        self._active_buttons: list[Any] = []
        self._active_progress: Optional[Any] = None
        self._views: dict[str, ctk.CTkFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._startup_entries: list[StartupEntry] = []
        self._uninstall_entries: list[UninstallEntry] = []
        self._uninstall_residuals: list[ResidualEntry] = []
        self._qr_window: Optional[ctk.CTkToplevel] = None
        self._qr_images: list[Any] = []

        self._telemetry_active: bool = False
        self._telemetry_thread: Optional[threading.Thread] = None

        self._build_sidebar()
        self._build_views()
        self._refresh_admin_status()

        self._show_view("Dashboard")
        self._start_telemetry()

    # ------------------------------------------------------------------ utils

    def _append_log(self, log: ctk.CTkTextbox, line: str, error: bool = False) -> None:
        """Insert one timestamped line into a read-only log textbox."""
        prefix = time.strftime("[%H:%M:%S]")
        tag = "error" if error else "info"
        try:
            log.configure(state="normal")
            log.insert("end", f"{prefix} {line}\n", tag)
            log.see("end")
            log.configure(state="disabled")
        except TclError:
            pass

    def _begin_operation(
        self,
        buttons: Sequence[ctk.CTkButton],
        progress: Optional[ctk.CTkProgressBar],
    ) -> None:
        """Disable the given buttons and start an indeterminate progress bar."""
        self._active_buttons = list(buttons)
        self._active_progress = progress
        for button in buttons:
            button.configure(state="disabled")
        if progress is not None:
            progress.configure(mode="indeterminate")
            progress.start()

    def _end_operation(
        self,
        buttons: Sequence[ctk.CTkButton],
        progress: Optional[ctk.CTkProgressBar],
    ) -> None:
        """Re-enable the given buttons and stop the progress bar."""
        for button in buttons:
            button.configure(state="normal")
        if progress is not None:
            progress.stop()
            progress.set(0)
        self._active_buttons = []
        self._active_progress = None

    def _launch(
        self,
        title: str,
        buttons: Sequence[ctk.CTkButton],
        progress: Optional[ctk.CTkProgressBar],
        log: ctk.CTkTextbox,
        starter: Callable[[Callable[[Any], None], Callable[[Any], None]], threading.Thread],
        on_done: Callable[[Any], None],
        on_error: Callable[[Any], None],
    ) -> None:
        """Run one background operation with the standard UI lifecycle.

        Args:
            title: Operation label shown in the log.
            buttons: Buttons to disable while the operation runs.
            progress: Progress bar to animate while the operation runs.
            log: Log textbox that receives the operation output.
            starter: Callable that launches the worker thread; it receives the
                completion and error callbacks and must call them from the
                worker thread.
            on_done: Main-thread handler invoked with the operation result.
            on_error: Main-thread handler invoked with the operation error.
        """
        self._begin_operation(buttons, progress)
        self._append_log(log, f"Iniciando: {title}")

        def _complete(result: Any) -> None:
            try:
                self.after(0, on_done, result)
            except TclError:
                pass

        def _fail(error: Any) -> None:
            try:
                self.after(0, on_error, error)
            except TclError:
                pass

        try:
            starter(_complete, _fail)
        except Exception as exc:
            self._end_operation(buttons, progress)
            self._append_log(log, f"No se pudo iniciar la operación: {exc}", error=True)

    def _run_sync_in_thread(
        self,
        fn: Callable[..., Any],
        complete: Callable[[Any], None],
        fail: Callable[[Any], None],
        *args: Any,
    ) -> threading.Thread:
        """Run a blocking callable in a daemon thread (core has no async variant)."""

        def _worker() -> None:
            try:
                result = fn(*args)
            except Exception as exc:
                fail(exc)
            else:
                complete(result)

        thread = threading.Thread(target=_worker, name="sync-worker", daemon=True)
        thread.start()
        return thread

    def _error_text(self, error: Any) -> str:
        """Extract a displayable message from any error/report object."""
        if isinstance(error, CommandResult):
            raw = (error.stderr or "").strip() or (error.error or "")
            return raw or f"Exit code {error.returncode}."
        if hasattr(error, "result") and isinstance(getattr(error, "result"), CommandResult):
            return self._error_text(error.result)
        return str(error) or "Error desconocido."

    def _admin_hint(self, text: str) -> Optional[str]:
        """Return a friendly admin hint when a message signals missing elevation."""
        if "administrador" in text.lower():
            return (
                "Se requieren permisos de administrador. Cierra la app y vuelve a "
                "abrirla con 'Ejecutar como administrador'."
            )
        return None

    def _log_error(self, log: ctk.CTkTextbox, error: Any) -> None:
        """Log an error, translating the 'admin required' case into a hint."""
        text = self._error_text(error)
        hint = self._admin_hint(text)
        self._append_log(log, hint if hint else text, error=True)

    # ---------------------------------------------------------------- helpers

    def _format_size(self, value: float) -> str:
        """Render a byte count as a human-readable size."""
        if value >= _GB:
            return f"{value / _GB:.2f} GB"
        if value >= _MB:
            return f"{value / _MB:.2f} MB"
        if value >= 1024.0:
            return f"{value / 1024.0:.1f} KB"
        return f"{value:.0f} B"

    def _format_command_result(self, result: CommandResult) -> list[str]:
        """Format a CommandResult into log lines."""
        lines: list[str] = []
        if result.error:
            hint = self._admin_hint(result.error)
            lines.append(hint if hint else f"Error: {result.error}")
        if result.stderr.strip() and result.stderr.strip() != (result.error or ""):
            lines.append(result.stderr.strip())
        if result.stdout.strip():
            lines.append(result.stdout.strip())
        lines.append(f"Exit code: {result.returncode}")
        lines.append(f"Resultado: {'Correcto' if result.success else 'Fallido'}")
        if result.timed_out:
            lines.append("La operación alcanzó el tiempo máximo permitido.")
        return lines

    def _format_cleanup_report(self, report: CleanupReport) -> list[str]:
        """Format a CleanupReport into log lines."""
        lines: list[str] = [
            f"Espacio liberado: {self._format_size(float(report.bytes_freed))} "
            f"({report.bytes_freed:,} bytes)",
            f"Archivos eliminados: {report.files_deleted:,}",
            f"Tiempo: {report.elapsed:.2f} s",
        ]
        if report.errors:
            lines.append("Avisos:")
            lines.extend(f"  - {err}" for err in report.errors)
        return lines

    def _format_ram_report(self, report: RamReport) -> list[str]:
        """Format a RamReport into log lines."""
        lines: list[str] = [
            f"RAM total: {report.ram_total_mb:,.0f} MB",
            f"RAM en uso: {report.ram_used_mb:,.0f} MB",
            f"RAM disponible: {report.ram_available_mb:,.0f} MB",
            f"Memoria recortada: {self._format_size(report.working_set_trimmed_mb * _MB)}",
            f"Procesos ajustados: {report.processes_trimmed}",
            f"Tiempo: {report.elapsed:.2f} s",
        ]
        if report.errors:
            lines.append("Avisos:")
            lines.extend(f"  - {err}" for err in report.errors)
        return lines

    def _format_combined_report(self, report: CombinedReport) -> list[str]:
        """Format a CombinedReport (temp cleanup + RAM) into log lines."""
        lines: list[str] = []
        for temp in report.temp_reports:
            lines.append(f"Temporales ({temp.path}):")
            lines.extend(f"  {line}" for line in self._format_cleanup_report(temp))
        if report.ram is not None:
            lines.append("Memoria RAM:")
            lines.extend(f"  {line}" for line in self._format_ram_report(report.ram))
        if report.errors:
            lines.append("Avisos generales:")
            lines.extend(f"  - {err}" for err in report.errors)
        return lines

    def _format_repair_report(self, report: RepairReport) -> list[str]:
        """Format a RepairReport into log lines."""
        lines: list[str] = [
            f"Operación: {report.operation}",
            f"Estado general: {'Correcto' if report.success else 'Fallido'}",
        ]
        for index, result in enumerate(report.results, start=1):
            lines.append(f"Paso {index}:")
            lines.extend(f"  {line}" for line in self._format_command_result(result))
        return lines

    def _format_scan_report(self, report: ScanReport) -> list[str]:
        """Format a ScanReport into log lines."""
        lines: list[str] = [f"Análisis: {report.scan_type}"]
        lines.extend(self._format_command_result(report.result))
        return lines

    def _format_winget_result(self, report: WingetResult) -> list[str]:
        """Format a WingetResult into log lines."""
        lines: list[str] = [
            f"Operación: {report.operation}",
            f"Paquete: {report.package or 'N/A'}",
        ]
        lines.extend(self._format_command_result(report.result))
        return lines

    # --------------------------------------------------------------- sidebar

    def _build_sidebar(self) -> None:
        """Create the fixed navigation sidebar with the admin status footer."""
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            self.sidebar, text="DanTech Studio", font=("Segoe UI", 18, "bold")
        )
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 2))

        subtitle = ctk.CTkLabel(
            self.sidebar,
            text="Optimización y reparación",
            font=("Segoe UI", 11),
            text_color="#8a8a8a",
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 16))

        for index, name in enumerate(_NAV_ITEMS):
            button = ctk.CTkButton(
                self.sidebar,
                text=name,
                anchor="w",
                height=34,
                corner_radius=6,
                fg_color=_NAV_INACTIVE_FG,
                hover_color="#2e2e2e",
                command=lambda nav_name=name: self._show_view(nav_name),
            )
            button.grid(row=2 + index, column=0, sticky="ew", padx=12, pady=2)
            self._nav_buttons[name] = button

        self.sidebar.grid_rowconfigure(11, weight=1)

        self._admin_status_label = ctk.CTkLabel(
            self.sidebar, text="Administrador: --", font=("Segoe UI", 12)
        )
        self._admin_status_label.grid(row=12, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._admin_button = ctk.CTkButton(
            self.sidebar,
            text="Ejecutar como administrador",
            height=32,
            command=self._relaunch_as_admin,
        )
        self._admin_button.grid(row=13, column=0, sticky="ew", padx=12, pady=(0, 16))

    def _refresh_admin_status(self) -> None:
        """Update the sidebar admin label and show/hide the elevation button."""
        try:
            admin = process_runner.is_admin()
        except Exception:
            admin = False
        self._admin_status_label.configure(text=f"Administrador: {'Sí' if admin else 'No'}")
        if admin:
            self._admin_button.grid_remove()
        else:
            self._admin_button.grid()

    def _relaunch_as_admin(self) -> None:
        """Relaunch this app elevated through UAC (runas)."""
        try:
            target = self._elevated_target()
            ok = process_runner.run_as_admin(target, show_window=True)
        except Exception as exc:
            self._admin_status_label.configure(text=f"Error al elevar: {exc}")
            return
        if ok:
            self._admin_status_label.configure(text="Solicitud de UAC enviada...")
        else:
            self._admin_status_label.configure(text="Elevación cancelada.")

    def _elevated_target(self) -> str:
        """Build the command line that relaunches this app elevated.

        Falls back to ``sys.executable`` alone when the current entry script
        cannot be resolved without spaces in the paths.
        """
        script: Optional[str] = None
        if sys.argv and sys.argv[0] and Path(sys.argv[0]).suffix.lower() in (".py", ".pyw"):
            script = str(Path(sys.argv[0]).resolve())
        if script and " " not in sys.executable and " " not in script:
            return f"{sys.executable} {script}"
        return sys.executable

    # ---------------------------------------------------------------- content

    def _build_views(self) -> None:
        """Create the content panel and register all six views."""
        self.content_panel = ctk.CTkFrame(self, corner_radius=0)
        self.content_panel.grid(row=0, column=1, sticky="nsew")
        self.content_panel.grid_rowconfigure(0, weight=1)
        self.content_panel.grid_columnconfigure(0, weight=1)

        self._views["Dashboard"] = self._build_dashboard()
        self._views["Memoria"] = self._build_memory()
        self._views["Reparación"] = self._build_repair()
        self._views["Seguridad"] = self._build_security()
        self._views["Apps"] = self._build_apps()
        self._views["Scripts"] = self._build_scripts()
        self._views["Recuperación"] = self._build_recovery()
        self._views["Red & Conectividad"] = self._build_network()
        self._views["Programas de Inicio"] = self._build_startup()

    def _show_view(self, name: str) -> None:
        """Display one view frame and highlight its navigation button."""
        for view_name, frame in self._views.items():
            if view_name == name:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        for nav_name, button in self._nav_buttons.items():
            button.configure(
                fg_color=_NAV_ACTIVE_FG if nav_name == name else _NAV_INACTIVE_FG
            )

    def _section_title(self, parent: ctk.CTkFrame, text: str) -> ctk.CTkLabel:
        """Create the standard view title label."""
        return ctk.CTkLabel(parent, text=text, font=("Segoe UI", 22, "bold"))

    def _section_log(
        self, parent: ctk.CTkFrame, row: int, height: int = 240
    ) -> ctk.CTkTextbox:
        """Create the standard readonly log textbox for a view.

        Note: never pass ``height=None`` to CTkTextbox — CustomTkinter 6.0.0
        scales the desired height and crashes with
        "unsupported operand type(s) for *: 'NoneType' and 'float'"
        in ``_apply_widget_scaling``.
        """
        log = ctk.CTkTextbox(parent, wrap="word", state="disabled", height=height)
        log.grid(row=row, column=0, sticky="nsew", padx=16, pady=16)
        log.tag_config("info", foreground=_LOG_COLOR_INFO)
        log.tag_config("error", foreground=_LOG_COLOR_ERROR)
        return log

    # --------------------------------------------------------------- dashboard

    def _build_dashboard(self) -> ctk.CTkFrame:
        """Build the telemetry view: gauges, quick actions, disk health and log."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(13, weight=1)

        title = self._section_title(frame, "Dashboard")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 12))

        self._dash_cpu_label = ctk.CTkLabel(
            frame, text="CPU: --", font=("Segoe UI", 14)
        )
        self._dash_cpu_label.grid(row=1, column=0, sticky="w", padx=16)

        self._dash_cpu_bar = ctk.CTkProgressBar(frame, height=18)
        self._dash_cpu_bar.set(0)
        self._dash_cpu_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 8))

        self._dash_ram_label = ctk.CTkLabel(
            frame, text="RAM: --", font=("Segoe UI", 14)
        )
        self._dash_ram_label.grid(row=3, column=0, sticky="w", padx=16)

        self._dash_ram_bar = ctk.CTkProgressBar(frame, height=18)
        self._dash_ram_bar.set(0)
        self._dash_ram_bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 4))

        self._dash_ram_info = ctk.CTkLabel(
            frame, text="Memoria disponible: --", font=("Segoe UI", 12), text_color="#8a8a8a"
        )
        self._dash_ram_info.grid(row=5, column=0, sticky="w", padx=16, pady=(0, 8))

        actions_row = ctk.CTkFrame(frame, fg_color="transparent")
        actions_row.grid(row=6, column=0, sticky="ew", padx=16, pady=(8, 4))
        actions_row.grid_columnconfigure(3, weight=1)

        self._dash_export_button = ctk.CTkButton(
            actions_row,
            text="Exportar Informe (PDF/HTML)",
            command=self._dashboard_export_report,
        )
        self._dash_export_button.grid(row=0, column=0, padx=(0, 8))

        self._dash_express_button = ctk.CTkButton(
            actions_row,
            text="⚡ Mantenimiento Express (1-Clic)",
            fg_color=_NAV_ACTIVE_FG,
            hover_color="#155a8a",
            command=self._dashboard_express_maintenance,
        )
        self._dash_express_button.grid(row=0, column=1)

        self._dash_qr_button = ctk.CTkButton(
            actions_row,
            text="Mostrar QR de Contacto",
            command=self._dashboard_show_qr,
        )
        self._dash_qr_button.grid(row=0, column=2, padx=(8, 0))

        self._dash_express_progress = ctk.CTkProgressBar(frame, mode="determinate")
        self._dash_express_progress.set(0)
        self._dash_express_progress.grid(row=7, column=0, sticky="ew", padx=16, pady=(4, 2))

        self._dash_express_status = ctk.CTkLabel(
            frame,
            text="Mantenimiento Express disponible.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        self._dash_express_status.grid(row=8, column=0, sticky="w", padx=16, pady=(0, 6))

        self._dash_banner = ctk.CTkLabel(
            frame,
            text="",
            font=("Segoe UI", 13, "bold"),
            corner_radius=6,
            fg_color=_COLOR_OK,
            text_color="#0d0d0d",
        )
        self._dash_banner.grid_remove()

        disk_header = ctk.CTkLabel(
            frame, text="Salud de Disco", font=("Segoe UI", 14, "bold")
        )
        disk_header.grid(row=10, column=0, sticky="w", padx=16, pady=(8, 2))

        self._dash_disk_label = ctk.CTkLabel(
            frame,
            text="Consultando salud del disco...",
            font=("Segoe UI", 13),
            text_color="#8a8a8a",
        )
        self._dash_disk_label.grid(row=11, column=0, sticky="w", padx=16, pady=(0, 6))

        processes_header = ctk.CTkLabel(
            frame, text="Procesos con más memoria", font=("Segoe UI", 14, "bold")
        )
        processes_header.grid(row=12, column=0, sticky="w", padx=16, pady=(8, 4))

        self._dash_procs_box = ctk.CTkTextbox(frame, wrap="word", state="disabled")
        self._dash_procs_box.grid(row=13, column=0, sticky="nsew", padx=16, pady=(0, 8))

        self._dash_log = self._section_log(frame, row=14, height=110)
        self._start_disk_health_fetch()
        return frame

    def _dashboard_export_report(self) -> None:
        """Export a diagnostic report (PDF/HTML) chosen through a save dialog."""
        target = filedialog.asksaveasfilename(
            title="Exportar informe",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("HTML", "*.html")],
            initialfile="informe_dantech_studio",
        )
        if not target:
            return
        kind = "html" if Path(target).suffix.lower() == ".html" else "pdf"
        audit("report_export", f"formato={kind}")
        actions = self._session_actions()
        self._launch(
            "Exportar informe",
            [self._dash_export_button],
            None,
            self._dash_log,
            lambda complete, fail: self._report_generator.generate_async(
                kind, Path(target), actions, on_complete=complete, on_error=fail
            ),
            self._dashboard_report_done,
            self._dashboard_report_error,
        )

    def _session_actions(self) -> list[ActionRecord]:
        """Build the action list for the report export (no session registry yet)."""
        return [
            ActionRecord(
                label="Informe generado desde DanTech Studio",
                detail=time.strftime("Exportado el %d/%m/%Y a las %H:%M"),
                status="ok",
            )
        ]

    def _dashboard_report_done(self, path: Path) -> None:
        """Log the path where the report was written."""
        self._end_operation([self._dash_export_button], None)
        self._append_log(self._dash_log, f"Informe guardado: {path}")

    def _dashboard_report_error(self, error: Any) -> None:
        """Log a report-export error."""
        self._end_operation([self._dash_export_button], None)
        self._log_error(self._dash_log, error)

    def _dashboard_express_maintenance(self) -> None:
        """Run the one-click maintenance sequence on a daemon thread.

        Memory optimization is synchronous (10-60 s), so the whole sequence
        runs off the main thread; only the progress bar and status labels are
        refreshed through ``self.after``.
        """
        self._begin_operation([self._dash_express_button], self._dash_express_progress)
        self._dash_express_status.configure(text="Limpiando temporales...")
        self._hide_dashboard_banner()

        def _update_status(text: str) -> None:
            try:
                self.after(0, lambda: self._dash_express_status.configure(text=text))
            except TclError:
                pass

        def _worker() -> None:
            total_bytes: float = 0.0
            warnings: list[str] = []
            try:
                _update_status("Limpiando temporales y memoria RAM...")
                combined = self._memory_optimizer.optimize_all()
                total_bytes += sum(
                    float(temp.bytes_freed) for temp in combined.temp_reports
                )
                if combined.ram is not None:
                    total_bytes += combined.ram.working_set_trimmed_mb * _MB
                warnings.extend(combined.errors)

                _update_status("Vaciando cache de red...")
                dns = process_runner.run_command(["ipconfig", "/flushdns"])
                if not dns.success:
                    warnings.append(dns.stderr.strip() or "ipconfig /flushdns falló.")

                _update_status("Vaciando papelera de reciclaje...")
                recycle = process_runner.run_command(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-Command",
                        "Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
                    ]
                )
                if not recycle.success:
                    warnings.append(
                        recycle.stderr.strip() or "No se pudo vaciar la papelera."
                    )

                _update_status("Optimizando Windows...")
                if not self._script_path.is_file():
                    warnings.append(
                        f"No se encontró el script de mantenimiento en {self._script_path}."
                    )
                else:
                    powershell = process_runner.find_executable(("powershell.exe",))
                    if powershell is None:
                        warnings.append("No se encontró powershell.exe.")
                    else:
                        ps1 = process_runner.run_command(
                            [
                                str(powershell),
                                "-NoProfile",
                                "-ExecutionPolicy",
                                "Bypass",
                                "-File",
                                str(self._script_path.resolve()),
                            ]
                        )
                        if not ps1.success:
                            warnings.append(
                                "El script de mantenimiento no completó correctamente."
                            )
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(f"Error inesperado: {exc}")
            try:
                self.after(0, self._dashboard_express_done, total_bytes, warnings)
            except TclError:
                pass

        threading.Thread(target=_worker, name="express-maintenance", daemon=True).start()

    def _dashboard_express_done(self, total_bytes: float, warnings: list[str]) -> None:
        """Stop the express indicators and show the result banner."""
        audit("express_maintenance", "terminado")
        self._end_operation([self._dash_express_button], self._dash_express_progress)
        self._dash_express_status.configure(text="Mantenimiento Express disponible.")
        freed_text = self._format_size(total_bytes)
        if warnings:
            self._show_dashboard_banner(
                f"Mantenimiento Express finalizado - {freed_text} liberados "
                "(con advertencias)",
                _COLOR_WARN,
            )
        else:
            self._show_dashboard_banner(
                f"Mantenimiento Express completado - {freed_text} liberados",
                _COLOR_OK,
            )

    def _show_dashboard_banner(self, text: str, color: str) -> None:
        """Display the colored result banner on the dashboard."""
        self._dash_banner.configure(text=text, fg_color=color, text_color="#0d0d0d")
        self._dash_banner.grid(row=9, column=0, sticky="ew", padx=16, pady=(0, 6))

    def _hide_dashboard_banner(self) -> None:
        """Hide the result banner (reset before the next express run)."""
        self._dash_banner.grid_remove()

    def _dashboard_show_qr(self) -> None:
        """Open a child Toplevel showing the two contact QR codes.

        qrcode/Pillow are optional: when they are missing the action degrades
        to an error log line instead of crashing.
        """
        if qrcode is None or Image is None:
            audit("qr", "Mostrar QR de contacto")
            self._append_log(
                self._dash_log,
                "No se pudo generar el QR: faltan las librerías qrcode o Pillow.",
                error=True,
            )
            return
        if self._qr_window is not None and self._qr_window.winfo_exists():
            self._qr_window.lift()
            self._qr_window.focus_force()
            return

        window = ctk.CTkToplevel(self)
        window.title("QR de Contacto")
        qr_size = 180 if self.winfo_screenwidth() >= 1280 else 160
        window.geometry("480x360")
        window.resizable(False, False)
        window.transient(self)
        window.after(50, window.lift)

        data_pairs: tuple[tuple[str, str], ...] = (
            ("WhatsApp", "https://wa.me/541131797343"),
            ("Sitio Web", "https://dantech-landing.vercel.app"),
        )

        container = ctk.CTkFrame(window, fg_color="transparent")
        container.grid(row=0, column=0, padx=16, pady=(20, 8))
        container.grid_columnconfigure(0, weight=1)
        container.grid_columnconfigure(1, weight=1)

        for column, (label_text, data) in enumerate(data_pairs):
            cell = ctk.CTkFrame(container, fg_color="transparent")
            cell.grid(row=0, column=column, padx=16)
            try:
                image = qrcode.make(data)
            except Exception as exc:
                audit("qr", f"Fallo al generar QR {label_text}: {exc}")
                self._append_log(
                    self._dash_log,
                    f"No se pudo generar el QR de {label_text}: {exc}",
                    error=True,
                )
                return
            photo = ctk.CTkImage(
                light_image=image, dark_image=image, size=(qr_size, qr_size)
            )
            self._qr_images.append(photo)
            image_label = ctk.CTkLabel(cell, image=photo, text="")
            image_label.grid(row=0, column=0, sticky="ew")
            caption = ctk.CTkLabel(cell, text=label_text, font=("Segoe UI", 13, "bold"))
            caption.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        close_button = ctk.CTkButton(window, text="Cerrar", command=window.destroy)
        close_button.grid(row=1, column=0, pady=(4, 16))

        self._qr_window = window
        audit("qr", "Mostrar QR de contacto")

    def _start_disk_health_fetch(self) -> None:
        """Fetch SMART health and C: usage once, asynchronously."""
        analyzer = self._disk_analyzer

        def _complete(disks: list[DiskHealth]) -> None:
            try:
                usage = analyzer.get_disk_usage()
            except Exception:
                usage = None
            try:
                self.after(0, self._dashboard_disk_done, disks, usage)
            except TclError:
                pass

        def _fail(error: Any) -> None:
            try:
                self.after(0, self._dashboard_disk_error, error)
            except TclError:
                pass

        try:
            analyzer.get_smart_health_async(_complete, _fail)
        except Exception as exc:
            try:
                self.after(0, self._dashboard_disk_error, exc)
            except TclError:
                pass

    def _dashboard_disk_done(
        self, disks: list[DiskHealth], usage: Optional[DiskUsage]
    ) -> None:
        """Render the first disk health plus the C: free space."""
        disk_text = "Sin datos (requiere permisos de administrador)"
        color = _COLOR_WARN
        if disks and disks[0].health_status != "Unknown":
            first = disks[0]
            health = first.health_status
            if health == "Unhealthy":
                color = _COLOR_BAD
            elif health == "Healthy":
                color = _COLOR_OK
            temperature = (
                f", {first.temperature_c:.0f} °C"
                if first.temperature_c is not None
                else ""
            )
            disk_text = (
                f"{first.friendly_name} ({first.media_type}) - {health}{temperature}"
            )
        if usage is not None and usage.total_gb > 0:
            disk_text += f" | C: libre {usage.free_gb:.1f} GB de {usage.total_gb:.1f} GB"
        self._dash_disk_label.configure(text=disk_text, text_color=color)

    def _dashboard_disk_error(self, error: Any) -> None:
        """Show a warning when the disk query fails entirely."""
        self._dash_disk_label.configure(
            text="Sin datos (requiere permisos de administrador)",
            text_color=_COLOR_WARN,
        )
        self._append_log(self._dash_log, f"Consulta de salud del disco: {error}", error=True)

    def _top_memory_processes(self, limit: int = 5) -> list[tuple[str, float]]:
        """Return the top-N processes by RSS as (name, RSS in MB)."""
        if psutil is None:
            return []
        procs: list[tuple[float, str]] = []
        for proc in psutil.process_iter(["name", "memory_info"]):
            try:
                rss = int(getattr(proc.info.get("memory_info"), "rss", 0) or 0)
                name = str(proc.info.get("name") or "?")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if rss > 0:
                procs.append((float(rss), name))
        procs.sort(key=lambda item: item[0], reverse=True)
        return [(name, rss / _MB) for rss, name in procs[:limit]]

    def _start_telemetry(self) -> None:
        """Start the daemon thread that samples CPU/RAM every two seconds."""
        if psutil is None:
            return
        self._telemetry_active = True
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_loop, name="telemetry", daemon=True
        )
        self._telemetry_thread.start()

    def _telemetry_loop(self) -> None:
        """Sample system telemetry and schedule UI updates on the main thread."""
        while self._telemetry_active:
            try:
                cpu = float(psutil.cpu_percent(interval=None) or 0.0)
                memory = psutil.virtual_memory()
                ram_pct = float(memory.percent or 0.0)
                available_gb = memory.available / _GB
                total_gb = memory.total / _GB
                top_procs = self._top_memory_processes(5)
            except Exception:
                self._telemetry_active = False
                return
            try:
                self.after(
                    0,
                    self._update_telemetry,
                    cpu,
                    ram_pct,
                    available_gb,
                    total_gb,
                    top_procs,
                )
            except TclError:
                self._telemetry_active = False
                return
            time.sleep(2.0)

    def _update_telemetry(
        self,
        cpu_pct: float,
        ram_pct: float,
        available_gb: float,
        total_gb: float,
        top_procs: list[tuple[str, float]],
    ) -> None:
        """Refresh the dashboard widgets with the latest telemetry sample."""
        try:
            self._dash_cpu_label.configure(text=f"CPU: {cpu_pct:.0f}%")
            self._dash_cpu_bar.set(max(0.0, min(cpu_pct / 100.0, 1.0)))
            self._dash_ram_label.configure(text=f"RAM: {ram_pct:.0f}%")
            self._dash_ram_bar.set(max(0.0, min(ram_pct / 100.0, 1.0)))
            self._dash_ram_info.configure(
                text=f"Memoria disponible: {available_gb:.1f} GB de {total_gb:.1f} GB"
            )
            self._dash_procs_box.configure(state="normal")
            self._dash_procs_box.delete("1.0", "end")
            for name, rss_mb in top_procs:
                self._dash_procs_box.insert(
                    "end", f"{name:<42}{rss_mb:>10.1f} MB\n"
                )
            self._dash_procs_box.configure(state="disabled")
        except TclError:
            pass

    # ---------------------------------------------------------------- memoria

    def _build_memory(self) -> ctk.CTkFrame:
        """Build the memory/cleanup view: configurable SystemCleaner plus RAM."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        title = self._section_title(frame, "Optimización de memoria")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Analiza y limpia archivos temporales/cachés de forma selectiva "
            "y libera memoria RAM sin cerrar procesos.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        main = ctk.CTkFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 4))
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        config = ctk.CTkFrame(main)
        config.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        config.grid_columnconfigure(0, weight=1)

        config_title = ctk.CTkLabel(
            config, text="Configuración y Filtros", font=("Segoe UI", 14, "bold")
        )
        config_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        self._memory_flags: dict[str, ctk.CTkCheckBox] = {}
        for index, (category, label) in enumerate(
            _CLEANER_CATEGORY_LABELS.items(), start=1
        ):
            checkbox = ctk.CTkCheckBox(config, text=label)
            checkbox.grid(row=index, column=0, sticky="w", padx=12, pady=3)
            self._memory_flags[category] = checkbox
        self._memory_flags["temp_user"].select()
        self._memory_flags["temp_system"].select()

        exclusions_label = ctk.CTkLabel(
            config, text="Exclusiones (una ruta por línea):", font=("Segoe UI", 12)
        )
        exclusions_label.grid(row=8, column=0, sticky="w", padx=12, pady=(8, 2))

        self._memory_exclusions = ctk.CTkTextbox(config, height=70)
        self._memory_exclusions.grid(row=9, column=0, sticky="ew", padx=12, pady=(0, 8))

        reset_memory_btn = ctk.CTkButton(
            config,
            text="Restablecer Configuración por Defecto",
            command=self._memory_reset_config,
        )
        reset_memory_btn.grid(row=10, column=0, sticky="ew", padx=12, pady=(4, 12))

        actions = ctk.CTkFrame(main)
        actions.grid(row=0, column=1, sticky="nsew")
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_rowconfigure(3, weight=1)

        actions_title = ctk.CTkLabel(
            actions, text="Acciones y Consola", font=("Segoe UI", 14, "bold")
        )
        actions_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        buttons_row = ctk.CTkFrame(actions, fg_color="transparent")
        buttons_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        buttons_row.grid_columnconfigure(3, weight=1)

        analyze_btn = ctk.CTkButton(
            buttons_row, text="Analizar", command=self._memory_analyze
        )
        analyze_btn.grid(row=0, column=0, padx=(0, 8))

        apply_btn = ctk.CTkButton(
            buttons_row,
            text="Aplicar Cambios",
            fg_color=_NAV_ACTIVE_FG,
            hover_color="#155a8a",
            command=self._memory_apply,
        )
        apply_btn.grid(row=0, column=1, padx=8)

        ram_btn = ctk.CTkButton(
            buttons_row, text="Optimizar todo", command=self._memory_optimize_all
        )
        ram_btn.grid(row=0, column=2, padx=8)

        self._memory_buttons = [analyze_btn, apply_btn, ram_btn]

        self._memory_progress = ctk.CTkProgressBar(actions, mode="indeterminate")
        self._memory_progress.set(0)
        self._memory_progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 4))

        self._memory_log = ctk.CTkTextbox(
            actions, wrap="word", state="disabled", height=200
        )
        self._memory_log.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self._memory_log.tag_config("info", foreground=_LOG_COLOR_INFO)
        self._memory_log.tag_config("error", foreground=_LOG_COLOR_ERROR)

        return frame

    def _memory_build_config(self) -> CleanConfig:
        """Assemble a CleanConfig from the current filter widgets."""
        return CleanConfig(
            temp_user=self._memory_flags["temp_user"].get() == 1,
            temp_system=self._memory_flags["temp_system"].get() == 1,
            prefetch=self._memory_flags["prefetch"].get() == 1,
            software_distribution=self._memory_flags["software_distribution"].get() == 1,
            recycle_bin=self._memory_flags["recycle_bin"].get() == 1,
            browser_cache_chrome=self._memory_flags["browser_cache_chrome"].get() == 1,
            browser_cache_edge=self._memory_flags["browser_cache_edge"].get() == 1,
            whitelist_paths=tuple(
                line.strip()
                for line in self._memory_exclusions.get("1.0", "end").splitlines()
                if line.strip()
            ),
        )

    def _memory_reset_config(self) -> None:
        """Restore the default cleanup filters and clear the exclusions."""
        for category in _CLEANER_CATEGORY_LABELS:
            if category in ("temp_user", "temp_system"):
                self._memory_flags[category].select()
            else:
                self._memory_flags[category].deselect()
        self._memory_exclusions.delete("1.0", "end")
        self._append_log(self._memory_log, "Configuración de limpieza restablecida.")

    def _memory_analyze(self) -> None:
        """Run a read-only SystemCleaner analysis in the background."""
        config = self._memory_build_config()
        self._launch(
            "Analizar espacio recuperable",
            self._memory_buttons,
            self._memory_progress,
            self._memory_log,
            lambda complete, fail: self._system_cleaner.analyze_only_async(
                config, on_complete=complete, on_error=fail
            ),
            self._memory_analyze_done,
            self._memory_error,
        )

    def _memory_analyze_done(self, report: CleanAnalysisReport) -> None:
        """Log the analysis totals and the per-category breakdown."""
        self._end_operation(self._memory_buttons, self._memory_progress)
        audit("clean_analyze", f"total={report.total_bytes} files={report.file_count}")
        self._append_log(
            self._memory_log,
            f"Análisis: {self._format_size(report.total_bytes)} a liberar "
            f"en {report.file_count:,} archivos.",
        )
        for category, total in report.category_totals.items():
            label = _CLEANER_CATEGORY_LABELS.get(category, category)
            self._append_log(self._memory_log, f"  {label}: {self._format_size(total)}")
        for error in report.errors:
            hint = self._admin_hint(error)
            self._append_log(self._memory_log, hint or error, error=True)
        self._append_log(self._memory_log, f"Tiempo: {report.elapsed:.2f} s")

    def _memory_apply(self) -> None:
        """Run a real SystemCleaner pass with the current filters."""
        config = self._memory_build_config()
        self._launch(
            "Aplicar cambios de limpieza",
            self._memory_buttons,
            self._memory_progress,
            self._memory_log,
            lambda complete, fail: self._system_cleaner.clean_now_async(
                config, on_complete=complete, on_error=fail
            ),
            self._memory_apply_done,
            self._memory_error,
        )

    def _memory_apply_done(self, result: CleanResult) -> None:
        """Log the cleanup outcome and the per-category freed space."""
        self._end_operation(self._memory_buttons, self._memory_progress)
        audit("clean_now", f"freed={result.total_freed} files={result.files_deleted}")
        self._append_log(
            self._memory_log,
            f"Limpieza: {self._format_size(result.total_freed)} liberados "
            f"({result.files_deleted:,} archivos).",
        )
        for category, freed in result.category_freed.items():
            label = _CLEANER_CATEGORY_LABELS.get(category, category)
            self._append_log(self._memory_log, f"  {label}: {self._format_size(freed)}")
        for error in result.errors:
            hint = self._admin_hint(error)
            self._append_log(self._memory_log, hint or error, error=True)
        self._append_log(self._memory_log, f"Tiempo: {result.elapsed:.2f} s")

    def _memory_optimize_all(self) -> None:
        """Run temp cleanup plus RAM trim in one background pass."""
        self._launch(
            "Optimización completa",
            self._memory_buttons,
            self._memory_progress,
            self._memory_log,
            lambda complete, fail: self._memory_optimizer.optimize_all_async(
                on_complete=complete, on_error=fail
            ),
            self._memory_all_done,
            self._memory_error,
        )

    def _memory_all_done(self, report: CombinedReport) -> None:
        """Log the outcome of the combined optimization pass."""
        audit("memory_optimize", "terminado")
        self._end_operation(self._memory_buttons, self._memory_progress)
        self._append_log(self._memory_log, "Optimización completa finalizada.")
        for line in self._format_combined_report(report):
            self._append_log(self._memory_log, line)

    def _memory_error(self, error: Any) -> None:
        """Log a memory-operation error."""
        self._end_operation(self._memory_buttons, self._memory_progress)
        self._log_error(self._memory_log, error)

    # -------------------------------------------------------------- reparación

    def _build_repair(self) -> ctk.CTkFrame:
        """Build the system repair view: SFC/DISM/network plus a drivers section."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        title = self._section_title(frame, "Reparación del sistema")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="SFC y DISM reparan la integridad de Windows; el restablecimiento "
            "de red corrige la pila TCP/IP. Requieren administrador.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        main = ctk.CTkFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        repair_col = ctk.CTkFrame(main)
        repair_col.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        repair_col.grid_columnconfigure(0, weight=1)

        repair_header = ctk.CTkLabel(
            repair_col, text="Reparación", font=("Segoe UI", 14, "bold")
        )
        repair_header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        self._repair_progress = ctk.CTkProgressBar(repair_col, mode="indeterminate")
        self._repair_progress.set(0)
        self._repair_progress.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        sfc_btn = ctk.CTkButton(repair_col, text="SFC /scannow", command=self._repair_sfc)
        sfc_btn.grid(row=2, column=0, sticky="ew", padx=12, pady=3)

        dism_btn = ctk.CTkButton(
            repair_col, text="DISM RestoreHealth", command=self._repair_dism
        )
        dism_btn.grid(row=3, column=0, sticky="ew", padx=12, pady=3)

        network_btn = ctk.CTkButton(
            repair_col, text="Restablecer red", command=self._repair_network
        )
        network_btn.grid(row=4, column=0, sticky="ew", padx=12, pady=3)

        restore_btn = ctk.CTkButton(
            repair_col,
            text="Crear punto de restauración",
            command=self._repair_restore_point,
        )
        restore_btn.grid(row=5, column=0, sticky="ew", padx=12, pady=(3, 12))

        self._repair_buttons = [sfc_btn, dism_btn, network_btn, restore_btn]

        drivers_col = ctk.CTkFrame(main)
        drivers_col.grid(row=0, column=1, sticky="nsew")
        drivers_col.grid_columnconfigure(0, weight=1)

        drivers_header = ctk.CTkLabel(
            drivers_col, text="Drivers", font=("Segoe UI", 14, "bold")
        )
        drivers_header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        drivers_info = ctk.CTkLabel(
            drivers_col,
            text="Lista los drivers firmados y fuerza un rescan de hardware. "
            "La consulta puede tardar 15-30 segundos.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
            justify="left",
            wraplength=380,
        )
        drivers_info.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))

        list_btn = ctk.CTkButton(drivers_col, text="Listar drivers", command=self._drivers_list)
        list_btn.grid(row=2, column=0, sticky="ew", padx=12, pady=3)

        self._drivers_restore_switch = ctk.CTkSwitch(
            drivers_col, text="Crear punto de restauración antes"
        )
        self._drivers_restore_switch.select()
        self._drivers_restore_switch.grid(row=3, column=0, sticky="w", padx=12, pady=3)

        update_btn = ctk.CTkButton(
            drivers_col,
            text="Actualizar drivers (best effort)",
            fg_color=_NAV_ACTIVE_FG,
            hover_color="#155a8a",
            command=self._drivers_update,
        )
        update_btn.grid(row=4, column=0, sticky="ew", padx=12, pady=3)

        reset_drivers_btn = ctk.CTkButton(
            drivers_col,
            text="Restablecer Configuración por Defecto",
            command=self._drivers_reset,
        )
        reset_drivers_btn.grid(row=5, column=0, sticky="ew", padx=12, pady=(3, 8))

        self._drivers_buttons = [list_btn, update_btn]
        self._drivers_progress = ctk.CTkProgressBar(drivers_col, mode="indeterminate")
        self._drivers_progress.set(0)
        self._drivers_progress.grid(row=6, column=0, sticky="ew", padx=12, pady=(0, 12))

        self._repair_log = self._section_log(frame, row=3)
        return frame

    def _drivers_list(self) -> None:
        """List signed drivers in the background."""
        self._launch(
            "Listar drivers",
            self._drivers_buttons,
            self._drivers_progress,
            self._repair_log,
            lambda complete, fail: self._driver_manager.list_drivers_async(
                on_complete=complete, on_error=fail
            ),
            self._drivers_list_done,
            self._repair_error,
        )

    def _drivers_list_done(self, payload: tuple[list[DriverInfo], list[str]]) -> None:
        """Log the driver count and the first five rows."""
        self._end_operation(self._drivers_buttons, self._drivers_progress)
        infos, errors = payload
        self._append_log(self._repair_log, f"Drivers encontrados: {len(infos)}.")
        for error in errors:
            self._append_log(self._repair_log, error, error=True)
        for info in infos[:5]:
            self._append_log(
                self._repair_log,
                f"  {info.device_name} | {info.driver_version} | {info.manufacturer}",
            )
        if len(infos) > 5:
            self._append_log(self._repair_log, f"  ... y {len(infos) - 5} más.")

    def _drivers_update(self) -> None:
        """Run the best-effort driver update sequence in the background."""
        create_restore = self._drivers_restore_switch.get() == 1
        self._launch(
            "Actualizar drivers (best effort)",
            self._drivers_buttons,
            self._drivers_progress,
            self._repair_log,
            lambda complete, fail: self._driver_manager.update_drivers_async(
                create_restore, on_complete=complete, on_error=fail
            ),
            self._drivers_update_done,
            self._repair_error,
        )

    def _drivers_update_done(self, result: DriverUpdateResult) -> None:
        """Log the restore-point outcome and each update step."""
        self._end_operation(self._drivers_buttons, self._drivers_progress)
        audit(
            "drivers_update",
            f"restore={result.restore_point_created} success={result.success}",
        )
        if result.restore_point_created:
            self._append_log(
                self._repair_log, "Punto de restauración creado correctamente."
            )
        elif result.restore_point_error:
            self._append_log(self._repair_log, result.restore_point_error, error=True)
        for step in result.steps:
            self._append_log(
                self._repair_log,
                f"{'OK' if step.result.success else 'FAIL'}: {step.label}",
            )
        self._append_log(
            self._repair_log,
            "Actualización de drivers: "
            + ("completada." if result.success else "finalizada con errores."),
        )

    def _drivers_reset(self) -> None:
        """Restore the drivers section defaults (restore-point switch ON)."""
        self._drivers_restore_switch.select()
        self._append_log(self._repair_log, "Configuración de drivers restablecida.")

    def _repair_sfc(self) -> None:
        """Run ``sfc /scannow`` in the background."""
        self._launch(
            "SFC /scannow",
            self._repair_buttons,
            self._repair_progress,
            self._repair_log,
            lambda complete, fail: self._system_repair.sfc_scan_async(
                on_complete=complete, on_error=fail
            ),
            lambda result: self._repair_command_done("SFC /scannow", result),
            self._repair_error,
        )

    def _repair_dism(self) -> None:
        """Run ``DISM /Online /Cleanup-Image /RestoreHealth`` in the background."""
        self._launch(
            "DISM RestoreHealth",
            self._repair_buttons,
            self._repair_progress,
            self._repair_log,
            lambda complete, fail: self._system_repair.dism_restore_health_async(
                on_complete=complete, on_error=fail
            ),
            lambda result: self._repair_command_done("DISM RestoreHealth", result),
            self._repair_error,
        )

    def _repair_network(self) -> None:
        """Run the network-stack reset in the background."""
        self._launch(
            "Restablecer red",
            self._repair_buttons,
            self._repair_progress,
            self._repair_log,
            lambda complete, fail: self._system_repair.reset_network_stack_async(
                on_complete=complete, on_error=fail
            ),
            self._repair_network_done,
            self._repair_error,
        )

    def _repair_restore_point(self) -> None:
        """Create a System Restore checkpoint in the background."""
        self._launch(
            "Crear punto de restauración",
            self._repair_buttons,
            self._repair_progress,
            self._repair_log,
            lambda complete, fail: self._system_repair.create_restore_point_async(
                on_complete=complete, on_error=fail
            ),
            self._repair_restore_done,
            self._repair_error,
        )

    def _repair_restore_done(self, result: CommandResult) -> None:
        """Log the restore-point outcome (System Restore can be disabled)."""
        audit("restore_point", "resultado")
        self._end_operation(self._repair_buttons, self._repair_progress)
        if result.success:
            self._append_log(self._repair_log, "Punto de restauración creado correctamente.")
            return
        detail = (result.stderr or "").strip()
        if not detail:
            detail = (
                "No se pudo crear (System Restore deshabilitado o "
                "requiere administrador)."
            )
        self._append_log(self._repair_log, detail, error=True)

    def _repair_command_done(self, operation: str, result: CommandResult) -> None:
        """Log the outcome of a single-command repair (SFC/DISM)."""
        self._end_operation(self._repair_buttons, self._repair_progress)
        self._append_log(self._repair_log, f"{operation} finalizado.")
        for line in self._format_command_result(result):
            self._append_log(self._repair_log, line)

    def _repair_network_done(self, report: RepairReport) -> None:
        """Log the outcome of the network-stack reset."""
        self._end_operation(self._repair_buttons, self._repair_progress)
        self._append_log(self._repair_log, "Restablecimiento de red finalizado.")
        for line in self._format_repair_report(report):
            self._append_log(self._repair_log, line)

    def _repair_error(self, error: Any) -> None:
        """Log a repair-operation error (also handles the admin-required case)."""
        self._end_operation(self._active_buttons, self._active_progress)
        self._log_error(self._repair_log, error)

    # --------------------------------------------------------------- seguridad

    def _build_security(self) -> ctk.CTkFrame:
        """Build the security view: Defender scans plus hosts management."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        title = self._section_title(frame, "Seguridad")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Escaneos con Windows Defender y administración del archivo hosts. "
            "Requieren administrador.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        main = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 4))
        main.grid_columnconfigure(1, weight=1)

        config = ctk.CTkFrame(main)
        config.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        config.grid_columnconfigure(0, weight=1)

        config_title = ctk.CTkLabel(
            config, text="Configuración", font=("Segoe UI", 14, "bold")
        )
        config_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        scan_label = ctk.CTkLabel(config, text="Tipo de escaneo:", font=("Segoe UI", 12))
        scan_label.grid(row=1, column=0, sticky="w", padx=12, pady=(4, 2))

        self._security_scan_type = ctk.CTkComboBox(
            config, values=["Rápido", "Completo", "Custom"], width=200
        )
        self._security_scan_type.set("Rápido")
        self._security_scan_type.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 6))

        folder_label = ctk.CTkLabel(
            config, text="Ruta (solo Custom):", font=("Segoe UI", 12)
        )
        folder_label.grid(row=3, column=0, sticky="w", padx=12, pady=(4, 2))

        self._security_folder_entry = ctk.CTkEntry(
            config, placeholder_text="C:\\Users\\Usuario\\Carpeta"
        )
        self._security_folder_entry.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._security_update_signatures_switch = ctk.CTkSwitch(
            config, text="Actualizar firmas antes de escanear"
        )
        self._security_update_signatures_switch.select()
        self._security_update_signatures_switch.grid(
            row=5, column=0, sticky="w", padx=12, pady=4
        )

        reset_security_btn = ctk.CTkButton(
            config,
            text="Restablecer Configuración por Defecto",
            command=self._security_reset_config,
        )
        reset_security_btn.grid(row=6, column=0, sticky="ew", padx=12, pady=(8, 12))

        actions = ctk.CTkFrame(main)
        actions.grid(row=0, column=1, sticky="nsew")
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_rowconfigure(3, weight=1)

        actions_title = ctk.CTkLabel(
            actions, text="Acciones y Consola", font=("Segoe UI", 14, "bold")
        )
        actions_title.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        scan_btn = ctk.CTkButton(
            actions,
            text="Escanear ahora",
            fg_color=_NAV_ACTIVE_FG,
            hover_color="#155a8a",
            command=self._security_scan_now,
        )
        scan_btn.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 4))

        self._security_buttons = [scan_btn]

        self._security_progress = ctk.CTkProgressBar(actions, mode="indeterminate")
        self._security_progress.set(0)
        self._security_progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 4))

        self._security_log = ctk.CTkTextbox(
            actions, wrap="word", state="disabled", height=190
        )
        self._security_log.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self._security_log.tag_config("info", foreground=_LOG_COLOR_INFO)
        self._security_log.tag_config("error", foreground=_LOG_COLOR_ERROR)

        hosts = ctk.CTkFrame(frame)
        hosts.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 12))
        hosts.grid_columnconfigure(2, weight=1)

        hosts_header = ctk.CTkLabel(
            hosts, text="Archivo hosts", font=("Segoe UI", 14, "bold")
        )
        hosts_header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        view_hosts_btn = ctk.CTkButton(
            hosts, text="Ver hosts", command=self._security_view_hosts
        )
        view_hosts_btn.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 12))

        restore_hosts_btn = ctk.CTkButton(
            hosts,
            text="Restaurar hosts por defecto",
            fg_color=_COLOR_WARN,
            hover_color="#d68910",
            command=self._security_restore_hosts,
        )
        restore_hosts_btn.grid(row=1, column=1, sticky="w", padx=12, pady=(0, 12))

        self._hosts_buttons = [view_hosts_btn, restore_hosts_btn]
        self._hosts_progress = ctk.CTkProgressBar(hosts, mode="indeterminate")
        self._hosts_progress.set(0)
        self._hosts_progress.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(0, 12))

        return frame

    def _security_reset_config(self) -> None:
        """Restore the security defaults: Rápido scan and signatures ON."""
        self._security_scan_type.set("Rápido")
        self._security_folder_entry.delete(0, "end")
        self._security_update_signatures_switch.select()
        self._append_log(self._security_log, "Configuración de seguridad restablecida.")

    def _security_scan_now(self) -> None:
        """Run the configured Defender scan, updating signatures first if set."""
        scan_kind = self._security_scan_type.get().strip()
        folder = self._security_folder_entry.get().strip()
        if scan_kind == "Custom" and not folder:
            self._append_log(
                self._security_log, "Ingresa una ruta para el escaneo Custom.", error=True
            )
            return
        if self._security_update_signatures_switch.get() == 1:
            self._begin_operation(self._security_buttons, self._security_progress)
            self._append_log(self._security_log, "Actualizando firmas de Defender...")

            def _after(report: Any) -> None:
                try:
                    self.after(0, self._security_signatures_done, report, scan_kind, folder)
                except TclError:
                    pass

            try:
                self._malware_cleaner.update_signatures_async(
                    on_complete=_after, on_error=_after
                )
            except Exception as exc:
                self._end_operation(self._security_buttons, self._security_progress)
                self._append_log(
                    self._security_log,
                    f"No se pudo iniciar la actualización: {exc}",
                    error=True,
                )
            return
        self._security_run_scan(scan_kind, folder)

    def _security_signatures_done(
        self, report: ScanReport, scan_kind: str, folder: str
    ) -> None:
        """Log the signature update result and continue with the scan."""
        for line in self._format_scan_report(report):
            self._append_log(self._security_log, line)
        self._security_run_scan(scan_kind, folder)

    def _security_run_scan(self, scan_kind: str, folder: str) -> None:
        """Launch the actual Defender scan for the selected kind."""
        if scan_kind == "Completo":
            label = "Análisis completo"
            starter: Callable[[Callable[[Any], None], Callable[[Any], None]], threading.Thread] = (
                lambda complete, fail: self._malware_cleaner.full_scan_async(
                    on_complete=complete, on_error=fail
                )
            )
        elif scan_kind == "Custom":
            label = f"Análisis personalizado: {folder}"
            starter = (
                lambda complete, fail: self._malware_cleaner.custom_scan_async(
                    folder, on_complete=complete, on_error=fail
                )
            )
        else:
            label = "Análisis rápido"
            starter = (
                lambda complete, fail: self._malware_cleaner.quick_scan_async(
                    on_complete=complete, on_error=fail
                )
            )
        self._launch(
            label,
            self._security_buttons,
            self._security_progress,
            self._security_log,
            starter,
            lambda report: self._security_scan_done(report, scan_kind),
            self._security_error,
        )

    def _security_scan_done(self, report: ScanReport, scan_kind: str) -> None:
        """Log a Defender scan outcome."""
        self._end_operation(self._security_buttons, self._security_progress)
        audit("defender_scan", f"type={scan_kind}")
        self._append_log(
            self._security_log, f"Operación '{report.scan_type}' finalizada."
        )
        for line in self._format_scan_report(report):
            self._append_log(self._security_log, line)

    def _security_view_hosts(self) -> None:
        """Read and log the first lines of the hosts file."""
        self._launch(
            "Ver archivo hosts",
            self._hosts_buttons,
            self._hosts_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.read_hosts_async(
                on_complete=complete, on_error=fail
            ),
            self._security_hosts_read_done,
            self._security_error,
        )

    def _security_hosts_read_done(self, payload: tuple[str, list[str]]) -> None:
        """Log the hosts content (first 30 lines) and any read errors."""
        self._end_operation(self._hosts_buttons, self._hosts_progress)
        content, errors = payload
        for error in errors:
            self._append_log(self._security_log, error, error=True)
        lines = content.splitlines()
        for line in lines[:30]:
            self._append_log(self._security_log, line)
        if len(lines) > 30:
            self._append_log(self._security_log, f"... y {len(lines) - 30} líneas más.")

    def _security_restore_hosts(self) -> None:
        """Restore the default hosts file after confirmation (backup first)."""
        if not messagebox.askyesno(
            "Restaurar hosts",
            "Se reemplazará el archivo hosts por el predeterminado. Se hace un "
            "respaldo primero. ¿Continuar?",
        ):
            return
        self._launch(
            "Restaurar hosts por defecto",
            self._hosts_buttons,
            self._hosts_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.restore_hosts_default_async(
                on_complete=complete, on_error=fail
            ),
            self._security_hosts_restore_done,
            self._security_error,
        )

    def _security_hosts_restore_done(self, result: CommandResult) -> None:
        """Log the hosts restore outcome."""
        self._end_operation(self._hosts_buttons, self._hosts_progress)
        audit("hosts_restore", f"success={result.success}")
        for line in self._format_command_result(result):
            self._append_log(self._security_log, line)

    def _security_error(self, error: Any) -> None:
        """Log a security-operation error."""
        self._end_operation(self._active_buttons, self._active_progress)
        self._log_error(self._security_log, error)

    # ------------------------------------------------------------------- apps

    def _build_apps(self) -> ctk.CTkFrame:
        """Build the apps view: winget installer plus an uninstaller section."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        title = self._section_title(frame, "Aplicaciones")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Busca, instala y actualiza programas con winget (Windows Package "
            "Manager) y desinstala aplicaciones Win32 o UWP.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        main = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 4))
        main.grid_columnconfigure(1, weight=1)

        install_col = ctk.CTkFrame(main)
        install_col.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        install_col.grid_columnconfigure(0, weight=1)

        install_header = ctk.CTkLabel(
            install_col, text="Instalador (winget)", font=("Segoe UI", 14, "bold")
        )
        install_header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        self._apps_search_entry = ctk.CTkEntry(
            install_col, placeholder_text="Buscar paquete (ej: Firefox)"
        )
        self._apps_search_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=3)

        search_btn = ctk.CTkButton(install_col, text="Buscar", command=self._apps_search)
        search_btn.grid(row=2, column=0, sticky="ew", padx=12, pady=3)

        self._apps_install_entry = ctk.CTkEntry(
            install_col, placeholder_text="ID del paquete (ej: Microsoft.PowerToys)"
        )
        self._apps_install_entry.grid(row=3, column=0, sticky="ew", padx=12, pady=3)

        install_btn = ctk.CTkButton(install_col, text="Instalar", command=self._apps_install)
        install_btn.grid(row=4, column=0, sticky="ew", padx=12, pady=3)

        upgrade_all_btn = ctk.CTkButton(
            install_col, text="Actualizar todo", command=self._apps_upgrade_all
        )
        upgrade_all_btn.grid(row=5, column=0, sticky="ew", padx=12, pady=(3, 12))

        self._apps_buttons = [search_btn, install_btn, upgrade_all_btn]

        uninstall_col = ctk.CTkFrame(main)
        uninstall_col.grid(row=0, column=1, sticky="nsew")
        uninstall_col.grid_columnconfigure(0, weight=1)

        uninstall_header = ctk.CTkLabel(
            uninstall_col, text="Desinstalador", font=("Segoe UI", 14, "bold")
        )
        uninstall_header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        kind_label = ctk.CTkLabel(uninstall_col, text="Tipo:", font=("Segoe UI", 12))
        kind_label.grid(row=1, column=0, sticky="w", padx=12, pady=(4, 2))

        self._apps_uninstall_kind = ctk.CTkComboBox(
            uninstall_col, values=["Win32", "UWP (AppX)"], width=180
        )
        self._apps_uninstall_kind.set("Win32")
        self._apps_uninstall_kind.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 6))

        load_btn = ctk.CTkButton(
            uninstall_col, text="Cargar programas", command=self._apps_uninstall_load
        )
        load_btn.grid(row=3, column=0, sticky="ew", padx=12, pady=3)

        self._apps_uninstall_combo = ctk.CTkComboBox(uninstall_col, values=[])
        self._apps_uninstall_combo.grid(row=4, column=0, sticky="ew", padx=12, pady=3)

        self._apps_uninstall_silent = ctk.CTkSwitch(uninstall_col, text="Modo silencioso")
        self._apps_uninstall_silent.select()
        self._apps_uninstall_silent.grid(row=5, column=0, sticky="w", padx=12, pady=3)

        self._apps_uninstall_deep = ctk.CTkSwitch(uninstall_col, text="Limpieza profunda")
        self._apps_uninstall_deep.grid(row=6, column=0, sticky="w", padx=12, pady=3)

        uninstall_btn = ctk.CTkButton(
            uninstall_col,
            text="Desinstalar seleccionado",
            fg_color=_NAV_ACTIVE_FG,
            hover_color="#155a8a",
            command=self._apps_uninstall_selected,
        )
        uninstall_btn.grid(row=7, column=0, sticky="ew", padx=12, pady=(6, 3))

        self._apps_uninstall_residuals_btn = ctk.CTkButton(
            uninstall_col,
            text="Eliminar residuales",
            command=self._apps_remove_residuals,
        )
        self._apps_uninstall_residuals_btn.grid(
            row=8, column=0, sticky="ew", padx=12, pady=3
        )
        self._apps_uninstall_residuals_btn.grid_remove()

        reset_apps_btn = ctk.CTkButton(
            uninstall_col,
            text="Restablecer Configuración por Defecto",
            command=self._apps_reset_config,
        )
        reset_apps_btn.grid(row=9, column=0, sticky="ew", padx=12, pady=(6, 12))

        self._apps_uninstall_buttons = [
            load_btn,
            uninstall_btn,
            self._apps_uninstall_residuals_btn,
        ]

        self._apps_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._apps_progress.set(0)
        self._apps_progress.grid(row=3, column=0, sticky="ew", padx=16, pady=4)

        self._apps_log = self._section_log(frame, row=4, height=200)
        return frame

    def _apps_reset_config(self) -> None:
        """Restore the uninstaller defaults: Win32, silent ON, deep OFF."""
        self._apps_uninstall_kind.set("Win32")
        self._apps_uninstall_combo.set("")
        self._apps_uninstall_silent.select()
        self._apps_uninstall_deep.deselect()
        self._uninstall_entries = []
        self._uninstall_residuals = []
        self._apps_uninstall_residuals_btn.grid_remove()
        self._append_log(self._apps_log, "Configuración de desinstalador restablecida.")

    def _apps_uninstall_load(self) -> None:
        """Load installed programs for the selected kind in the background."""
        kind = self._apps_uninstall_kind.get().strip()
        if kind.startswith("UWP"):
            title = "Cargar paquetes UWP (AppX)"
            starter: Callable[[Callable[[Any], None], Callable[[Any], None]], threading.Thread] = (
                lambda complete, fail: self._app_uninstaller.list_appx_async(
                    on_complete=complete, on_error=fail
                )
            )
        else:
            title = "Cargar programas Win32"
            starter = (
                lambda complete, fail: self._app_uninstaller.list_win32_async(
                    on_complete=complete, on_error=fail
                )
            )
        self._launch(
            title,
            self._apps_uninstall_buttons,
            self._apps_progress,
            self._apps_log,
            starter,
            self._apps_uninstall_list_done,
            self._apps_uninstall_error,
        )

    def _apps_uninstall_list_done(
        self, payload: tuple[list[UninstallEntry], list[str]]
    ) -> None:
        """Fill the program combo from the loaded entries."""
        self._end_operation(self._apps_uninstall_buttons, self._apps_progress)
        entries, errors = payload
        for error in errors:
            self._append_log(self._apps_log, error, error=True)
        self._uninstall_entries = entries
        names: list[str] = []
        for entry in entries:
            if entry.display_name not in names:
                names.append(entry.display_name)
        self._apps_uninstall_combo.configure(values=names)
        if names:
            self._apps_uninstall_combo.set(names[0])
            self._append_log(self._apps_log, f"Programas cargados: {len(names)}.")
        else:
            self._append_log(self._apps_log, "No se encontraron programas.")

    def _apps_find_entry(self, display_name: str) -> Optional[UninstallEntry]:
        """Return the last entry whose display name matches exactly."""
        found: Optional[UninstallEntry] = None
        for entry in self._uninstall_entries:
            if entry.display_name == display_name:
                found = entry
        return found

    def _apps_uninstall_selected(self) -> None:
        """Uninstall the program selected in the combo after confirmation."""
        name = self._apps_uninstall_combo.get().strip()
        if not name:
            self._append_log(
                self._apps_log, "Selecciona un programa para desinstalar.", error=True
            )
            return
        entry = self._apps_find_entry(name)
        if entry is None:
            self._append_log(
                self._apps_log, "El programa seleccionado no está cargado.", error=True
            )
            return
        if not messagebox.askyesno("Desinstalar", f"¿Desinstalar {name}?"):
            return
        force_silent = self._apps_uninstall_silent.get() == 1
        deep_clean = self._apps_uninstall_deep.get() == 1
        self._launch(
            f"Desinstalar {name}",
            self._apps_uninstall_buttons,
            self._apps_progress,
            self._apps_log,
            lambda complete, fail: self._app_uninstaller.uninstall_async(
                entry,
                force_silent=force_silent,
                deep_clean=deep_clean,
                on_complete=complete,
                on_error=fail,
            ),
            lambda result: self._apps_uninstall_done(result, deep_clean),
            self._apps_uninstall_error,
        )

    def _apps_uninstall_done(self, result: UninstallResult, deep_clean: bool) -> None:
        """Log the uninstall outcome and surface residuals when present."""
        self._end_operation(self._apps_uninstall_buttons, self._apps_progress)
        audit("uninstall", f"name={result.entry.display_name} deep={int(deep_clean)}")
        if result.success:
            self._append_log(
                self._apps_log,
                f"Desinstalación de {result.entry.display_name} finalizada.",
            )
            self._uninstall_residuals = list(result.residuals)
            if deep_clean and result.residuals:
                self._append_log(
                    self._apps_log, f"Residuales detectados: {len(result.residuals)}."
                )
                self._apps_uninstall_residuals_btn.configure(
                    text=f"Eliminar residuales ({len(result.residuals)})"
                )
                self._apps_uninstall_residuals_btn.grid()
            else:
                self._apps_uninstall_residuals_btn.configure(text="Eliminar residuales")
                self._apps_uninstall_residuals_btn.grid_remove()
            return
        self._append_log(
            self._apps_log,
            f"No se pudo desinstalar {result.entry.display_name}.",
            error=True,
        )
        for line in self._format_command_result(result.result):
            self._append_log(self._apps_log, line, error=True)
        for error in result.errors:
            self._append_log(self._apps_log, error, error=True)

    def _apps_uninstall_error(self, error: Any) -> None:
        """Log an uninstaller-operation error."""
        self._end_operation(self._apps_uninstall_buttons, self._apps_progress)
        self._log_error(self._apps_log, error)

    def _apps_remove_residuals(self) -> None:
        """Remove every pending residual sequentially in the background."""
        if not self._uninstall_residuals:
            self._append_log(self._apps_log, "No hay residuales pendientes.", error=True)
            return
        pending = list(self._uninstall_residuals)
        self._uninstall_residuals = []
        if not messagebox.askyesno(
            "Eliminar residuales",
            f"¿Eliminar {len(pending)} residuales de la aplicación?",
        ):
            self._uninstall_residuals = pending
            return
        self._begin_operation(self._apps_uninstall_buttons, self._apps_progress)
        self._append_log(self._apps_log, f"Eliminando {len(pending)} residuales...")
        self._apps_uninstall_residuals_btn.grid_remove()

        def _next(index: int) -> None:
            if index >= len(pending):
                try:
                    self.after(0, self._apps_residuals_done)
                except TclError:
                    pass
                return
            entry = pending[index]

            def _done(ok: bool) -> None:
                try:
                    self.after(
                        0,
                        self._append_log,
                        self._apps_log,
                        f"{'OK' if ok else 'FAIL'}: {entry.description}",
                        not ok,
                    )
                except TclError:
                    pass
                _next(index + 1)

            def _fail(error: Any) -> None:
                try:
                    self.after(
                        0,
                        self._append_log,
                        self._apps_log,
                        f"Error al eliminar residual: {error}",
                        True,
                    )
                except TclError:
                    pass
                _next(index + 1)

            try:
                self._app_uninstaller.remove_residual_async(
                    entry, on_complete=_done, on_error=_fail
                )
            except Exception as exc:  # pragma: no cover - defensive
                _fail(exc)

        _next(0)

    def _apps_residuals_done(self) -> None:
        """Re-enable the uninstaller controls after the residual pass."""
        self._end_operation(self._apps_uninstall_buttons, self._apps_progress)
        audit("uninstall_residuals", "terminado")
        self._append_log(self._apps_log, "Eliminación de residuales finalizada.")

    def _apps_search(self) -> None:
        """Search winget sources for a package (sync core, run in a thread)."""
        query = self._apps_search_entry.get().strip()
        if not query:
            self._append_log(self._apps_log, "Ingresa un término de búsqueda.", error=True)
            return
        self._launch(
            f"Buscar '{query}'",
            self._apps_buttons,
            self._apps_progress,
            self._apps_log,
            lambda complete, fail: self._run_sync_in_thread(
                self._installer.search, complete, fail, query
            ),
            self._apps_done,
            self._apps_error,
        )

    def _apps_install(self) -> None:
        """Install a package by its winget id in the background."""
        package = self._apps_install_entry.get().strip()
        if not package:
            self._append_log(self._apps_log, "Ingresa el ID del paquete a instalar.", error=True)
            return
        self._launch(
            f"Instalar {package}",
            self._apps_buttons,
            self._apps_progress,
            self._apps_log,
            lambda complete, fail: self._installer.install_async(
                package, on_complete=complete, on_error=fail
            ),
            self._apps_done,
            self._apps_error,
        )

    def _apps_upgrade_all(self) -> None:
        """Upgrade every pending winget package in the background."""
        self._launch(
            "Actualizar todos los paquetes",
            self._apps_buttons,
            self._apps_progress,
            self._apps_log,
            lambda complete, fail: self._installer.upgrade_all_async(
                on_complete=complete, on_error=fail
            ),
            self._apps_done,
            self._apps_error,
        )

    def _apps_done(self, report: WingetResult) -> None:
        """Log the outcome of a winget operation."""
        self._end_operation(self._apps_buttons, self._apps_progress)
        self._append_log(self._apps_log, f"Operación '{report.operation}' finalizada.")
        for line in self._format_winget_result(report):
            self._append_log(self._apps_log, line)

    def _apps_error(self, error: Any) -> None:
        """Log a winget-operation error."""
        self._end_operation(self._apps_buttons, self._apps_progress)
        self._log_error(self._apps_log, error)

    # ---------------------------------------------------------------- scripts

    def _build_scripts(self) -> ctk.CTkFrame:
        """Build the maintenance-script view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(4, weight=1)

        title = self._section_title(frame, "Mantenimiento completo")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Ejecuta el script optimizado de mantenimiento: limpieza de temporales, "
            "reparación SFC/DISM, restablecimiento de red y actualizaciones winget. "
            "El script se auto-eleva con UAC si es necesario.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
            wraplength=820,
            justify="left",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        self._scripts_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._scripts_progress.set(0)
        self._scripts_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=8)

        run_btn = ctk.CTkButton(
            frame,
            text="Ejecutar mantenimiento completo",
            command=self._run_maintenance_script,
        )
        run_btn.grid(row=3, column=0, sticky="w", padx=16, pady=(8, 4))

        self._scripts_buttons = [run_btn]

        self._scripts_log = self._section_log(frame, row=4)
        return frame

    def _run_maintenance_script(self) -> None:
        """Launch the PowerShell maintenance script through process_runner."""
        if not self._script_path.is_file():
            self._append_log(
                self._scripts_log,
                f"No se encontró el script de mantenimiento en {self._script_path}.",
                error=True,
            )
            return
        self._launch(
            "Mantenimiento completo de Windows",
            self._scripts_buttons,
            self._scripts_progress,
            self._scripts_log,
            lambda complete, fail: process_runner.run_powershell_async(
                str(self._script_path), on_complete=complete, on_error=fail
            ),
            self._scripts_done,
            self._scripts_error,
        )

    def _scripts_done(self, result: CommandResult) -> None:
        """Log the full output of the maintenance script."""
        self._end_operation(self._scripts_buttons, self._scripts_progress)
        self._append_log(self._scripts_log, "Mantenimiento finalizado.")
        for line in self._format_command_result(result):
            self._append_log(self._scripts_log, line)

    def _scripts_error(self, error: Any) -> None:
        """Log a maintenance-script error."""
        self._end_operation(self._scripts_buttons, self._scripts_progress)
        self._log_error(self._scripts_log, error)

    # ------------------------------------------------------------ recuperación

    def _build_recovery(self) -> ctk.CTkFrame:
        """Build the file-recovery view (light mode by extension)."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(8, weight=1)

        title = self._section_title(frame, "Recuperación de archivos")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Busca archivos por tipo en una unidad y los copia a la carpeta "
            "de destino manteniendo la estructura de carpetas.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        try:
            drives = self._recovery_manager.get_drives() or ["C:\\"]
        except Exception:
            drives = ["C:\\"]

        source_row = ctk.CTkFrame(frame, fg_color="transparent")
        source_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 4))
        source_row.grid_columnconfigure(1, weight=1)

        source_label = ctk.CTkLabel(source_row, text="Unidad de origen:")
        source_label.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._recovery_drive_combo = ctk.CTkComboBox(source_row, values=drives, width=140)
        self._recovery_drive_combo.set(drives[0] if drives else "C:\\")
        self._recovery_drive_combo.grid(row=0, column=1, sticky="w", padx=(0, 16))

        dest_button = ctk.CTkButton(
            source_row,
            text="Seleccionar destino...",
            command=self._recovery_select_destination,
        )
        dest_button.grid(row=0, column=2, sticky="e")

        self._recovery_dest_path: Optional[str] = None
        self._recovery_dest_label = ctk.CTkLabel(
            frame,
            text="Destino: no seleccionado",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        self._recovery_dest_label.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 4))

        filters_row = ctk.CTkFrame(frame, fg_color="transparent")
        filters_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 4))

        filters_label = ctk.CTkLabel(filters_row, text="Tipos de archivo:")
        filters_label.grid(row=0, column=0, sticky="w", padx=(0, 12))

        self._recovery_filters: dict[str, ctk.CTkCheckBox] = {}
        for column, group in enumerate(EXTENSION_GROUPS, start=1):
            checkbox = ctk.CTkCheckBox(filters_row, text=group)
            checkbox.grid(row=0, column=column, padx=(0, 12))
            self._recovery_filters[group] = checkbox
        self._recovery_filters["Documentos"].select()

        run_row = ctk.CTkFrame(frame, fg_color="transparent")
        run_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 4))
        run_row.grid_columnconfigure(2, weight=1)

        self._recovery_run_button = ctk.CTkButton(
            run_row, text="Escanear y Recuperar", command=self._recovery_run
        )
        self._recovery_run_button.grid(row=0, column=0, padx=(0, 8))

        reset_filters_btn = ctk.CTkButton(
            run_row,
            text="Restablecer Filtros",
            command=self._recovery_reset_filters,
        )
        reset_filters_btn.grid(row=0, column=1, padx=(0, 8))

        self._recovery_progress = ctk.CTkProgressBar(run_row, mode="indeterminate")
        self._recovery_progress.set(0)
        self._recovery_progress.grid(row=0, column=2, sticky="ew")

        self._recovery_summary = ctk.CTkLabel(
            frame,
            text="",
            font=("Segoe UI", 12, "bold"),
            text_color=_COLOR_OK,
        )
        self._recovery_summary.grid(row=6, column=0, sticky="w", padx=16, pady=(0, 4))

        self._recovery_log = self._section_log(frame, row=8)
        return frame

    def _recovery_select_destination(self) -> None:
        """Pick the recovery destination folder through a directory dialog."""
        selected = filedialog.askdirectory(title="Seleccionar carpeta de destino")
        if not selected:
            return
        self._recovery_dest_path = selected
        self._recovery_dest_label.configure(text=f"Destino: {selected}")

    def _recovery_reset_filters(self) -> None:
        """Restore recovery defaults: Documentos ON, others OFF, no destination."""
        for group, checkbox in self._recovery_filters.items():
            if group == "Documentos":
                checkbox.select()
            else:
                checkbox.deselect()
        self._recovery_dest_path = None
        self._recovery_dest_label.configure(text="Destino: no seleccionado")
        self._append_log(self._recovery_log, "Filtros de recuperación restablecidos.")

    def _recovery_run(self) -> None:
        """Start a light recovery job from the selected drive and filters."""
        source_text = self._recovery_drive_combo.get().strip()
        if not source_text:
            self._append_log(self._recovery_log, "Selecciona una unidad de origen.", error=True)
            return
        if not self._recovery_dest_path:
            self._append_log(
                self._recovery_log, "Selecciona una carpeta de destino.", error=True
            )
            return
        extensions: list[str] = []
        for group, checkbox in self._recovery_filters.items():
            if checkbox.get() == 1:
                extensions.extend(EXTENSION_GROUPS[group])
        if not extensions:
            self._append_log(
                self._recovery_log, "Selecciona al menos un tipo de archivo.", error=True
            )
            return
        source = Path(source_text)
        destination = Path(self._recovery_dest_path)
        self._launch(
            "Escanear y recuperar archivos",
            [self._recovery_run_button],
            self._recovery_progress,
            self._recovery_log,
            lambda complete, fail: self._recovery_manager.recover_async(
                source,
                destination,
                extensions,
                mode="light",
                on_complete=complete,
                on_error=fail,
            ),
            self._recovery_done,
            self._recovery_error,
        )

    def _recovery_done(self, result: RecoveryJobResult) -> None:
        """Log the recovery outcome and show the summary banner."""
        self._end_operation([self._recovery_run_button], self._recovery_progress)
        summary = (
            f"Recuperación completada: {result.files_recovered} archivos, "
            f"{self._format_size(result.bytes_recovered)}"
        )
        self._append_log(self._recovery_log, summary)
        self._recovery_summary.configure(text=summary, text_color=_COLOR_OK)
        if result.errors:
            for error in result.errors[:5]:
                self._append_log(self._recovery_log, f"  - {error}", error=True)
            if len(result.errors) > 5:
                self._append_log(
                    self._recovery_log,
                    f"  ... y {len(result.errors) - 5} avisos más.",
                    error=True,
                )
        audit(
            "recovery",
            f"origen={result.source} destino={result.destination} "
            f"archivos={result.files_recovered}",
        )

    def _recovery_error(self, error: Any) -> None:
        """Log a recovery-job error."""
        self._end_operation(self._active_buttons, self._active_progress)
        self._log_error(self._recovery_log, error)

    # ------------------------------------------------------------ red & conexión

    def _build_network(self) -> ctk.CTkFrame:
        """Build the network diagnostics and reset view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(10, weight=1)

        title = self._section_title(frame, "Red y conectividad")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Diagnostica la conexión y resetea la pila de red de Windows.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        diag_header = ctk.CTkLabel(
            frame, text="Diagnóstico de conectividad", font=("Segoe UI", 14, "bold")
        )
        diag_header.grid(row=2, column=0, sticky="w", padx=16, pady=(8, 4))

        diag_row = ctk.CTkFrame(frame, fg_color="transparent")
        diag_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 4))
        diag_row.grid_columnconfigure(1, weight=1)

        self._net_ping_button = ctk.CTkButton(
            diag_row, text="Diagnosticar Ping", command=self._network_ping
        )
        self._net_ping_button.grid(row=0, column=0, padx=(0, 12))

        self._net_ping_progress = ctk.CTkProgressBar(diag_row, mode="indeterminate")
        self._net_ping_progress.set(0)
        self._net_ping_progress.grid(row=0, column=1, sticky="ew")

        self._net_ping_summary = ctk.CTkLabel(
            frame, text="", font=("Segoe UI", 12, "bold"), text_color=_COLOR_OK
        )
        self._net_ping_summary.grid(row=4, column=0, sticky="w", padx=16, pady=(0, 4))

        config_row = ctk.CTkFrame(frame, fg_color="transparent")
        config_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(4, 4))
        config_row.grid_columnconfigure(4, weight=1)

        hosts_label = ctk.CTkLabel(config_row, text="Hosts:", font=("Segoe UI", 12))
        hosts_label.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._net_hosts_entry = ctk.CTkEntry(
            config_row, placeholder_text="8.8.8.8, 1.1.1.1", width=220
        )
        self._net_hosts_entry.insert(0, "8.8.8.8, 1.1.1.1")
        self._net_hosts_entry.grid(row=0, column=1, sticky="w", padx=(0, 16))

        count_label = ctk.CTkLabel(config_row, text="Pings:", font=("Segoe UI", 12))
        count_label.grid(row=0, column=2, sticky="w", padx=(0, 8))

        self._net_count_entry = ctk.CTkEntry(config_row, width=60)
        self._net_count_entry.insert(0, "4")
        self._net_count_entry.grid(row=0, column=3, sticky="w", padx=(0, 16))

        net_reset_btn = ctk.CTkButton(
            config_row, text="Restablecer", command=self._network_reset_config
        )
        net_reset_btn.grid(row=0, column=4, sticky="e")

        reset_header = ctk.CTkLabel(
            frame, text="Reseteo profundo de red", font=("Segoe UI", 14, "bold")
        )
        reset_header.grid(row=6, column=0, sticky="w", padx=16, pady=(8, 4))

        reset_warning = ctk.CTkLabel(
            frame,
            text="Puede requerir permisos de administrador y cortar la red "
            "momentáneamente.",
            font=("Segoe UI", 12),
            text_color=_COLOR_WARN,
        )
        reset_warning.grid(row=7, column=0, sticky="w", padx=16, pady=(0, 4))

        reset_row = ctk.CTkFrame(frame, fg_color="transparent")
        reset_row.grid(row=8, column=0, sticky="ew", padx=16, pady=(4, 4))
        reset_row.grid_columnconfigure(1, weight=1)

        self._net_reset_button = ctk.CTkButton(
            reset_row,
            text="Reseteo Profundo de Red",
            fg_color=_COLOR_WARN,
            hover_color="#d68910",
            command=self._network_reset,
        )
        self._net_reset_button.grid(row=0, column=0, padx=(0, 12))

        self._net_reset_progress = ctk.CTkProgressBar(reset_row, mode="indeterminate")
        self._net_reset_progress.set(0)
        self._net_reset_progress.grid(row=0, column=1, sticky="ew")

        self._net_reset_summary = ctk.CTkLabel(
            frame, text="", font=("Segoe UI", 12, "bold"), text_color=_COLOR_OK
        )
        self._net_reset_summary.grid(row=9, column=0, sticky="w", padx=16, pady=(0, 4))

        self._net_log = self._section_log(frame, row=11)
        return frame

    def _network_ping(self) -> None:
        """Ping the configured hosts in the background."""
        hosts = [
            part.strip()
            for part in self._net_hosts_entry.get().replace(";", ",").split(",")
            if part.strip()
        ]
        try:
            count = int(self._net_count_entry.get().strip() or "4")
        except ValueError:
            count = 4
        if not hosts:
            self._append_log(self._net_log, "Ingresa al menos un host.", error=True)
            return
        self._launch(
            "Diagnóstico de ping",
            [self._net_ping_button],
            self._net_ping_progress,
            self._net_log,
            lambda complete, fail: self._run_sync_in_thread(
                self._network_diagnostic.ping_hosts, complete, fail, hosts, count
            ),
            self._network_ping_done,
            self._network_error,
        )

    def _network_reset_config(self) -> None:
        """Restore the ping defaults: 8.8.8.8/1.1.1.1 and count 4."""
        self._net_hosts_entry.delete(0, "end")
        self._net_hosts_entry.insert(0, "8.8.8.8, 1.1.1.1")
        self._net_count_entry.delete(0, "end")
        self._net_count_entry.insert(0, "4")
        self._append_log(self._net_log, "Configuración de red restablecida.")

    def _network_ping_done(self, results: list[PingResult]) -> None:
        """Log each ping result and the connectivity summary."""
        self._end_operation([self._net_ping_button], self._net_ping_progress)
        parts: list[str] = []
        all_ok = True
        for result in results:
            all_ok = all_ok and result.loss_percent == 0.0
            self._append_log(
                self._net_log,
                f"Ping {result.host}: {result.received}/{result.sent} "
                f"perdida={result.loss_percent}% promedio={result.avg_ms}ms",
            )
            if result.errors:
                for error in result.errors[:2]:
                    self._append_log(self._net_log, f"  - {error}", error=True)
            parts.append(f"{result.host}: {result.loss_percent}% pérdida")
        summary = f"Conectividad: {'OK' if all_ok else 'con pérdida'} - " + " | ".join(parts)
        self._net_ping_summary.configure(
            text=summary, text_color=_COLOR_OK if all_ok else _COLOR_WARN
        )
        audit("ping", f"hosts={','.join(result.host for result in results)}")

    def _network_reset(self) -> None:
        """Run the deep network-stack reset in the background."""
        self._append_log(
            self._net_log,
            "Puede requerir permisos de administrador y cortar la red momentáneamente.",
        )
        self._launch(
            "Reseteo profundo de red",
            [self._net_reset_button],
            self._net_reset_progress,
            self._net_log,
            lambda complete, fail: self._network_diagnostic.reset_network_stack_async(
                on_complete=complete, on_error=fail
            ),
            self._network_reset_done,
            self._network_error,
        )

    def _network_reset_done(self, result: NetworkResetResult) -> None:
        """Log every reset step and the overall result."""
        self._end_operation([self._net_reset_button], self._net_reset_progress)
        ok_steps = 0
        total = len(result.steps)
        for step in result.steps:
            if step.result.success:
                ok_steps += 1
            self._append_log(
                self._net_log,
                f"{'OK' if step.result.success else 'FAIL'}: {step.label}",
            )
        summary = f"Reseteo completado: {ok_steps}/{total} pasos correctos."
        self._append_log(self._net_log, summary)
        self._net_reset_summary.configure(
            text=summary,
            text_color=_COLOR_OK if result.success else _COLOR_BAD,
        )
        audit("network_reset", f"steps={ok_steps}/{total}")

    def _network_error(self, error: Any) -> None:
        """Log a network-operation error."""
        self._end_operation(self._active_buttons, self._active_progress)
        self._log_error(self._net_log, error)

    # ----------------------------------------------------------- programas inicio

    def _build_startup(self) -> ctk.CTkFrame:
        """Build the startup programs manager view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        title = self._section_title(frame, "Programas de inicio")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        self._startup_boot_label = ctk.CTkLabel(
            frame,
            text="Consultando último inicio...",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        self._startup_boot_label.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        startup_row = ctk.CTkFrame(frame, fg_color="transparent")
        startup_row.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 8))
        startup_row.grid_columnconfigure(1, weight=1)

        self._startup_refresh_button = ctk.CTkButton(
            startup_row, text="Actualizar lista", command=self._startup_refresh
        )
        self._startup_refresh_button.grid(row=0, column=0, padx=(0, 8))

        self._startup_reset_button = ctk.CTkButton(
            startup_row, text="Restablecer", command=self._startup_reset
        )
        self._startup_reset_button.grid(row=0, column=1)

        self._startup_list = ctk.CTkScrollableFrame(frame)
        self._startup_list.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 8))
        self._startup_list.grid_columnconfigure(0, weight=1)

        self._startup_log = self._section_log(frame, row=5)

        def _complete(info: BootInfo) -> None:
            try:
                self.after(0, self._startup_boot_done, info)
            except (TclError, RuntimeError):
                pass

        try:
            self._startup_manager.get_boot_info_async(_complete, None)
        except Exception:
            pass
        self._startup_reload_list()
        return frame

    def _startup_refresh(self) -> None:
        """Reload the startup entries list in the background."""
        self._launch(
            "Cargar lista de programas de inicio",
            [self._startup_refresh_button],
            None,
            self._startup_log,
            lambda complete, fail: self._startup_manager.list_entries_async(
                on_complete=complete, on_error=fail
            ),
            self._startup_list_done,
            self._startup_list_error,
        )

    def _startup_reset(self) -> None:
        """Restore the startup view: reload the entries list."""
        self._startup_refresh()

    def _startup_list_done(
        self, payload: tuple[list[StartupEntry], list[str]]
    ) -> None:
        """Store the loaded entries and render the rows."""
        self._end_operation([self._startup_refresh_button], None)
        self._startup_store_entries(payload)

    def _startup_store_entries(
        self, payload: tuple[list[StartupEntry], list[str]]
    ) -> None:
        """Persist the entries, log read errors and re-render the list."""
        entries, errors = payload
        self._startup_entries = entries
        for error in errors:
            self._append_log(self._startup_log, error, error=True)
        self._render_startup_entries()

    def _startup_list_error(self, error: Any) -> None:
        """Log a startup-list load error."""
        self._end_operation(self._active_buttons, self._active_progress)
        self._log_error(self._startup_log, error)

    def _render_startup_entries(self) -> None:
        """Rebuild the scrollable rows from the current entry snapshot."""
        for child in self._startup_list.winfo_children():
            child.destroy()
        if not self._startup_entries:
            empty = ctk.CTkLabel(
                self._startup_list,
                text="No se encontraron programas de inicio.",
                text_color="#8a8a8a",
            )
            empty.grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return
        for index, entry in enumerate(self._startup_entries):
            row = ctk.CTkFrame(self._startup_list, fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", padx=4, pady=3)
            row.grid_columnconfigure(0, weight=1)

            command = entry.command or "(sin comando)"
            if len(command) > 60:
                command = command[:60] + "..."
            info_text = f"{entry.name}  [{entry.hive}]\n{command}"
            info = ctk.CTkLabel(
                row,
                text=info_text,
                font=("Segoe UI", 12),
                justify="left",
                anchor="w",
            )
            info.grid(row=0, column=0, sticky="w", padx=(6, 8), pady=2)

            action = "Deshabilitar" if entry.enabled else "Habilitar"
            toggle = ctk.CTkButton(
                row,
                text=action,
                width=110,
                height=28,
                command=lambda e=entry: self._startup_toggle(e),
            )
            toggle.grid(row=0, column=1, padx=6, pady=2)

    def _startup_toggle(self, entry: StartupEntry) -> None:
        """Enable or disable one startup entry in the background."""
        target = not entry.enabled
        action = "Habilitar" if target else "Deshabilitar"
        self._launch(
            f"{action} {entry.name}",
            [self._startup_refresh_button],
            None,
            self._startup_log,
            lambda complete, fail: self._startup_manager.set_enabled_async(
                entry, target, on_complete=complete, on_error=fail
            ),
            self._startup_toggle_done,
            self._startup_list_error,
        )

    def _startup_toggle_done(self, result: StartupActionResult) -> None:
        """Log the toggle result and reload the list on success."""
        self._end_operation([self._startup_refresh_button], None)
        action = "habilitada" if result.enabled else "deshabilitada"
        if result.success:
            self._append_log(self._startup_log, f"Entrada {result.name} {action}.")
            audit("startup", f"name={result.name} estado={action}")
            self._startup_reload_list()
        else:
            self._append_log(
                self._startup_log,
                f"No se pudo actualizar {result.name}: "
                f"{result.error or 'Error desconocido.'}",
                error=True,
            )

    def _startup_reload_list(self) -> None:
        """Quietly reload the entries and re-render without UI lifecycle noise."""
        def _complete(payload: tuple[list[StartupEntry], list[str]]) -> None:
            try:
                self.after(0, self._startup_store_entries, payload)
            except (TclError, RuntimeError):
                pass

        try:
            self._startup_manager.list_entries_async(_complete, None)
        except Exception:
            pass

    def _startup_boot_done(self, info: BootInfo) -> None:
        """Show the boot time and uptime label."""
        uptime = float(info.uptime_hours or 0.0)
        self._startup_boot_label.configure(
            text=f"Último inicio: {info.boot_time} (hace {uptime:.1f} h)"
        )
        for error in info.errors:
            self._append_log(self._startup_log, error, error=True)

    # ------------------------------------------------------------------ close

    def _on_close(self) -> None:
        """Stop background threads and destroy the window."""
        self._telemetry_active = False
        self.destroy()


def main() -> None:
    """Entry point for manual testing (the production entry lives in main.py)."""
    app = DanTechStudioApp()
    app.mainloop()


if __name__ == "__main__":
    main()
