# Project Conventions

This document is the canonical home for durable, project-specific decisions and
operator-facing assumptions for `modbus-softsplit`.

## Active Rewrite Path

- The Maxem rewrite path uses Domoticz device `IDX 20` `Usage` as the live
  grid-import watt reading.
- House load is ignored for the Maxem rewrite path.
- Negative values are clamped to `0` before encoding.
- The ABB target register is `instantaneous_values` at `0x5B14/0x5B15`.
- Preview logs should stay short and verifiable:
  - `ABB source: X W`
  - `DZ Usage to Maxem: Y W`

## Register Tooling

- `tools/dump_register_block.py` defaults to `instantaneous_values` only.
- `tools/replay_maxem_preview.py` replays the instantaneous-power preview from a
  captured bundle.
- `total_accumulators` is legacy prototype output and is not part of the active
  Maxem rewrite story.

## Runtime Guardrails

- `--dry-run-maxem-home` is opt-in and must not open the RTU serial adapter.
- Domoticz polling must stay off the RTU serving path and remain non-blocking.
- The serving loop should remain timing-safe for the RTU client.

## Documentation and Memory Hygiene

- Project-specific durable knowledge belongs in `docs/` first.
- Prefer `docs/decisions/` for stable implementation rules and `docs/runbooks/`
  for operational procedures.
- External memory notes are supplemental and should not be the only durable
  record for this repository.
- When project behavior changes, update the relevant repo docs in the same task
  so future runs do not need to infer intent from chat history.
