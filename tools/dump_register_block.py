#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.register_capture_tools import (
    build_dump_bundle,
    capture_register_blocks,
    write_dump_bundle,
)
from lib.maxem_home_usage import INSTANTANEOUS_VALUES_REGISTER_NAME
from lib.synthetic_home import DomoticzClient


def _get_env_setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value not in (None, ""):
        return value
    return default


def _get_env_bool(name: str, default: str = "0") -> bool:
    value = str(_get_env_setting(name, default)).strip().lower()
    return value not in {"0", "false", "no", "off", ""}


def _get_env_optional_int(name: str, default: str | None = None) -> int | None:
    value = _get_env_setting(name, default)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _parse_int_list(values: Sequence[str]) -> list[int]:
    return [int(value, 0) for value in values]


def _default_register_names() -> list[str]:
    return [INSTANTANEOUS_VALUES_REGISTER_NAME]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump ABB register blocks and Domoticz Usage into a replay bundle")
    parser.add_argument(
        "--slave",
        dest="source_slaves",
        action="append",
        default=None,
        help="Modbus source slave to dump. Repeat to capture multiple slaves. Default: 100 and 2.",
    )
    parser.add_argument(
        "--register",
        dest="register_names",
        action="append",
        default=None,
        help="Register block to dump. Repeat to capture additional blocks. Default: instantaneous_values only.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default=None,
        help="Write the capture bundle to this file instead of stdout.",
    )
    parser.add_argument(
        "--skip-domoticz",
        action="store_true",
        help="Do not fetch the Domoticz reading for the bundle.",
    )
    parser.add_argument(
        "--domoticz-url",
        default=_get_env_setting("DOMOTICZ_URL", "http://dz-insecure.hs.mfis.net"),
        help="Domoticz base URL. Defaults to DOMOTICZ_URL or the repository default.",
    )
    parser.add_argument(
        "--domoticz-grid-idx",
        type=int,
        default=int(_get_env_setting("DOMOTICZ_GRID_IDX", "20")),
        help="Domoticz grid device index. Defaults to DOMOTICZ_GRID_IDX or 20.",
    )
    parser.add_argument(
        "--domoticz-phase-l1-idx",
        type=int,
        default=int(_get_env_setting("DOMOTICZ_PHASE_L1_IDX", "26")),
        help="Domoticz phase L1 device index. Defaults to DOMOTICZ_PHASE_L1_IDX or 26.",
    )
    parser.add_argument(
        "--domoticz-phase-l2-idx",
        type=int,
        default=int(_get_env_setting("DOMOTICZ_PHASE_L2_IDX", "25")),
        help="Domoticz phase L2 device index. Defaults to DOMOTICZ_PHASE_L2_IDX or 25.",
    )
    parser.add_argument(
        "--domoticz-phase-l3-idx",
        type=int,
        default=int(_get_env_setting("DOMOTICZ_PHASE_L3_IDX", "24")),
        help="Domoticz phase L3 device index. Defaults to DOMOTICZ_PHASE_L3_IDX or 24.",
    )
    parser.add_argument(
        "--domoticz-phase-export-l1-idx",
        type=int,
        default=_get_env_optional_int("DOMOTICZ_PHASE_EXPORT_L1_IDX", "32"),
        help="Domoticz phase L1 export device index. Defaults to DOMOTICZ_PHASE_EXPORT_L1_IDX or 32.",
    )
    parser.add_argument(
        "--domoticz-phase-export-l2-idx",
        type=int,
        default=_get_env_optional_int("DOMOTICZ_PHASE_EXPORT_L2_IDX", "31"),
        help="Domoticz phase L2 export device index. Defaults to DOMOTICZ_PHASE_EXPORT_L2_IDX or 31.",
    )
    parser.add_argument(
        "--domoticz-phase-export-l3-idx",
        type=int,
        default=_get_env_optional_int("DOMOTICZ_PHASE_EXPORT_L3_IDX", "33"),
        help="Domoticz phase L3 export device index. Defaults to DOMOTICZ_PHASE_EXPORT_L3_IDX or 33.",
    )
    parser.add_argument(
        "--domoticz-use-signed-net-power",
        action=argparse.BooleanOptionalAction,
        default=_get_env_bool("DOMOTICZ_USE_SIGNED_NET_POWER", "1"),
        help=(
            "Encode signed net power semantics in the capture bundle (Usage-UsageDeliv and per-phase import-export). "
            "Defaults to DOMOTICZ_USE_SIGNED_NET_POWER or true."
        ),
    )
    parser.add_argument(
        "--domoticz-timeout",
        type=float,
        default=float(_get_env_setting("DOMOTICZ_TIMEOUT_SECONDS", "1.0")),
        help="Domoticz HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--modbus-gw-host",
        default=_get_env_setting("MODBUS_TCP_GW_IP", "192.168.1.140"),
        help="Modbus TCP gateway host.",
    )
    parser.add_argument(
        "--modbus-gw-port",
        type=int,
        default=int(_get_env_setting("MODBUS_TCP_GW_PORT", "8899")),
        help="Modbus TCP gateway port.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s dump-registers: %(message)s",
        level=logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    source_slaves = _parse_int_list(args.source_slaves or ["100", "2"])
    register_names = args.register_names or _default_register_names()

    from modbus_tk import modbus_tcp
    import modbus_tk.defines as cst

    tcp_master = modbus_tcp.TcpMaster(
        host=args.modbus_gw_host,
        port=args.modbus_gw_port,
        timeout_in_sec=5.0,
    )
    try:
        captures = capture_register_blocks(
            tcp_master=tcp_master,
            source_slaves=source_slaves,
            register_names=register_names,
            read_holding_registers=cst.READ_HOLDING_REGISTERS,
        )

        domoticz_reading = None
        domoticz_phase_usage_watts = None
        domoticz_phase_import_watts = None
        domoticz_phase_export_watts = None
        domoticz_url = None
        domoticz_grid_idx = None
        if not args.skip_domoticz:
            domoticz_client = DomoticzClient(
                args.domoticz_url,
                args.domoticz_grid_idx,
                timeout_seconds=args.domoticz_timeout,
            )
            requested_indices = [
                int(args.domoticz_grid_idx),
                int(args.domoticz_phase_l1_idx),
                int(args.domoticz_phase_l2_idx),
                int(args.domoticz_phase_l3_idx),
            ]
            export_phase_idxs = (
                args.domoticz_phase_export_l1_idx,
                args.domoticz_phase_export_l2_idx,
                args.domoticz_phase_export_l3_idx,
            )
            if args.domoticz_use_signed_net_power and all(
                value is not None and int(value) > 0 for value in export_phase_idxs
            ):
                requested_indices.extend(int(value) for value in export_phase_idxs if value is not None)

            devices = domoticz_client.fetch_devices(requested_indices)
            domoticz_reading = domoticz_client.fetch_reading_from_device(devices[int(args.domoticz_grid_idx)])
            domoticz_phase_import_watts = (
                domoticz_client.data_watts_from_device(devices[int(args.domoticz_phase_l1_idx)]),
                domoticz_client.data_watts_from_device(devices[int(args.domoticz_phase_l2_idx)]),
                domoticz_client.data_watts_from_device(devices[int(args.domoticz_phase_l3_idx)]),
            )
            domoticz_phase_usage_watts = domoticz_phase_import_watts
            if args.domoticz_use_signed_net_power:
                if all(value is not None and int(value) > 0 for value in export_phase_idxs):
                    domoticz_phase_export_watts = (
                        domoticz_client.data_watts_from_device(devices[int(export_phase_idxs[0])]),
                        domoticz_client.data_watts_from_device(devices[int(export_phase_idxs[1])]),
                        domoticz_client.data_watts_from_device(devices[int(export_phase_idxs[2])]),
                    )
                    domoticz_phase_usage_watts = tuple(
                        float(domoticz_phase_import_watts[index]) - float(domoticz_phase_export_watts[index])
                        for index in range(3)
                    )
                else:
                    logging.warning(
                        "DOMOTICZ_USE_SIGNED_NET_POWER is enabled but one or more DOMOTICZ_PHASE_EXPORT_*_IDX values are missing; "
                        "phase values in this bundle remain unsigned import."
                    )
            domoticz_url = domoticz_client.url
            domoticz_grid_idx = args.domoticz_grid_idx

        bundle = build_dump_bundle(
            captures,
            modbus_tcp_gateway=args.modbus_gw_host,
            modbus_tcp_port=args.modbus_gw_port,
            request_source_slaves=source_slaves,
            request_register_names=register_names,
            domoticz_reading=domoticz_reading,
            domoticz_url=domoticz_url,
            domoticz_grid_idx=domoticz_grid_idx,
            domoticz_use_signed_net_power=bool(args.domoticz_use_signed_net_power),
            domoticz_phase_import_watts=domoticz_phase_import_watts,
            domoticz_phase_export_watts=domoticz_phase_export_watts,
            domoticz_phase_usage_watts=domoticz_phase_usage_watts,
        )
        write_dump_bundle(bundle, output_path=args.output_path)
    finally:
        tcp_master.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
