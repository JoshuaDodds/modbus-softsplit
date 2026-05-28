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
INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS = 0x5B16
INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS = 0x5B18
INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS = 0x5B1A
INSTANTANEOUS_ACTIVE_POWER_PHASE_OFFSETS = (
    INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
)
INSTANTANEOUS_ACTIVE_POWER_PHASE_LENGTH = 2


@dataclass(frozen=True)
class InstantaneousFieldSpec:
    name: str
    address: int
    scale: float
    unit: str
    signed: bool
    register_length: int = 2

    @property
    def offset(self) -> int:
        return self.address - INSTANTANEOUS_VALUES_REGISTER_ADDRESS


INSTANTANEOUS_FIELD_SPECS: tuple[InstantaneousFieldSpec, ...] = (
    InstantaneousFieldSpec("voltage_l1_n", 0x5B00, 0.1, "V", False),
    InstantaneousFieldSpec("voltage_l2_n", 0x5B02, 0.1, "V", False),
    InstantaneousFieldSpec("voltage_l3_n", 0x5B04, 0.1, "V", False),
    InstantaneousFieldSpec("current_l1", 0x5B0C, 0.01, "A", False),
    InstantaneousFieldSpec("current_l2", 0x5B0E, 0.01, "A", False),
    InstantaneousFieldSpec("current_l3", 0x5B10, 0.01, "A", False),
    InstantaneousFieldSpec("current_n", 0x5B12, 0.01, "A", False),
    InstantaneousFieldSpec("active_power_total", 0x5B14, 0.01, "W", True),
    InstantaneousFieldSpec("active_power_l1", 0x5B16, 0.01, "W", True),
    InstantaneousFieldSpec("active_power_l2", 0x5B18, 0.01, "W", True),
    InstantaneousFieldSpec("active_power_l3", 0x5B1A, 0.01, "W", True),
)


@dataclass(frozen=True)
class DomoticzUsageSnapshot:
    sequence: int
    reading: DomoticzReading | None
    phase_usage_watts: tuple[float, float, float] | None = None
    use_signed_net_power: bool = False

    @property
    def grid_import_watts(self) -> float | None:
        if self.reading is None:
            return None
        return max(self.reading.import_watts, 0.0)

    @property
    def grid_net_watts(self) -> float | None:
        if self.reading is None:
            return None
        return float(self.reading.import_watts) - float(self.reading.export_watts)

    @property
    def rewrite_usage_watts(self) -> float | None:
        if self.reading is None:
            return None
        if self.use_signed_net_power:
            return self.grid_net_watts
        return self.grid_import_watts

    @property
    def usage_watts(self) -> float | None:
        return self.rewrite_usage_watts


class DomoticzUsageCache:
    def __init__(self, *, use_signed_net_power: bool = False) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._reading: DomoticzReading | None = None
        self._phase_usage_watts: tuple[float, float, float] | None = None
        self._use_signed_net_power = bool(use_signed_net_power)

    def update(
        self,
        reading: DomoticzReading,
        *,
        phase_usage_watts: tuple[float, float, float] | None = None,
    ) -> DomoticzUsageSnapshot:
        with self._lock:
            self._sequence += 1
            self._reading = reading
            if phase_usage_watts is not None:
                self._phase_usage_watts = tuple(float(value) for value in phase_usage_watts)
            return DomoticzUsageSnapshot(
                sequence=self._sequence,
                reading=self._reading,
                phase_usage_watts=self._phase_usage_watts,
                use_signed_net_power=self._use_signed_net_power,
            )

    def snapshot(self) -> DomoticzUsageSnapshot:
        with self._lock:
            return DomoticzUsageSnapshot(
                sequence=self._sequence,
                reading=self._reading,
                phase_usage_watts=self._phase_usage_watts,
                use_signed_net_power=self._use_signed_net_power,
            )


class DomoticzUsagePoller(threading.Thread):
    def __init__(
        self,
        client: DomoticzClient,
        cache: DomoticzUsageCache,
        *,
        phase_l1_idx: int = 26,
        phase_l2_idx: int = 25,
        phase_l3_idx: int = 24,
        phase_export_l1_idx: int | None = None,
        phase_export_l2_idx: int | None = None,
        phase_export_l3_idx: int | None = None,
        use_signed_net_power: bool = False,
        poll_interval_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(name="domoticz-usage-poller", daemon=True)
        self._client = client
        self._cache = cache
        self._phase_import_indices = (
            int(phase_l1_idx),
            int(phase_l2_idx),
            int(phase_l3_idx),
        )
        self._phase_export_indices = (
            self._normalize_optional_idx(phase_export_l1_idx),
            self._normalize_optional_idx(phase_export_l2_idx),
            self._normalize_optional_idx(phase_export_l3_idx),
        )
        self._use_signed_net_power = bool(use_signed_net_power)
        self._poll_interval_seconds = max(poll_interval_seconds, 0.1)
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._warned_missing_phase_export_indices = False

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _normalize_optional_idx(value: int | None) -> int | None:
        if value in (None, "", 0):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def _has_phase_export_indices(self) -> bool:
        return all(value is not None for value in self._phase_export_indices)

    def _build_requested_indices(self) -> tuple[int, ...]:
        indices = [self._client.grid_idx, *self._phase_import_indices]
        if self._use_signed_net_power and self._has_phase_export_indices():
            indices.extend(int(value) for value in self._phase_export_indices if value is not None)
        return tuple(indices)

    def run(self) -> None:
        if not self._client.enabled:
            self._logger.info("Domoticz polling is disabled; Maxem preview will stay on the last known reading.")
            return

        while not self._stop_event.is_set():
            try:
                requested_indices = self._build_requested_indices()
                self._logger.debug(
                    "Domoticz batch request: %s",
                    self._client.url_for_indices(requested_indices),
                )
                devices = self._client.fetch_devices(requested_indices)

                reading = self._client.fetch_reading_from_device(devices[self._client.grid_idx])
                phase_import_watts = tuple(
                    self._client.data_watts_from_device(devices[phase_idx])
                    for phase_idx in self._phase_import_indices
                )
                phase_usage_watts = phase_import_watts
                if self._use_signed_net_power:
                    if self._has_phase_export_indices():
                        phase_export_watts = tuple(
                            self._client.data_watts_from_device(devices[phase_idx])
                            for phase_idx in self._phase_export_indices
                            if phase_idx is not None
                        )
                        phase_usage_watts = tuple(
                            float(phase_import_watts[index]) - float(phase_export_watts[index])
                            for index in range(3)
                        )
                    elif not self._warned_missing_phase_export_indices:
                        self._logger.warning(
                            "DOMOTICZ_USE_SIGNED_NET_POWER is enabled but one or more DOMOTICZ_PHASE_EXPORT_*_IDX values are missing; "
                            "falling back to unsigned phase import values."
                        )
                        self._warned_missing_phase_export_indices = True

                snapshot = self._cache.update(reading, phase_usage_watts=phase_usage_watts)
                self._logger.debug(
                    (
                        "Domoticz usage snapshot updated: sequence=%s signed_net_power=%s "
                        "grid_import_watts=%.0f grid_export_watts=%.0f grid_rewrite_watts=%.0f "
                        "phase_watts=(%.0f, %.0f, %.0f)"
                    ),
                    snapshot.sequence,
                    int(snapshot.use_signed_net_power),
                    max(reading.import_watts, 0.0),
                    max(reading.export_watts, 0.0),
                    snapshot.rewrite_usage_watts or 0.0,
                    snapshot.phase_usage_watts[0] if snapshot.phase_usage_watts else 0.0,
                    snapshot.phase_usage_watts[1] if snapshot.phase_usage_watts else 0.0,
                    snapshot.phase_usage_watts[2] if snapshot.phase_usage_watts else 0.0,
                )
            except Exception as exc:  # pragma: no cover - defensive log path
                self._logger.warning("Domoticz poll failed: %s", exc)

            self._stop_event.wait(self._poll_interval_seconds)


def _format_watts(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,} W"
    return f"{value:,.2f} W"


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,} {unit}"
    return f"{value:,.2f} {unit}"


def describe_instantaneous_preview_basis() -> str:
    return (
        "Preview basis: ABB instantaneous active power total lives in 0x5B14/0x5B15 as a signed 0.01 W register. "
        "When DOMOTICZ_USE_SIGNED_NET_POWER=1, we encode signed net watts (Usage-UsageDeliv) for active_power_total "
        "and signed per-phase net watts (phase import minus phase export indices) for active_power_l1/l2/l3. "
        "When disabled, we encode non-negative grid import Usage and non-negative phase import values. "
        "All other registers in instantaneous_values are copied verbatim from the ABB source. House load is ignored."
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

    raw_bit_count = register_length * 16
    invalid_unsigned = (1 << raw_bit_count) - 1
    invalid_signed = (1 << (raw_bit_count - 1)) - 1
    if not signed and raw == invalid_unsigned:
        return None
    if signed and raw == invalid_signed:
        return None

    if signed and raw & (1 << (raw_bit_count - 1)):
        raw -= 1 << raw_bit_count
    return raw * scale


def decode_signed_scaled_watts(register_values: Sequence[int], *, offset: int = INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET) -> float | None:
    return _decode_scaled_value(
        register_values,
        offset=offset,
        scale=INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE,
        signed=True,
        register_length=INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH,
    )


def decode_instantaneous_fields(register_values: Sequence[int]) -> dict[str, float | None]:
    decoded: dict[str, float | None] = {}
    for spec in INSTANTANEOUS_FIELD_SPECS:
        decoded[spec.name] = _decode_scaled_value(
            register_values,
            offset=spec.offset,
            scale=spec.scale,
            signed=spec.signed,
            register_length=spec.register_length,
        )
    return decoded


def encode_signed_scaled_watts(
    value_watts: float,
    *,
    allow_negative: bool = False,
) -> tuple[int, int]:
    raw = int(round(float(value_watts) / INSTANTANEOUS_ACTIVE_POWER_TOTAL_SCALE))
    if allow_negative:
        raw = max(min(raw, 0x7FFFFFFF), -0x80000000)
        payload = raw.to_bytes(4, byteorder="big", signed=True)
    else:
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
    phase_usage_watts: tuple[float, float, float] | None = None,
    allow_negative: bool = False,
) -> tuple[int, ...]:
    values = list(int(value) & 0xFFFF for value in source_values)
    if len(values) < INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH:
        return tuple(values)

    high_word, low_word = encode_signed_scaled_watts(usage_watts, allow_negative=allow_negative)
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET] = high_word
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1] = low_word

    if phase_usage_watts is not None:
        for phase_index, phase_offset in enumerate(INSTANTANEOUS_ACTIVE_POWER_PHASE_OFFSETS):
            if len(values) < phase_offset + INSTANTANEOUS_ACTIVE_POWER_PHASE_LENGTH:
                continue
            phase_high_word, phase_low_word = encode_signed_scaled_watts(
                phase_usage_watts[phase_index],
                allow_negative=allow_negative,
            )
            values[phase_offset] = phase_high_word
            values[phase_offset + 1] = phase_low_word

    return tuple(values)


def preview_signature(
    capture: RegisterCapture,
    *,
    snapshot: DomoticzUsageSnapshot | None,
) -> tuple[Any, ...]:
    usage_watts = snapshot.rewrite_usage_watts if snapshot else None
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
    usage_watts = snapshot.rewrite_usage_watts if snapshot else None

    if source_watts is None or usage_watts is None:
        return [
            "ABB source: awaiting baseline",
            "DZ Usage to Maxem: awaiting baseline",
        ]

    lines = [
        f"ABB source: {_format_watts(source_watts)}",
        f"DZ Usage to Maxem: {_format_watts(usage_watts)}",
    ]
    if snapshot and snapshot.phase_usage_watts is not None:
        lines.append(
            "DZ Phase Watts to Maxem: "
            f"L1={_format_watts(snapshot.phase_usage_watts[0])}, "
            f"L2={_format_watts(snapshot.phase_usage_watts[1])}, "
            f"L3={_format_watts(snapshot.phase_usage_watts[2])}"
        )
    return lines


def changed_instantaneous_words(
    source_values: Sequence[int],
    rewritten_values: Sequence[int],
) -> list[int]:
    changed_addresses: list[int] = []
    max_words = min(len(source_values), len(rewritten_values))
    for offset in range(max_words):
        source_word = int(source_values[offset]) & 0xFFFF
        rewritten_word = int(rewritten_values[offset]) & 0xFFFF
        if source_word != rewritten_word:
            changed_addresses.append(INSTANTANEOUS_VALUES_REGISTER_ADDRESS + offset)
    return changed_addresses


def format_instantaneous_diff_lines(
    source_values: Sequence[int],
    rewritten_values: Sequence[int],
) -> list[str]:
    source_decoded = decode_instantaneous_fields(source_values)
    rewritten_decoded = decode_instantaneous_fields(rewritten_values)
    lines: list[str] = []

    for spec in INSTANTANEOUS_FIELD_SPECS:
        source_value = source_decoded.get(spec.name)
        rewritten_value = rewritten_decoded.get(spec.name)
        changed_marker = ""
        if source_value is not None and rewritten_value is not None:
            if abs(source_value - rewritten_value) > 1e-9:
                changed_marker = " [changed]"
        lines.append(
            f"{spec.name}: {_format_value(source_value, spec.unit)} -> {_format_value(rewritten_value, spec.unit)}{changed_marker}"
        )

    changed_addresses = changed_instantaneous_words(source_values, rewritten_values)
    if changed_addresses:
        changed_words_text = ", ".join(f"0x{address:04X}" for address in changed_addresses)
    else:
        changed_words_text = "none"
    lines.append(f"changed_words: {changed_words_text}")
    return lines
