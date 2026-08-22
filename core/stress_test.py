"""stress_test.py - StressTest: short CPU/RAM stability test with thermal guard.

Windows-only core module. Runs a bounded synthetic load (default 60-180 s)
on daemon threads while a monitor loop samples CPU/RAM usage and temperature.
When a valid temperature source reports >= ``temp_limit_c`` (85 C by default)
the load is aborted PREVENTIVELY so ventilation or thermal-paste failures are
caught before any repair work is attempted.

Thermal sources, best effort and in order:
  1. ``psutil.sensors_temperatures`` (not implemented on most Windows builds).
  2. WMI ``MSAcpI_ThermalZoneTemperature`` (root/wmi), tenths of Kelvin,
     polled through PowerShell with a throttled cadence because spawning
     PowerShell on a saturated machine is expensive.

Every thread is a daemon: closing the application always kills the load.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from utils.process_runner import ensure_windows, run_command

try:
    import psutil
except ImportError:  # pragma: no cover - guarded so the module still imports
    psutil = None  # type: ignore[assignment]

#: Thermal-zone WMI query; CurrentTemperature is in tenths of Kelvin.
_THERMAL_ZONE_COMMAND = (
    "powershell.exe",
    "-NoProfile",
    "-Command",
    "@(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
    "-ErrorAction SilentlyContinue | ForEach-Object { $_.CurrentTemperature } "
    "| Measure-Object -Maximum).Maximum | ConvertTo-Json -Compress",
)

#: Consecutive failed reads before the sensor is declared unavailable.
_MAX_SENSOR_FAILURES = 3

#: Minimum seconds between expensive WMI thermal polls.
_TEMP_POLL_INTERVAL_S = 5.0


@dataclass
class StressConfig:
    """Parameters of one stress-test run."""

    duration_seconds: int = 60
    cpu_workers: int = field(default_factory=lambda: max(1, os.cpu_count() or 2))
    ram_target_mb: int = 512
    temp_limit_c: float = 85.0
    sample_interval_s: float = 2.0


@dataclass
class StressSample:
    """One live telemetry point emitted during the test."""

    elapsed_s: float
    cpu_percent: float
    ram_percent: float
    temperature_c: Optional[float]


@dataclass
class StressReport:
    """Final stability verdict of one stress run.

    ``outcome`` is one of ``completed``, ``thermal-abort``, ``user-abort``
    or ``error``; ``verdict`` carries the Spanish user-facing conclusion.
    """

    outcome: str = "completed"
    verdict: str = ""
    duration_s: float = 0.0
    max_temperature_c: Optional[float] = None
    avg_cpu_percent: float = 0.0
    peak_ram_percent: float = 0.0
    samples: int = 0
    thermal_sensor_ok: bool = False
    warnings: list[str] = field(default_factory=list)


class StressTest:
    """Bounded CPU/RAM load generator with preventive thermal abort.

    One instance runs ONE test at a time; ``start`` refuses concurrent runs.
    All worker threads are daemons, so destroying the window can never leave
    a burning load behind.
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._user_aborted = False
        self._running = False
        self._lock = threading.Lock()
        self._samples: list[StressSample] = []
        self._ram_blocks: list[bytes] = []

    # ------------------------------------------------------------ properties

    @property
    def running(self) -> bool:
        """True while a stress test is executing."""
        return self._running

    # ----------------------------------------------------------- public API

    def start(
        self,
        config: Optional[StressConfig] = None,
        on_sample: Optional[Callable[[StressSample], None]] = None,
        on_complete: Optional[Callable[[StressReport], None]] = None,
    ) -> bool:
        """Launch the load and its monitor on daemon threads.

        Args:
            config: Run parameters; defaults to :class:`StressConfig`.
            on_sample: Called from the monitor thread for every sample;
                GUI callers MUST marshal updates via ``widget.after``.
            on_complete: Called with the final :class:`StressReport`.

        Returns:
            True when the test started, False when another run is active.
        """
        ensure_windows()
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._user_aborted = False
            self._stop_event.clear()
            self._samples = []
            self._ram_blocks = []

        cfg = config or StressConfig()
        threading.Thread(
            target=self._monitor,
            args=(cfg, on_sample, on_complete),
            name="stress-monitor",
            daemon=True,
        ).start()
        for index in range(max(1, cfg.cpu_workers)):
            threading.Thread(
                target=self._cpu_worker,
                name=f"stress-cpu-{index}",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._ram_holder,
            args=(cfg,),
            name="stress-ram",
            daemon=True,
        ).start()
        return True

    def stop(self) -> None:
        """Abort the current run (panic button) and release resources."""
        self._user_aborted = True
        self._stop_event.set()

    # ---------------------------------------------------------------- internals

    def _cpu_worker(self) -> None:
        """Burn CPU cycles until the stop event fires."""
        x = 1.000001
        while not self._stop_event.is_set():
            for _ in range(20000):
                x = (x * x) % 999983.0
            time.sleep(0.005)

    def _ram_holder(self, cfg: StressConfig) -> None:
        """Progressively allocate and hold RAM up to the configured target."""
        chunk_mb = 16
        blocks_needed = max(0, cfg.ram_target_mb // chunk_mb)
        for _ in range(blocks_needed):
            if self._stop_event.is_set():
                break
            try:
                self._ram_blocks.append(bytes(chunk_mb * 1024 * 1024))
            except MemoryError:
                break
            time.sleep(0.15)
        while not self._stop_event.is_set():
            time.sleep(0.25)
        self._ram_blocks.clear()

    def _read_temperature_wmi(self) -> Optional[float]:
        """Read the hottest ACPI thermal zone through PowerShell.

        Returns:
            Degrees Celsius, or None when no zone reports a value.
        """
        try:
            result = run_command(_THERMAL_ZONE_COMMAND, timeout=10.0)
        except Exception:  # pragma: no cover - defensive
            return None
        text = result.stdout.strip().strip('"')
        if not result.success or not text or text.lower() == "null":
            return None
        try:
            raw_tenths_kelvin = float(text)
        except ValueError:
            return None
        celsius = raw_tenths_kelvin / 10.0 - 273.15
        if celsius < -20.0 or celsius > 150.0:
            return None
        return round(celsius, 1)

    def _read_temperature_psutil(self) -> Optional[float]:
        """Best-effort psutil thermal read (rarely available on Windows)."""
        if psutil is None:
            return None
        getter = getattr(psutil, "sensors_temperatures", None)
        if getter is None:
            return None
        try:
            readings = getter() or {}
        except (AttributeError, NotImplementedError, OSError):
            return None
        hottest: Optional[float] = None

        def _offer(value: float, source: str) -> None:
            nonlocal hottest
            if "cpu" in source or "core" in source or "package" in source:
                hottest = value if hottest is None else max(hottest, value)
            elif hottest is None:
                hottest = value

        for chip, entries in readings.items():
            for entry in entries:
                current = getattr(entry, "current", None)
                if current is None:
                    continue
                try:
                    value = float(current)
                except (TypeError, ValueError):
                    continue
                label = f"{chip} {getattr(entry, 'label', '') or ''}".lower()
                _offer(value, label)
        return hottest

    def _monitor(
        self,
        cfg: StressConfig,
        on_sample: Optional[Callable[[StressSample], None]],
        on_complete: Optional[Callable[[StressReport], None]],
    ) -> None:
        """Sample telemetry, enforce the thermal limit and build the report."""
        started = time.monotonic()
        deadline = started + max(5, int(cfg.duration_seconds))
        outcome = "completed"
        warnings: list[str] = []
        sensor_failures = 0
        last_temp_poll = 0.0
        temperature: Optional[float] = None

        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:  # pragma: no cover - defensive
                pass

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                break

            cpu_pct = ram_pct = 0.0
            if psutil is not None:
                try:
                    cpu_pct = float(psutil.cpu_percent(interval=None) or 0.0)
                    ram_pct = float(psutil.virtual_memory().percent or 0.0)
                except Exception:  # pragma: no cover - defensive
                    pass

            if now - last_temp_poll >= _TEMP_POLL_INTERVAL_S:
                last_temp_poll = now
                reading = self._read_temperature_psutil()
                if reading is None:
                    reading = self._read_temperature_wmi()
                if reading is None:
                    sensor_failures += 1
                    if sensor_failures == _MAX_SENSOR_FAILURES:
                        warnings.append(
                            "Sensor termico no disponible: el test continua sin "
                            "aborto automatico por temperatura."
                        )
                else:
                    sensor_failures = 0
                    temperature = reading

            sample = StressSample(
                elapsed_s=round(now - started, 2),
                cpu_percent=cpu_pct,
                ram_percent=ram_pct,
                temperature_c=temperature,
            )
            with self._lock:
                self._samples.append(sample)
            if on_sample is not None:
                try:
                    on_sample(sample)
                except Exception:  # pragma: no cover - callback safety
                    pass

            if (
                temperature is not None
                and temperature >= cfg.temp_limit_c
            ):
                outcome = "thermal-abort"
                self._stop_event.set()
                break

            self._stop_event.wait(cfg.sample_interval_s)

        duration = time.monotonic() - started
        if outcome != "thermal-abort" and self._user_aborted:
            outcome = "user-abort"

        with self._lock:
            samples = list(self._samples)
        self._ram_blocks.clear()
        self._stop_event.set()

        temps = [s.temperature_c for s in samples if s.temperature_c is not None]
        cpus = [s.cpu_percent for s in samples]
        rams = [s.ram_percent for s in samples]

        report = StressReport(
            outcome=outcome,
            duration_s=duration,
            max_temperature_c=max(temps) if temps else None,
            avg_cpu_percent=(sum(cpus) / len(cpus)) if cpus else 0.0,
            peak_ram_percent=max(rams) if rams else 0.0,
            samples=len(samples),
            thermal_sensor_ok=bool(temps),
            warnings=warnings,
        )
        report.verdict = self._build_verdict(report)
        self._running = False
        if on_complete is not None:
            try:
                on_complete(report)
            except Exception:  # pragma: no cover - callback safety
                pass

    @staticmethod
    def _build_verdict(report: StressReport) -> str:
        """Compose the Spanish stability verdict shown to the technician."""
        if report.outcome == "thermal-abort":
            temp = f"{report.max_temperature_c:.0f} °C" if report.max_temperature_c else "?"
            return (
                f"INESTABLE: aborto térmico preventivo a {temp}. "
                "Revise ventilación y pasta térmica antes de continuar."
            )
        if report.outcome == "user-abort":
            return (
                f"Detenido por el usuario a los {report.duration_s:.0f} s "
                "sin conclusión de estabilidad."
            )
        if report.outcome == "error":
            return "El test terminó con errores internos."
        peak = (
            f", pico térmico {report.max_temperature_c:.0f} °C"
            if report.max_temperature_c is not None
            else ""
        )
        if not report.thermal_sensor_ok:
            return (
                f"COMPLETADO en {report.duration_s:.0f} s sin datos térmicos "
                "del equipo: monitoree temperaturas manualmente."
            )
        return (
            f"ESTABLE: {report.duration_s:.0f} s completados{peak}; "
            "sin signos de sobrecalentamiento."
        )
