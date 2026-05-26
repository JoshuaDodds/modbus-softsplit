from lib.maxem_home_usage import (
    INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET,
    INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VALUES_REGISTER_LENGTH,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    encode_signed_scaled_watts,
    format_instantaneous_preview_lines,
)
from lib.register_capture_tools import (
    build_dump_bundle,
    build_replay_preview_lines,
    build_replay_snapshot,
    bundle_captures,
    capture_register_blocks,
    load_dump_bundle,
    write_dump_bundle,
)
from lib.synthetic_home import DomoticzReading, RegisterCapture
from tools.dump_register_block import _default_register_names


class FakeMaster:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int]] = []

    def execute(self, slave: int, function_code: int, address: int, length: int):
        self.calls.append((slave, function_code, address, length))
        return (slave, address, length)


def _sample_instantaneous_capture(source_watts: float = 1_234.5) -> RegisterCapture:
    values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH
    high_word, low_word = encode_signed_scaled_watts(source_watts)
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET] = high_word
    values[INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1] = low_word
    return RegisterCapture(
        target_slave=100,
        source_slave=100,
        register_name=INSTANTANEOUS_VALUES_REGISTER_NAME,
        address=INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
        address_length=INSTANTANEOUS_VALUES_REGISTER_LENGTH,
        source_values=tuple(values),
    )


def _sample_captures() -> list[RegisterCapture]:
    return [
        _sample_instantaneous_capture(),
        RegisterCapture(
            target_slave=100,
            source_slave=100,
            register_name="settings",
            address=0x8C04,
            address_length=8,
            source_values=(5, 6, 7, 8),
        ),
    ]


def test_capture_register_blocks_uses_requested_registers() -> None:
    master = FakeMaster()

    captures = capture_register_blocks(
        tcp_master=master,
        source_slaves=[100, 2],
        register_names=[INSTANTANEOUS_VALUES_REGISTER_NAME, "settings"],
        read_holding_registers=3,
    )

    assert master.calls == [
        (100, 3, INSTANTANEOUS_VALUES_REGISTER_ADDRESS, INSTANTANEOUS_VALUES_REGISTER_LENGTH),
        (100, 3, 0x8C04, 8),
        (2, 3, INSTANTANEOUS_VALUES_REGISTER_ADDRESS, INSTANTANEOUS_VALUES_REGISTER_LENGTH),
        (2, 3, 0x8C04, 8),
    ]
    assert len(captures) == 4
    assert captures[0].source_values == (100, INSTANTANEOUS_VALUES_REGISTER_ADDRESS, INSTANTANEOUS_VALUES_REGISTER_LENGTH)
    assert captures[1].source_values == (100, 0x8C04, 8)


def test_dump_tool_defaults_to_instantaneous_values_only() -> None:
    assert _default_register_names() == [INSTANTANEOUS_VALUES_REGISTER_NAME]


def test_dump_bundle_round_trips_through_json(tmp_path) -> None:
    bundle = build_dump_bundle(
        _sample_captures(),
        modbus_tcp_gateway="192.168.1.140",
        modbus_tcp_port=8899,
        request_source_slaves=[100],
        request_register_names=[INSTANTANEOUS_VALUES_REGISTER_NAME, "settings"],
        domoticz_reading=DomoticzReading(
            import_kwh=12.5,
            export_kwh=3.25,
            import_watts=321.0,
            export_watts=0.0,
            last_update="2026-05-26 09:00:00",
        ),
        domoticz_url="http://dz-insecure.hs.mfis.net",
        domoticz_grid_idx=20,
    )

    bundle_path = tmp_path / "maxem-bundle.json"
    write_dump_bundle(bundle, output_path=str(bundle_path))
    loaded = load_dump_bundle(bundle_path)

    assert loaded["bundle_format_version"] == 1
    assert loaded["request"]["source_slaves"] == [100]
    assert loaded["request"]["register_names"] == [INSTANTANEOUS_VALUES_REGISTER_NAME, "settings"]
    assert loaded["domoticz"]["reading"]["import_watts"] == 321.0
    assert bundle_captures(loaded)[0].register_name == INSTANTANEOUS_VALUES_REGISTER_NAME


def test_replay_preview_highlights_the_grid_import_rewrite(tmp_path) -> None:
    bundle = build_dump_bundle(
        _sample_captures(),
        modbus_tcp_gateway="192.168.1.140",
        modbus_tcp_port=8899,
        request_source_slaves=[100],
        request_register_names=[INSTANTANEOUS_VALUES_REGISTER_NAME, "settings"],
        domoticz_reading=DomoticzReading(
            import_kwh=100.0,
            export_kwh=10.0,
            import_watts=500.0,
            export_watts=0.0,
            last_update="2026-05-26 09:00:00",
        ),
        domoticz_url="http://dz-insecure.hs.mfis.net",
        domoticz_grid_idx=20,
    )

    snapshot = build_replay_snapshot(bundle)
    lines = build_replay_preview_lines(bundle, snapshot=snapshot)

    assert lines == [
        "ABB source: 1,234.50 W",
        "DZ Usage to Maxem: 500 W",
    ]


def test_register_preview_uses_mirror_mode_for_victron_slave() -> None:
    capture = RegisterCapture(
        target_slave=2,
        source_slave=2,
        register_name=INSTANTANEOUS_VALUES_REGISTER_NAME,
        address=INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
        address_length=INSTANTANEOUS_VALUES_REGISTER_LENGTH,
        source_values=(0, 1, 2, 3),
    )

    lines = format_instantaneous_preview_lines(capture, snapshot=None)

    assert lines == []
