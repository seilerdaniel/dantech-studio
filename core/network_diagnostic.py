"""network_diagnostic.py - NetworkDiagnostic: ping diagnostics and network reset.

Windows-only core module. Ping measurement runs through PowerShell's
``Test-Connection`` so results are locale-independent (the JSON payload is
parsed instead of the localized text of ping.exe). The network reset sequence
chains ``ipconfig``/``netsh`` commands through ``utils.process_runner``; every
step keeps its own :class:`CommandResult` and a failure never aborts the rest.

Note: ``/release``, ``/renew`` and the ``netsh`` resets require Administrator
privileges and can momentarily cut the network connection.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from utils.process_runner import CommandResult, ensure_windows, run_command

#: PowerShell script that returns a compact JSON summary for one ping target.
_PING_SCRIPT = (
    "$r = Test-Connection -ComputerName {host} -Count {count} -ErrorAction SilentlyContinue; "
    "[pscustomobject]@{{ Received = @($r).Count; AvgMs = if (@($r).Count) "
    "{{ [math]::Round((@($r) | Measure-Object -Property ResponseTime -Average).Average, 1) }} "
    "else {{ 0 }} }} | ConvertTo-Json -Compress"
)

#: Default targets offered by ``ping_hosts``.
DEFAULT_PING_HOSTS = ("8.8.8.8", "1.1.1.1")


@dataclass
class PingResult:
    """Outcome of pinging one host."""

    host: str
    sent: int
    received: int
    loss_percent: float
    avg_ms: float
    errors: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    """One command step of a multi-step operation."""

    label: str
    result: CommandResult


@dataclass
class NetworkResetResult:
    """Outcome of the full network stack reset sequence."""

    steps: list[StepResult] = field(default_factory=list)
    success: bool = False
    elapsed: float = 0.0


def _run_in_thread(
    fn: Callable[[], object],
    on_complete: Callable[[object], None],
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


class NetworkDiagnostic:
    """Ping hosts and reset the Windows network stack."""

    # ------------------------------------------------------------ public: sync

    def ping(self, host: str, count: int = 4, timeout: float = 120.0) -> PingResult:
        """Ping one host and return loss/latency numbers.

        The measurement runs through PowerShell ``Test-Connection`` and the
        result is parsed from JSON, so no localization differences affect the
        logic. A missing or malformed payload degrades to full loss.

        Args:
            host: Target host name or IP address.
            count: Number of probes to send.
            timeout: Wall-clock limit for the whole ping in seconds.

        Returns:
            A :class:`PingResult` with sent/received counts, loss percentage
            and average round-trip time in milliseconds.
        """
        ensure_windows()
        errors: list[str] = []
        received = 0
        avg_ms = 0.0

        safe_count = max(count, 0)
        script = _PING_SCRIPT.format(host=host, count=safe_count)
        command = ("powershell.exe", "-NoProfile", "-Command", script)
        completed = run_command(command, timeout=timeout)

        if completed.success:
            try:
                payload = json.loads((completed.stdout or "").strip())
                if isinstance(payload, list) and payload:
                    payload = payload[0]
                if isinstance(payload, dict):
                    received = int(payload.get("Received", 0) or 0)
                    avg_ms = float(payload.get("AvgMs", 0.0) or 0.0)
                else:
                    received = 0
                    avg_ms = 0.0
            except (ValueError, TypeError, json.JSONDecodeError):
                errors.append(
                    f"No se pudo interpretar la respuesta del ping: "
                    f"{(completed.stderr or completed.stdout or '').strip() or 'salida vacia'}"
                )
                received = 0
                avg_ms = 0.0
        else:
            stderr = completed.stderr.strip()
            errors.append(f"El comando ping fallo: {stderr or completed.error or 'codigo de salida no nulo'}")

        if safe_count > 0:
            loss_percent = round((1.0 - received / safe_count) * 100.0, 1)
        else:
            loss_percent = 100.0

        return PingResult(
            host=host,
            sent=safe_count,
            received=received,
            loss_percent=loss_percent,
            avg_ms=avg_ms,
            errors=errors,
        )

    def ping_hosts(self, hosts: Sequence[str], count: int = 4) -> list[PingResult]:
        """Ping several hosts sequentially.

        Args:
            hosts: Target hosts, typically ``8.8.8.8`` and ``1.1.1.1``.
            count: Number of probes per host.

        Returns:
            One :class:`PingResult` per host, in the same order.
        """
        return [self.ping(host, count=count) for host in hosts]

    def reset_network_stack(
        self,
        flush_dns: bool = True,
        release_renew: bool = True,
        ip_reset: bool = True,
        winsock_reset: bool = True,
        timeout: float = 600.0,
    ) -> NetworkResetResult:
        """Run the network reset sequence step by step.

        Steps: ``ipconfig /flushdns``, ``ipconfig /release``, ``ipconfig
        /renew``, ``netsh int ip reset`` and ``netsh winsock reset``. Only the
        enabled flags are executed; a failed step never aborts the remaining
        ones and each keeps its own :class:`CommandResult`.

        Warning: release/renew and the netsh resets require Administrator
        privileges and can momentarily cut the network connection.

        Args:
            flush_dns: Run ``ipconfig /flushdns``.
            release_renew: Run ``ipconfig /release`` then ``ipconfig /renew``.
            ip_reset: Run ``netsh int ip reset``.
            winsock_reset: Run ``netsh winsock reset``.
            timeout: Wall-clock limit per step in seconds.

        Returns:
            A :class:`NetworkResetResult` whose ``success`` is True only when
            every executed step succeeded.
        """
        ensure_windows()
        started = time.monotonic()
        steps: list[StepResult] = []

        if flush_dns:
            steps.append(
                StepResult(label="flushdns", result=run_command(("ipconfig", "/flushdns"), timeout=timeout))
            )
        if release_renew:
            steps.append(
                StepResult(label="release", result=run_command(("ipconfig", "/release"), timeout=timeout))
            )
            steps.append(
                StepResult(label="renew", result=run_command(("ipconfig", "/renew"), timeout=timeout))
            )
        if ip_reset:
            steps.append(
                StepResult(
                    label="ip_reset",
                    result=run_command(("netsh", "int", "ip", "reset"), timeout=timeout),
                )
            )
        if winsock_reset:
            steps.append(
                StepResult(
                    label="winsock_reset",
                    result=run_command(("netsh", "winsock", "reset"), timeout=timeout),
                )
            )

        return NetworkResetResult(
            steps=steps,
            success=all(step.result.success for step in steps),
            elapsed=time.monotonic() - started,
        )

    # ----------------------------------------------------------- public: async

    def ping_async(
        self,
        host: str,
        on_complete: Callable[[PingResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
        count: int = 4,
    ) -> threading.Thread:
        """Run ``ping`` in a daemon thread.

        Args:
            host: Target host name or IP address.
            on_complete: Called with the :class:`PingResult` when done.
            on_error: Called with the exception when the ping raises.
            count: Number of probes to send.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.ping(host, count=count),
            on_complete,
            on_error,
            "network-ping",
        )

    def ping_hosts_async(
        self,
        hosts: Sequence[str],
        on_complete: Callable[[list[PingResult]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``ping_hosts`` in a daemon thread.

        Args:
            hosts: Target hosts to ping sequentially.
            on_complete: Called with the list of :class:`PingResult` when done.
            on_error: Called with the exception when the sequence raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            lambda: self.ping_hosts(hosts),
            on_complete,
            on_error,
            "network-ping-hosts",
        )

    def reset_network_stack_async(
        self,
        on_complete: Callable[[NetworkResetResult], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> threading.Thread:
        """Run ``reset_network_stack`` in a daemon thread.

        Args:
            on_complete: Called with the :class:`NetworkResetResult` when done.
            on_error: Called with the exception when the sequence raises.

        Returns:
            The started daemon thread.
        """
        return _run_in_thread(
            self.reset_network_stack,
            on_complete,
            on_error,
            "network-reset",
        )
