#!/usr/bin/python3 -u
import argparse
import logging as logger
import os
import time

from dotenv import dotenv_values

import modbus_tk
import modbus_tk.defines as cst
from modbus_tk import modbus_rtu, modbus_tcp

import serial

from lib.register_maps import MAXEM_HOLDING_REGISTERS, VICTRON_HOLDING_REGISTERS
from lib.maxem_home_usage import (
    CerboMqttCache,
    CerboMqttPoller,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    describe_instantaneous_preview_basis,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    preview_signature,
    rewrite_instantaneous_values,
)
from lib.synthetic_home import RegisterCapture

_DOTENV = dotenv_values(".env")


def _get_setting(name, default=None):
    value = os.environ.get(name)
    if value not in (None, ""):
        return value

    value = _DOTENV.get(name)
    if value in (None, ""):
        return default

    return value


def _get_setting_source(name: str) -> str:
    env_value = os.environ.get(name)
    if env_value not in (None, ""):
        return "env"
    dotenv_value = _DOTENV.get(name)
    if dotenv_value not in (None, ""):
        return ".env"
    return "default"


def _parse_bool_setting(name: str, default: str = "0") -> bool:
    raw_value = str(_get_setting(name, default)).strip().lower()
    return raw_value not in {"0", "false", "no", "off", ""}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Modbus softsplit proxy")
    parser.add_argument(
        "--dry-run-maxem-home",
        action="store_true",
        default=_parse_bool_setting("DRY_RUN_MAXEM_HOME", "0"),
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
MOSQUITTO_IP = _get_setting("MOSQUITTO_IP", "mosquitto.hs.mfis.net")
MOSQUITTO_PORT = int(_get_setting("MOSQUITTO_PORT", "1883"))
CERBO_AC_OUT_TOPIC = _get_setting("CERBO_AC_OUT_TOPIC", "N/48e7da878d35/vebus/276/Ac/Out")
CERBO_AC_ACTIVEIN_TOPIC = _get_setting("CERBO_AC_ACTIVEIN_TOPIC", "N/48e7da878d35/vebus/276/Ac/ActiveIn")
LOG_LEVEL_NAME = str(_get_setting("LOG_LEVEL", "INFO")).strip().upper()
LOG_LEVEL = getattr(logger, LOG_LEVEL_NAME, logger.INFO)
STATUS_LOG_INTERVAL_SECONDS = max(float(_get_setting("STATUS_LOG_INTERVAL_SECONDS", "30.0")), 0.0)
SUPPRESS_SHORT_RTU_REQUEST_LOGS = _parse_bool_setting("SUPPRESS_SHORT_RTU_REQUEST_LOGS", "1")

logger.basicConfig(
    format='%(asctime)s modbus-gw: %(message)s',
    level=LOG_LEVEL,
    datefmt='%Y-%m-%d %H:%M:%S')


class _SuppressShortRtuRequestNoiseFilter(logger.Filter):
    _noise_message = "invalid request: Request length is invalid 1"

    def filter(self, record: logger.LogRecord) -> bool:
        if record.name != "modbus_tk":
            return True
        try:
            return record.getMessage() != self._noise_message
        except Exception:  # pragma: no cover - defensive fallback
            return True


class _LoopStatusTicker:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = max(float(interval_seconds), 0.0)
        self._next_log_time = time.monotonic() + self._interval_seconds if self._interval_seconds > 0.0 else 0.0
        self._tcp_cycles = 0
        self._rtu_cycles = 0
        self._loop_errors = 0

    def mark_tcp_cycle(self) -> None:
        self._tcp_cycles += 1

    def mark_rtu_cycle(self) -> None:
        self._rtu_cycles += 1

    def mark_loop_error(self) -> None:
        self._loop_errors += 1

    def maybe_log(self) -> None:
        if self._interval_seconds <= 0.0:
            return

        now = time.monotonic()
        if now < self._next_log_time:
            return

        logger.info(
            "Mirror loop heartbeat: tcp_cycles=%d rtu_cycles=%d loop_errors=%d",
            self._tcp_cycles,
            self._rtu_cycles,
            self._loop_errors,
        )
        self._tcp_cycles = 0
        self._rtu_cycles = 0
        self._loop_errors = 0
        self._next_log_time = now + self._interval_seconds


if SUPPRESS_SHORT_RTU_REQUEST_LOGS:
    modbus_tk.LOGGER.addFilter(_SuppressShortRtuRequestNoiseFilter())


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


def _log_source_effective_config() -> None:
    logger.info(
        (
            "Cerbo MQTT source: host=%s(%s) port=%s(%s) ac_out_topic=%s(%s) ac_activein_topic=%s(%s)"
        ),
        MOSQUITTO_IP,
        _get_setting_source("MOSQUITTO_IP"),
        MOSQUITTO_PORT,
        _get_setting_source("MOSQUITTO_PORT"),
        CERBO_AC_OUT_TOPIC,
        _get_setting_source("CERBO_AC_OUT_TOPIC"),
        CERBO_AC_ACTIVEIN_TOPIC,
        _get_setting_source("CERBO_AC_ACTIVEIN_TOPIC"),
    )
    if CERBO_AC_OUT_TOPIC == CERBO_AC_ACTIVEIN_TOPIC:
        logger.warning(
            "CERBO_AC_OUT_TOPIC and CERBO_AC_ACTIVEIN_TOPIC are equal. "
            "This can corrupt register intent between current and active-power rewrites."
        )


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
    rewrite_cache = None
    rewrite_poller = None
    tcp_master = None
    status_ticker = _LoopStatusTicker(STATUS_LOG_INTERVAL_SECONDS)

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
        _log_source_effective_config()

        rewrite_cache = CerboMqttCache()
        rewrite_poller = CerboMqttPoller(
            broker_host=MOSQUITTO_IP,
            broker_port=MOSQUITTO_PORT,
            ac_out_topic_base=CERBO_AC_OUT_TOPIC,
            ac_active_in_topic_base=CERBO_AC_ACTIVEIN_TOPIC,
            cache=rewrite_cache,
            logger=logger,
        )
        rewrite_poller.start()

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
                status_ticker.mark_tcp_cycle()

                # Maxem
                for register_name in MAXEM_HOLDING_REGISTERS:
                    addr = MAXEM_HOLDING_REGISTERS[register_name][0]
                    addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]

                    if dry_run_maxem_home and register_name != INSTANTANEOUS_VALUES_REGISTER_NAME:
                        continue

                    acload_values = tcp_master.execute(100, cst.READ_HOLDING_REGISTERS, addr, addr_len)
                    if acload_values:
                        if dry_run_maxem_home:
                            capture = RegisterCapture(
                                target_slave=100,
                                source_slave=100,
                                register_name=register_name,
                                address=addr,
                                address_length=addr_len,
                                source_values=tuple(int(value) for value in acload_values),
                            )
                            preview_snapshot = rewrite_cache.snapshot() if rewrite_cache is not None else None
                            usage_watts = preview_snapshot.rewrite_usage_watts if preview_snapshot else 0.0
                            if usage_watts is None:
                                usage_watts = 0.0
                            phase_usage_watts = preview_snapshot.phase_usage_watts if preview_snapshot else None
                            phase_current_amps = preview_snapshot.phase_current_amps if preview_snapshot else None
                            current_n_amps = preview_snapshot.current_n_amps if preview_snapshot else None
                            rewritten_values = rewrite_instantaneous_values(
                                acload_values,
                                usage_watts=usage_watts,
                                phase_usage_watts=phase_usage_watts,
                                phase_current_amps=phase_current_amps,
                                current_n_amps=current_n_amps,
                                allow_negative=True,
                                allow_negative_phase=True,
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
                                    logger.debug(preview_line)
                                if trace_instantaneous_payload:
                                    for trace_line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
                                        logger.info(f"trace {trace_line}")
                                last_preview_signatures[
                                    (capture.target_slave, capture.source_slave, capture.register_name)
                                ] = preview_signature_value
                        elif rtu_slave_server and maxem_100:
                            # Rewrite only the selected instantaneous current/power words from Cerbo MQTT;
                            # mirror every other Maxem register block verbatim from ABB.
                            if register_name == INSTANTANEOUS_VALUES_REGISTER_NAME:
                                usage_snapshot = rewrite_cache.snapshot() if rewrite_cache is not None else None
                                capture = RegisterCapture(
                                    target_slave=100,
                                    source_slave=100,
                                    register_name=register_name,
                                    address=addr,
                                    address_length=addr_len,
                                    source_values=tuple(int(value) for value in acload_values),
                                )
                                usage_watts = usage_snapshot.rewrite_usage_watts if usage_snapshot else 0.0
                                if usage_watts is None:
                                    usage_watts = 0.0
                                phase_usage_watts = usage_snapshot.phase_usage_watts if usage_snapshot else None
                                phase_current_amps = usage_snapshot.phase_current_amps if usage_snapshot else None
                                current_n_amps = usage_snapshot.current_n_amps if usage_snapshot else None
                                rewritten_values = rewrite_instantaneous_values(
                                    acload_values,
                                    usage_watts=usage_watts,
                                    phase_usage_watts=phase_usage_watts,
                                    phase_current_amps=phase_current_amps,
                                    current_n_amps=current_n_amps,
                                    allow_negative=True,
                                    allow_negative_phase=True,
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
                                        logger.debug(preview_line)
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
                    status_ticker.mark_rtu_cycle()
                status_ticker.maybe_log()
            except Exception as exc:
                status_ticker.mark_loop_error()
                logger.error(f"loop error: {exc}")

    except KeyboardInterrupt:
        logger.info("Shutdown requested via Ctrl-C; stopping cleanly...")
    except Exception as exc:
        logger.error(f"tcp_master(error): {exc}")
    finally:
        _stop_runtime(tcp_master, tcp_slave_server, rtu_slave_server, rewrite_poller)

if __name__ == "__main__":
    main()
