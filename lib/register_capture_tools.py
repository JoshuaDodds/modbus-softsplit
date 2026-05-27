from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .register_maps import MAXEM_HOLDING_REGISTERS
from .maxem_home_usage import (
    DomoticzUsageSnapshot,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    format_instantaneous_preview_lines,
)
from .synthetic_home import (
    DomoticzReading,
    RegisterCapture,
    register_capture_from_dict,
    register_capture_to_dict,
)

BUNDLE_FORMAT_VERSION = 1


def capture_register_blocks(
    tcp_master,
    source_slaves: Iterable[int],
    register_names: Sequence[str],
    *,
    read_holding_registers: int,
) -> list[RegisterCapture]:
    captures: list[RegisterCapture] = []
    for source_slave in source_slaves:
        for register_name in register_names:
            address, address_length = MAXEM_HOLDING_REGISTERS[register_name]
            values = tcp_master.execute(source_slave, read_holding_registers, address, address_length)
            captures.append(
                RegisterCapture(
                    target_slave=source_slave,
                    source_slave=source_slave,
                    register_name=register_name,
                    address=address,
                    address_length=address_length,
                    source_values=tuple(int(value) for value in values or ()),
                )
            )
    return captures


def build_dump_bundle(
    captures: Sequence[RegisterCapture],
    *,
    modbus_tcp_gateway: str,
    modbus_tcp_port: int,
    request_source_slaves: Sequence[int],
    request_register_names: Sequence[str],
    domoticz_reading: DomoticzReading | None,
    domoticz_url: str | None,
    domoticz_grid_idx: int | None,
    domoticz_phase_usage_watts: Sequence[float] | None = None,
) -> dict[str, object]:
    return {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "source_slaves": [int(slave) for slave in request_source_slaves],
            "register_names": list(request_register_names),
        },
        "modbus_tcp_gateway": {
            "host": modbus_tcp_gateway,
            "port": modbus_tcp_port,
        },
        "domoticz": {
            "url": domoticz_url,
            "grid_idx": domoticz_grid_idx,
            "reading": domoticz_reading.to_dict() if domoticz_reading else None,
            "phase_usage_watts": [float(value) for value in domoticz_phase_usage_watts] if domoticz_phase_usage_watts else None,
        },
        "captures": [register_capture_to_dict(capture) for capture in captures],
    }


def dump_bundle_text(bundle: Mapping[str, object]) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True) + "\n"


def write_dump_bundle(bundle: Mapping[str, object], *, output_path: str | None = None) -> None:
    serialized = dump_bundle_text(bundle)
    if output_path:
        Path(output_path).write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")


def load_dump_bundle(bundle_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("Capture bundle must be a JSON object")
    bundle = dict(payload)

    version = int(bundle.get("bundle_format_version", 0) or 0)
    if version != BUNDLE_FORMAT_VERSION:
        raise ValueError(f"Unsupported bundle format version: {version}")

    return bundle


def bundle_captures(bundle: Mapping[str, Any]) -> list[RegisterCapture]:
    raw_captures = bundle.get("captures", [])
    if not isinstance(raw_captures, Sequence):
        raise TypeError("Capture bundle captures field must be a sequence")
    return [register_capture_from_dict(capture) for capture in raw_captures]


def bundle_domoticz_reading(bundle: Mapping[str, Any]) -> DomoticzReading | None:
    domoticz_block = bundle.get("domoticz", {})
    if not isinstance(domoticz_block, Mapping):
        raise TypeError("Capture bundle domoticz field must be a mapping")

    reading = domoticz_block.get("reading")
    if reading in (None, ""):
        return None
    if not isinstance(reading, Mapping):
        raise TypeError("Capture bundle domoticz.reading field must be a mapping")
    return DomoticzReading.from_dict(reading)


def build_replay_snapshot(
    bundle: Mapping[str, Any],
) -> DomoticzUsageSnapshot:
    reading = bundle_domoticz_reading(bundle)
    domoticz_block = bundle.get("domoticz", {})
    phase_usage_watts = None
    if isinstance(domoticz_block, Mapping):
        raw_phase_values = domoticz_block.get("phase_usage_watts")
        if isinstance(raw_phase_values, Sequence) and len(raw_phase_values) == 3:
            phase_usage_watts = tuple(float(value) for value in raw_phase_values)
    sequence = 1 if reading is not None else 0
    return DomoticzUsageSnapshot(sequence=sequence, reading=reading, phase_usage_watts=phase_usage_watts)


def build_replay_preview_lines(
    bundle: Mapping[str, Any],
    *,
    snapshot: DomoticzUsageSnapshot,
) -> list[str]:
    captures = bundle_captures(bundle)
    for capture in captures:
        if capture.target_slave == 100 and capture.register_name == INSTANTANEOUS_VALUES_REGISTER_NAME:
            return format_instantaneous_preview_lines(capture, snapshot=snapshot)

    return []
