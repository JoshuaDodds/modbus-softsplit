import unittest
from unittest.mock import patch

from lib.maxem_home_usage import (
    DomoticzUsageCache,
    DomoticzUsagePoller,
    DomoticzUsageSnapshot,
    INSTANTANEOUS_ACTIVE_POWER_L1_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L2_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_L3_REGISTER_ADDRESS,
    INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET,
    INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VALUES_REGISTER_LENGTH,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    changed_instantaneous_words,
    decode_instantaneous_fields,
    decode_signed_scaled_watts,
    describe_instantaneous_preview_basis,
    encode_signed_scaled_watts,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    rewrite_instantaneous_values,
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

    def test_preview_basis_explains_instantaneous_power_semantics(self) -> None:
        message = describe_instantaneous_preview_basis()

        self.assertIn("ABB instantaneous active power total", message)
        self.assertIn("0x5B14/0x5B15", message)
        self.assertIn("DOMOTICZ_USE_SIGNED_NET_POWER=1", message)
        self.assertIn("Usage-UsageDeliv", message)
        self.assertIn("When disabled", message)
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


if __name__ == "__main__":
    unittest.main()
