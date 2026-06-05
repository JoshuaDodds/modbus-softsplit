import unittest
from unittest.mock import patch

import main as runtime_main

from lib.maxem_home_usage import (
    CerboMqttSnapshot,
    DomoticzUsageCache,
    DomoticzUsagePoller,
    DomoticzUsageSnapshot,
    INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_L1_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_L2_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_L3_REGISTER_ADDRESS,
    INSTANTANEOUS_CURRENT_N_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET,
    INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VALUES_REGISTER_LENGTH,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    changed_instantaneous_words,
    decode_instantaneous_fields,
    decode_signed_scaled_watts,
    describe_instantaneous_preview_basis,
    derive_phase_currents_from_watts,
    derive_phase_watts_from_currents,
    encode_signed_scaled_watts,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    net_signed_phase_watts_to_nonnegative_import,
    rewrite_pv_instantaneous_values,
    rewrite_instantaneous_values,
    split_total_watts_evenly,
)
from lib.synthetic_home import DomoticzClient, DomoticzReading, RegisterCapture


def _instantaneous_capture(source_watts: float) -> RegisterCapture:
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


class MaxemHomeUsageTests(unittest.TestCase):
    def test_domoticz_payload_parsing_from_result_list(self) -> None:
        reading = DomoticzReading.from_payload(
            {
                "result": [
                    {
                        "Counter": "64989.818",
                        "CounterDeliv": "6787.095",
                        "Usage": "2 Watt",
                        "UsageDeliv": "0 Watt",
                        "LastUpdate": "2026-05-25 15:13:57",
                    }
                ]
            }
        )

        self.assertAlmostEqual(reading.import_kwh, 64989.818)
        self.assertAlmostEqual(reading.export_kwh, 6787.095)
        self.assertAlmostEqual(reading.import_watts, 2.0)
        self.assertAlmostEqual(reading.export_watts, 0.0)
        self.assertEqual(reading.last_update, "2026-05-25 15:13:57")

    def test_instantaneous_register_round_trip_and_negative_clamp(self) -> None:
        capture = _instantaneous_capture(1234.5)

        self.assertAlmostEqual(decode_signed_scaled_watts(capture.source_values), 1234.5)

        rewritten = rewrite_instantaneous_values(capture.source_values, usage_watts=-25.0)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten), 0.0)

    def test_instantaneous_register_supports_negative_rewrite_when_enabled(self) -> None:
        capture = _instantaneous_capture(1234.5)

        rewritten = rewrite_instantaneous_values(
            capture.source_values,
            usage_watts=-25.0,
            allow_negative=True,
        )
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten), -25.0, places=2)

    def test_instantaneous_only_rewrites_total_field(self) -> None:
        capture = _instantaneous_capture(1234.5)
        source_values = list(capture.source_values)
        for index in range(len(source_values)):
            source_values[index] = 10_000 + index

        rewritten = rewrite_instantaneous_values(tuple(source_values), usage_watts=18.0)

        for index, value in enumerate(source_values):
            if index in (INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET, INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET + 1):
                continue
            self.assertEqual(rewritten[index], value)

        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten), 18.0)
        self.assertEqual(changed_instantaneous_words(source_values, rewritten), [0x5B14, 0x5B15])

    def test_instantaneous_rewrites_total_and_phase_power_fields(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH
        for index in range(len(source_values)):
            source_values[index] = 20_000 + index

        rewritten = rewrite_instantaneous_values(
            tuple(source_values),
            usage_watts=87.0,
            phase_usage_watts=(1559.24, 1284.30, 1931.40),
        )

        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten), 87.0, places=2)

        phase_l1_offset = INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        phase_l2_offset = INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        phase_l3_offset = INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l1_offset), 1559.24, places=2)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l2_offset), 1284.30, places=2)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l3_offset), 1931.40, places=2)

        changed_words = changed_instantaneous_words(source_values, rewritten)
        self.assertEqual(changed_words, [0x5B14, 0x5B15, 0x5B16, 0x5B17, 0x5B18, 0x5B19, 0x5B1A, 0x5B1B])

    def test_instantaneous_rewrite_can_use_signed_total_with_unsigned_phase_values(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH
        rewritten = rewrite_instantaneous_values(
            tuple(source_values),
            usage_watts=-50.0,
            phase_usage_watts=(-10.0, 20.0, -30.0),
            allow_negative=True,
            allow_negative_phase=False,
        )

        phase_l1_offset = INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        phase_l2_offset = INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        phase_l3_offset = INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS - INSTANTANEOUS_VALUES_REGISTER_ADDRESS

        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten), -50.0, places=2)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l1_offset), 0.0, places=2)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l2_offset), 20.0, places=2)
        self.assertAlmostEqual(decode_signed_scaled_watts(rewritten, offset=phase_l3_offset), 0.0, places=2)

    def test_instantaneous_rewrites_current_fields_from_ac_out(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH
        rewritten = rewrite_instantaneous_values(
            tuple(source_values),
            usage_watts=0.0,
            phase_usage_watts=(0.0, 0.0, 0.0),
            phase_current_amps=(1.23, 2.34, 3.45),
            current_n_amps=0.56,
        )

        decoded = decode_instantaneous_fields(rewritten)
        self.assertAlmostEqual(decoded["current_l1"] or 0.0, 1.23, places=2)
        self.assertAlmostEqual(decoded["current_l2"] or 0.0, 2.34, places=2)
        self.assertAlmostEqual(decoded["current_l3"] or 0.0, 3.45, places=2)
        self.assertAlmostEqual(decoded["current_n"] or 0.0, 0.56, places=2)

        changed_words = changed_instantaneous_words(source_values, rewritten)
        expected = [
            INSTANTANEOUS_CURRENT_L1_REGISTER_ADDRESS + 1,
            INSTANTANEOUS_CURRENT_L2_REGISTER_ADDRESS + 1,
            INSTANTANEOUS_CURRENT_L3_REGISTER_ADDRESS + 1,
            INSTANTANEOUS_CURRENT_N_REGISTER_ADDRESS + 1,
        ]
        self.assertEqual(changed_words, expected)

    def test_decode_instantaneous_fields_extracts_expected_values(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        def set_field(address: int, scale: float, value: float, *, signed: bool) -> None:
            offset = address - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
            raw = int(round(value / scale))
            if signed:
                raw &= 0xFFFFFFFF
            raw = max(min(raw, 0xFFFFFFFF), 0)
            payload = raw.to_bytes(4, byteorder="big", signed=False)
            source_values[offset] = int.from_bytes(payload[:2], byteorder="big")
            source_values[offset + 1] = int.from_bytes(payload[2:], byteorder="big")

        set_field(0x5B00, 0.1, 228.7, signed=False)
        set_field(0x5B0C, 0.01, 2.34, signed=False)
        set_field(0x5B0E, 0.01, 1.17, signed=False)
        set_field(0x5B10, 0.01, 4.38, signed=False)
        set_field(0x5B14, 0.01, 3470.91, signed=True)

        decoded = decode_instantaneous_fields(source_values)

        self.assertAlmostEqual(decoded["voltage_l1_n"] or 0.0, 228.7, places=1)
        self.assertAlmostEqual(decoded["current_l1"] or 0.0, 2.34, places=2)
        self.assertAlmostEqual(decoded["current_l2"] or 0.0, 1.17, places=2)
        self.assertAlmostEqual(decoded["current_l3"] or 0.0, 4.38, places=2)
        self.assertAlmostEqual(decoded["active_power_total"] or 0.0, 3470.91, places=2)

    def test_derive_phase_watts_from_currents_uses_source_voltages(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        def set_voltage(address: int, value_volts: float) -> None:
            offset = address - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
            raw = int(round(value_volts / 0.1))
            payload = raw.to_bytes(4, byteorder="big", signed=False)
            source_values[offset] = int.from_bytes(payload[:2], byteorder="big")
            source_values[offset + 1] = int.from_bytes(payload[2:], byteorder="big")

        set_voltage(0x5B00, 230.0)
        set_voltage(0x5B02, 231.0)
        set_voltage(0x5B04, 232.0)

        derived = derive_phase_watts_from_currents(tuple(source_values), (10.0, 1.0, 0.5))

        self.assertIsNotNone(derived)
        self.assertAlmostEqual(derived[0], 2300.0, places=2)
        self.assertAlmostEqual(derived[1], 231.0, places=2)
        self.assertAlmostEqual(derived[2], 116.0, places=2)

    def test_derive_phase_watts_from_currents_falls_back_to_default_voltage(self) -> None:
        source_values = [0xFFFF] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        derived = derive_phase_watts_from_currents(
            tuple(source_values),
            (1.0, 2.0, 3.0),
            fallback_phase_voltage_volts=230.0,
        )

        self.assertIsNotNone(derived)
        self.assertAlmostEqual(derived[0], 230.0, places=2)
        self.assertAlmostEqual(derived[1], 460.0, places=2)
        self.assertAlmostEqual(derived[2], 690.0, places=2)

    def test_split_total_watts_evenly_distributes_sum_without_loss(self) -> None:
        phase_watts = split_total_watts_evenly(1000.0)

        self.assertIsNotNone(phase_watts)
        self.assertAlmostEqual(sum(phase_watts), 1000.0, places=6)
        self.assertAlmostEqual(phase_watts[0], phase_watts[1], places=6)
        self.assertGreaterEqual(phase_watts[2], 0.0)

    def test_derive_phase_currents_from_watts_uses_source_phase_voltages(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        def set_voltage(address: int, value_volts: float) -> None:
            offset = address - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
            raw = int(round(value_volts / 0.1))
            payload = raw.to_bytes(4, byteorder="big", signed=False)
            source_values[offset] = int.from_bytes(payload[:2], byteorder="big")
            source_values[offset + 1] = int.from_bytes(payload[2:], byteorder="big")

        set_voltage(0x5B00, 230.0)
        set_voltage(0x5B02, 231.0)
        set_voltage(0x5B04, 232.0)

        currents = derive_phase_currents_from_watts(
            tuple(source_values),
            (230.0, 462.0, 116.0),
        )

        self.assertIsNotNone(currents)
        self.assertAlmostEqual(currents[0], 1.0, places=4)
        self.assertAlmostEqual(currents[1], 2.0, places=4)
        self.assertAlmostEqual(currents[2], 0.5, places=4)

    def test_rewrite_pv_instantaneous_values_supports_negative_single_phase_power(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        def set_voltage(address: int, value_volts: float) -> None:
            offset = address - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
            raw = int(round(value_volts / 0.1))
            payload = raw.to_bytes(4, byteorder="big", signed=False)
            source_values[offset] = int.from_bytes(payload[:2], byteorder="big")
            source_values[offset + 1] = int.from_bytes(payload[2:], byteorder="big")

        set_voltage(0x5B00, 230.0)
        set_voltage(0x5B02, 230.0)
        set_voltage(0x5B04, 230.0)

        rewritten = rewrite_pv_instantaneous_values(
            tuple(source_values),
            pv_total_watts=900.0,
            pv_negative=True,
        )
        decoded = decode_instantaneous_fields(rewritten)

        self.assertAlmostEqual(decoded["active_power_total"] or 0.0, -900.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l1"] or 0.0, -900.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l2"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l3"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["current_l1"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["current_l2"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["current_l3"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["current_n"] or 0.0, 0.0, places=2)

    def test_rewrite_pv_instantaneous_values_supports_positive_single_phase_power(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH

        rewritten = rewrite_pv_instantaneous_values(
            tuple(source_values),
            pv_total_watts=900.0,
            pv_negative=False,
        )
        decoded = decode_instantaneous_fields(rewritten)

        self.assertAlmostEqual(decoded["active_power_total"] or 0.0, 900.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l1"] or 0.0, 900.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l2"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(decoded["active_power_l3"] or 0.0, 0.0, places=2)

    def test_net_signed_phase_watts_to_nonnegative_import_offsets_exports(self) -> None:
        netted = net_signed_phase_watts_to_nonnegative_import((-350.0, 350.0, 0.0))

        self.assertIsNotNone(netted)
        self.assertAlmostEqual(netted[0], 0.0, places=6)
        self.assertAlmostEqual(netted[1], 0.0, places=6)
        self.assertAlmostEqual(netted[2], 0.0, places=6)

    def test_net_signed_phase_watts_to_nonnegative_import_scales_positive_phases(self) -> None:
        netted = net_signed_phase_watts_to_nonnegative_import((-250.0, 270.0, 50.0))

        self.assertIsNotNone(netted)
        self.assertAlmostEqual(sum(netted), 70.0, places=6)
        self.assertGreaterEqual(netted[0], 0.0)
        self.assertGreaterEqual(netted[1], 0.0)
        self.assertGreaterEqual(netted[2], 0.0)

    def test_decode_instantaneous_fields_treats_invalid_sentinel_words_as_none(self) -> None:
        source_values = [0] * INSTANTANEOUS_VALUES_REGISTER_LENGTH
        current_n_offset = 0x5B12 - INSTANTANEOUS_VALUES_REGISTER_ADDRESS
        source_values[current_n_offset] = 0xFFFF
        source_values[current_n_offset + 1] = 0xFFFF

        decoded = decode_instantaneous_fields(source_values)

        self.assertIsNone(decoded["current_n"])

    def test_format_instantaneous_diff_marks_only_total_power_changed(self) -> None:
        capture = _instantaneous_capture(3470.91)
        rewritten = rewrite_instantaneous_values(capture.source_values, usage_watts=87.0)

        lines = format_instantaneous_diff_lines(capture.source_values, rewritten)
        active_total_line = [line for line in lines if line.startswith("active_power_total:")][0]
        active_l1_line = [line for line in lines if line.startswith("active_power_l1:")][0]
        changed_words_line = lines[-1]

        self.assertIn("[changed]", active_total_line)
        self.assertNotIn("[changed]", active_l1_line)
        self.assertEqual(changed_words_line, "changed_words: 0x5B14, 0x5B15")

    def test_preview_message_is_about_grid_import_watts(self) -> None:
        capture = _instantaneous_capture(1234.5)
        snapshot = DomoticzUsageSnapshot(
            sequence=1,
            reading=DomoticzReading(
                import_kwh=100.0,
                export_kwh=10.0,
                import_watts=18.0,
                export_watts=0.0,
                last_update="2026-05-26 09:00:00",
            ),
            phase_usage_watts=(10.0, 20.0, 30.0),
        )

        message = format_instantaneous_preview_lines(capture, snapshot=snapshot)

        self.assertEqual(
            message,
            [
                "ABB source: 1,234.50 W",
                "DZ Usage to Maxem: 18 W",
                "DZ Phase Watts to Maxem: L1=10 W, L2=20 W, L3=30 W",
            ],
        )

    def test_preview_message_uses_cerbo_label_and_includes_currents(self) -> None:
        capture = _instantaneous_capture(1234.5)
        snapshot = CerboMqttSnapshot(
            sequence=1,
            ac_in_phase_watts=(10.0, 20.0, 30.0),
            ac_in_total_watts=60.0,
            ac_out_phase_currents=(0.11, 0.22, 0.33),
            ac_out_current_n=0.44,
        )

        message = format_instantaneous_preview_lines(capture, snapshot=snapshot)

        self.assertEqual(
            message,
            [
                "ABB source: 1,234.50 W",
                "Cerbo Usage to Maxem: 60 W",
                "Cerbo Phase Watts to Maxem: L1=10 W, L2=20 W, L3=30 W",
                "Cerbo Phase Currents to Maxem: L1=0.11 A, L2=0.22 A, L3=0.33 A, N=0.44 A",
            ],
        )

    def test_preview_basis_explains_instantaneous_power_semantics(self) -> None:
        message = describe_instantaneous_preview_basis()

        self.assertIn("active_power_total (0x5B14/0x5B15)", message)
        self.assertIn("CERBO_PHASE_POWER_SOURCE", message)
        self.assertIn("CERBO_ALLOW_SIGNED_INSTANTANEOUS_POWER", message)
        self.assertIn("CERBO_PV_SIGN_NEGATIVE", message)
        self.assertIn("Ac/ActiveIn", message)
        self.assertIn("Ac/Out", message)
        self.assertIn("non-negative clamp", message)
        self.assertIn("copied verbatim", message)

    def test_domoticz_client_parses_multi_idx_payload(self) -> None:
        client = DomoticzClient("http://example.invalid", 20)
        payload = {
            "status": "OK",
            "result": [
                {
                    "idx": "20",
                    "Counter": "64989.818",
                    "CounterDeliv": "6787.095",
                    "Usage": "87 Watt",
                    "UsageDeliv": "15 Watt",
                    "LastUpdate": "2026-05-28 10:00:00",
                },
                {"idx": "26", "Data": "1559.24 W"},
                {"idx": "32", "Data": "120.00 W"},
            ],
        }

        with patch.object(client, "fetch_payload", return_value=payload):
            devices = client.fetch_devices((20, 26, 32))

        self.assertEqual(sorted(devices.keys()), [20, 26, 32])
        self.assertAlmostEqual(client.data_watts_from_device(devices[26]), 1559.24, places=2)
        self.assertAlmostEqual(client.data_watts_from_device(devices[32]), 120.00, places=2)

        reading = client.fetch_reading_from_device(devices[20])
        self.assertAlmostEqual(reading.import_watts, 87.0)
        self.assertAlmostEqual(reading.export_watts, 15.0)

    def test_usage_poller_requests_one_batched_snapshot_per_cycle(self) -> None:
        class _FakeBatchClient:
            enabled = True
            grid_idx = 20

            def __init__(self) -> None:
                import threading

                self.calls: list[tuple[int, ...]] = []
                self.called = threading.Event()

            def fetch_devices(self, rids):
                self.calls.append(tuple(int(value) for value in rids))
                self.called.set()
                return {
                    20: {
                        "idx": "20",
                        "Counter": "64989.818",
                        "CounterDeliv": "6787.095",
                        "Usage": "87 Watt",
                        "UsageDeliv": "15 Watt",
                    },
                    26: {"idx": "26", "Data": "1559.24 W"},
                    25: {"idx": "25", "Data": "1284.30 W"},
                    24: {"idx": "24", "Data": "1931.40 W"},
                    32: {"idx": "32", "Data": "120.00 W"},
                    31: {"idx": "31", "Data": "80.00 W"},
                    33: {"idx": "33", "Data": "50.00 W"},
                }

            @staticmethod
            def url_for_indices(rids):
                return "http://example.invalid/json.htm?type=devices&rid=" + ",".join(str(int(value)) for value in rids)

            @staticmethod
            def data_watts_from_device(candidate):
                return float(str(candidate["Data"]).split()[0])

            @staticmethod
            def fetch_reading_from_device(candidate):
                return DomoticzReading.from_payload({"result": [candidate]})

        fake_client = _FakeBatchClient()
        cache = DomoticzUsageCache(use_signed_net_power=True)
        poller = DomoticzUsagePoller(
            fake_client,  # type: ignore[arg-type]
            cache,
            phase_l1_idx=26,
            phase_l2_idx=25,
            phase_l3_idx=24,
            phase_export_l1_idx=32,
            phase_export_l2_idx=31,
            phase_export_l3_idx=33,
            use_signed_net_power=True,
            use_signed_net_phase_power=True,
            poll_interval_seconds=0.1,
        )

        poller.start()
        self.assertTrue(fake_client.called.wait(timeout=1.0))
        poller.stop()
        poller.join(timeout=1.0)

        self.assertGreaterEqual(len(fake_client.calls), 1)
        self.assertEqual(fake_client.calls[0], (20, 26, 25, 24, 32, 31, 33))
        snapshot = cache.snapshot()
        self.assertAlmostEqual(snapshot.rewrite_usage_watts or 0.0, 72.0, places=2)
        self.assertIsNotNone(snapshot.phase_usage_watts)
        self.assertAlmostEqual(snapshot.phase_usage_watts[0], 1439.24, places=2)
        self.assertAlmostEqual(snapshot.phase_usage_watts[1], 1204.30, places=2)
        self.assertAlmostEqual(snapshot.phase_usage_watts[2], 1881.40, places=2)

    def test_usage_poller_defaults_to_import_only_phase_values(self) -> None:
        class _FakeBatchClient:
            enabled = True
            grid_idx = 20

            def __init__(self) -> None:
                import threading

                self.called = threading.Event()

            def fetch_devices(self, rids):
                self.called.set()
                return {
                    20: {
                        "idx": "20",
                        "Counter": "64989.818",
                        "CounterDeliv": "6787.095",
                        "Usage": "87 Watt",
                        "UsageDeliv": "15 Watt",
                    },
                    26: {"idx": "26", "Data": "100.00 W"},
                    25: {"idx": "25", "Data": "200.00 W"},
                    24: {"idx": "24", "Data": "300.00 W"},
                    32: {"idx": "32", "Data": "90.00 W"},
                    31: {"idx": "31", "Data": "190.00 W"},
                    33: {"idx": "33", "Data": "290.00 W"},
                }

            @staticmethod
            def url_for_indices(rids):
                return "http://example.invalid/json.htm?type=devices&rid=" + ",".join(str(int(value)) for value in rids)

            @staticmethod
            def data_watts_from_device(candidate):
                return float(str(candidate["Data"]).split()[0])

            @staticmethod
            def fetch_reading_from_device(candidate):
                return DomoticzReading.from_payload({"result": [candidate]})

        fake_client = _FakeBatchClient()
        cache = DomoticzUsageCache(use_signed_net_power=True, use_signed_net_phase_power=False)
        poller = DomoticzUsagePoller(
            fake_client,  # type: ignore[arg-type]
            cache,
            phase_l1_idx=26,
            phase_l2_idx=25,
            phase_l3_idx=24,
            phase_export_l1_idx=32,
            phase_export_l2_idx=31,
            phase_export_l3_idx=33,
            use_signed_net_power=True,
            use_signed_net_phase_power=False,
            poll_interval_seconds=0.1,
        )

        poller.start()
        self.assertTrue(fake_client.called.wait(timeout=1.0))
        poller.stop()
        poller.join(timeout=1.0)

        snapshot = cache.snapshot()
        self.assertIsNotNone(snapshot.phase_usage_watts)
        self.assertAlmostEqual(snapshot.phase_usage_watts[0], 100.0, places=2)
        self.assertAlmostEqual(snapshot.phase_usage_watts[1], 200.0, places=2)
        self.assertAlmostEqual(snapshot.phase_usage_watts[2], 300.0, places=2)

    def test_signed_total_split_preserves_negative_sum(self) -> None:
        phase_watts = runtime_main._split_total_watts_evenly_signed(-450.0)

        self.assertAlmostEqual(sum(phase_watts), -450.0, places=6)
        self.assertAlmostEqual(phase_watts[0], -150.0, places=6)
        self.assertAlmostEqual(phase_watts[1], -150.0, places=6)
        self.assertAlmostEqual(phase_watts[2], -150.0, places=6)

    def test_apply_pv_offset_to_home_usage_keeps_signed_result_and_phase_consistency(self) -> None:
        snapshot = CerboMqttSnapshot(
            sequence=1,
            ac_in_phase_watts=(100.0, 200.0, 300.0),
            ac_in_total_watts=600.0,
            pv_total_watts=900.0,
        )

        with patch.object(runtime_main, "CERBO_SUBTRACT_PV_FROM_HOME_USAGE", True):
            adjusted_usage, adjusted_phase_usage = runtime_main._apply_pv_offset_to_home_usage(
                usage_watts=100.0,
                phase_usage_watts=(10.0, 20.0, 70.0),
                usage_snapshot=snapshot,
            )

        self.assertAlmostEqual(adjusted_usage, -800.0, places=6)
        self.assertIsNotNone(adjusted_phase_usage)
        self.assertAlmostEqual(sum(adjusted_phase_usage), -800.0, places=6)

    def test_prepare_home_instantaneous_power_for_rewrite_can_keep_signed_total_and_equal_split(self) -> None:
        with patch.object(runtime_main, "CERBO_ALLOW_SIGNED_INSTANTANEOUS_POWER", True):
            usage_watts, phase_usage_watts, allow_negative, allow_negative_phase = (
                runtime_main._prepare_home_instantaneous_power_for_rewrite(
                    usage_watts=-450.0,
                    phase_usage_watts=(10.0, 20.0, 30.0),
                )
            )

        self.assertAlmostEqual(usage_watts, -450.0, places=6)
        self.assertTrue(allow_negative)
        self.assertTrue(allow_negative_phase)
        self.assertIsNotNone(phase_usage_watts)
        self.assertAlmostEqual(sum(phase_usage_watts), -450.0, places=6)
        self.assertAlmostEqual(phase_usage_watts[0], -150.0, places=6)
        self.assertAlmostEqual(phase_usage_watts[1], -150.0, places=6)
        self.assertAlmostEqual(phase_usage_watts[2], -150.0, places=6)

    def test_prepare_home_instantaneous_power_for_rewrite_clamps_when_signed_mode_is_off(self) -> None:
        with patch.object(runtime_main, "CERBO_ALLOW_SIGNED_INSTANTANEOUS_POWER", False):
            usage_watts, phase_usage_watts, allow_negative, allow_negative_phase = (
                runtime_main._prepare_home_instantaneous_power_for_rewrite(
                    usage_watts=-450.0,
                    phase_usage_watts=(-10.0, 20.0, -30.0),
                )
            )

        self.assertAlmostEqual(usage_watts, 0.0, places=6)
        self.assertFalse(allow_negative)
        self.assertFalse(allow_negative_phase)
        self.assertIsNotNone(phase_usage_watts)
        self.assertAlmostEqual(phase_usage_watts[0], 0.0, places=6)
        self.assertAlmostEqual(phase_usage_watts[1], 20.0, places=6)
        self.assertAlmostEqual(phase_usage_watts[2], 0.0, places=6)

    def test_disable_negative_phase_power_when_home_offset_enabled(self) -> None:
        with patch.object(runtime_main, "CERBO_SUBTRACT_PV_FROM_HOME_USAGE", True):
            with patch.object(runtime_main, "CERBO_FORCE_NONNEGATIVE_PHASE_POWER", True):
                self.assertFalse(runtime_main._allow_negative_phase_power_for_rewrite())


if __name__ == "__main__":
    unittest.main()
