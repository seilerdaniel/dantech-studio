"""build_exe.py - Package DanTech Studio as a portable UAC .exe with PyInstaller.

This script intentionally uses ``subprocess`` directly: it is a build-time
utility, NOT part of the suite runtime, so it does not route through
``utils.process_runner``.

What the flags mean:
  - ``--onefile``: single portable .exe (extracted to a temp dir at runtime).
  - ``--windowed``: GUI app, no console window.
  - ``--uac-admin``: the exe manifest requests elevation, so Windows shows the
    UAC consent dialog automatically when the app is launched.
  - ``--add-data``: embeds ``scripts\\optimize_windows.ps1`` so the packaged
    app can locate it at runtime (Windows path separator is ``;``).

Run from the project root (this file's directory) with PyInstaller installed:
    pip install pyinstaller
    python build_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: Name of the packaged executable (also passed to PyInstaller's ``--name``).
_EXE_NAME = "DanTechStudio"

#: Relative path where PyInstaller places the finished executable.
_EXE_OUTPUT = Path("dist") / f"{_EXE_NAME}.exe"


def _pyinstaller_command() -> list[str]:
    """Build the PyInstaller command line for a portable UAC .exe.

    Returns:
        The command tokens; ``cwd`` must be the build root when executed.
    """
    return [
        "pyinstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--uac-admin",
        "--name",
        _EXE_NAME,
        "--add-data",
        r"scripts\optimize_windows.ps1;scripts",
        "main.py",
    ]


def main() -> None:
    """Run PyInstaller from the build root and print the expected .exe path.

    Exits with a clear message when PyInstaller is not on PATH.
    """
    build_root = Path(__file__).resolve().parent

    if shutil.which("pyinstaller") is None:
        print(
            "Error: PyInstaller no se encontro en el PATH.\n"
            "Instalalo con: pip install pyinstaller",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Empaquetando DanTech Studio desde {build_root} ...")
    subprocess.run(_pyinstaller_command(), cwd=str(build_root), check=True)
    print(f"Build completado. Ejecutable: {build_root / _EXE_OUTPUT}")


if __name__ == "__main__":
    main()
