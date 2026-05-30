"""Legacy cumulative-energy helpers kept for historical reference.

The active Maxem rewrite path is watts-based and lives in
lib.maxem_home_usage.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(payload: Mapping[str, Any], field: str) -> float:
    value = payload.get(field)
    if value is None:
        raise KeyError(f"Domoticz payload is missing required field: {field}")

    match = _NUMBER_RE.search(str(value).replace(",", ""))
    if not match:
        raise ValueError(f"Could not parse numeric value for {field!r}: {value!r}")

    return float(match.group(0))


def _state_to_dict(state: "SyntheticHomeState") -> dict[str, Any]:
    return {
        "previous_import_kwh": state.previous_import_kwh,
        "previous_export_kwh": state.previous_export_kwh,
        "synthetic_home_kwh": state.synthetic_home_kwh,
        "sequence": state.sequence,
        "last_update": state.last_update,
        "last_reason": state.last_reason,
    }


@dataclass(frozen=True)
class RegisterCapture:
    target_slave: int
    source_slave: int
    register_name: str
    address: int
    address_length: int
    source_values: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_slave": self.target_slave,
            "source_slave": self.source_slave,
            "register_name": self.register_name,
            "address": self.address,
            "address_length": self.address_length,
            "source_values": list(self.source_values),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegisterCapture":
        raw_values = payload.get("source_values", [])
        if not isinstance(raw_values, Sequence):
            raise TypeError("Register capture source_values must be a sequence")

        return cls(
            target_slave=int(payload.get("target_slave", 0) or 0),
            source_slave=int(payload.get("source_slave", 0) or 0),
            register_name=str(payload.get("register_name", "")),
            address=int(payload.get("address", 0) or 0),
            address_length=int(payload.get("address_length", 0) or 0),
            source_values=tuple(int(value) for value in raw_values),
        )


def register_capture_to_dict(capture: RegisterCapture) -> dict[str, Any]:
    return capture.to_dict()


def register_capture_from_dict(payload: Mapping[str, Any]) -> RegisterCapture:
    return RegisterCapture.from_dict(payload)


@dataclass(frozen=True)
class DomoticzReading:
    import_kwh: float
    export_kwh: float
    import_watts: float
    export_watts: float
    last_update: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DomoticzReading":
        candidate: Mapping[str, Any]
        if "result" in payload and isinstance(payload["result"], list) and payload["result"]:
            candidate = payload["result"][0]
        else:
            candidate = payload

        if not isinstance(candidate, Mapping):
            raise TypeError("Domoticz payload must be a mapping")

        return cls(
            import_kwh=_coerce_float(candidate, "Counter"),
            export_kwh=_coerce_float(candidate, "CounterDeliv"),
            import_watts=_coerce_float(candidate, "Usage"),
            export_watts=_coerce_float(candidate, "UsageDeliv"),
            last_update=str(candidate.get("LastUpdate")) if candidate.get("LastUpdate") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_kwh": self.import_kwh,
            "export_kwh": self.export_kwh,
            "import_watts": self.import_watts,
            "export_watts": self.export_watts,
            "last_update": self.last_update,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DomoticzReading":
        return cls(
            import_kwh=float(payload.get("import_kwh", 0.0) or 0.0),
            export_kwh=float(payload.get("export_kwh", 0.0) or 0.0),
            import_watts=float(payload.get("import_watts", 0.0) or 0.0),
            export_watts=float(payload.get("export_watts", 0.0) or 0.0),
            last_update=str(payload.get("last_update")) if payload.get("last_update") else None,
        )


@dataclass(frozen=True)
class SyntheticHomeState:
    previous_import_kwh: float | None = None
    previous_export_kwh: float | None = None
    synthetic_home_kwh: float = 0.0
    sequence: int = 0
    last_update: str | None = None
    last_reason: str = "uninitialized"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SyntheticHomeState":
        return cls(
            previous_import_kwh=_optional_float(payload.get("previous_import_kwh")),
            previous_export_kwh=_optional_float(payload.get("previous_export_kwh")),
            synthetic_home_kwh=_optional_float(payload.get("synthetic_home_kwh")) or 0.0,
            sequence=_optional_int(payload.get("sequence"), 0),
            last_update=str(payload.get("last_update")) if payload.get("last_update") else None,
            last_reason=str(payload.get("last_reason", "uninitialized") or "uninitialized"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _state_to_dict(self)


@dataclass(frozen=True)
class SyntheticHomeSnapshot:
    changed: bool
    reason: str
    sequence: int
    previous_import_kwh: float | None
    previous_export_kwh: float | None
    current_import_kwh: float | None
    current_export_kwh: float | None
    import_delta_kwh: float
    export_delta_kwh: float
    synthetic_delta_kwh: float
    synthetic_home_kwh: float
    last_update: str | None
    last_reason: str


def register_capture_signature(
    capture: RegisterCapture,
    *,
    snapshot: SyntheticHomeSnapshot | None = None,
) -> tuple[Any, ...]:
    if snapshot is None:
        return (
            capture.target_slave,
            capture.source_slave,
            capture.register_name,
            capture.address,
            capture.address_length,
            capture.source_values,
        )

    return (
        capture.target_slave,
        capture.source_slave,
        capture.register_name,
        capture.address,
        capture.address_length,
        capture.source_values,
        snapshot.sequence,
        snapshot.synthetic_home_kwh,
        snapshot.reason,
    )


def format_register_preview_lines(
    capture: RegisterCapture,
    *,
    snapshot: SyntheticHomeSnapshot | None = None,
) -> list[str]:
    if capture.target_slave != 100 or capture.register_name != "total_accumulators":
        return []

    if snapshot is None:
        return [
            "Legacy ABB source: awaiting baseline",
            "Legacy DZ Rewrite to Maxem: awaiting baseline",
        ]

    if snapshot.current_import_kwh is None:
        abb_source_line = "Legacy ABB source: awaiting baseline"
    else:
        abb_source_line = f"Legacy ABB source: {snapshot.current_import_kwh:.3f} kWh"

    if snapshot.synthetic_home_kwh is None:
        rewrite_line = "Legacy DZ Rewrite to Maxem: awaiting baseline"
    else:
        rewrite_line = f"Legacy DZ Rewrite to Maxem: {snapshot.synthetic_home_kwh:.3f} kWh"

    return [abb_source_line, rewrite_line]


def format_register_preview(
    capture: RegisterCapture,
    *,
    snapshot: SyntheticHomeSnapshot | None = None,
) -> str:
    return "\n".join(format_register_preview_lines(capture, snapshot=snapshot))


def format_total_accumulators_preview(
    *,
    register_name: str,
    address: int,
    address_length: int,
    source_slave: int,
    source_values: Sequence[int],
    snapshot: SyntheticHomeSnapshot,
) -> str:
    capture = RegisterCapture(
        target_slave=source_slave,
        source_slave=source_slave,
        register_name=register_name,
        address=address,
        address_length=address_length,
        source_values=tuple(int(value) for value in source_values),
    )
    return format_register_preview(capture, snapshot=snapshot)


def describe_total_accumulators_preview_basis() -> str:
    return (
        "Preview basis: retired cumulative-energy prototype output. The active v1 story now lives in "
        "lib.maxem_home_usage.py and rewrites selected ABB instantaneous current/power words from Cerbo MQTT. "
        "This helper remains only for historical reference."
    )


class SyntheticHomeTracker:
    def __init__(
        self,
        state_path: str | Path,
        *,
        allow_export_decrement: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self._state_path = Path(state_path)
        self._allow_export_decrement = allow_export_decrement
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._state = self._load_state()

    def snapshot(self) -> SyntheticHomeSnapshot:
        with self._lock:
            return self._snapshot_for_state(
                self._state,
                changed=False,
                reason=self._state.last_reason,
            )

    def observe(self, reading: DomoticzReading) -> SyntheticHomeSnapshot:
        with self._lock:
            state = self._state

            if state.previous_import_kwh is None or state.previous_export_kwh is None:
                new_state = replace(
                    state,
                    previous_import_kwh=reading.import_kwh,
                    previous_export_kwh=reading.export_kwh,
                    sequence=state.sequence + 1,
                    last_update=reading.last_update or state.last_update,
                    last_reason="baseline",
                )
                self._state = new_state
                self._persist_locked()
                return self._snapshot_for_state(
                    new_state,
                    observed=reading,
                    import_delta_kwh=0.0,
                    export_delta_kwh=0.0,
                    synthetic_delta_kwh=0.0,
                    changed=True,
                    reason="baseline",
                )

            if reading.import_kwh == state.previous_import_kwh and reading.export_kwh == state.previous_export_kwh:
                return self._snapshot_for_state(
                    state,
                    observed=reading,
                    changed=False,
                    reason="unchanged",
                )

            if (
                reading.import_kwh < state.previous_import_kwh
                or reading.export_kwh < state.previous_export_kwh
            ):
                return self._snapshot_for_state(
                    state,
                    observed=reading,
                    changed=False,
                    reason="ignored_backwards_jump",
                )

            import_delta_kwh = reading.import_kwh - state.previous_import_kwh
            export_delta_kwh = reading.export_kwh - state.previous_export_kwh

            if self._allow_export_decrement:
                synthetic_next = max(state.synthetic_home_kwh + import_delta_kwh - export_delta_kwh, 0.0)
            else:
                synthetic_next = max(state.synthetic_home_kwh + max(import_delta_kwh - export_delta_kwh, 0.0), 0.0)

            synthetic_delta_kwh = synthetic_next - state.synthetic_home_kwh
            new_reason = "advanced" if synthetic_delta_kwh > 0 else "flat"
            new_state = replace(
                state,
                previous_import_kwh=reading.import_kwh,
                previous_export_kwh=reading.export_kwh,
                synthetic_home_kwh=synthetic_next,
                sequence=state.sequence + 1,
                last_update=reading.last_update or state.last_update,
                last_reason=new_reason,
            )
            self._state = new_state
            self._persist_locked()
            return self._snapshot_for_state(
                new_state,
                observed=reading,
                import_delta_kwh=import_delta_kwh,
                export_delta_kwh=export_delta_kwh,
                synthetic_delta_kwh=synthetic_delta_kwh,
                changed=True,
                reason=new_reason,
            )

    def _load_state(self) -> SyntheticHomeState:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SyntheticHomeState()
        except OSError as exc:
            self._logger.warning("Could not read synthetic home state from %s: %s", self._state_path, exc)
            return SyntheticHomeState()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._logger.warning("Could not parse synthetic home state from %s: %s", self._state_path, exc)
            return SyntheticHomeState()

        if not isinstance(payload, Mapping):
            self._logger.warning("Synthetic home state file %s did not contain a mapping; starting fresh", self._state_path)
            return SyntheticHomeState()

        return SyntheticHomeState.from_dict(payload)

    def _persist_locked(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_path.with_name(f"{self._state_path.name}.tmp")
            tmp_path.write_text(
                json.dumps(self._state.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self._state_path)
        except OSError as exc:
            self._logger.warning("Could not persist synthetic home state to %s: %s", self._state_path, exc)

    def _snapshot_for_state(
        self,
        state: SyntheticHomeState,
        *,
        observed: DomoticzReading | None = None,
        import_delta_kwh: float = 0.0,
        export_delta_kwh: float = 0.0,
        synthetic_delta_kwh: float = 0.0,
        changed: bool,
        reason: str,
    ) -> SyntheticHomeSnapshot:
        return SyntheticHomeSnapshot(
            changed=changed,
            reason=reason,
            sequence=state.sequence,
            previous_import_kwh=state.previous_import_kwh,
            previous_export_kwh=state.previous_export_kwh,
            current_import_kwh=observed.import_kwh if observed else state.previous_import_kwh,
            current_export_kwh=observed.export_kwh if observed else state.previous_export_kwh,
            import_delta_kwh=import_delta_kwh,
            export_delta_kwh=export_delta_kwh,
            synthetic_delta_kwh=synthetic_delta_kwh,
            synthetic_home_kwh=state.synthetic_home_kwh,
            last_update=state.last_update,
            last_reason=state.last_reason,
        )


class DomoticzClient:
    def __init__(self, base_url: str, grid_idx: int, *, timeout_seconds: float = 1.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._grid_idx = grid_idx
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    @property
    def url(self) -> str:
        return f"{self._base_url}/json.htm?type=devices&rid={self._grid_idx}"

    @property
    def grid_idx(self) -> int:
        return int(self._grid_idx)

    def url_for_idx(self, rid: int) -> str:
        return f"{self._base_url}/json.htm?type=devices&rid={int(rid)}"

    @staticmethod
    def _normalize_indices(rids: Sequence[int]) -> tuple[int, ...]:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_rid in rids:
            rid = int(raw_rid)
            if rid <= 0 or rid in seen:
                continue
            normalized.append(rid)
            seen.add(rid)

        if not normalized:
            raise ValueError("At least one positive Domoticz IDX is required")
        return tuple(normalized)

    def url_for_indices(self, rids: Sequence[int]) -> str:
        normalized = self._normalize_indices(rids)
        joined = ",".join(str(rid) for rid in normalized)
        return f"{self._base_url}/json.htm?type=devices&rid={joined}"

    def fetch_payload(
        self,
        *,
        rid: int | None = None,
        rids: Sequence[int] | None = None,
    ) -> Mapping[str, Any]:
        if not self.enabled:
            raise RuntimeError("Domoticz client is disabled because no base URL was configured")

        if rid is not None and rids is not None:
            raise ValueError("Use either rid or rids, not both")

        if rids is not None:
            target_url = self.url_for_indices(rids)
        else:
            target_url = self.url if rid is None else self.url_for_idx(rid)
        request = Request(target_url, headers={"Accept": "application/json", "User-Agent": "modbus-softsplit/1.0"})
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("Domoticz payload must be a mapping")
        return payload

    @staticmethod
    def _result_candidates(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        result = payload.get("result")
        if isinstance(result, list) and result:
            return [candidate for candidate in result if isinstance(candidate, Mapping)]
        if any(field in payload for field in ("idx", "Data", "Usage", "Counter")):
            return [payload]
        return []

    def fetch_devices(self, rids: Sequence[int]) -> dict[int, Mapping[str, Any]]:
        normalized = self._normalize_indices(rids)
        payload = self.fetch_payload(rids=normalized)
        candidates = self._result_candidates(payload)

        devices: dict[int, Mapping[str, Any]] = {}
        for candidate in candidates:
            raw_idx = candidate.get("idx")
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                if len(normalized) == 1:
                    idx = normalized[0]
                else:
                    continue
            if idx in normalized and idx not in devices:
                devices[idx] = candidate

        if len(normalized) == 1 and not devices and candidates:
            devices[normalized[0]] = candidates[0]

        missing = [rid for rid in normalized if rid not in devices]
        if missing:
            missing_text = ", ".join(str(rid) for rid in missing)
            raise KeyError(f"Domoticz payload missing requested IDX values: {missing_text}")
        return devices

    @staticmethod
    def data_watts_from_device(candidate: Mapping[str, Any]) -> float:
        return _coerce_float(candidate, "Data")

    @staticmethod
    def fetch_reading_from_device(candidate: Mapping[str, Any]) -> DomoticzReading:
        return DomoticzReading.from_payload({"result": [candidate]})

    def fetch_data_watts_map(self, rids: Sequence[int]) -> dict[int, float]:
        devices = self.fetch_devices(rids)
        return {rid: self.data_watts_from_device(candidate) for rid, candidate in devices.items()}

    def fetch_data_watts(self, rid: int) -> float:
        return self.fetch_data_watts_map((rid,))[int(rid)]

    def fetch_reading(self) -> DomoticzReading:
        payload = self.fetch_payload()
        return DomoticzReading.from_payload(payload)


class DomoticzPoller(threading.Thread):
    def __init__(
        self,
        client: DomoticzClient,
        tracker: SyntheticHomeTracker,
        *,
        poll_interval_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(name="domoticz-poller", daemon=True)
        self._client = client
        self._tracker = tracker
        self._poll_interval_seconds = max(poll_interval_seconds, 0.1)
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self._client.enabled:
            self._logger.info("Domoticz polling is disabled; synthetic Maxem preview will stay on the last known snapshot.")
            return

        while not self._stop_event.is_set():
            try:
                reading = self._client.fetch_reading()
                snapshot = self._tracker.observe(reading)
                if snapshot.changed:
                    self._logger.debug(
                        "Synthetic Home tracker updated: sequence=%s reason=%s import_kwh=%.3f export_kwh=%.3f synthetic_home_kwh=%.3f",
                        snapshot.sequence,
                        snapshot.reason,
                        snapshot.current_import_kwh or 0.0,
                        snapshot.current_export_kwh or 0.0,
                        snapshot.synthetic_home_kwh,
                    )
            except (HTTPError, URLError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._logger.warning("Domoticz poll failed: %s", exc)

            self._stop_event.wait(self._poll_interval_seconds)
