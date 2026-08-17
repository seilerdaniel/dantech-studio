"""main_window.py - DanTechStudioApp: main GUI window of the DanTech Studio suite.

A dark-mode desktop suite for Windows built on CustomTkinter. It provides a
sidebar navigation with six views (Dashboard, Memoria, Reparación, Seguridad,
Apps, Scripts) that expose the functionality of the ``core`` modules.

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
from tkinter import TclError

from core.disk_analyzer import DiskAnalyzer, DiskHealth, DiskUsage
from core.installer import Installer, WingetResult
from core.malware_cleaner import MalwareCleaner, ScanReport
from core.memory_optimizer import (
    CleanupReport,
    CombinedReport,
    MemoryOptimizer,
    RamReport,
)
from core.report_generator import ActionRecord, ReportGenerator
from core.system_repair import RepairReport, SystemRepair
from utils import process_runner
from utils.process_runner import CommandResult

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - telemetry degrades gracefully
    psutil = None  # type: ignore[assignment]

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
)

#: Highlight colors for the active navigation button.
_NAV_ACTIVE_FG: str = "#1f6aa5"
_NAV_INACTIVE_FG: str = "transparent"

#: Log line colors.
_LOG_COLOR_INFO: str = "#dce4ee"
_LOG_COLOR_ERROR: str = "#ff6b6b"

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
        """Build the window, the sidebar and the six views, then start telemetry.

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

        self._active_buttons: list[Any] = []
        self._active_progress: Optional[Any] = None
        self._views: dict[str, ctk.CTkFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}

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
                height=36,
                corner_radius=6,
                fg_color=_NAV_INACTIVE_FG,
                hover_color="#2e2e2e",
                command=lambda nav_name=name: self._show_view(nav_name),
            )
            button.grid(row=2 + index, column=0, sticky="ew", padx=12, pady=3)
            self._nav_buttons[name] = button

        self.sidebar.grid_rowconfigure(8, weight=1)

        self._admin_status_label = ctk.CTkLabel(
            self.sidebar, text="Administrador: --", font=("Segoe UI", 12)
        )
        self._admin_status_label.grid(row=9, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._admin_button = ctk.CTkButton(
            self.sidebar,
            text="Ejecutar como administrador",
            height=32,
            command=self._relaunch_as_admin,
        )
        self._admin_button.grid(row=10, column=0, sticky="ew", padx=12, pady=(0, 16))

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
        actions_row.grid_columnconfigure(2, weight=1)

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
        """Build the memory optimization view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        title = self._section_title(frame, "Optimización de memoria")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Limpia archivos temporales y libera memoria RAM sin cerrar procesos.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        self._memory_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._memory_progress.set(0)
        self._memory_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=8)

        buttons_row = ctk.CTkFrame(frame, fg_color="transparent")
        buttons_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 4))
        buttons_row.grid_columnconfigure(3, weight=1)

        clean_temp_btn = ctk.CTkButton(
            buttons_row, text="Limpiar temporales", command=self._memory_clean_temp
        )
        clean_temp_btn.grid(row=0, column=0, padx=(0, 8))

        clean_ram_btn = ctk.CTkButton(
            buttons_row, text="Limpiar RAM", command=self._memory_clean_ram
        )
        clean_ram_btn.grid(row=0, column=1, padx=8)

        optimize_btn = ctk.CTkButton(
            buttons_row, text="Optimizar todo", command=self._memory_optimize_all
        )
        optimize_btn.grid(row=0, column=2, padx=8)

        self._memory_buttons = [clean_temp_btn, clean_ram_btn, optimize_btn]

        self._memory_log = self._section_log(frame, row=5)
        return frame

    def _memory_clean_temp(self) -> None:
        """Run the temporary-folder cleanup in the background."""
        self._launch(
            "Limpiar archivos temporales",
            self._memory_buttons,
            self._memory_progress,
            self._memory_log,
            lambda complete, fail: self._memory_optimizer.clean_temp_folders_async(
                on_complete=complete, on_error=fail
            ),
            self._memory_cleanup_done,
            self._memory_error,
        )

    def _memory_clean_ram(self) -> None:
        """Run the RAM working-set trim in the background."""
        self._launch(
            "Limpiar memoria RAM",
            self._memory_buttons,
            self._memory_progress,
            self._memory_log,
            lambda complete, fail: self._memory_optimizer.cleanup_ram_async(
                on_complete=complete, on_error=fail
            ),
            self._memory_ram_done,
            self._memory_error,
        )

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

    def _memory_cleanup_done(self, report: CleanupReport) -> None:
        """Log the outcome of a temp-folder cleanup."""
        self._end_operation(self._memory_buttons, self._memory_progress)
        self._append_log(self._memory_log, "Limpieza de temporales completada.")
        for line in self._format_cleanup_report(report):
            self._append_log(self._memory_log, line)

    def _memory_ram_done(self, report: RamReport) -> None:
        """Log the outcome of a RAM cleanup."""
        self._end_operation(self._memory_buttons, self._memory_progress)
        self._append_log(self._memory_log, "Limpieza de RAM completada.")
        for line in self._format_ram_report(report):
            self._append_log(self._memory_log, line)

    def _memory_all_done(self, report: CombinedReport) -> None:
        """Log the outcome of the combined optimization pass."""
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
        """Build the system repair view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        title = self._section_title(frame, "Reparación del sistema")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="SFC y DISM reparan la integridad de Windows; el restablecimiento "
            "de red corrige la pila TCP/IP. Requieren administrador.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        self._repair_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._repair_progress.set(0)
        self._repair_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=8)

        buttons_row = ctk.CTkFrame(frame, fg_color="transparent")
        buttons_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 4))
        buttons_row.grid_columnconfigure(3, weight=1)

        sfc_btn = ctk.CTkButton(buttons_row, text="SFC /scannow", command=self._repair_sfc)
        sfc_btn.grid(row=0, column=0, padx=(0, 8))

        dism_btn = ctk.CTkButton(
            buttons_row, text="DISM RestoreHealth", command=self._repair_dism
        )
        dism_btn.grid(row=0, column=1, padx=8)

        network_btn = ctk.CTkButton(
            buttons_row, text="Restablecer red", command=self._repair_network
        )
        network_btn.grid(row=0, column=2, padx=8)

        restore_btn = ctk.CTkButton(
            buttons_row,
            text="Crear punto de restauración",
            command=self._repair_restore_point,
        )
        restore_btn.grid(row=0, column=3, padx=8)

        self._repair_buttons = [sfc_btn, dism_btn, network_btn, restore_btn]

        self._repair_log = self._section_log(frame, row=5)
        return frame

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
        self._end_operation(self._repair_buttons, self._repair_progress)
        self._log_error(self._repair_log, error)

    # --------------------------------------------------------------- seguridad

    def _build_security(self) -> ctk.CTkFrame:
        """Build the security (Windows Defender) view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(7, weight=1)

        title = self._section_title(frame, "Seguridad")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Escaneos con Windows Defender y actualización de firmas. "
            "Requieren administrador.",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        self._security_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._security_progress.set(0)
        self._security_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=8)

        buttons_row = ctk.CTkFrame(frame, fg_color="transparent")
        buttons_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 4))
        buttons_row.grid_columnconfigure(3, weight=1)

        update_btn = ctk.CTkButton(
            buttons_row, text="Actualizar firmas", command=self._security_update_signatures
        )
        update_btn.grid(row=0, column=0, padx=(0, 8))

        quick_btn = ctk.CTkButton(
            buttons_row, text="Análisis rápido", command=self._security_quick_scan
        )
        quick_btn.grid(row=0, column=1, padx=8)

        full_btn = ctk.CTkButton(
            buttons_row, text="Análisis completo", command=self._security_full_scan
        )
        full_btn.grid(row=0, column=2, padx=8)

        custom_row = ctk.CTkFrame(frame, fg_color="transparent")
        custom_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 4))
        custom_row.grid_columnconfigure(0, weight=1)

        self._security_folder_entry = ctk.CTkEntry(
            custom_row, placeholder_text="Ruta de archivo o carpeta (ej: C:\\Users\\Usuario)"
        )
        self._security_folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        custom_btn = ctk.CTkButton(
            custom_row, text="Análisis personalizado", command=self._security_custom_scan
        )
        custom_btn.grid(row=0, column=1)

        self._security_buttons = [update_btn, quick_btn, full_btn, custom_btn]

        self._security_log = self._section_log(frame, row=7)
        return frame

    def _security_update_signatures(self) -> None:
        """Update Defender signatures in the background."""
        self._launch(
            "Actualizar firmas",
            self._security_buttons,
            self._security_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.update_signatures_async(
                on_complete=complete, on_error=fail
            ),
            self._security_done,
            self._security_error,
        )

    def _security_quick_scan(self) -> None:
        """Run a quick Defender scan in the background."""
        self._launch(
            "Análisis rápido",
            self._security_buttons,
            self._security_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.quick_scan_async(
                on_complete=complete, on_error=fail
            ),
            self._security_done,
            self._security_error,
        )

    def _security_full_scan(self) -> None:
        """Run a full Defender scan in the background."""
        self._launch(
            "Análisis completo",
            self._security_buttons,
            self._security_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.full_scan_async(
                on_complete=complete, on_error=fail
            ),
            self._security_done,
            self._security_error,
        )

    def _security_custom_scan(self) -> None:
        """Run a custom Defender scan of the entered path in the background."""
        folder = self._security_folder_entry.get().strip()
        if not folder:
            self._append_log(self._security_log, "Ingresa una ruta válida.", error=True)
            return
        self._launch(
            f"Análisis personalizado: {folder}",
            self._security_buttons,
            self._security_progress,
            self._security_log,
            lambda complete, fail: self._malware_cleaner.custom_scan_async(
                folder, on_complete=complete, on_error=fail
            ),
            self._security_done,
            self._security_error,
        )

    def _security_done(self, report: ScanReport) -> None:
        """Log the outcome of a Defender scan."""
        self._end_operation(self._security_buttons, self._security_progress)
        self._append_log(self._security_log, f"Operación '{report.scan_type}' finalizada.")
        for line in self._format_scan_report(report):
            self._append_log(self._security_log, line)

    def _security_error(self, error: Any) -> None:
        """Log a security-operation error."""
        self._end_operation(self._security_buttons, self._security_progress)
        self._log_error(self._security_log, error)

    # ------------------------------------------------------------------- apps

    def _build_apps(self) -> ctk.CTkFrame:
        """Build the apps (winget) view."""
        frame = ctk.CTkFrame(self.content_panel, corner_radius=0)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(7, weight=1)

        title = self._section_title(frame, "Aplicaciones")
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(20, 4))

        description = ctk.CTkLabel(
            frame,
            text="Busca, instala y actualiza programas con winget (Windows Package Manager).",
            font=("Segoe UI", 12),
            text_color="#8a8a8a",
        )
        description.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        self._apps_progress = ctk.CTkProgressBar(frame, mode="indeterminate")
        self._apps_progress.set(0)
        self._apps_progress.grid(row=2, column=0, sticky="ew", padx=16, pady=8)

        search_row = ctk.CTkFrame(frame, fg_color="transparent")
        search_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 4))
        search_row.grid_columnconfigure(0, weight=1)

        self._apps_search_entry = ctk.CTkEntry(
            search_row, placeholder_text="Buscar paquete (ej: Firefox)"
        )
        self._apps_search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        search_btn = ctk.CTkButton(search_row, text="Buscar", command=self._apps_search)
        search_btn.grid(row=0, column=1)

        install_row = ctk.CTkFrame(frame, fg_color="transparent")
        install_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 4))
        install_row.grid_columnconfigure(0, weight=1)

        self._apps_install_entry = ctk.CTkEntry(
            install_row, placeholder_text="ID del paquete (ej: Microsoft.PowerToys)"
        )
        self._apps_install_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        install_btn = ctk.CTkButton(install_row, text="Instalar", command=self._apps_install)
        install_btn.grid(row=0, column=1)

        upgrade_row = ctk.CTkFrame(frame, fg_color="transparent")
        upgrade_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 4))

        upgrade_all_btn = ctk.CTkButton(
            upgrade_row, text="Actualizar todo", command=self._apps_upgrade_all
        )
        upgrade_all_btn.grid(row=0, column=0, sticky="w")

        self._apps_buttons = [search_btn, install_btn, upgrade_all_btn]

        self._apps_log = self._section_log(frame, row=7)
        return frame

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
