"""main.py - DanTech Studio entry point.

Boots the desktop suite: dark-mode CustomTkinter window with sidebar
navigation and the hardware telemetry dashboard. All system work is
delegated to core modules through utils/process_runner; this file only
starts the UI.
"""

from __future__ import annotations

from gui.main_window import DanTechStudioApp


def main() -> None:
    """Create the main window and start the Tk event loop."""
    app = DanTechStudioApp()
    app.mainloop()


if __name__ == "__main__":
    main()