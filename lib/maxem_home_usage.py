from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .synthetic_home import DomoticzClient, DomoticzReading, RegisterCapture

INSTANTANEOUS_VALUES_REGISTER_NAME = "instantaneous_values"
INSTANTANEOUS_VALUES_REGISTER_ADDRESS = 0x5B00
INSTANTANEOUS_VALUES_REGISTER_LENGTH = 66

INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_ADDRESS = 0x5B14
INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET = INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH = 2
INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE = 0.01


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


def _format_watts(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,} W"
    return f"{value:,.2f} W"


def describe_instantaneous_preview_basis() -> str:
    return (
        "Preview basis: ABB instantaneous active power total lives in 0x5B14/0x5B15 as a signed 0.01 W register. "
        "Domoticz IDX 20 Usage is the live grid-import watt reading we will encode back into that ABB format after "
        "clamping negatives to zero. All other registers in instantaneous_values are copied verbatim from the ABB "
        "source. House load is ignored."
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


def encode_signed_scaled_watts(value_watts: float) -> tuple[int, int]:
    clamped_watts = max(float(value_watts), 0.0)
    raw = int(round(clamped_watts / INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE))
    raw = max(min(raw, 0x7FFFFFFF), 0)
    payload = raw.to_bytes(4, byteorder="big", signed=False)
    return (
        int.from_bytes(payload[:2], byteorder="big"),
        int.from_bytes(payload[2:], byteorder="big"),
    )


def rewrite_instantaneous_values(
    source_values: Sequence[int],
    *,
    usage_watts: float,
) -> tuple[int, ...]:
    values = list(int(value) & 0xFFFF for value in source_values)
    if len(values) < INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH:
        return tuple(values)

    high_word, low_word = encode_signed_scaled_watts(usage_watts)
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET] = high_word
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1] = low_word

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

    return [
        f"ABB source: {_format_watts(source_watts)}",
        f"DZ Usage to Maxem: {_format_watts(usage_watts)}",
    ]
