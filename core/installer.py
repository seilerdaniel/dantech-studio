"""installer.py - Installer: software install/upgrade/search through winget.

Windows-only core module. winget.exe is resolved dynamically via
``utils.process_runner.find_executable`` because its location varies between
Windows versions and installs. Every invocation goes through process_runner
(NEVER raw subprocess).

Note on privileges: winget normally installs per-user and does NOT require
Administrator. No ``is_admin`` gate is applied here; some package installers
may still trigger a UAC prompt internally, which is Windows behavior.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from utils.process_runner import (
    CommandResult,
    ProcessRunnerError,
    ensure_windows,
    find_executable,
    run_command,
    run_command_async,
)

#: Long timeouts because winget downloads packages; upgrade --all can take very long.
_INSTALL_TIMEOUT = 900.0
_UPGRADE_ALL_TIMEOUT = 1800.0

#: Flags reused across install/upgrade commands. ``--disable-interactivity``
#: keeps winget from prompting and avoids msstore source locks
#: (error 0x8a15000f) when running non-interactively.
_AGREEMENT_FLAGS: Sequence[str] = (
    "--silent",
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--disable-interactivity",
)


@dataclass
class WingetResult:
    """Outcome of a single winget operation."""

    operation: str
    package: Optional[str] = None
    result: CommandResult = field(default_factory=CommandResult)


class Installer:
    """Manage software through the Windows Package Manager (winget).

    Sync methods return a :class:`WingetResult`. Async variants deliver the
    same :class:`WingetResult` to both ``on_complete`` and ``on_error`` so the
    GUI always receives the wrapped result instead of a raw CommandResult.
    """

    # ------------------------------------------------------------------ helpers

    def _winget(self) -> Path:
        """Resolve the absolute path of winget.exe.

        Returns:
            Absolute path of the winget executable.

        Raises:
            ProcessRunnerError: When winget.exe cannot be located.
        """
        try:
            found = find_executable(("winget.exe",))
        except OSError as exc:  # pragma: no cover - defensive
            raise ProcessRunnerError(f"No se pudo buscar winget.exe: {exc}") from exc
        if found is None:
            raise ProcessRunnerError(
                "No se encontro winget.exe. Instala el App Installer desde "
                "la Microsoft Store o verifica la instalacion de Windows."
            )
        return found

    def _winget_async(
        self,
        operation: str,
        package: Optional[str],
        command: Sequence[str],
        timeout: Optional[float],
        on_complete: Callable[[WingetResult], None],
        on_error: Optional[Callable[[WingetResult], None]],
    ) -> threading.Thread:
        """Run a winget command in the background, wrapping results in WingetResult.

        Args:
            operation: Label stored in the :class:`WingetResult`.
            package: Package id, or None for all-package operations.
            command: Full winget command tokens.
            timeout: Wall-clock limit in seconds.
            on_complete: Called with the :class:`WingetResult` when finished.
            on_error: Called with the :class:`WingetResult` when it cannot start.

        Returns:
            The started daemon thread.
        """

        def _complete(result: CommandResult) -> None:
            on_complete(WingetResult(operation=operation, package=package, result=result))

        def _error(result: CommandResult) -> None:
            wrapped = WingetResult(operation=operation, package=package, result=result)
            if on_error is not None:
                on_error(wrapped)
            else:
                on_complete(wrapped)

        return run_command_async(command, on_complete=_complete, on_error=_error, timeout=timeout)

    def _spawn_winget_result(
        self,
        result: WingetResult,
        on_complete: Callable[[WingetResult], None],
        on_error: Optional[Callable[[WingetResult], None]],
    ) -> threading.Thread:
        """Deliver a precomputed WingetResult through the async callback contract.

        Args:
            result: Already-built :class:`WingetResult` (e.g. resolution failure).
            on_complete: Completion callback.
            on_error: Failure callback (receives the wrapped result).

        Returns:
            The started daemon thread.
        """

        def _worker() -> None:
            if on_error is not None:
                on_error(result)
            else:
                on_complete(result)

        thread = threading.Thread(target=_worker, name="winget-immediate", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------ public: sync

    def install(self, package: str, exact: bool = True) -> WingetResult:
        """Install a package by its winget id.

        Args:
            package: Winget package id, e.g. ``Microsoft.PowerToys``.
            exact: Add ``--exact`` so the id is matched exactly.

        Returns:
            A :class:`WingetResult` for operation ``install``.

        Raises:
            ProcessRunnerError: When ``package`` is empty or winget is missing.
        """
        ensure_windows()
        if not package or not package.strip():
            raise ProcessRunnerError("install requiere un id de paquete valido.")
        exe = self._winget()

        command: list[str] = [
            str(exe),
            "install",
            "--id",
            package.strip(),
            *_AGREEMENT_FLAGS,
        ]
        if exact:
            command.append("--exact")

        result = run_command(command, timeout=_INSTALL_TIMEOUT)
        return WingetResult(operation="install", package=package.strip(), result=result)

    def upgrade(self, package: str) -> WingetResult:
        """Upgrade a single package by its winget id.

        Args:
            package: Winget package id to upgrade.

        Returns:
            A :class:`WingetResult` for operation ``upgrade``.

        Raises:
            ProcessRunnerError: When ``package`` is empty or winget is missing.
        """
        ensure_windows()
        if not package or not package.strip():
            raise ProcessRunnerError("upgrade requiere un id de paquete valido.")
        exe = self._winget()

        command = [
            str(exe),
            "upgrade",
            "--id",
            package.strip(),
            *_AGREEMENT_FLAGS,
        ]
        result = run_command(command, timeout=_INSTALL_TIMEOUT)
        return WingetResult(operation="upgrade", package=package.strip(), result=result)

    def upgrade_all(self) -> WingetResult:
        """Upgrade every installed package that has a pending update.

        Returns:
            A :class:`WingetResult` for operation ``upgrade_all`` with
            ``package=None``.
        """
        ensure_windows()
        exe = self._winget()

        command = [str(exe), "upgrade", "--all", *_AGREEMENT_FLAGS]
        result = run_command(command, timeout=_UPGRADE_ALL_TIMEOUT)
        return WingetResult(operation="upgrade_all", package=None, result=result)

    def search(self, query: str) -> WingetResult:
        """Search the winget sources for packages matching a query.

        The table output is kept readable, so ``--silent`` is intentionally
        NOT used here.

        Args:
            query: Free-text search term.

        Returns:
            A :class:`WingetResult` for operation ``search``.

        Raises:
            ProcessRunnerError: When ``query`` is empty or winget is missing.
        """
        ensure_windows()
        if not query or not query.strip():
            raise ProcessRunnerError("search requiere un termino de busqueda.")
        exe = self._winget()

        command = [str(exe), "search", "--query", query.strip()]
        result = run_command(command)
        return WingetResult(operation="search", package=query.strip(), result=result)

    def list_installed(self) -> WingetResult:
        """List the software currently installed/managed by winget.

        Returns:
            A :class:`WingetResult` for operation ``list`` with ``package=None``.
        """
        ensure_windows()
        exe = self._winget()

        result = run_command((str(exe), "list"))
        return WingetResult(operation="list", package=None, result=result)

    # ----------------------------------------------------------- public: async

    def install_async(
        self,
        package: str,
        exact: bool = True,
        on_complete: Callable[[WingetResult], None] | None = None,
        on_error: Optional[Callable[[WingetResult], None]] = None,
    ) -> threading.Thread:
        """Run ``install`` in a background thread (network operation).

        Args:
            package: Winget package id.
            exact: Add ``--exact`` for an exact id match.
            on_complete: Called with the :class:`WingetResult` when finished.
            on_error: Called with the :class:`WingetResult` when it cannot start.

        Returns:
            The started daemon thread.

        Raises:
            ProcessRunnerError: When ``package`` is empty.
        """
        ensure_windows()
        if not package or not package.strip():
            raise ProcessRunnerError("install_async requiere un id de paquete valido.")
        try:
            exe = self._winget()
        except ProcessRunnerError as exc:
            return self._spawn_winget_result(
                WingetResult(
                    operation="install",
                    package=package,
                    result=CommandResult(stderr=str(exc), success=False, error=str(exc)),
                ),
                on_complete,
                on_error,
            )

        command: list[str] = [str(exe), "install", "--id", package.strip(), *_AGREEMENT_FLAGS]
        if exact:
            command.append("--exact")
        return self._winget_async(
            "install", package.strip(), command, _INSTALL_TIMEOUT, on_complete, on_error
        )

    def upgrade_async(
        self,
        package: str,
        on_complete: Callable[[WingetResult], None] | None = None,
        on_error: Optional[Callable[[WingetResult], None]] = None,
    ) -> threading.Thread:
        """Run ``upgrade`` in a background thread (network operation).

        Args:
            package: Winget package id to upgrade.
            on_complete: Called with the :class:`WingetResult` when finished.
            on_error: Called with the :class:`WingetResult` when it cannot start.

        Returns:
            The started daemon thread.

        Raises:
            ProcessRunnerError: When ``package`` is empty.
        """
        ensure_windows()
        if not package or not package.strip():
            raise ProcessRunnerError("upgrade_async requiere un id de paquete valido.")
        try:
            exe = self._winget()
        except ProcessRunnerError as exc:
            return self._spawn_winget_result(
                WingetResult(
                    operation="upgrade",
                    package=package,
                    result=CommandResult(stderr=str(exc), success=False, error=str(exc)),
                ),
                on_complete,
                on_error,
            )

        command = [str(exe), "upgrade", "--id", package.strip(), *_AGREEMENT_FLAGS]
        return self._winget_async(
            "upgrade", package.strip(), command, _INSTALL_TIMEOUT, on_complete, on_error
        )

    def upgrade_all_async(
        self,
        on_complete: Callable[[WingetResult], None],
        on_error: Optional[Callable[[WingetResult], None]] = None,
    ) -> threading.Thread:
        """Run ``upgrade_all`` in a background thread (network operation).

        Args:
            on_complete: Called with the :class:`WingetResult` when finished.
            on_error: Called with the :class:`WingetResult` when it cannot start.

        Returns:
            The started daemon thread.
        """
        ensure_windows()
        try:
            exe = self._winget()
        except ProcessRunnerError as exc:
            return self._spawn_winget_result(
                WingetResult(
                    operation="upgrade_all",
                    package=None,
                    result=CommandResult(stderr=str(exc), success=False, error=str(exc)),
                ),
                on_complete,
                on_error,
            )

        command = [str(exe), "upgrade", "--all", *_AGREEMENT_FLAGS]
        return self._winget_async(
            "upgrade_all", None, command, _UPGRADE_ALL_TIMEOUT, on_complete, on_error
        )