#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.maxem_home_usage import (
    INSTANTANEOUS_VALUES_REGISTER_NAME,
    describe_instantaneous_preview_basis,
    format_instantaneous_diff_lines,
    format_instantaneous_preview_lines,
    DomoticzUsageSnapshot,
    rewrite_instantaneous_values,
)
from lib.register_capture_tools import (
    build_replay_snapshot,
    bundle_captures,
    load_dump_bundle,
)
from lib.synthetic_home import DomoticzReading


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect ABB instantaneous_values payload and show exactly what words/fields change after the "
            "Domoticz Usage rewrite."
        )
    )
    parser.add_argument(
        "--bundle",
        required=True,
        help="Path to capture bundle JSON produced by tools/dump_register_block.py.",
    )
    parser.add_argument(
        "--usage-watts",
        type=float,
        default=None,
        help="Override Usage watts instead of using domoticz.reading.import_watts from the bundle.",
    )
    parser.add_argument("--phase-l1-watts", type=float, default=None, help="Override phase L1 watts.")
    parser.add_argument("--phase-l2-watts", type=float, default=None, help="Override phase L2 watts.")
    parser.add_argument("--phase-l3-watts", type=float, default=None, help="Override phase L3 watts.")
    return parser


def _find_instantaneous_capture(bundle) -> object:
    captures = bundle_captures(bundle)
    for capture in captures:
        if capture.target_slave == 100 and capture.register_name == INSTANTANEOUS_VALUES_REGISTER_NAME:
            return capture
    raise ValueError("Bundle does not include target_slave=100 instantaneous_values capture")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    bundle = load_dump_bundle(args.bundle)
    capture = _find_instantaneous_capture(bundle)

    replay_snapshot = build_replay_snapshot(bundle)
    preview_snapshot = replay_snapshot
    usage_watts = preview_snapshot.grid_import_watts
    phase_usage_watts = preview_snapshot.phase_usage_watts
    if args.usage_watts is not None:
        usage_watts = args.usage_watts
    override_phase_values = [args.phase_l1_watts, args.phase_l2_watts, args.phase_l3_watts]
    if any(value is not None for value in override_phase_values):
        if any(value is None for value in override_phase_values):
            raise ValueError("Provide all three phase overrides together: --phase-l1-watts --phase-l2-watts --phase-l3-watts")
        phase_usage_watts = (float(args.phase_l1_watts), float(args.phase_l2_watts), float(args.phase_l3_watts))
    if args.usage_watts is not None or any(value is not None for value in override_phase_values):
        preview_snapshot = DomoticzUsageSnapshot(
            sequence=replay_snapshot.sequence + 1,
            reading=DomoticzReading(
                import_kwh=0.0,
                export_kwh=0.0,
                import_watts=usage_watts if usage_watts is not None else 0.0,
                export_watts=0.0,
                last_update=None,
            ),
            phase_usage_watts=phase_usage_watts,
        )
    if usage_watts is None:
        usage_watts = 0.0

    rewritten_values = rewrite_instantaneous_values(
        capture.source_values,
        usage_watts=usage_watts,
        phase_usage_watts=phase_usage_watts,
    )

    print(describe_instantaneous_preview_basis())
    for line in format_instantaneous_preview_lines(capture, snapshot=preview_snapshot):
        print(line)
    if args.usage_watts is not None:
        print(f"Override Usage applied: {args.usage_watts:.2f} W")
    if any(value is not None for value in override_phase_values):
        print(
            "Override Phase Watts applied: "
            f"L1={args.phase_l1_watts:.2f} W, L2={args.phase_l2_watts:.2f} W, L3={args.phase_l3_watts:.2f} W"
        )

    print("---- instantaneous field diff ----")
    for line in format_instantaneous_diff_lines(capture.source_values, rewritten_values):
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
