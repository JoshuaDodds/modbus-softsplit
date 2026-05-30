from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .synthetic_home import DomoticzClient, DomoticzReading, RegisterCapture
import paho.mqtt.client as mqtt

INSTANTANEOUS_VALUES_REGISTER_NAME = "instantaneous_values"
INSTANTANEOUS_VALUES_REGISTER_ADDRESS = 0x5B00
INSTANTANEOUS_VALUES_REGISTER_LENGTH = 66

INSTANTANEOUS_VOLTAGE_L1_REGISTER_ADDRESS = 0x5B00
INSTANTANEOUS_VOLTAGE_L2_REGISTER_ADDRESS = 0x5B02
INSTANTANEOUS_VOLTAGE_L3_REGISTER_ADDRESS = 0x5B04
INSTANTANEOUS_VOLTAGE_PHASE_OFFSETS = (
    INSTANTANEOUS_VOLTAGE_L1_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VOLTAGE_L2_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VOLTAGE_L3_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
)
INSTANTANEOUS_VOLTAGE_PHASE_SCALE = 0.1
INSTANTANEOUS_VOLTAGE_PHASE_LENGTH = 2

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
INSTANTANEOUS_CURRENT_L1_REGISTER_ADDRESS = 0x5B0C
INSTANTANEOUS_CURRENT_L2_REGISTER_ADDRESS = 0x5B0E
INSTANTANEOUS_CURRENT_L3_REGISTER_ADDRESS = 0x5B10
INSTANTANEOUS_CURRENT_N_REGISTER_ADDRESS = 0x5B12
INSTANTANEOUS_CURRENT_PHASE_OFFSETS = (
    INSTANTANEOUS_CURRENT_L1_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_L2_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_L3_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
)
INSTANTANEOUS_CURRENT_N_OFFSET = INSTANTANEOUS_CURRENT_N_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
INSTANTANEOUS_CURRENT_REGISTER_LENGTH = 2
INSTANTANEOUS_CURRENT_SCALE = 0.01


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
    use_signed_net_phase_power: bool = False

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
    def __init__(
        self,
        *,
        use_signed_net_power: bool = False,
        use_signed_net_phase_power: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._reading: DomoticzReading | None = None
        self._phase_usage_watts: tuple[float, float, float] | None = None
        self._use_signed_net_power = bool(use_signed_net_power)
        self._use_signed_net_phase_power = bool(use_signed_net_phase_power)

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
                use_signed_net_phase_power=self._use_signed_net_phase_power,
            )

    def snapshot(self) -> DomoticzUsageSnapshot:
        with self._lock:
            return DomoticzUsageSnapshot(
                sequence=self._sequence,
                reading=self._reading,
                phase_usage_watts=self._phase_usage_watts,
                use_signed_net_power=self._use_signed_net_power,
                use_signed_net_phase_power=self._use_signed_net_phase_power,
            )


class DomoticzUsagePoller(threading.Thread):
    def __init__(
        self,
        client: DomoticzClient,
        cache: DomoticzUsageCache,
        *,
        phase_l1_idx: int = 26,
        phase_l2_idx: int = 24,
        phase_l3_idx: int = 25,
        phase_export_l1_idx: int | None = None,
        phase_export_l2_idx: int | None = None,
        phase_export_l3_idx: int | None = None,
        use_signed_net_power: bool = False,
        use_signed_net_phase_power: bool = False,
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
        self._use_signed_net_phase_power = bool(use_signed_net_phase_power)
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
        if self._use_signed_net_phase_power and self._has_phase_export_indices():
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
                if self._use_signed_net_phase_power:
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
                            "DOMOTICZ_USE_SIGNED_NET_PHASE_POWER is enabled but one or more DOMOTICZ_PHASE_EXPORT_*_IDX values are missing; "
                            "falling back to unsigned phase import values."
                        )
                        self._warned_missing_phase_export_indices = True

                snapshot = self._cache.update(reading, phase_usage_watts=phase_usage_watts)
                self._logger.debug(
                    (
                        "Domoticz usage snapshot updated: sequence=%s signed_total=%s signed_phase=%s "
                        "grid_import_watts=%.0f grid_export_watts=%.0f grid_rewrite_watts=%.0f "
                        "phase_watts=(%.0f, %.0f, %.0f)"
                    ),
                    snapshot.sequence,
                    int(snapshot.use_signed_net_power),
                    int(self._use_signed_net_phase_power),
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


@dataclass(frozen=True)
class CerboMqttSnapshot:
    sequence: int
    ac_in_phase_watts: tuple[float, float, float] | None = None
    ac_in_total_watts: float | None = None
    ac_out_phase_currents: tuple[float, float, float] | None = None
    ac_out_current_n: float | None = None
    pv_total_watts: float | None = None
    source_label: str = "Cerbo"

    @property
    def rewrite_usage_watts(self) -> float | None:
        return self.ac_in_total_watts

    @property
    def phase_usage_watts(self) -> tuple[float, float, float] | None:
        return self.ac_in_phase_watts

    @property
    def phase_current_amps(self) -> tuple[float, float, float] | None:
        return self.ac_out_phase_currents

    @property
    def current_n_amps(self) -> float | None:
        return self.ac_out_current_n


class CerboMqttCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0
        self._ac_in_phase_watts: tuple[float, float, float] | None = None
        self._ac_in_total_watts: float | None = None
        self._ac_out_phase_currents: tuple[float, float, float] | None = None
        self._ac_out_current_n: float | None = None
        self._pv_total_watts: float | None = None

    def update(
        self,
        *,
        ac_in_phase_watts: tuple[float, float, float],
        ac_out_phase_currents: tuple[float, float, float],
        ac_out_current_n: float | None,
        pv_total_watts: float | None = None,
    ) -> CerboMqttSnapshot:
        with self._lock:
            self._sequence += 1
            self._ac_in_phase_watts = tuple(float(value) for value in ac_in_phase_watts)
            self._ac_in_total_watts = float(sum(self._ac_in_phase_watts))
            self._ac_out_phase_currents = tuple(max(float(value), 0.0) for value in ac_out_phase_currents)
            self._ac_out_current_n = None if ac_out_current_n is None else max(float(ac_out_current_n), 0.0)
            self._pv_total_watts = None if pv_total_watts is None else max(float(pv_total_watts), 0.0)
            return CerboMqttSnapshot(
                sequence=self._sequence,
                ac_in_phase_watts=self._ac_in_phase_watts,
                ac_in_total_watts=self._ac_in_total_watts,
                ac_out_phase_currents=self._ac_out_phase_currents,
                ac_out_current_n=self._ac_out_current_n,
                pv_total_watts=self._pv_total_watts,
            )

    def snapshot(self) -> CerboMqttSnapshot:
        with self._lock:
            return CerboMqttSnapshot(
                sequence=self._sequence,
                ac_in_phase_watts=self._ac_in_phase_watts,
                ac_in_total_watts=self._ac_in_total_watts,
                ac_out_phase_currents=self._ac_out_phase_currents,
                ac_out_current_n=self._ac_out_current_n,
                pv_total_watts=self._pv_total_watts,
            )


class CerboMqttPoller(threading.Thread):
    def __init__(
        self,
        *,
        broker_host: str,
        broker_port: int,
        ac_out_topic_base: str,
        ac_active_in_topic_base: str,
        pv_power_topics: Sequence[str] | None,
        cache: CerboMqttCache,
        protocol_debug: bool = False,
        snapshot_debug_interval_seconds: float = 0.0,
        coherent_phase_frames: bool = True,
        coherent_phase_frame_max_skew_seconds: float = 1.5,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(name="cerbo-mqtt-poller", daemon=True)
        self._broker_host = str(broker_host).strip()
        self._broker_port = int(broker_port)
        self._ac_out_topic_base = ac_out_topic_base.rstrip("/")
        self._ac_active_in_topic_base = ac_active_in_topic_base.rstrip("/")
        self._cache = cache
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._protocol_debug = bool(protocol_debug)
        self._snapshot_debug_interval_seconds = max(float(snapshot_debug_interval_seconds), 0.0)
        self._next_snapshot_log_time = 0.0
        self._coherent_phase_frames = bool(coherent_phase_frames)
        self._coherent_phase_frame_max_skew_seconds = max(float(coherent_phase_frame_max_skew_seconds), 0.0)
        self._phase_in_watts: list[float | None] = [None, None, None]
        self._phase_out_currents: list[float | None] = [None, None, None]
        self._phase_in_update_times: list[float] = [0.0, 0.0, 0.0]
        self._phase_out_update_times: list[float] = [0.0, 0.0, 0.0]
        self._phase_in_updated_mask = 0
        self._phase_out_updated_mask = 0
        self._current_n: float | None = None
        self._pv_power_topics = self._normalize_pv_topics(pv_power_topics)
        self._pv_topic_values: dict[str, float | None] = {topic: None for topic in self._pv_power_topics}
        self._client = mqtt.Client(client_id=f"modbus-softsplit-{int(time.time())}", protocol=mqtt.MQTTv311)
        if self._protocol_debug:
            self._client.enable_logger(self._logger)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    @staticmethod
    def _normalize_pv_topics(topics: Sequence[str] | None) -> tuple[str, ...]:
        if not topics:
            return ()

        normalized: list[str] = []
        seen: set[str] = set()
        for topic in topics:
            cleaned = str(topic or "").strip().rstrip("/")
            if not cleaned or cleaned in seen:
                continue
            normalized.append(cleaned)
            seen.add(cleaned)
        return tuple(normalized)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self._logger.warning("Cerbo MQTT connect failed: rc=%s", rc)
            return

        subscriptions = [
            (f"{self._ac_active_in_topic_base}/L1/P", 0),
            (f"{self._ac_active_in_topic_base}/L2/P", 0),
            (f"{self._ac_active_in_topic_base}/L3/P", 0),
            (f"{self._ac_out_topic_base}/L1/I", 0),
            (f"{self._ac_out_topic_base}/L2/I", 0),
            (f"{self._ac_out_topic_base}/L3/I", 0),
            (f"{self._ac_out_topic_base}/N/I", 0),
        ]
        for pv_topic in self._pv_power_topics:
            subscriptions.append((pv_topic, 0))
        for topic, qos in subscriptions:
            client.subscribe(topic, qos=qos)
        if self._pv_power_topics:
            self._logger.info(
                "Cerbo MQTT connected to %s:%s; subscribed read-only to Ac/ActiveIn, Ac/Out, and %d PV topic(s).",
                self._broker_host,
                self._broker_port,
                len(self._pv_power_topics),
            )
        else:
            self._logger.info(
                "Cerbo MQTT connected to %s:%s; subscribed read-only to Ac/ActiveIn and Ac/Out topics.",
                self._broker_host,
                self._broker_port,
            )

    def _on_disconnect(self, client, userdata, rc):
        if self._stop_event.is_set():
            return
        self._logger.warning("Cerbo MQTT disconnected unexpectedly: rc=%s", rc)

    @staticmethod
    def _payload_value(payload: bytes) -> float:
        raw = payload.decode("utf-8", "replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping) or "value" not in parsed:
            raise ValueError(f"Unexpected MQTT payload shape: {raw!r}")
        return float(parsed["value"])

    def _update_phase_slot(self, topic: str, value: float) -> bool:
        if topic in self._pv_topic_values:
            self._pv_topic_values[topic] = max(float(value), 0.0)
            return True

        now = time.monotonic()
        for phase_index, phase_name in enumerate(("L1", "L2", "L3")):
            if topic.endswith(f"/{phase_name}/P"):
                self._phase_in_watts[phase_index] = float(value)
                self._phase_in_update_times[phase_index] = now
                self._phase_in_updated_mask |= 1 << phase_index
                return True
            if topic.endswith(f"/{phase_name}/I"):
                self._phase_out_currents[phase_index] = max(float(value), 0.0)
                self._phase_out_update_times[phase_index] = now
                self._phase_out_updated_mask |= 1 << phase_index
                return True
        if topic.endswith("/N/I"):
            self._current_n = max(float(value), 0.0)
            return True
        return False

    def _current_pv_total_watts(self) -> float | None:
        if not self._pv_topic_values:
            return None
        values = list(self._pv_topic_values.values())
        if any(value is None for value in values):
            return None
        return float(sum(float(value) for value in values))

    def _has_baseline_values(self) -> bool:
        return not any(v is None for v in self._phase_in_watts) and not any(v is None for v in self._phase_out_currents)

    def _has_full_coherent_frame(self) -> bool:
        full_mask = 0b111
        return self._phase_in_updated_mask == full_mask and self._phase_out_updated_mask == full_mask

    def _phase_skew_exceeds_threshold(self) -> bool:
        if self._coherent_phase_frame_max_skew_seconds <= 0.0:
            return False

        phase_in_skew = max(self._phase_in_update_times) - min(self._phase_in_update_times)
        phase_out_skew = max(self._phase_out_update_times) - min(self._phase_out_update_times)
        return (
            phase_in_skew > self._coherent_phase_frame_max_skew_seconds
            or phase_out_skew > self._coherent_phase_frame_max_skew_seconds
        )

    def _ready_for_publish(self) -> bool:
        if not self._has_baseline_values():
            return False
        if not self._coherent_phase_frames:
            return True
        if not self._has_full_coherent_frame():
            return False
        if self._phase_skew_exceeds_threshold():
            return False
        return True

    def _on_message(self, client, userdata, msg):
        try:
            value = self._payload_value(msg.payload)
        except Exception as exc:
            self._logger.warning("Cerbo MQTT payload parse failed for topic=%s: %s", msg.topic, exc)
            return

        if not self._update_phase_slot(msg.topic, value):
            return

        if not self._ready_for_publish():
            return

        snapshot = self._cache.update(
            ac_in_phase_watts=(
                float(self._phase_in_watts[0]),
                float(self._phase_in_watts[1]),
                float(self._phase_in_watts[2]),
            ),
            ac_out_phase_currents=(
                float(self._phase_out_currents[0]),
                float(self._phase_out_currents[1]),
                float(self._phase_out_currents[2]),
            ),
            ac_out_current_n=self._current_n,
            pv_total_watts=self._current_pv_total_watts(),
        )
        if self._coherent_phase_frames:
            self._phase_in_updated_mask = 0
            self._phase_out_updated_mask = 0

        if self._snapshot_debug_interval_seconds <= 0.0:
            return

        now = time.monotonic()
        if now < self._next_snapshot_log_time:
            return
        self._next_snapshot_log_time = now + self._snapshot_debug_interval_seconds

        current_n_text = "n/a" if snapshot.current_n_amps is None else f"{snapshot.current_n_amps:.2f}"
        self._logger.debug(
            (
                "Cerbo MQTT snapshot updated: sequence=%s ac_in_total_watts=%.2f ac_in_phase_watts=(%.2f, %.2f, %.2f) "
                "ac_out_phase_currents=(%.2f, %.2f, %.2f) ac_out_current_n=%s pv_total_watts=%s"
            ),
            snapshot.sequence,
            snapshot.rewrite_usage_watts or 0.0,
            snapshot.phase_usage_watts[0] if snapshot.phase_usage_watts else 0.0,
            snapshot.phase_usage_watts[1] if snapshot.phase_usage_watts else 0.0,
            snapshot.phase_usage_watts[2] if snapshot.phase_usage_watts else 0.0,
            snapshot.phase_current_amps[0] if snapshot.phase_current_amps else 0.0,
            snapshot.phase_current_amps[1] if snapshot.phase_current_amps else 0.0,
            snapshot.phase_current_amps[2] if snapshot.phase_current_amps else 0.0,
            current_n_text,
            "n/a" if snapshot.pv_total_watts is None else f"{snapshot.pv_total_watts:.2f}",
        )

    def run(self) -> None:
        if not self._broker_host:
            self._logger.info("Cerbo MQTT polling disabled: empty broker host.")
            return

        try:
            self._client.connect(self._broker_host, self._broker_port, 60)
            self._client.loop_start()
            while not self._stop_event.wait(0.2):
                pass
        except Exception as exc:  # pragma: no cover - defensive log path
            self._logger.warning("Cerbo MQTT poller failed: %s", exc)
        finally:
            try:
                self._client.loop_stop()
            except Exception:
                pass
            try:
                self._client.disconnect()
            except Exception:
                pass


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
        "Preview basis: active_power_total (0x5B14/0x5B15) is rewritten from Cerbo Ac/ActiveIn total watts. "
        "active_power_l1/l2/l3 (0x5B16..0x5B1B) follow CERBO_PHASE_POWER_SOURCE mode (activein, acout-derived, or abb passthrough). "
        "current_l1/l2/l3/n (0x5B0C..0x5B13) are rewritten from Cerbo Ac/Out phase currents with non-negative clamp. "
        "All other registers in instantaneous_values are copied verbatim from the ABB source."
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


def derive_phase_watts_from_currents(
    source_values: Sequence[int],
    phase_current_amps: tuple[float, float, float] | None,
    *,
    fallback_phase_voltage_volts: float = 230.0,
) -> tuple[float, float, float] | None:
    if phase_current_amps is None:
        return None

    derived_watts: list[float] = []
    for phase_index, phase_offset in enumerate(INSTANTANEOUS_VOLTAGE_PHASE_OFFSETS):
        voltage = _decode_scaled_value(
            source_values,
            offset=phase_offset,
            scale=INSTANTANEOUS_VOLTAGE_PHASE_SCALE,
            signed=False,
            register_length=INSTANTANEOUS_VOLTAGE_PHASE_LENGTH,
        )
        if voltage is None or voltage <= 0.0:
            voltage = float(fallback_phase_voltage_volts)
        amps = max(float(phase_current_amps[phase_index]), 0.0)
        derived_watts.append(float(voltage) * amps)

    return (derived_watts[0], derived_watts[1], derived_watts[2])


def net_signed_phase_watts_to_nonnegative_import(
    phase_watts: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    if phase_watts is None:
        return None

    phase_values = tuple(float(value) for value in phase_watts)
    phase_import = [max(value, 0.0) for value in phase_values]
    total_import = sum(phase_import)
    if total_import <= 0.0:
        return (0.0, 0.0, 0.0)

    total_export = -sum(min(value, 0.0) for value in phase_values)
    if total_export <= 0.0:
        return (phase_import[0], phase_import[1], phase_import[2])

    net_import = max(total_import - total_export, 0.0)
    if net_import <= 0.0:
        return (0.0, 0.0, 0.0)

    scale = net_import / total_import
    return (
        phase_import[0] * scale,
        phase_import[1] * scale,
        phase_import[2] * scale,
    )


def split_total_watts_evenly(total_watts: float | None) -> tuple[float, float, float] | None:
    if total_watts is None:
        return None

    clamped_total_watts = max(float(total_watts), 0.0)
    per_phase = clamped_total_watts / 3.0
    return (
        per_phase,
        per_phase,
        clamped_total_watts - (2.0 * per_phase),
    )


def derive_phase_currents_from_watts(
    source_values: Sequence[int],
    phase_watts: tuple[float, float, float] | None,
    *,
    fallback_phase_voltage_volts: float = 230.0,
) -> tuple[float, float, float] | None:
    if phase_watts is None:
        return None

    derived_currents: list[float] = []
    for phase_index, phase_offset in enumerate(INSTANTANEOUS_VOLTAGE_PHASE_OFFSETS):
        voltage = _decode_scaled_value(
            source_values,
            offset=phase_offset,
            scale=INSTANTANEOUS_VOLTAGE_PHASE_SCALE,
            signed=False,
            register_length=INSTANTANEOUS_VOLTAGE_PHASE_LENGTH,
        )
        if voltage is None or voltage <= 0.0:
            voltage = float(fallback_phase_voltage_volts)
        watts = max(float(phase_watts[phase_index]), 0.0)
        derived_currents.append(watts / float(voltage))
    return (
        derived_currents[0],
        derived_currents[1],
        derived_currents[2],
    )


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


def encode_unsigned_scaled_amps(value_amps: float) -> tuple[int, int]:
    raw = int(round(max(float(value_amps), 0.0) / INSTANTANEOUS_CURRENT_SCALE))
    raw = max(min(raw, 0xFFFFFFFF), 0)
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
    phase_current_amps: tuple[float, float, float] | None = None,
    current_n_amps: float | None = None,
    allow_negative: bool = False,
    allow_negative_phase: bool | None = None,
) -> tuple[int, ...]:
    values = list(int(value) & 0xFFFF for value in source_values)
    if len(values) < INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + INSTANTANEOUS_ACTIVE_POWER_TOTAL_REGISTER_LENGTH:
        return tuple(values)

    high_word, low_word = encode_signed_scaled_watts(usage_watts, allow_negative=allow_negative)
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET] = high_word
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1] = low_word

    phase_allow_negative = allow_negative if allow_negative_phase is None else bool(allow_negative_phase)
    if phase_usage_watts is not None:
        for phase_index, phase_offset in enumerate(INSTANTANEOUS_ACTIVE_POWER_PHASE_OFFSETS):
            if len(values) < phase_offset + INSTANTANEOUS_ACTIVE_POWER_PHASE_LENGTH:
                continue
            phase_high_word, phase_low_word = encode_signed_scaled_watts(
                phase_usage_watts[phase_index],
                allow_negative=phase_allow_negative,
            )
            values[phase_offset] = phase_high_word
            values[phase_offset + 1] = phase_low_word

    if phase_current_amps is not None:
        for phase_index, phase_offset in enumerate(INSTANTANEOUS_CURRENT_PHASE_OFFSETS):
            if len(values) < phase_offset + INSTANTANEOUS_CURRENT_REGISTER_LENGTH:
                continue
            phase_high_word, phase_low_word = encode_unsigned_scaled_amps(phase_current_amps[phase_index])
            values[phase_offset] = phase_high_word
            values[phase_offset + 1] = phase_low_word

    if current_n_amps is not None and len(values) >= INSTANTANEOUS_CURRENT_N_OFFSET + INSTANTANEOUS_CURRENT_REGISTER_LENGTH:
        n_high_word, n_low_word = encode_unsigned_scaled_amps(current_n_amps)
        values[INSTANTANEOUS_CURRENT_N_OFFSET] = n_high_word
        values[INSTANTANEOUS_CURRENT_N_OFFSET + 1] = n_low_word

    return tuple(values)


def rewrite_pv_instantaneous_values(
    source_values: Sequence[int],
    *,
    pv_total_watts: float | None,
) -> tuple[int, ...]:
    phase_watts = split_total_watts_evenly(pv_total_watts)
    phase_currents = derive_phase_currents_from_watts(source_values, phase_watts)
    total_watts = 0.0 if pv_total_watts is None else max(float(pv_total_watts), 0.0)
    return rewrite_instantaneous_values(
        source_values,
        usage_watts=total_watts,
        phase_usage_watts=phase_watts,
        phase_current_amps=phase_currents,
        current_n_amps=0.0,
        allow_negative=False,
        allow_negative_phase=False,
    )


def _snapshot_rewrite_usage_watts(snapshot: Any | None) -> float | None:
    if snapshot is None:
        return None
    return getattr(snapshot, "rewrite_usage_watts", None)


def _snapshot_phase_usage_watts(snapshot: Any | None) -> tuple[float, float, float] | None:
    if snapshot is None:
        return None
    return getattr(snapshot, "phase_usage_watts", None)


def _snapshot_phase_current_amps(snapshot: Any | None) -> tuple[float, float, float] | None:
    if snapshot is None:
        return None
    return getattr(snapshot, "phase_current_amps", None)


def _snapshot_current_n_amps(snapshot: Any | None) -> float | None:
    if snapshot is None:
        return None
    return getattr(snapshot, "current_n_amps", None)


def _snapshot_source_label(snapshot: Any | None) -> str:
    if snapshot is None:
        return "Cerbo"
    explicit_label = getattr(snapshot, "source_label", None)
    if explicit_label not in (None, ""):
        return str(explicit_label)
    if hasattr(snapshot, "reading"):
        return "DZ"
    return "Cerbo"


def preview_signature(
    capture: RegisterCapture,
    *,
    snapshot: Any | None,
) -> tuple[Any, ...]:
    usage_watts = _snapshot_rewrite_usage_watts(snapshot)
    sequence = getattr(snapshot, "sequence", None) if snapshot else None
    phase_usage_watts = _snapshot_phase_usage_watts(snapshot)
    phase_currents = _snapshot_phase_current_amps(snapshot)
    current_n = _snapshot_current_n_amps(snapshot)
    return (
        capture.target_slave,
        capture.source_slave,
        capture.register_name,
        capture.address,
        capture.address_length,
        capture.source_values,
        sequence,
        usage_watts,
        phase_usage_watts,
        phase_currents,
        current_n,
    )


def format_instantaneous_preview_lines(
    capture: RegisterCapture,
    *,
    snapshot: Any | None,
) -> list[str]:
    if capture.target_slave != 100 or capture.register_name != INSTANTANEOUS_VALUES_REGISTER_NAME:
        return []

    source_watts = decode_signed_scaled_watts(capture.source_values)
    usage_watts = _snapshot_rewrite_usage_watts(snapshot)
    source_label = _snapshot_source_label(snapshot)

    if source_watts is None or usage_watts is None:
        return [
            "ABB source: awaiting baseline",
            f"{source_label} Usage to Maxem: awaiting baseline",
        ]

    lines = [
        f"ABB source: {_format_watts(source_watts)}",
        f"{source_label} Usage to Maxem: {_format_watts(usage_watts)}",
    ]
    phase_usage_watts = _snapshot_phase_usage_watts(snapshot)
    if phase_usage_watts is not None:
        lines.append(
            f"{source_label} Phase Watts to Maxem: "
            f"L1={_format_watts(phase_usage_watts[0])}, "
            f"L2={_format_watts(phase_usage_watts[1])}, "
            f"L3={_format_watts(phase_usage_watts[2])}"
        )
    phase_current_amps = _snapshot_phase_current_amps(snapshot)
    current_n_amps = _snapshot_current_n_amps(snapshot)
    if phase_current_amps is not None:
        lines.append(
            f"{source_label} Phase Currents to Maxem: "
            f"L1={_format_value(phase_current_amps[0], 'A')}, "
            f"L2={_format_value(phase_current_amps[1], 'A')}, "
            f"L3={_format_value(phase_current_amps[2], 'A')}, "
            f"N={_format_value(current_n_amps, 'A')}"
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
