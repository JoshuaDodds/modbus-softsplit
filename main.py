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
    CerboMqttSnapshot,
    CerboMqttCache,
    CerboMqttPoller,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    describe_instantaneous_preview_basis,
    derive_phase_watts_from_currents,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    net_signed_phase_watts_to_nonnegative_import,
    preview_signature,
    rewrite_pv_instantaneous_values,
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


def _parse_csv_setting(name: str, default: str = "") -> tuple[str, ...]:
    raw_value = str(_get_setting(name, default) or "")
    items = [item.strip().strip("'").strip('"') for item in raw_value.split(",")]
    return tuple(item for item in items if item)


def _normalize_phase_power_source(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"activein", "acout", "abb"}:
        return normalized
    return "activein"


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
CERBO_PV_TOPICS = _parse_csv_setting(
    "CERBO_PV_TOPICS",
    "N/48e7da878d35/system/0/Dc/Pv/Power",
)
CERBO_ENABLE_PV_SLAVE = _parse_bool_setting("CERBO_ENABLE_PV_SLAVE", "1")
CERBO_PV_TARGET_SLAVE = max(int(_get_setting("CERBO_PV_TARGET_SLAVE", "1")), 1)
CERBO_PV_SIGN_NEGATIVE = _parse_bool_setting("CERBO_PV_SIGN_NEGATIVE", "1")
CERBO_SUBTRACT_PV_FROM_HOME_USAGE = _parse_bool_setting("CERBO_SUBTRACT_PV_FROM_HOME_USAGE", "1")
CERBO_PHASE_POWER_SOURCE = _normalize_phase_power_source(_get_setting("CERBO_PHASE_POWER_SOURCE", "activein"))
CERBO_FORCE_NONNEGATIVE_PHASE_POWER = _parse_bool_setting("CERBO_FORCE_NONNEGATIVE_PHASE_POWER", "0")
CERBO_COHERENT_PHASE_FRAMES = _parse_bool_setting("CERBO_COHERENT_PHASE_FRAMES", "1")
CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS = max(
    float(_get_setting("CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS", "1.5")),
    0.0,
)
CERBO_MQTT_PROTOCOL_DEBUG = _parse_bool_setting("CERBO_MQTT_PROTOCOL_DEBUG", "0")
CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS = max(
    float(_get_setting("CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS", "0.0")),
    0.0,
)
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
            "Cerbo MQTT source: host=%s(%s) port=%s(%s) ac_out_topic=%s(%s) ac_activein_topic=%s(%s) "
            "pv_topics=%s(%s) pv_slave_enabled=%s(%s) pv_target_slave=%s(%s) "
            "pv_sign_negative=%s(%s) subtract_pv_from_home_usage=%s(%s) "
            "phase_power_source=%s(%s) clamp_negative_phase_power=%s(%s) "
            "coherent_phase_frames=%s(%s) coherent_phase_frame_max_skew_seconds=%.2f(%s) "
            "protocol_debug=%s(%s) snapshot_debug_interval_seconds=%.2f(%s)"
        ),
        MOSQUITTO_IP,
        _get_setting_source("MOSQUITTO_IP"),
        MOSQUITTO_PORT,
        _get_setting_source("MOSQUITTO_PORT"),
        CERBO_AC_OUT_TOPIC,
        _get_setting_source("CERBO_AC_OUT_TOPIC"),
        CERBO_AC_ACTIVEIN_TOPIC,
        _get_setting_source("CERBO_AC_ACTIVEIN_TOPIC"),
        ",".join(CERBO_PV_TOPICS) if CERBO_PV_TOPICS else "(none)",
        _get_setting_source("CERBO_PV_TOPICS"),
        int(CERBO_ENABLE_PV_SLAVE),
        _get_setting_source("CERBO_ENABLE_PV_SLAVE"),
        CERBO_PV_TARGET_SLAVE,
        _get_setting_source("CERBO_PV_TARGET_SLAVE"),
        int(CERBO_PV_SIGN_NEGATIVE),
        _get_setting_source("CERBO_PV_SIGN_NEGATIVE"),
        int(CERBO_SUBTRACT_PV_FROM_HOME_USAGE),
        _get_setting_source("CERBO_SUBTRACT_PV_FROM_HOME_USAGE"),
        CERBO_PHASE_POWER_SOURCE,
        _get_setting_source("CERBO_PHASE_POWER_SOURCE"),
        int(CERBO_FORCE_NONNEGATIVE_PHASE_POWER),
        _get_setting_source("CERBO_FORCE_NONNEGATIVE_PHASE_POWER"),
        int(CERBO_COHERENT_PHASE_FRAMES),
        _get_setting_source("CERBO_COHERENT_PHASE_FRAMES"),
        CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS,
        _get_setting_source("CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS"),
        int(CERBO_MQTT_PROTOCOL_DEBUG),
        _get_setting_source("CERBO_MQTT_PROTOCOL_DEBUG"),
        CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS,
        _get_setting_source("CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS"),
    )
    if CERBO_AC_OUT_TOPIC == CERBO_AC_ACTIVEIN_TOPIC:
        logger.warning(
            "CERBO_AC_OUT_TOPIC and CERBO_AC_ACTIVEIN_TOPIC are equal. "
            "This can corrupt register intent between current and active-power rewrites."
        )
    if _get_setting("CERBO_PHASE_POWER_SOURCE", "activein").strip().lower() not in {"activein", "acout", "abb"}:
        logger.warning(
            "Unsupported CERBO_PHASE_POWER_SOURCE=%r; using 'activein'. "
            "Supported values: activein, acout, abb.",
            _get_setting("CERBO_PHASE_POWER_SOURCE", "activein"),
        )
    if CERBO_ENABLE_PV_SLAVE and CERBO_PV_TARGET_SLAVE in {2, 100}:
        logger.warning(
            "CERBO_PV_TARGET_SLAVE=%s collides with existing virtual meters (2,100); "
            "PV slave emulation will be disabled at runtime.",
            CERBO_PV_TARGET_SLAVE,
        )


def _resolve_phase_usage_watts_for_rewrite(
    *,
    source_values,
    usage_snapshot,
):
    if usage_snapshot is None:
        return None

    if CERBO_PHASE_POWER_SOURCE == "abb":
        phase_usage_watts = None
    elif CERBO_PHASE_POWER_SOURCE == "acout":
        phase_usage_watts = derive_phase_watts_from_currents(
            source_values,
            usage_snapshot.phase_current_amps,
        )
    else:
        phase_usage_watts = usage_snapshot.phase_usage_watts

    if phase_usage_watts is None:
        return None
    if CERBO_FORCE_NONNEGATIVE_PHASE_POWER and CERBO_PHASE_POWER_SOURCE == "activein":
        # In activein mode we synthesize import-only phase words by netting
        # export against import across phases first, then clamping to >= 0.
        return net_signed_phase_watts_to_nonnegative_import(
            tuple(float(value) for value in phase_usage_watts)
        )
    if CERBO_FORCE_NONNEGATIVE_PHASE_POWER:
        return tuple(max(float(value), 0.0) for value in phase_usage_watts)
    return tuple(float(value) for value in phase_usage_watts)


def _allow_negative_phase_power_for_rewrite() -> bool:
    if CERBO_SUBTRACT_PV_FROM_HOME_USAGE:
        # Keep only total power signed in home-PV offset mode.
        # Phase power words are intentionally non-negative for Maxem stability.
        return False
    if CERBO_FORCE_NONNEGATIVE_PHASE_POWER:
        return False
    return CERBO_PHASE_POWER_SOURCE == "activein"


def _build_preview_snapshot_for_logging(
    usage_snapshot,
    usage_watts,
    phase_usage_watts,
):
    if usage_snapshot is None:
        return None
    return CerboMqttSnapshot(
        sequence=getattr(usage_snapshot, "sequence", 0),
        ac_in_phase_watts=(
            tuple(float(value) for value in phase_usage_watts)
            if phase_usage_watts is not None
            else getattr(usage_snapshot, "phase_usage_watts", None)
        ),
        ac_in_total_watts=float(usage_watts) if usage_watts is not None else getattr(usage_snapshot, "rewrite_usage_watts", 0.0),
        ac_out_phase_currents=getattr(usage_snapshot, "phase_current_amps", None),
        ac_out_current_n=getattr(usage_snapshot, "current_n_amps", None),
        pv_total_watts=getattr(usage_snapshot, "pv_total_watts", None),
    )


def _snapshot_pv_total_watts(usage_snapshot) -> float | None:
    if usage_snapshot is None:
        return None
    pv_total_watts = getattr(usage_snapshot, "pv_total_watts", None)
    if pv_total_watts is None:
        return None
    return max(float(pv_total_watts), 0.0)


def _split_total_watts_evenly_signed(total_watts: float) -> tuple[float, float, float]:
    value = float(total_watts)
    per_phase = value / 3.0
    return (
        per_phase,
        per_phase,
        value - (2.0 * per_phase),
    )


def _apply_pv_offset_to_home_usage(
    *,
    usage_watts: float,
    phase_usage_watts: tuple[float, float, float] | None,
    usage_snapshot,
) -> tuple[float, tuple[float, float, float] | None]:
    if not CERBO_SUBTRACT_PV_FROM_HOME_USAGE:
        return float(usage_watts), phase_usage_watts

    pv_total_watts = _snapshot_pv_total_watts(usage_snapshot)
    if pv_total_watts is None or pv_total_watts <= 0.0:
        return float(usage_watts), phase_usage_watts

    # Offset PV generation from home/grid usage rewrite; signed result is intentional.
    adjusted_usage_watts = float(usage_watts) - float(pv_total_watts)

    # Keep total/phase power coherent by spreading signed remainder across L1/L2/L3.
    adjusted_phase_usage_watts = _split_total_watts_evenly_signed(adjusted_usage_watts)
    return adjusted_usage_watts, adjusted_phase_usage_watts


def _clamp_unsigned_usage_for_rewrite(
    *,
    usage_watts: float,
    phase_usage_watts: tuple[float, float, float] | None,
) -> tuple[float, tuple[float, float, float] | None]:
    clamped_usage_watts = max(float(usage_watts), 0.0)
    if phase_usage_watts is None:
        return clamped_usage_watts, None
    return clamped_usage_watts, tuple(max(float(value), 0.0) for value in phase_usage_watts)


def _pv_preview_signature(
    capture: RegisterCapture,
    *,
    pv_total_watts: float | None,
    pv_negative: bool,
) -> tuple[object, ...]:
    return (
        capture.target_slave,
        capture.source_slave,
        capture.register_name,
        capture.address,
        capture.address_length,
        capture.source_values,
        pv_total_watts,
        pv_negative,
    )


def _format_pv_preview_lines(
    *,
    pv_target_slave: int,
    pv_total_watts: float | None,
    pv_negative: bool,
) -> list[str]:
    if pv_total_watts is None:
        return [f"Cerbo PV to Maxem (slave {pv_target_slave:03d}): awaiting baseline"]

    pv_raw_watts = max(float(pv_total_watts), 0.0)
    pv_signed_total_watts = -pv_raw_watts if pv_negative else pv_raw_watts
    return [
        f"Cerbo PV to Maxem (slave {pv_target_slave:03d}): {pv_signed_total_watts:,.2f} W",
        (
            f"Cerbo PV Phase Watts to Maxem (slave {pv_target_slave:03d}): "
            f"L1={pv_signed_total_watts:,.2f} W, L2=0.00 W, L3=0.00 W"
        ),
    ]


def main():
    args = _parse_args()
    dry_run_maxem_home = args.dry_run_maxem_home
    trace_instantaneous_payload = args.trace_instantaneous_payload
    tcp_slave_server = None
    rtu_slave_server = None
    maxem_100 = None
    maxem_2 = None
    maxem_pv = None
    victron_100 = None
    victron_2 = None
    rewrite_cache = None
    rewrite_poller = None
    tcp_master = None
    status_ticker = _LoopStatusTicker(STATUS_LOG_INTERVAL_SECONDS)
    pv_slave_enabled_runtime = CERBO_ENABLE_PV_SLAVE and CERBO_PV_TARGET_SLAVE not in {2, 100}

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
            pv_power_topics=CERBO_PV_TOPICS,
            cache=rewrite_cache,
            protocol_debug=CERBO_MQTT_PROTOCOL_DEBUG,
            snapshot_debug_interval_seconds=CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS,
            coherent_phase_frames=CERBO_COHERENT_PHASE_FRAMES,
            coherent_phase_frame_max_skew_seconds=CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS,
            logger=logger,
        )
        rewrite_poller.start()
        if CERBO_ENABLE_PV_SLAVE and not pv_slave_enabled_runtime:
            logger.warning(
                "PV virtual meter disabled because CERBO_PV_TARGET_SLAVE=%s collides with existing slave addresses.",
                CERBO_PV_TARGET_SLAVE,
            )
        elif pv_slave_enabled_runtime:
            logger.info(
                "PV virtual meter slave %03d enabled: only instantaneous_values are synthesized; non-instantaneous blocks are not mirrored from slave 100.",
                CERBO_PV_TARGET_SLAVE,
            )

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
            if pv_slave_enabled_runtime:
                maxem_pv = rtu_slave_server.add_slave(CERBO_PV_TARGET_SLAVE)

            # Maxem Home compatible memory blocks
            for register_name in MAXEM_HOLDING_REGISTERS:
                addr = MAXEM_HOLDING_REGISTERS[register_name][0]
                addr_len = MAXEM_HOLDING_REGISTERS[register_name][1]
                maxem_100.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
                maxem_2.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)
                if maxem_pv is not None:
                    maxem_pv.add_block(register_name, cst.HOLDING_REGISTERS, addr, addr_len)

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
                            phase_usage_watts = _resolve_phase_usage_watts_for_rewrite(
                                source_values=acload_values,
                                usage_snapshot=preview_snapshot,
                            )
                            usage_watts, phase_usage_watts = _apply_pv_offset_to_home_usage(
                                usage_watts=float(usage_watts),
                                phase_usage_watts=phase_usage_watts,
                                usage_snapshot=preview_snapshot,
                            )
                            usage_watts, phase_usage_watts = _clamp_unsigned_usage_for_rewrite(
                                usage_watts=usage_watts,
                                phase_usage_watts=phase_usage_watts,
                            )
                            phase_current_amps = preview_snapshot.phase_current_amps if preview_snapshot else None
                            current_n_amps = preview_snapshot.current_n_amps if preview_snapshot else None
                            preview_display_snapshot = _build_preview_snapshot_for_logging(
                                preview_snapshot,
                                usage_watts,
                                phase_usage_watts,
                            )
                            rewritten_values = rewrite_instantaneous_values(
                                acload_values,
                                usage_watts=usage_watts,
                                phase_usage_watts=phase_usage_watts,
                                phase_current_amps=phase_current_amps,
                                current_n_amps=current_n_amps,
                                allow_negative=False,
                                allow_negative_phase=False,
                            )
                            preview_signature_value = preview_signature(
                                capture,
                                snapshot=preview_display_snapshot,
                            )
                            preview_signature_key = (capture.target_slave, capture.source_slave, capture.register_name)
                            if preview_signature_value != last_preview_signatures.get(preview_signature_key):
                                for preview_line in format_instantaneous_preview_lines(
                                    capture,
                                    snapshot=preview_display_snapshot,
                                ):
                                    logger.debug(preview_line)
                                if trace_instantaneous_payload:
                                    for trace_line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
                                        logger.info(f"trace {trace_line}")
                                last_preview_signatures[preview_signature_key] = preview_signature_value

                            if pv_slave_enabled_runtime:
                                pv_total_watts = _snapshot_pv_total_watts(preview_snapshot)
                                pv_capture = RegisterCapture(
                                    target_slave=CERBO_PV_TARGET_SLAVE,
                                    source_slave=100,
                                    register_name=register_name,
                                    address=addr,
                                    address_length=addr_len,
                                    source_values=tuple(int(value) for value in acload_values),
                                )
                                pv_rewritten_values = rewrite_pv_instantaneous_values(
                                    acload_values,
                                    pv_total_watts=pv_total_watts,
                                    pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                )
                                pv_preview_signature_value = _pv_preview_signature(
                                    pv_capture,
                                    pv_total_watts=pv_total_watts,
                                    pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                )
                                pv_preview_signature_key = (
                                    pv_capture.target_slave,
                                    pv_capture.source_slave,
                                    pv_capture.register_name,
                                )
                                if pv_preview_signature_value != last_preview_signatures.get(pv_preview_signature_key):
                                    for preview_line in _format_pv_preview_lines(
                                        pv_target_slave=CERBO_PV_TARGET_SLAVE,
                                        pv_total_watts=pv_total_watts,
                                        pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                    ):
                                        logger.debug(preview_line)
                                    if trace_instantaneous_payload:
                                        for trace_line in format_instantaneous_diff_lines(
                                            pv_capture.source_values,
                                            pv_rewritten_values,
                                        ):
                                            logger.info(f"trace pv {trace_line}")
                                    last_preview_signatures[pv_preview_signature_key] = pv_preview_signature_value
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
                                phase_usage_watts = _resolve_phase_usage_watts_for_rewrite(
                                    source_values=acload_values,
                                    usage_snapshot=usage_snapshot,
                                )
                                usage_watts, phase_usage_watts = _apply_pv_offset_to_home_usage(
                                    usage_watts=float(usage_watts),
                                    phase_usage_watts=phase_usage_watts,
                                    usage_snapshot=usage_snapshot,
                                )
                                usage_watts, phase_usage_watts = _clamp_unsigned_usage_for_rewrite(
                                    usage_watts=usage_watts,
                                    phase_usage_watts=phase_usage_watts,
                                )
                                phase_current_amps = usage_snapshot.phase_current_amps if usage_snapshot else None
                                current_n_amps = usage_snapshot.current_n_amps if usage_snapshot else None
                                live_preview_snapshot = _build_preview_snapshot_for_logging(
                                    usage_snapshot,
                                    usage_watts,
                                    phase_usage_watts,
                                )
                                rewritten_values = rewrite_instantaneous_values(
                                    acload_values,
                                    usage_watts=usage_watts,
                                    phase_usage_watts=phase_usage_watts,
                                    phase_current_amps=phase_current_amps,
                                    current_n_amps=current_n_amps,
                                    allow_negative=False,
                                    allow_negative_phase=False,
                                )
                                live_preview_signature = preview_signature(
                                    capture,
                                    snapshot=live_preview_snapshot,
                                )
                                live_preview_signature_key = (capture.target_slave, capture.source_slave, capture.register_name)
                                if live_preview_signature != last_preview_signatures.get(live_preview_signature_key):
                                    for preview_line in format_instantaneous_preview_lines(
                                        capture,
                                        snapshot=live_preview_snapshot,
                                    ):
                                        logger.debug(preview_line)
                                    if trace_instantaneous_payload:
                                        for trace_line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
                                            logger.info(f"trace {trace_line}")
                                    last_preview_signatures[live_preview_signature_key] = live_preview_signature
                                maxem_100.set_values(register_name, addr, rewritten_values)

                                if maxem_pv is not None:
                                    pv_total_watts = _snapshot_pv_total_watts(usage_snapshot)
                                    pv_capture = RegisterCapture(
                                        target_slave=CERBO_PV_TARGET_SLAVE,
                                        source_slave=100,
                                        register_name=register_name,
                                        address=addr,
                                        address_length=addr_len,
                                        source_values=tuple(int(value) for value in acload_values),
                                    )
                                    pv_rewritten_values = rewrite_pv_instantaneous_values(
                                        acload_values,
                                        pv_total_watts=pv_total_watts,
                                        pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                    )
                                    pv_preview_signature_value = _pv_preview_signature(
                                        pv_capture,
                                        pv_total_watts=pv_total_watts,
                                        pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                    )
                                    pv_preview_signature_key = (
                                        pv_capture.target_slave,
                                        pv_capture.source_slave,
                                        pv_capture.register_name,
                                    )
                                    if pv_preview_signature_value != last_preview_signatures.get(pv_preview_signature_key):
                                        for preview_line in _format_pv_preview_lines(
                                            pv_target_slave=CERBO_PV_TARGET_SLAVE,
                                            pv_total_watts=pv_total_watts,
                                            pv_negative=CERBO_PV_SIGN_NEGATIVE,
                                        ):
                                            logger.debug(preview_line)
                                        if trace_instantaneous_payload:
                                            for trace_line in format_instantaneous_diff_lines(
                                                pv_capture.source_values,
                                                pv_rewritten_values,
                                            ):
                                                logger.info(f"trace pv {trace_line}")
                                        last_preview_signatures[pv_preview_signature_key] = pv_preview_signature_value
                                    maxem_pv.set_values(register_name, addr, pv_rewritten_values)
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
