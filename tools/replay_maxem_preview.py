#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.register_capture_tools import build_replay_preview_lines, build_replay_snapshot, load_dump_bundle
from lib.maxem_home_usage import describe_instantaneous_preview_basis


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a captured instantaneous-power bundle as a dry-run preview")
    parser.add_argument(
        "--bundle",
        required=True,
        help="Path to the capture bundle JSON produced by tools/dump_register_block.py.",
    )
    return parser


def _render_bundle(bundle_path: str) -> list[str]:
    bundle = load_dump_bundle(bundle_path)
    snapshot = build_replay_snapshot(bundle)
    return build_replay_preview_lines(bundle, snapshot=snapshot)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s replay-maxem-preview: %(message)s",
        level=logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    lines = _render_bundle(args.bundle)

    if lines:
        print(describe_instantaneous_preview_basis())

    for line in lines:
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
