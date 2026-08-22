"""system_info.py - SystemInfo: battery health, OEM product key and quotes.

Windows-only core module covering three commercial-grade diagnostics:

  - ``get_battery_health``: laptop battery degradation and charge cycles,
    read from the ``powercfg /batteryreport`` XML output with WMI/psutil
    fallbacks so a missing sensor never crashes the caller.
  - ``get_windows_product_key``: OEM key embedded in BIOS/UEFI via the
    ``SoftwareLicensingService.OA3xOriginalProductKey`` CIM property.
  - ``generate_quote_pdf``: branded service quote/receipt PDF built with
    fpdf2 using the same visual language as :mod:`core.report_generator`.

Every subprocess is routed through ``utils.process_runner.run_command``
(never raw subprocess); every long operation exposes a ``*_async`` variant
running on a daemon thread with the standard callback contract.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from utils.process_runner import (
    ProcessRunnerError,
    ensure_windows,
    run_command,
)

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so callers degrade gracefully
    psutil = None  # type: ignore[assignment]

try:
    from fpdf import FPDF
except ImportError:  # pragma: no cover - guarded so quotes fail gracefully
    FPDF = None  # type: ignore[assignment]

#: Institutional branding shared with the diagnostic reports.
TECHNICIAN_NAME = "Daniel"
CONTACT_WHATSAPP = "WhatsApp: 11-3179-7343"
CONTACT_WEB = "https://dantech-landing.vercel.app"

#: Quote PDF geometry/colors (A4 millimetres), matching ReportGenerator.
_PDF_MARGIN_X = 10.0
_PDF_HEADER_BLUE = (37, 99, 235)
_PDF_ROW_ALT = (240, 242, 245)
_PDF_BORDER = (148, 163, 184)
_PDF_TEXT_DARK = (31, 41, 55)
_PDF_GREEN = (16, 185, 129)

#: Wall-clock limits (seconds) for the read-only system queries.
_BATTERY_REPORT_TIMEOUT = 60.0
_WMI_QUERY_TIMEOUT = 45.0


@dataclass
class BatteryHealth:
    """Outcome of the battery diagnostics.

    ``wear_percent`` is the capacity degradation (design vs full-charge);
    a desktop machine or a device without a battery reports ``present=False``
    instead of failing.
    """

    present: bool = False
    charge_percent: Optional[float] = None
    wear_percent: Optional[float] = None
    cycle_count: Optional[int] = None
    design_capacity_mwh: Optional[int] = None
    full_charge_capacity_mwh: Optional[int] = None
    status_message: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class ProductKeyInfo:
    """OEM Windows product key plus the detected OS edition."""

    product_key: Optional[str] = None
    edition: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class QuoteService:
    """One billable line of a technical-service quote."""

    description: str
    detail: str = ""
    amount: float = 0.0


def _pdf_sanitize(text: str) -> str:
    """Force a string into the latin-1 range used by the core fpdf2 fonts."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _format_amount(value: float) -> str:
    """Render an amount as a plain currency string."""
    return f"$ {value:,.2f}"


def _last_tag_int(xml_text: str, tag: str) -> Optional[int]:
    """Return the integer payload of the LAST ``<tag>`` occurrence in ``xml_text``.

    The battery-report XML repeats some elements across history entries;
    the last occurrence always belongs to the current life estimates.
    """
    matches = re.findall(rf"<{tag}>\s*([0-9.,\s]+?)\s*</{tag}>", xml_text)
    if not matches:
        return None
    digits = re.sub(r"[^\d]", "", matches[-1])
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:  # pragma: no cover - defensive
        return None


class SystemInfo:
    """Read battery health, the OEM Windows key and build service quotes.

    All methods are read-only regarding system state (the hardening actions
    live in :mod:`core.malware_cleaner`); failures accumulate into their
    report's ``errors`` list instead of raising.
    """

    # -------------------------------------------------------------- helpers

    def _reports_dir(self) -> Path:
        """Resolve/create the scratch directory for the battery XML report."""
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "DanTechStudio" / "reports"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessRunnerError(
                f"No se pudo crear el directorio de reportes ({base}): {exc}"
            ) from exc
        return base

    def _battery_xml_fallback(self, report: BatteryHealth) -> None:
        """Best-effort WMI fill-in when the powercfg XML yielded no capacities."""
        script = (
            "$d=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData "
            "-ErrorAction SilentlyContinue).DesignedCapacity;"
            "$f=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity "
            "-ErrorAction SilentlyContinue).FullChargedCapacity;"
            "$c=(Get-CimInstance -Namespace root\\wmi -ClassName BatteryCycleCount "
            "-ErrorAction SilentlyContinue).CycleCount;"
            "@{Design=$d;Full=$f;Cycles=$c}|ConvertTo-Json -Compress"
        )
        result = run_command(
            ("powershell.exe", "-NoProfile", "-Command", script),
            timeout=_WMI_QUERY_TIMEOUT,
        )
        if not result.success or not result.stdout.strip():
            report.errors.append("No se pudo leer la capacidad de bateria vía WMI.")
            return
        import json as _json

        try:
            payload = _json.loads(result.stdout.strip())
        except ValueError:
            report.errors.append("Salida WMI de bateria no interpretable.")
            return
        design = int(payload.get("Design") or 0)
        full = int(payload.get("Full") or 0)
        cycles = int(payload.get("Cycles") or 0)
        if design > 0:
            report.design_capacity_mwh = design
        if full > 0:
            report.full_charge_capacity_mwh = full
        if cycles > 0 and report.cycle_count is None:
            report.cycle_count = cycles

    def _battery_presence_fallback(self, report: BatteryHealth) -> bool:
        """Return True when ``Win32_Battery`` reports at least one unit."""
        result = run_command(
            (
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue "
                "| Measure-Object).Count",
            ),
            timeout=_WMI_QUERY_TIMEOUT,
        )
        if result.success and result.stdout.strip().isdigit():
            return int(result.stdout.strip()) > 0
        report.errors.append("No se pudo verificar la presencia de bateria vía WMI.")
        return False

    # ------------------------------------------------------------ public sync

    def get_battery_health(self) -> BatteryHealth:
        """Diagnose battery presence, wear percentage and charge cycles.

        Primary source is ``powercfg /batteryreport /XML`` parsed with
        namespace-tolerant regular expressions; psutil provides the instant
        charge percentage, and WMI classes fill any gap left by both.

        Returns:
            A :class:`BatteryHealth`; partial sensor failures are collected
            in ``errors`` without aborting the diagnosis.
        """
        ensure_windows()
        report = BatteryHealth()

        if psutil is not None:
            try:
                sensor = psutil.sensors_battery()
                if sensor is not None:
                    report.present = True
                    report.charge_percent = float(sensor.percent)
            except Exception:  # pragma: no cover - defensive
                report.errors.append("psutil no pudo leer el sensor de bateria.")

        xml_path = self._reports_dir() / "battery-report.xml"
        result = run_command(
            ("powercfg", "/batteryreport", "/output", str(xml_path), "/XML"),
            timeout=_BATTERY_REPORT_TIMEOUT,
        )
        design = full = cycles = None
        if result.success and xml_path.is_file():
            try:
                xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
                design = _last_tag_int(xml_text, "DesignCapacity")
                full = _last_tag_int(xml_text, "FullChargeCapacity")
                cycles = _last_tag_int(xml_text, "CycleCount")
            finally:
                try:
                    xml_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            report.errors.append(
                f"No se pudo generar el informe de bateria: "
                f"{result.stderr.strip() or 'powercfg fallo'}."
            )

        if design and design > 0:
            report.design_capacity_mwh = design
        if full and full > 0:
            report.full_charge_capacity_mwh = full
        if cycles and cycles > 0:
            report.cycle_count = cycles

        if report.design_capacity_mwh is None:
            self._battery_xml_fallback(report)

        design_v = report.design_capacity_mwh
        full_v = report.full_charge_capacity_mwh
        if design_v and full_v and design_v > 0:
            report.present = True
            report.wear_percent = round(max((design_v - full_v) / design_v * 100.0, 0.0), 1)

        if not report.present:
            report.present = self._battery_presence_fallback(report)

        if not report.present:
            report.status_message = "Sin bateria detectada (equipo de escritorio?)."
        elif report.wear_percent is None:
            report.status_message = "Bateria presente sin datos de desgaste."
        elif report.wear_percent <= 20.0:
            report.status_message = "Bateria en excelente estado."
        elif report.wear_percent <= 40.0:
            report.status_message = "Bateria con desgaste aceptable."
        else:
            report.status_message = "Bateria degradada: considere reemplazo."
        return report

    def get_windows_product_key(self) -> ProductKeyInfo:
        """Recover the OEM Windows key embedded in BIOS/UEFI.

        The key comes from the CIM class ``SoftwareLicensingService`` property
        ``OA3xOriginalProductKey``; the friendly edition name is read from the
        registry in-process. Machines without an embedded key (custom builds,
        volume licensing) return ``product_key=None`` WITHOUT errors.

        Returns:
            A :class:`ProductKeyInfo` with the recovered key and edition.
        """
        ensure_windows()
        info = ProductKeyInfo()

        result = run_command(
            (
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance -ClassName SoftwareLicensingService "
                ").OA3xOriginalProductKey",
            ),
            timeout=_WMI_QUERY_TIMEOUT,
        )
        if result.success:
            candidate = result.stdout.strip()
            if candidate and re.fullmatch(r"[A-Z0-9]{5}(-[A-Z0-9]{5}){4}", candidate):
                info.product_key = candidate
        else:
            info.errors.append(
                f"No se pudo consultar la licencia OEM: "
                f"{result.stderr.strip() or result.error or 'error desconocido'}."
            )

        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as handle:
                value, _ = winreg.QueryValueEx(handle, "ProductName")
                info.edition = str(value)
        except OSError:
            info.edition = ""

        return info

    def generate_quote_pdf(
        self,
        services_list: Sequence[Union[QuoteService, tuple]],
        total_amount: float,
        path: Optional[Path] = None,
    ) -> Path:
        """Build a branded quote/receipt PDF for the performed services.

        Args:
            services_list: Billable lines; each item is a :class:`QuoteService`
                or a ``(description, amount)`` / ``(description, detail,
                amount)`` tuple.
            total_amount: Grand total rendered in the summary row.
            path: Destination PDF; defaults to
                ``Documents\\DanTechStudio\\presupuestos\\presupuesto_<stamp>.pdf``.

        Returns:
            The path where the PDF was written.

        Raises:
            ProcessRunnerError: When fpdf2 is missing, the list is empty or
                the destination cannot be written.
        """
        if FPDF is None:
            raise ProcessRunnerError("fpdf2 no esta instalado; no se puede generar el PDF.")
        if not services_list:
            raise ProcessRunnerError(
                "El presupuesto requiere al menos un servicio en la lista."
            )

        normalized: list[QuoteService] = []
        for item in services_list:
            if isinstance(item, QuoteService):
                normalized.append(item)
            elif isinstance(item, (tuple, list)):
                if len(item) >= 3:
                    normalized.append(QuoteService(str(item[0]), str(item[1]), float(item[2])))
                elif len(item) == 2:
                    normalized.append(QuoteService(str(item[0]), "", float(item[1])))
                else:
                    raise ProcessRunnerError("Servicio con formato invalido en la lista.")
            else:
                raise ProcessRunnerError("Servicio con tipo invalido en la lista.")

        if path is None:
            documents = Path(os.environ.get("USERPROFILE", "")) / "Documents"
            path = documents / "DanTechStudio" / "presupuestos"
        path = Path(path)
        if path.suffix.lower() != ".pdf":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = path / f"presupuesto_{stamp}.pdf"

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.set_margins(_PDF_MARGIN_X, 14, _PDF_MARGIN_X)
        pdf.add_page()

        pdf.set_font("helvetica", style="B", size=16)
        pdf.set_text_color(*_PDF_HEADER_BLUE)
        pdf.multi_cell(
            w=0,
            h=8,
            text=_pdf_sanitize("DanTech Studio - Presupuesto y Comprobante Técnico"),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(2)
        pdf.set_draw_color(6, 182, 212)
        pdf.set_line_width(0.8)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

        pdf.set_font("helvetica", size=11)
        pdf.set_text_color(*_PDF_TEXT_DARK)
        pdf.multi_cell(
            w=0,
            h=6,
            text=_pdf_sanitize(f"Técnico: {TECHNICIAN_NAME}   |   {CONTACT_WHATSAPP}"),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(*_PDF_GREEN)
        pdf.multi_cell(
            w=0,
            h=6,
            text=_pdf_sanitize(CONTACT_WEB),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(
            w=0,
            h=6,
            text=_pdf_sanitize(
                f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}   |   "
                f"Equipo: {socket.gethostname()}"
            ),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(5)

        pdf.set_font("helvetica", style="B", size=12)
        pdf.set_text_color(*_PDF_HEADER_BLUE)
        pdf.multi_cell(
            w=0,
            h=7,
            text=_pdf_sanitize("Detalle de Servicios"),
            align="L",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(1)

        pdf.set_font("helvetica", style="B", size=10)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*_PDF_HEADER_BLUE)
        pdf.cell(130, 8, _pdf_sanitize("Concepto"), border=1, align="L", fill=True)
        pdf.cell(50, 8, _pdf_sanitize("Importe"), border=1, align="R", fill=True)
        pdf.ln(8)

        pdf.set_font("helvetica", size=10)
        pdf.set_text_color(*_PDF_TEXT_DARK)
        for index, service in enumerate(normalized):
            concept = service.description
            if service.detail:
                concept = f"{service.description} ({service.detail})"
            fill = index % 2 == 1
            if fill:
                pdf.set_fill_color(*_PDF_ROW_ALT)
            pdf.cell(130, 8, _pdf_sanitize(concept), border=1, align="L", fill=fill)
            pdf.cell(50, 8, _pdf_sanitize(_format_amount(service.amount)), border=1, align="R", fill=fill)
            pdf.ln(8)

        pdf.set_font("helvetica", style="B", size=11)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*_PDF_HEADER_BLUE)
        pdf.cell(130, 9, _pdf_sanitize("TOTAL A PAGAR"), border=1, align="L", fill=True)
        pdf.cell(
            50,
            9,
            _pdf_sanitize(_format_amount(float(total_amount))),
            border=1,
            align="R",
            fill=True,
        )
        pdf.ln(12)

        pdf.set_font("helvetica", size=8)
        pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(
            w=0,
            h=5,
            text=_pdf_sanitize(
                "Documento generado por DanTech Studio. Presupuesto valido por 7 dias. "
                f"Contacto directo: {CONTACT_WHATSAPP} - {CONTACT_WEB}"
            ),
            align="C",
            new_x="LMARGIN",
            new_y="NEXT",
        )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pdf.output(str(path))
        except OSError as exc:
            raise ProcessRunnerError(
                f"No se pudo escribir el presupuesto PDF en {path}: {exc}"
            ) from exc
        return path

    # ----------------------------------------------------------- public async

    def get_battery_health_async(
        self,
        on_complete: Callable[[BatteryHealth], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_battery_health`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`BatteryHealth` when done.
            on_error: Called with the exception when the diagnosis raises.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            try:
                result = self.get_battery_health()
            except Exception as exc:  # pragma: no cover - defensive
                if on_error is not None:
                    on_error(exc)
                return
            on_complete(result)

        thread = threading.Thread(target=_worker, name="battery-health", daemon=True)
        thread.start()
        return thread

    def get_windows_product_key_async(
        self,
        on_complete: Callable[[ProductKeyInfo], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``get_windows_product_key`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`ProductKeyInfo` when done.
            on_error: Called with the exception when the query raises.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            try:
                result = self.get_windows_product_key()
            except Exception as exc:  # pragma: no cover - defensive
                if on_error is not None:
                    on_error(exc)
                return
            on_complete(result)

        thread = threading.Thread(target=_worker, name="product-key", daemon=True)
        thread.start()
        return thread

    def generate_quote_pdf_async(
        self,
        services_list: Sequence[Union[QuoteService, tuple]],
        total_amount: float,
        path: Optional[Path],
        on_complete: Callable[[Path], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``generate_quote_pdf`` in a daemon thread without blocking the GUI.

        Args:
            services_list: Billable lines (see :meth:`generate_quote_pdf`).
            total_amount: Grand total rendered in the summary row.
            path: Destination PDF (``None`` uses the default folder).
            on_complete: Called with the written :class:`~pathlib.Path`.
            on_error: Called with the exception when generation fails.

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            try:
                result = self.generate_quote_pdf(services_list, total_amount, path)
            except Exception as exc:  # pragma: no cover - defensive
                if on_error is not None:
                    on_error(exc)
                return
            on_complete(result)

        thread = threading.Thread(target=_worker, name="quote-pdf", daemon=True)
        thread.start()
        return thread
