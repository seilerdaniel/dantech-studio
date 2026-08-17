"""main.py - DanTech Studio entry point.

Boots the desktop suite: dark-mode CustomTkinter window with sidebar
navigation and the hardware telemetry dashboard. All system work is
delegated to core modules through utils/process_runner; this file only
starts the UI and resolves the bundled maintenance script.
"""

from __future__ import annotations

from gui.main_window import DanTechStudioApp
from utils.process_runner import get_resource_path


def main() -> None:
    """Create the main window and start the Tk event loop."""
    app = DanTechStudioApp(script_path=get_resource_path("scripts/optimize_windows.ps1"))
    app.mainloop()


if __name__ == "__main__":
    main()