#!/usr/bin/python3 -u
import argparse
import logging as logger
import os

from dotenv import dotenv_values

import modbus_tk.defines as cst
from modbus_tk import modbus_rtu, modbus_tcp

import serial

from lib.register_maps import MAXEM_HOLDING_REGISTERS, VICTRON_HOLDING_REGISTERS
from lib.maxem_home_usage import (
    DomoticzUsageCache,
    DomoticzUsagePoller,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    describe_instantaneous_preview_basis,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    preview_signature,
    rewrite_instantaneous_values,
)
from lib.synthetic_home import DomoticzClient, RegisterCapture

_DOTENV = dotenv_values(".env")


def _get_setting(name, default=None):
    value = os.environ.get(name)
    if value not in (None, ""):
        return value

    value = _DOTENV.get(name)
    if value in (None, ""):
        return default

    return value


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Modbus softsplit proxy")
    parser.add_argument(
        "--dry-run-maxem-home",
        action="store_true",
        help=(
            "Skip the RTU serial adapter and log the ABB instantaneous-power rewrite plan instead of writing "
            "to the RTU slave."
        ),
    )
    parser.add_argument(
        "--trace-instantaneous-payload",
        action="store_true",
        help=(
            "Log detailed field-level ABB source vs rewritten instantaneous_values diagnostics "
            "(for deep-dive debugging)."
        ),
    )
    return parser.parse_args(argv)


SERIAL_PORT = _get_setting("SERIAL_PORT", "/dev/ttyXRUSB0")
MODBUS_TCP_GW = _get_setting("MODBUS_TCP_GW_IP", "192.168.1.140")
MODBUS_TCP_GW_PORT = int(_get_setting("MODBUS_TCP_GW_PORT", "8899"))
DOMOTICZ_URL = _get_setting("DOMOTICZ_URL", "http://dz-insecure.hs.mfis.net")
DOMOTICZ_GRID_IDX = int(_get_setting("DOMOTICZ_GRID_IDX", "20"))
DOMOTICZ_TIMEOUT_SECONDS = float(_get_setting("DOMOTICZ_TIMEOUT_SECONDS", "1.0"))
DOMOTICZ_USAGE_POLL_INTERVAL_SECONDS = float(
    _get_setting("DOMOTICZ_USAGE_POLL_INTERVAL_SECONDS", "5.0")
)
DOMOTICZ_PHASE_L1_IDX = int(_get_setting("DOMOTICZ_PHASE_L1_IDX", "26"))
DOMOTICZ_PHASE_L2_IDX = int(_get_setting("DOMOTICZ_PHASE_L2_IDX", "25"))
DOMOTICZ_PHASE_L3_IDX = int(_get_setting("DOMOTICZ_PHASE_L3_IDX", "24"))

logger.basicConfig(
    format='%(asctime)s modbus-gw: %(message)s',
    level=logger.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')


def _stop_runtime(
    tcp_master,
    tcp_slave_server,
    rtu_slave_server,
    usage_poller,
):
    if usage_poller:
        usage_poller.stop()
        usage_poller.join(timeout=2.0)

    if rtu_slave_server:
        rtu_slave_server.stop()

    if tcp_slave_server:
        tcp_slave_server.stop()

    if tcp_master:
        tcp_master.close()


def main():
    args = _parse_args()
    dry_run_maxem_home = args.dry_run_maxem_home
    trace_instantaneous_payload = args.trace_instantaneous_payload
    tcp_slave_server = None
    rtu_slave_server = None
    maxem_100 = None
    maxem_2 = None
    victron_100 = None
    victron_2 = None
    usage_cache = None
    usage_poller = None
    tcp_master = None

    try:
        tcp_slave_server = modbus_tcp.TcpServer(port=502)

        victron_100 = tcp_slave_server.add_slave(100)
        victron_2 = tcp_slave_server.add_slave(2)

        # Create registers for virtual slave devices
        # Victron Energy compatible memory blocks
        for register_name in VICTRON_HOLDING_REGISTERS:
            addr = VICTRON_HOLDING_REGISTERS[register_name][0]
            addr_len = VICTRON_HOLDING_REGISTERS[register_name][1]
            victron_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
            victron_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

        tcp_slave_server.start()
        logger.info(f"Modbus TCP slave server started...")

        domoticz_client = DomoticzClient(
            DOMOTICZ_URL,
            DOMOTICZ_GRID_IDX,
            timeout_seconds=DOMOTICZ_TIMEOUT_SECONDS,
        )
        usage_cache = DomoticzUsageCache()
        usage_poller = DomoticzUsagePoller(
            domoticz_client,
            usage_cache,
            phase_l1_idx=DOMOTICZ_PHASE_L1_IDX,
            phase_l2_idx=DOMOTICZ_PHASE_L2_IDX,
            phase_l3_idx=DOMOTICZ_PHASE_L3_IDX,
            poll_interval_seconds=DOMOTICZ_USAGE_POLL_INTERVAL_SECONDS,
            logger=logger,
        )
        usage_poller.start()

        if dry_run_maxem_home:
            logger.info(
                "Maxem Home dry-run preview enabled; the RTU serial adapter will not be opened and Maxem writes will be logged only."
            )
            logger.info(describe_instantaneous_preview_basis())
        else:
            rtu_slave_server = modbus_rtu.RtuServer(
                serial.Serial(
                    port=SERIAL_PORT,
                    baudrate=19200,
                    bytesize=8,
                    parity=serial.PARITY_EVEN,
                    stopbits=serial.STOPBITS_ONE,
                    xonxoff=0,
                    timeout=1,
                )
            )
            maxem_100 = rtu_slave_server.add_slave(100)
            maxem_2 = rtu_slave_server.add_slave(2)

            # Maxem Home compatible memory blocks
            for register_name in MAXEM_HOLDING_REGISTERS:
                addr = MAXEM_HOLDING_REGISTERS[register_name][0]
                addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]
                maxem_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
                maxem_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

            rtu_slave_server.start()
            logger.info(f"Modbus RTU slave server started...")

        tcp_master = modbus_tcp.TcpMaster(host=MODBUS_TCP_GW, port=MODBUS_TCP_GW_PORT, timeout_in_sec=5.0)
        last_preview_signatures = {}

        while True:
            try:
                # Poll the real ABB B23 hardware slaves via network connected Modbus-TCP server (waveshare / EW-11 / etc.)
                # and copy that data to the 'virtual' slaves.
                # Victron
                for register_name in VICTRON_HOLDING_REGISTERS:
                    addr = VICTRON_HOLDING_REGISTERS[register_name][0]
                    addr_len = VICTRON_HOLDING_REGISTERS[register_name][1]

                    acload_values = tcp_master.execute(100, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                    if acload_values:
                        if tcp_slave_server and victron_100:
                            victron_100.set_values(register_name, addr, acload_values)
                    tesla_values = tcp_master.execute(2, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                    if tesla_values:
                        if tcp_slave_server and victron_2:
                            victron_2.set_values(register_name, addr, tesla_values)
                if not dry_run_maxem_home:
                    logger.info(f"TCP slave data updated.")

                # Maxem
                for register_name in MAXEM_HOLDING_REGISTERS:
                    addr = MAXEM_HOLDING_REGISTERS[register_name][0]
                    addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]

                    if dry_run_maxem_home and register_name != INSTANTANEOUS_VALUES_REGISTER_NAME:
                        continue

                    acload_values = tcp_master.execute(100, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                    if acload_values:
                        capture = RegisterCapture(
                            target_slave=100,
                            source_slave=100,
                            register_name=register_name,
                            address=addr,
                            address_length=addr_len,
                            source_values=tuple(int(value) for value in acload_values),
                        )
                        if dry_run_maxem_home:
                            preview_snapshot = usage_cache.snapshot() if usage_cache is not None else None
                            usage_watts = preview_snapshot.grid_import_watts if preview_snapshot else 0.0
                            phase_usage_watts = preview_snapshot.phase_usage_watts if preview_snapshot else None
                            rewritten_values = rewrite_instantaneous_values(
                                acload_values,
                                usage_watts=usage_watts,
                                phase_usage_watts=phase_usage_watts,
                            )
                            preview_signature_value = preview_signature(
                                capture,
                                snapshot=preview_snapshot,
                            )
                            if preview_signature_value != last_preview_signatures.get((capture.target_slave, capture.source_slave, capture.register_name)):
                                for preview_line in format_instantaneous_preview_lines(
                                    capture,
                                    snapshot=preview_snapshot,
                                ):
                                    logger.info(preview_line)
                                if trace_instantaneous_payload:
                                    for trace_line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
                                        logger.info(f"trace {trace_line}")
                                last_preview_signatures[
                                    (capture.target_slave, capture.source_slave, capture.register_name)
                                ] = preview_signature_value
                        elif rtu_slave_server and maxem_100:
                            # Rewrite the instantaneous ABB power block from Domoticz Usage; mirror every other Maxem block.
                            usage_snapshot = usage_cache.snapshot() if usage_cache is not None else None
                            if register_name == INSTANTANEOUS_VALUES_REGISTER_NAME:
                                usage_watts = usage_snapshot.grid_import_watts if usage_snapshot else 0.0
                                phase_usage_watts = usage_snapshot.phase_usage_watts if usage_snapshot else None
                                rewritten_values = rewrite_instantaneous_values(
                                    acload_values,
                                    usage_watts=usage_watts,
                                    phase_usage_watts=phase_usage_watts,
                                )
                                live_preview_signature = preview_signature(
                                    capture,
                                    snapshot=usage_snapshot,
                                )
                                if live_preview_signature != last_preview_signatures.get((capture.target_slave, capture.source_slave, capture.register_name)):
                                    for preview_line in format_instantaneous_preview_lines(
                                        capture,
                                        snapshot=usage_snapshot,
                                    ):
                                        logger.info(preview_line)
                                    if trace_instantaneous_payload:
                                        for trace_line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
                                            logger.info(f"trace {trace_line}")
                                    last_preview_signatures[
                                        (capture.target_slave, capture.source_slave, capture.register_name)
                                    ] = live_preview_signature
                                maxem_100.set_values(register_name, addr, rewritten_values)
                            else:
                                maxem_100.set_values(register_name, addr, acload_values)
                    if dry_run_maxem_home:
                        continue

                    tesla_values = tcp_master.execute(2, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                    if tesla_values and rtu_slave_server and maxem_2:
                        maxem_2.set_values(register_name, addr, tesla_values)

                if not dry_run_maxem_home:
                    logger.info(f"RTU slave data updated.")
            except Exception as exc:
                logger.error(f"loop error: {exc}")

    except KeyboardInterrupt:
        logger.info("Shutdown requested via Ctrl-C; stopping cleanly...")
    except Exception as exc:
        logger.error(f"tcp_master(error): {exc}")
    finally:
        _stop_runtime(tcp_master, tcp_slave_server, rtu_slave_server, usage_poller)

if __name__ == "__main__":
    main()
