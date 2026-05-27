# Project Conventions

This document is the canonical home for durable, project-specific decisions and
operator-facing assumptions for `modbus-softsplit`.

## Active Rewrite Path

- The Maxem rewrite path uses Domoticz device `IDX 20` `Usage` as the live
  grid-import watt reading.
- The Maxem rewrite path also uses Domoticz per-phase `result.Data` readings
  from `rid=26` (L1), `rid=25` (L2), and `rid=24` (L3).
- House load is ignored for the Maxem rewrite path.
- Negative values are clamped to `0` before encoding.
- The ABB target register block is `instantaneous_values`.
- Rewritten words in that block are:
  - `0x5B14/0x5B15` active power total from Domoticz `IDX 20` Usage.
  - `0x5B16/0x5B17`, `0x5B18/0x5B19`, `0x5B1A/0x5B1B` active power L1/L2/L3
    from Domoticz phase `rid 26/25/24`.
- All other words in `instantaneous_values` and all other Maxem register blocks
  are mirrored unchanged from the ABB source.
- Preview logs should stay short and verifiable:
  - `ABB source: X W`
  - `DZ Usage to Maxem: Y W`
  - `DZ Phase Watts to Maxem: L1=..., L2=..., L3=...`
- The live RTU path should emit the same short preview when the instantaneous
  rewrite value changes so operators can verify the actual write path without
  enabling dry-run mode.

## Register Tooling

- `tools/dump_register_block.py` defaults to `instantaneous_values` only.
- `tools/replay_maxem_preview.py` replays the instantaneous-power preview from a
  captured bundle.
- `tools/inspect_instantaneous_payload.py` is the deep-dive tool for unpacking
  ABB source vs rewritten payload values and showing exactly which
  `instantaneous_values` words changed.
- `docs/runbooks/maxem-live-validation.md` is the canonical operational
  checklist for long-running live validation.
- `docs/Maxem MX Home 4 handleiding.pdf` is installation/wiring guidance and
  useful for system behavior context, but does not replace ABB register mapping
  references.
- `total_accumulators` is legacy prototype output and is not part of the active
  Maxem rewrite story.

## Runtime Guardrails

- `--dry-run-maxem-home` is opt-in and must not open the RTU serial adapter.
- `--trace-instantaneous-payload` is opt-in and intended for diagnostics only.
- Domoticz polling must stay off the RTU serving path and remain non-blocking.
- The serving loop should remain timing-safe for the RTU client.
- High-volume loop status logs should be periodic instead of per-cycle to avoid
  unnecessary log I/O overhead (`STATUS_LOG_INTERVAL_SECONDS`, default `30`).
- RTU protocol validation must remain strict; do not reinterpret malformed
  short frames as valid requests.
- `SUPPRESS_SHORT_RTU_REQUEST_LOGS=1` (default) suppresses only the specific
  known-noise line `invalid request: Request length is invalid 1` at logging
  time, without changing RTU frame parsing behavior.

## Documentation and Memory Hygiene

- Project-specific durable knowledge belongs in `docs/` first.
- Prefer `docs/decisions/` for stable implementation rules and `docs/runbooks/`
  for operational procedures.
- External memory notes are supplemental and should not be the only durable
  record for this repository.
- When project behavior changes, update the relevant repo docs in the same task
  so future runs do not need to infer intent from chat history.
