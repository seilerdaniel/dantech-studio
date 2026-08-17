"""report_generator.py - ReportGenerator: professional diagnostic HTML/PDF reports.

Windows-only core module. Builds a self-contained, dark-mode HTML report and a
print-friendly PDF (via fpdf2) summarizing the machine telemetry and every
maintenance action performed by the suite.

Design rules:
- Every user-facing string is neutral Spanish; identifiers/docstrings English.
- No process execution happens here: telemetry is read in-process (psutil,
  platform, socket) with explicit per-field try/except and fallbacks.
- The PDF relies on the fpdf2 core fonts (Helvetica, latin-1 only); every
  string is sanitized with ``encode("latin-1", "replace").decode("latin-1")``
  so Spanish accents and dashes never crash the encoder.
"""

from __future__ import annotations

import html
import platform
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from utils.process_runner import ProcessRunnerError, ensure_windows

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so reports fail gracefully
    psutil = None  # type: ignore[assignment]

try:
    from fpdf import FPDF
    from fpdf.enums import MethodReturnValue
except ImportError:  # pragma: no cover - guarded so reports fail gracefully
    FPDF = None  # type: ignore[assignment]
    MethodReturnValue = None  # type: ignore[assignment]

#: Institutional header shown on the HTML report and the PDF title block.
HEADER_TITLE = "DanTech Studio - Informe Técnico de Diagnóstico y Mantenimiento"
TECHNICIAN_NAME = "Daniel"
CONTACT_WHATSAPP = "WhatsApp: 11-3179-7343"

#: Export kinds accepted by ``generate``.
_SUPPORTED_KINDS = ("html", "pdf")

#: PDF geometry constants (A4 millimetres).
_PDF_MARGIN_X = 10.0
_PDF_HEADER_BLUE = (37, 99, 235)
_PDF_ROW_ALT = (240, 242, 245)
_PDF_BORDER = (148, 163, 184)
_PDF_TEXT_DARK = (31, 41, 55)
_PDF_GREEN = (16, 185, 129)
_PDF_RED = (239, 68, 68)

#: Inline stylesheet for the self-contained dark-mode HTML report.
_HTML_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0d14; color: #e5e7eb; font-family: 'Segoe UI', Roboto, Arial, sans-serif; padding: 40px 16px; }
.container { max-width: 920px; margin: 0 auto; }
.card { background: #121824; border: 1px solid rgba(37, 99, 235, 0.35); border-radius: 12px; padding: 24px 26px; margin-bottom: 20px; }
h1 { font-size: 24px; font-weight: 700; color: #ffffff; margin-bottom: 6px; }
.subtitle { color: #9ca3af; font-size: 14px; }
.brand-bar { height: 4px; border-radius: 4px; margin: 14px 0; background: linear-gradient(90deg, #2563eb, #06b6d4); }
.section-title { font-size: 16px; color: #06b6d4; margin-bottom: 12px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin-bottom: 4px; }
th { background: #2563eb; color: #ffffff; text-align: left; padding: 10px 12px; font-size: 13px; }
td { padding: 10px 12px; border-bottom: 1px solid #1f2937; font-size: 13px; vertical-align: top; }
tr:nth-child(even) td { background: #0d1117; }
.mono { font-family: Consolas, 'Cascadia Mono', monospace; }
.status-ok { color: #10b981; font-weight: 600; }
.status-error { color: #ef4444; font-weight: 600; }
.error-box { list-style: none; padding: 12px 14px; border-radius: 8px; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; }
.error-box li { margin-bottom: 6px; }
.footer { text-align: center; color: #6b7280; font-size: 12px; margin-top: 26px; line-height: 1.6; }
.meta { color: #9ca3af; font-size: 13px; margin-bottom: 4px; }
"""


@dataclass
class ActionRecord:
    """One maintenance/diagnostic action performed on the machine.

    Attributes:
        label: Short action name, e.g. "Temporales eliminados".
        detail: Human-readable detail, e.g. "1.2 GB (3.450 archivos)".
        status: Outcome flag, one of ``ok`` or ``error``.
    """

    label: str
    detail: str
    status: str = "ok"


@dataclass
class TelemetrySnapshot:
    """Hardware and OS telemetry collected for the report header.

    Every field carries a safe default so an export can never fail because a
    single sensor is unavailable; partial failures accumulate in ``errors``.
    """

    hostname: str = "No detectado"
    timestamp: str = ""
    cpu_model: str = "No detectado"
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    ram_percent: float = 0.0
    disk_c_free_gb: float = 0.0
    disk_c_total_gb: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class DiagnosticReport:
    """Full report payload: a telemetry snapshot plus performed actions."""

    telemetry: TelemetrySnapshot
    actions: list[ActionRecord]


def _pdf_sanitize(text: str) -> str:
    """Force a string into the latin-1 range used by the core fpdf2 fonts.

    Non-latin-1 characters (curly quotes, special symbols) are replaced instead
    of raising, keeping the encoder alive for the whole document.
    """
    return text.encode("latin-1", "replace").decode("latin-1")


def _pdf_measure(pdf: FPDF, width: float, text: str, line_h: float) -> float:
    """Return the height (mm) a ``multi_cell`` of ``text`` would occupy.

    Uses fpdf2's ``dry_run`` mode so table rows can reserve a uniform height
    before anything is drawn on the page.
    """
    if MethodReturnValue is None:
        return line_h
    height = pdf.multi_cell(
        w=width,
        h=line_h,
        text=_pdf_sanitize(text),
        border=0,
        align="L",
        dry_run=True,
        output=MethodReturnValue.HEIGHT,
    )
    if isinstance(height, float):
        return height
    return line_h


def _pdf_table_row(
    pdf: FPDF,
    widths: Sequence[float],
    texts: Sequence[str],
    aligns: Sequence[str],
    line_h: float = 6.0,
    fill: bool = False,
    fill_color: tuple[int, int, int] = (255, 255, 255),
    cell_colors: Optional[Sequence[Optional[tuple[int, int, int]]]] = None,
    x0: float = _PDF_MARGIN_X,
) -> float:
    """Draw one table row: measured cell heights plus a shared border.

    Cells are rendered without borders and the row is closed afterwards with a
    rectangle outline plus vertical separators, so every cell shares the same
    bottom edge even when the wrapped text heights differ.

    Args:
        pdf: The active document.
        widths: Column widths in millimetres.
        texts: Cell texts (sanitized inside for latin-1).
        aligns: Horizontal alignment per column (``L``/``C``/``R``).
        line_h: Line height per text line.
        fill: Whether the row gets a background fill.
        fill_color: Background RGB when ``fill`` is True.
        cell_colors: Optional per-cell text RGB; ``None`` entries keep the
            current text color.
        x0: Left margin where the row starts.

    Returns:
        The height in millimetres actually used by the row.
    """
    sanitized = [_pdf_sanitize(text) for text in texts]
    measured = [_pdf_measure(pdf, width, text, line_h) for width, text in zip(widths, sanitized)]
    row_h = max(measured) + 2.0

    if pdf.get_y() + row_h > pdf.page_break_trigger:
        pdf.add_page()
    y0 = pdf.get_y()

    pdf.set_fill_color(*fill_color)
    x = x0
    for index, (width, text, align) in enumerate(zip(widths, sanitized, aligns)):
        pdf.set_xy(x, y0)
        color = None
        if cell_colors is not None and index < len(cell_colors):
            color = cell_colors[index]
        if color is not None:
            pdf.set_text_color(*color)
        pdf.multi_cell(
            w=width,
            h=line_h,
            text=text,
            border=0,
            align=align,
            fill=fill,
            new_x="RIGHT",
            new_y="TOP",
        )
        if color is not None:
            pdf.set_text_color(*_PDF_TEXT_DARK)
        x += width

    total_width = float(sum(widths))
    pdf.set_draw_color(*_PDF_BORDER)
    pdf.rect(x0, y0, total_width, row_h)
    cx = x0
    for width in widths[:-1]:
        cx += float(width)
        pdf.line(cx, y0, cx, y0 + row_h)

    pdf.set_xy(x0, y0 + row_h)
    return row_h


def _pdf_section_title(pdf: FPDF, title: str) -> None:
    """Draw a section heading with an accent underline."""
    pdf.ln(4)
    pdf.set_font("helvetica", style="B", size=12)
    pdf.set_text_color(*_PDF_HEADER_BLUE)
    pdf.multi_cell(
        w=0,
        h=7,
        text=_pdf_sanitize(title),
        align="L",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_draw_color(6, 182, 212)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)
    pdf.set_text_color(*_PDF_TEXT_DARK)


class ReportGenerator:
    """Collect machine telemetry and export diagnostic/maintenance reports.

    Two export targets share one payload (:class:`DiagnosticReport`):
    a dark-mode HTML document for on-screen review and a print-friendly PDF
    for the client. Long-running exports expose ``*_async`` variants so the
    GUI never freezes.
    """

    # ------------------------------------------------------------ telemetry

    def collect_telemetry(self) -> TelemetrySnapshot:
        """Gather hostname, CPU, RAM and disk-C telemetry with per-field guards.

        Each sensor is read inside its own try/except so a single unavailable
        source produces a fallback value plus an entry in ``errors`` instead
        of aborting the whole snapshot.

        Returns:
            A :class:`TelemetrySnapshot` with safe fallbacks and partial errors.
        """
        ensure_windows()
        snapshot = TelemetrySnapshot()

        try:
            snapshot.hostname = socket.gethostname()
        except OSError:
            snapshot.errors.append("No se pudo detectar el nombre del equipo.")

        try:
            snapshot.timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        except Exception:  # pragma: no cover - defensive, datetime never fails
            snapshot.errors.append("No se pudo generar la fecha y hora del diagnostico.")

        try:
            cpu = platform.processor()
            snapshot.cpu_model = cpu.strip() if cpu and cpu.strip() else "No detectado"
        except Exception as exc:
            snapshot.errors.append(f"No se pudo detectar el modelo del procesador: {exc}")

        if psutil is None:
            snapshot.errors.append("psutil no esta disponible; sin datos de RAM y disco.")
            return snapshot

        try:
            virtual = psutil.virtual_memory()
            snapshot.ram_used_mb = virtual.used / (1024.0 ** 2)
            snapshot.ram_total_mb = virtual.total / (1024.0 ** 2)
            snapshot.ram_percent = float(virtual.percent)
        except (psutil.Error, OSError, ValueError) as exc:
            snapshot.errors.append(f"No se pudo leer la memoria RAM: {exc}")

        try:
            disk = psutil.disk_usage("C:/")
            snapshot.disk_c_free_gb = disk.free / (1024.0 ** 3)
            snapshot.disk_c_total_gb = disk.total / (1024.0 ** 3)
        except (psutil.Error, OSError, ValueError) as exc:
            snapshot.errors.append(f"No se pudo leer el disco C:: {exc}")

        return snapshot

    # ------------------------------------------------------------ HTML export

    def build_html(self, report: DiagnosticReport) -> str:
        """Build a self-contained dark-mode HTML5 document for ``report``.

        All dynamic text is HTML-escaped so neither user-supplied descriptions
        nor sensor strings can break the markup.

        Returns:
            The full HTML document as a string.
        """
        telemetry = report.telemetry

        def esc(value: object) -> str:
            return html.escape(str(value), quote=True)

        ram_detail = (
            f"{telemetry.ram_used_mb:.0f} MB usados / "
            f"{telemetry.ram_total_mb:.0f} MB totales ({telemetry.ram_percent:.1f} %)"
        )
        disk_detail = (
            f"{telemetry.disk_c_free_gb:.1f} GB libres / "
            f"{telemetry.disk_c_total_gb:.1f} GB totales"
        )

        telemetry_rows = [
            "<tr><td>Procesador (CPU)</td>"
            f"<td class='mono'>{esc(telemetry.cpu_model)}</td></tr>",
            "<tr><td>Memoria RAM</td>"
            f"<td class='mono'>{esc(ram_detail)}</td></tr>",
            "<tr><td>Disco C:</td>"
            f"<td class='mono'>{esc(disk_detail)}</td></tr>",
        ]

        action_rows: list[str] = []
        for action in report.actions:
            status_class = "status-ok" if action.status == "ok" else "status-error"
            status_text = "OK" if action.status == "ok" else "Error"
            action_rows.append(
                f"<tr><td>{esc(action.label)}</td><td>{esc(action.detail)}</td>"
                f"<td class='{status_class}'>{esc(status_text)}</td></tr>"
            )
        actions_table = "\n".join(action_rows)
        if not action_rows:
            actions_table = "<tr><td colspan='3'>Sin acciones registradas.</td></tr>"

        error_section = ""
        if telemetry.errors:
            error_items = "\n".join(f"<li>{esc(error)}</li>" for error in telemetry.errors)
            error_section = (
                "<div class='card'>"
                "<h2 class='section-title' style='color:#ef4444;'>Advertencias</h2>"
                f"<ul class='error-box'>{error_items}</ul>"
                "</div>"
            )

        return (
            "<!DOCTYPE html>\n"
            "<html lang='es'>\n"
            "<head>\n"
            "<meta charset='utf-8'>\n"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
            "<title>DanTech Studio - Informe Técnico</title>\n"
            f"<style>{_HTML_CSS}</style>\n"
            "</head>\n"
            "<body>\n"
            "<div class='container'>\n"
            "<div class='card'>\n"
            f"<h1>{esc(HEADER_TITLE)}</h1>\n"
            f"<p class='subtitle'>Técnico: {esc(TECHNICIAN_NAME)} | {esc(CONTACT_WHATSAPP)}</p>\n"
            "<div class='brand-bar'></div>\n"
            f"<p class='meta'>Fecha y hora: <span class='mono'>{esc(telemetry.timestamp)}</span></p>\n"
            f"<p class='meta'>Equipo: <span class='mono'>{esc(telemetry.hostname)}</span></p>\n"
            "</div>\n"
            "<div class='card'>\n"
            "<h2 class='section-title'>Telemetría del Sistema</h2>\n"
            "<table>\n"
            "<thead><tr><th style='width:30%;'>Métrica</th><th>Valor</th></tr></thead>\n"
            f"<tbody>{telemetry_rows[0]}\n{telemetry_rows[1]}\n{telemetry_rows[2]}</tbody>\n"
            "</table>\n"
            "</div>\n"
            "<div class='card'>\n"
            "<h2 class='section-title'>Acciones Realizadas</h2>\n"
            "<table>\n"
            "<thead><tr><th>Acción</th><th>Detalle</th><th style='width:12%;'>Estado</th></tr></thead>\n"
            f"<tbody>{actions_table}</tbody>\n"
            "</table>\n"
            "</div>\n"
            f"{error_section}\n"
            "<p class='footer'>"
            "Documento generado por DanTech Studio.<br>"
            "Los valores de telemetría corresponden al momento del diagnóstico."
            "</p>\n"
            "</div>\n"
            "</body>\n"
            "</html>\n"
        )

    def export_html(self, report: DiagnosticReport, path: Path) -> Path:
        """Write the HTML report to ``path`` as UTF-8.

        Args:
            report: The report to serialize.
            path: Destination file path.

        Returns:
            The resolved path where the file was written.
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.build_html(report), encoding="utf-8")
        except OSError as exc:
            raise ProcessRunnerError(f"No se pudo escribir el informe HTML en {path}: {exc}") from exc
        return path

    # ------------------------------------------------------------ PDF export

    def export_pdf(self, report: DiagnosticReport, path: Path) -> Path:
        """Write a print-friendly PDF report to ``path`` using fpdf2.

        Uses the core Helvetica fonts (latin-1 only); every string is sanitized
        before it reaches the encoder so Spanish accents never raise.

        Args:
            report: The report to serialize.
            path: Destination file path.

        Returns:
            The resolved path where the file was written.

        Raises:
            ProcessRunnerError: When fpdf2 is not installed or writing fails.
        """
        if FPDF is None:
            raise ProcessRunnerError("fpdf2 no esta instalado; no se puede generar el PDF.")

        telemetry = report.telemetry
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessRunnerError(
                f"No se pudo crear el directorio de destino del PDF: {exc}"
            ) from exc

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.set_margins(_PDF_MARGIN_X, 14, _PDF_MARGIN_X)
        pdf.add_page()

        # Title block.
        pdf.set_font("helvetica", style="B", size=17)
        pdf.set_text_color(*_PDF_HEADER_BLUE)
        pdf.multi_cell(w=0, h=9, text=_pdf_sanitize(HEADER_TITLE), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_draw_color(6, 182, 212)
        pdf.set_line_width(0.8)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(5)

        # Technical line.
        pdf.set_font("helvetica", size=11)
        pdf.set_text_color(*_PDF_TEXT_DARK)
        pdf.multi_cell(w=0, h=7, text=_pdf_sanitize(f"Técnico: {TECHNICIAN_NAME}"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(w=0, h=7, text=_pdf_sanitize(CONTACT_WHATSAPP), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(
            w=0,
            h=6,
            text=_pdf_sanitize(f"Fecha y hora: {telemetry.timestamp}   |   Equipo: {telemetry.hostname}"),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(5)

        # Telemetry section.
        _pdf_section_title(pdf, "Telemetría del Sistema")
        pdf.set_font("helvetica", style="B", size=10)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*_PDF_HEADER_BLUE)
        _pdf_table_row(pdf, [60, 130], ["Métrica", "Valor"], ["L", "L"], fill=True, fill_color=_PDF_HEADER_BLUE)
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(*_PDF_TEXT_DARK)
        telemetry_rows = [
            ("Procesador (CPU)", telemetry.cpu_model),
            (
                "Memoria RAM",
                f"{telemetry.ram_used_mb:.0f} MB usados de "
                f"{telemetry.ram_total_mb:.0f} MB ({telemetry.ram_percent:.1f} %)",
            ),
            (
                "Disco C:",
                f"{telemetry.disk_c_free_gb:.1f} GB libres de {telemetry.disk_c_total_gb:.1f} GB",
            ),
        ]
        for index, (metric, value) in enumerate(telemetry_rows):
            _pdf_table_row(
                pdf,
                [60, 130],
                [metric, value],
                ["L", "L"],
                fill=(index % 2 == 1),
                fill_color=_PDF_ROW_ALT,
            )
        pdf.ln(4)

        # Actions section.
        _pdf_section_title(pdf, "Acciones Realizadas")
        pdf.set_font("helvetica", style="B", size=10)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*_PDF_HEADER_BLUE)
        _pdf_table_row(pdf, [55, 95, 40], ["Acción", "Detalle", "Estado"], ["L", "L", "C"], fill=True, fill_color=_PDF_HEADER_BLUE)
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(*_PDF_TEXT_DARK)
        for index, action in enumerate(report.actions):
            status_text = "OK" if action.status == "ok" else "Error"
            status_color = _PDF_GREEN if action.status == "ok" else _PDF_RED
            _pdf_table_row(
                pdf,
                [55, 95, 40],
                [action.label, action.detail, status_text],
                ["L", "L", "C"],
                fill=(index % 2 == 1),
                fill_color=_PDF_ROW_ALT,
                cell_colors=[None, None, status_color],
            )
        pdf.ln(4)

        # Warnings section (only when telemetry collection had partial errors).
        if telemetry.errors:
            _pdf_section_title(pdf, "Advertencias")
            pdf.set_font("helvetica", size=10)
            pdf.set_text_color(185, 28, 28)
            for error in telemetry.errors:
                pdf.multi_cell(w=0, h=6, text=_pdf_sanitize(f"- {error}"), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)

        # Footer note.
        pdf.set_font("helvetica", size=8)
        pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(
            w=0,
            h=5,
            text=_pdf_sanitize(
                "Documento generado por DanTech Studio. "
                "Los valores de telemetría corresponden al momento del diagnóstico."
            ),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )

        try:
            pdf.output(str(path))
        except OSError as exc:
            raise ProcessRunnerError(f"No se pudo escribir el informe PDF en {path}: {exc}") from exc
        return path

    # ------------------------------------------------------------- orchestrate

    def generate(
        self,
        kind: str,
        path: Path,
        actions: list[ActionRecord],
    ) -> Path:
        """Collect telemetry, build the report and export it in one step.

        Args:
            kind: Output format, ``html`` or ``pdf``.
            path: Destination file path.
            actions: Performed maintenance actions to include.

        Returns:
            The path where the report was written.

        Raises:
            ProcessRunnerError: When ``kind`` is not supported.
        """
        export_kind = str(kind).strip().lower()
        if export_kind not in _SUPPORTED_KINDS:
            raise ProcessRunnerError(f"Tipo de informe no soportado: {kind}")

        telemetry = self.collect_telemetry()
        report = DiagnosticReport(telemetry=telemetry, actions=list(actions))
        if export_kind == "pdf":
            return self.export_pdf(report, Path(path))
        return self.export_html(report, Path(path))

    def generate_async(
        self,
        kind: str,
        path: Path,
        actions: list[ActionRecord],
        on_complete: Callable[[Path], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``generate`` in a daemon thread without blocking the GUI.

        Args:
            kind: Output format, ``html`` or ``pdf``.
            path: Destination file path.
            actions: Performed maintenance actions to include.
            on_complete: Called with the written :class:`~pathlib.Path`.
            on_error: Called with the exception when generation fails.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            try:
                result = self.generate(kind, path, actions)
            except Exception as exc:  # pragma: no cover - defensive
                if on_error is not None:
                    on_error(exc)
                return
            on_complete(result)

        thread = threading.Thread(target=_worker, name="report-generate", daemon=True)
        thread.start()
        return thread
