import unittest

from lib.maxem_home_usage import (
    DomoticzUsageSnapshot,
    INSTANTANEOUS_ACTIVE_POWER_TOTAL_OFFSET,
    INSTANTANEOUS_VALUES_REGISTER_ADDRESS,
    INSTANTANEOUS_VALUES_REGISTER_LENGTH,
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    decode_signed_scaled_watts,
    describe_instantaneous_preview_basis,
    encode_signed_scaled_watts,
    format_instantaneous_preview_lines,
    rewrite_instantaneous_values,
)
from lib.synthetic_home import DomoticzReading, RegisterCapture


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
        )

        message = format_instantaneous_preview_lines(capture, snapshot=snapshot)

        self.assertEqual(
            message,
            [
                "ABB source: 1,234.50 W",
                "DZ Usage to Maxem: 18 W",
            ],
        )

    def test_preview_basis_explains_instantaneous_power_semantics(self) -> None:
        message = describe_instantaneous_preview_basis()

        self.assertIn("ABB instantaneous active power total", message)
        self.assertIn("0x5B14/0x5B15", message)
        self.assertIn("Domoticz IDX 20 Usage", message)
        self.assertIn("grid-import watt reading", message)
        self.assertIn("clamping negatives to zero", message)


if __name__ == "__main__":
    unittest.main()
