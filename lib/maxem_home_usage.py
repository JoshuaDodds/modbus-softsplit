from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .synthetic_home import DomoticzClient, DomoticzReading, RegisterCapture

INSTANTANEOUS_VALUES_REGISTER_NAME = "instantaneous_values"
INSTANTANEOUS_VALUES_REGISTER_ADDRESS = 0x5B00
INSTANTANEOUS_VALUES_REGISTER_LENGTH = 66

INSTANTANEOUS_VOLTAGE_L1_OFFSET = 0
INSTANTANEOUS_VOLTAGE_L2_OFFSET = 2
INSTANTANEOUS_VOLTAGE_L3_OFFSET = 4
INSTANTANEOUS_VOLTAGE_REGISTER_LENGTH = 2
INSTANTANEOUS_VOLTAGE_SCALE = 0.1

INSTANTANEOUS_CURRENT_L1_OFFSET = 12
INSTANTANEOUS_CURRENT_L2_OFFSET = 14
INSTANTANEOUS_CURRENT_L3_OFFSET = 16
INSTANTANEOUS_CURRENT_NEUTRAL_OFFSET = 18
INSTANTANEOUS_CURRENT_REGISTER_LENGTH = 2
INSTANTANEOUS_CURRENT_SCALE = 0.01

INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_ADDRESS = 0x5B14
INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET = INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH = 2
INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE = 0.01

INSTANTANEOUS_ACTIVE_POWER_L1_OFFSET = 22
INSTANTANEOUS_ACTIVE_POWER_L2_OFFSET = 24
INSTANTANEOUS_ACTIVE_POWER_L3_OFFSET = 26
INSTANTANEOUS_ACTIVE_POWER_REGISTER_LENGTH = 2

_PHASE_COUNT = 3


@dataclass(frozen=True)
class DomoticzUsageSnapshot:
    sequence: int
    reading: DomoticzReading | None

    @property
    def grid_import_watts(self) -> float | None:
        if self.reading is None:
            return None
        return max(self.reading.import_watts, 0.0)

    @property
    def usage_watts(self) -> float | None:
        return self.grid_import_watts


class DomoticzUsageCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._reading: DomoticzReading | None = None

    def update(self, reading: DomoticzReading) -> DomoticzUsageSnapshot:
        with self._lock:
            self._sequence += 1
            self._reading = reading
            return DomoticzUsageSnapshot(sequence=self._sequence, reading=self._reading)

    def snapshot(self) -> DomoticzUsageSnapshot:
        with self._lock:
            return DomoticzUsageSnapshot(sequence=self._sequence, reading=self._reading)


class DomoticzUsagePoller(threading.Thread):
    def __init__(
        self,
        client: DomoticzClient,
        cache: DomoticzUsageCache,
        *,
        poll_interval_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(name="domoticz-usage-poller", daemon=True)
        self._client = client
        self._cache = cache
        self._poll_interval_seconds = max(poll_interval_seconds, 0.1)
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self._client.enabled:
            self._logger.info("Domoticz polling is disabled; Maxem preview will stay on the last known reading.")
            return

        while not self._stop_event.is_set():
            try:
                reading = self._client.fetch_reading()
                snapshot = self._cache.update(reading)
                self._logger.debug(
                    "Domoticz usage snapshot updated: sequence=%s grid_import_watts=%.0f export_watts=%.0f",
                    snapshot.sequence,
                    snapshot.grid_import_watts or 0.0,
                    max(reading.export_watts, 0.0),
                )
            except Exception as exc:  # pragma: no cover - defensive log path
                self._logger.warning("Domoticz poll failed: %s", exc)

            self._stop_event.wait(self._poll_interval_seconds)


@dataclass(frozen=True)
class InstantaneousRewritePlan:
    target_total_watts: float
    phase_power_watts: tuple[float, float, float]
    phase_current_amps: tuple[float, float, float]


def _format_watts(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,} W"
    return f"{value:,.2f} W"


def _format_amps(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,} A"
    return f"{value:,.2f} A"


def describe_instantaneous_preview_basis() -> str:
    return (
        "Preview basis: ABB instantaneous active power total lives in 0x5B14/0x5B15 as a signed 0.01 W register, "
        "with phase active power at 0x5B16/0x5B18/0x5B1A and phase current at 0x5B0C/0x5B0E/0x5B10. "
        "Domoticz IDX 20 Usage is the live grid-import watt reading we will encode back into that ABB format after "
        "clamping negatives to zero. House load is ignored."
    )


def _decode_scaled_value(
    register_values: Sequence[int],
    *,
    offset: int,
    scale: float,
    signed: bool,
    register_length: int = 2,
) -> float | None:
    if len(register_values) < offset + register_length:
        return None

    high_word = int(register_values[offset]) & 0xFFFF
    low_word = int(register_values[offset + 1]) & 0xFFFF
    raw = (high_word << 16) | low_word
    if signed and raw & 0x80000000:
        raw -= 0x100000000
    return raw * scale


def decode_signed_scaled_watts(register_values: Sequence[int], *, offset: int = INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET) -> float | None:
    return _decode_scaled_value(
        register_values,
        offset=offset,
        scale=INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE,
        signed=True,
        register_length=INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH,
    )


def decode_unsigned_scaled_volts(register_values: Sequence[int], *, offset: int) -> float | None:
    return _decode_scaled_value(
        register_values,
        offset=offset,
        scale=INSTANTANEOUS_VOLTAGE_SCALE,
        signed=False,
        register_length=INSTANTANEOUS_VOLTAGE_REGISTER_LENGTH,
    )


def decode_unsigned_scaled_amperes(register_values: Sequence[int], *, offset: int) -> float | None:
    return _decode_scaled_value(
        register_values,
        offset=offset,
        scale=INSTANTANEOUS_CURRENT_SCALE,
        signed=False,
        register_length=INSTANTANEOUS_CURRENT_REGISTER_LENGTH,
    )


def encode_signed_scaled_watts(value_watts: float) -> tuple[int, int]:
    clamped_watts = max(float(value_watts), 0.0)
    raw = int(round(clamped_watts / INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE))
    raw = max(min(raw, 0x7FFFFFFF), 0)
    payload = raw.to_bytes(4, byteorder="big", signed=False)
    return (
        int.from_bytes(payload[:2], byteorder="big"),
        int.from_bytes(payload[2:], byteorder="big"),
    )


def encode_unsigned_scaled_amperes(value_amps: float) -> tuple[int, int]:
    clamped_amps = max(float(value_amps), 0.0)
    raw = int(round(clamped_amps / INSTANTANEOUS_CURRENT_SCALE))
    raw = max(min(raw, 0xFFFFFFFF), 0)
    payload = raw.to_bytes(4, byteorder="big", signed=False)
    return (
        int.from_bytes(payload[:2], byteorder="big"),
        int.from_bytes(payload[2:], byteorder="big"),
    )


def _phase_weights(source_values: Sequence[int]) -> tuple[float, float, float]:
    phase_power_offsets = (
        INSTANTANEOUS_ACTIVE_POWER_L1_OFFSET,
        INSTANTANEOUS_ACTIVE_POWER_L2_OFFSET,
        INSTANTANEOUS_ACTIVE_POWER_L3_OFFSET,
    )
    phase_power_values: list[float] = []
    for offset in phase_power_offsets:
        value = decode_signed_scaled_watts(source_values, offset=offset)
        phase_power_values.append(abs(value) if value is not None else 0.0)

    total = sum(phase_power_values)
    if total > 0:
        return tuple(value / total for value in phase_power_values)

    phase_current_offsets = (
        INSTANTANEOUS_CURRENT_L1_OFFSET,
        INSTANTANEOUS_CURRENT_L2_OFFSET,
        INSTANTANEOUS_CURRENT_L3_OFFSET,
    )
    phase_current_values: list[float] = []
    for offset in phase_current_offsets:
        value = decode_unsigned_scaled_amperes(source_values, offset=offset)
        phase_current_values.append(value if value is not None else 0.0)

    total = sum(phase_current_values)
    if total > 0:
        return tuple(value / total for value in phase_current_values)

    return (1.0 / _PHASE_COUNT, 1.0 / _PHASE_COUNT, 1.0 / _PHASE_COUNT)


def _voltage_for_phase(source_values: Sequence[int], offset: int) -> float:
    voltage = decode_unsigned_scaled_volts(source_values, offset=offset)
    if voltage is None or voltage <= 0:
        return 230.0
    return voltage


def build_instantaneous_rewrite_plan(
    source_values: Sequence[int],
    *,
    usage_watts: float,
) -> InstantaneousRewritePlan:
    target_total_watts = max(float(usage_watts), 0.0)
    weights = _phase_weights(source_values)
    phase_power_watts = tuple(target_total_watts * weight for weight in weights)
    phase_voltage_offsets = (
        INSTANTANEOUS_VOLTAGE_L1_OFFSET,
        INSTANTANEOUS_VOLTAGE_L2_OFFSET,
        INSTANTANEOUS_VOLTAGE_L3_OFFSET,
    )
    phase_current_amps = tuple(
        phase_power_watts[index] / _voltage_for_phase(source_values, phase_voltage_offsets[index])
        for index in range(_PHASE_COUNT)
    )
    return InstantaneousRewritePlan(
        target_total_watts=target_total_watts,
        phase_power_watts=phase_power_watts,
        phase_current_amps=phase_current_amps,
    )


def rewrite_instantaneous_values(
    source_values: Sequence[int],
    *,
    usage_watts: float,
) -> tuple[int, ...]:
    values = list(int(value) & 0xFFFF for value in source_values)
    if len(values) < INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH:
        return tuple(values)

    plan = build_instantaneous_rewrite_plan(source_values, usage_watts=usage_watts)

    high_word, low_word = encode_signed_scaled_watts(plan.target_total_watts)
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET] = high_word
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1] = low_word

    phase_power_offsets = (
        INSTANTANEOUS_ACTIVE_POWER_L1_OFFSET,
        INSTANTANEOUS_ACTIVE_POWER_L2_OFFSET,
        INSTANTANEOUS_ACTIVE_POWER_L3_OFFSET,
    )
    for index, offset in enumerate(phase_power_offsets):
        high_word, low_word = encode_signed_scaled_watts(plan.phase_power_watts[index])
        values[offset] = high_word
        values[offset + 1] = low_word

    phase_current_offsets = (
        INSTANTANEOUS_CURRENT_L1_OFFSET,
        INSTANTANEOUS_CURRENT_L2_OFFSET,
        INSTANTANEOUS_CURRENT_L3_OFFSET,
    )
    for index, offset in enumerate(phase_current_offsets):
        high_word, low_word = encode_unsigned_scaled_amperes(plan.phase_current_amps[index])
        values[offset] = high_word
        values[offset + 1] = low_word

    return tuple(values)


def preview_signature(
    capture: RegisterCapture,
    *,
    snapshot: DomoticzUsageSnapshot | None,
) -> tuple[Any, ...]:
    usage_watts = snapshot.grid_import_watts if snapshot else None
    sequence = snapshot.sequence if snapshot else None
    return (
        capture.target_slave,
        capture.source_slave,
        capture.register_name,
        capture.address,
        capture.address_length,
        capture.source_values,
        sequence,
        usage_watts,
    )


def format_instantaneous_preview_lines(
    capture: RegisterCapture,
    *,
    snapshot: DomoticzUsageSnapshot | None,
) -> list[str]:
    if capture.target_slave != 100 or capture.register_name != INSTANTANEOUS_VALUES_REGISTER_NAME:
        return []

    source_watts = decode_signed_scaled_watts(capture.source_values)
    usage_watts = snapshot.grid_import_watts if snapshot else None

    if source_watts is None or usage_watts is None:
        return [
            "ABB source: awaiting baseline",
            "DZ Usage to Maxem: awaiting baseline",
        ]

    plan = build_instantaneous_rewrite_plan(capture.source_values, usage_watts=usage_watts)

    return [
        f"ABB source: {_format_watts(source_watts)}",
        f"DZ Usage to Maxem: {_format_watts(usage_watts)}",
        (
            "DZ Phase amps to Maxem: "
            f"L1={_format_amps(plan.phase_current_amps[0])}, "
            f"L2={_format_amps(plan.phase_current_amps[1])}, "
            f"L3={_format_amps(plan.phase_current_amps[2])}"
        ),
    ]
