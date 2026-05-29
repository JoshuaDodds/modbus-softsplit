# Project Conventions

This document is the canonical home for durable, project-specific decisions and
operator-facing assumptions for `modbus-softsplit`.

## Active Rewrite Path

- The Maxem rewrite path uses Domoticz device `IDX 20` `Usage` as the live
  grid usage signal.
- With `DOMOTICZ_USE_SIGNED_NET_POWER=1` (default), rewrite total watts are
  computed as `Usage - UsageDeliv` from `IDX 20`.
- With `DOMOTICZ_USE_SIGNED_NET_POWER=0`, rewrite total watts use non-negative
  `Usage` import-only behavior.
- With `DOMOTICZ_USE_SIGNED_NET_PHASE_POWER=0` (default), per-phase rewrite
  watts stay import-only to preserve AC-load behavior for Maxem fuse-protection
  logic.
- With `DOMOTICZ_USE_SIGNED_NET_PHASE_POWER=1`, per-phase rewrite watts use
  signed net values (phase import minus phase export).
- The Maxem rewrite path also uses Domoticz per-phase `result.Data` readings:
  import from `rid=26` (L1), `rid=24` (L2), and `rid=25` (L3), plus export
  from `rid=32` (L1), `rid=31` (L2), and `rid=33` (L3) when signed-net mode is enabled.
- House load is ignored for the Maxem rewrite path.
- The ABB target register block is `instantaneous_values`.
- Rewritten words in that block are:
  - `0x5B14/0x5B15` active power total from Domoticz grid watts.
  - `0x5B16/0x5B17`, `0x5B18/0x5B19`, `0x5B1A/0x5B1B` active power L1/L2/L3
    from Domoticz phase watts.
- All other words in `instantaneous_values` and all other Maxem register blocks
  are mirrored unchanged from the ABB source.
- Preview logs should stay short and verifiable:
  - `ABB source: X W`
  - `DZ Usage to Maxem: Y W`
  - `DZ Phase Watts to Maxem: L1=..., L2=..., L3=...`
- These preview lines are logged at `DEBUG` level and are intended as opt-in
  operator diagnostics (`LOG_LEVEL=DEBUG`).
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
- `DRY_RUN_MAXEM_HOME=1` in `.env` is the default-mode toggle equivalent to
  starting with `--dry-run-maxem-home`.
- `--trace-instantaneous-payload` is opt-in and intended for diagnostics only.
- Domoticz polling must stay off the RTU serving path and remain non-blocking.
- Domoticz polling should batch all required IDX values into one HTTP request
  per poll cycle to minimize overhead and jitter.
- Batch-request visibility should remain debug-only (`Domoticz batch request:
  ...`) so operators can verify URL/IDX composition without adding info-level
  log noise.
- Startup should log effective Domoticz mapping values and source precedence
  (`env` vs `.env` vs defaults) and warn when phase IDX values collide with
  grid IDX or with each other.
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
