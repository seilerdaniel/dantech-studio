"""main.py - DanTech Studio entry point.

Boots the desktop suite: dark-mode CustomTkinter window with sidebar
navigation and the hardware telemetry dashboard. All system work is
delegated to core modules through utils/process_runner; this file only
starts the UI and resolves the bundled maintenance script.

Headless mode: when launched with ``--mantenimiento-expreso`` (used by the
scheduled task created in core/task_scheduler.py) it runs the express
maintenance profile silently and exits without opening any window.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from gui.main_window import DanTechStudioApp
from core.memory_optimizer import MemoryOptimizer
from utils import process_runner
from utils.process_runner import get_resource_path

#: Log file for the unattended runs (one line per execution).
_SILENT_LOG = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "DanTechStudio"
    / "logs"
    / "mantenimiento_automatico.log"
)


def _run_silent_express() -> None:
    """Run the 1-click maintenance profile without any GUI.

    Mirrors the Dashboard 'Mantenimiento Express' sequence: temp cleanup +
    RAM trim, DNS cache flush, recycle bin and the bundled PowerShell
    optimizer script. Failures are accumulated into a log file instead of
    crashing the scheduled task.
    """
    started = time.monotonic()
    warnings: list[str] = []
    freed_bytes = 0.0

    try:
        combined = MemoryOptimizer().optimize_all()
        freed_bytes += sum(float(item.bytes_freed) for item in combined.temp_reports)
        if combined.ram is not None:
            freed_bytes += combined.ram.working_set_trimmed_mb * (1024.0 ** 2)
        warnings.extend(combined.errors)
    except Exception as exc:
        warnings.append(f"Optimizador de memoria: {exc}")

    try:
        dns = process_runner.run_command(["ipconfig", "/flushdns"])
        if not dns.success:
            warnings.append(dns.stderr.strip() or "ipconfig /flushdns falló.")
    except Exception as exc:
        warnings.append(f"flushdns: {exc}")

    try:
        recycle = process_runner.run_command(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
            ]
        )
        if not recycle.success:
            warnings.append(recycle.stderr.strip() or "No se pudo vaciar la papelera.")
    except Exception as exc:
        warnings.append(f"papelera: {exc}")

    script_path = Path(get_resource_path("scripts/optimize_windows.ps1"))
    if not script_path.is_file():
        warnings.append(f"No se encontró el script de mantenimiento en {script_path}.")
    else:
        powershell = process_runner.find_executable(("powershell.exe",))
        if powershell is None:
            warnings.append("No se encontró powershell.exe.")
        else:
            result = process_runner.run_command(
                [
                    str(powershell),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path.resolve()),
                ]
            )
            if not result.success:
                warnings.append("El script de optimización no completó correctamente.")

    elapsed = time.monotonic() - started
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if not [w for w in warnings if "requiere" not in w.lower()] else "CON AVISOS"
    line = (
        f"[{stamp}] Mantenimiento automático {status} - "
        f"{freed_bytes / (1024.0 ** 2):.1f} MB liberados en {elapsed:.0f}s"
    )
    for warning in warnings[:5]:
        line += f" | {warning}"

    try:
        _SILENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_SILENT_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def main() -> None:
    """Create the main window and start the Tk event loop."""
    if "--mantenimiento-expreso" in sys.argv[1:]:
        thread = threading.Thread(target=_run_silent_express, name="silent-express")
        thread.start()
        thread.join(timeout=900)
        return
    app = DanTechStudioApp(script_path=get_resource_path("scripts/optimize_windows.ps1"))
    app.mainloop()


if __name__ == "__main__":
    main()
