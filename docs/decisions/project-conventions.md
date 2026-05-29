# Project Conventions

This document is the canonical home for durable, project-specific decisions and
operator-facing assumptions for `modbus-softsplit`.

## Active Rewrite Path

- The Maxem rewrite path reads source data directly from Victron CerboGX MQTT
  (read-only), not from Domoticz.
- MQTT broker defaults:
  - host `mosquitto.hs.mfis.net`
  - port `1883`
  - active-in topic base `N/48e7da878d35/vebus/276/Ac/ActiveIn`
  - ac-out topic base `N/48e7da878d35/vebus/276/Ac/Out`
- Power rewrite source:
  - `Ac/ActiveIn/L1|L2|L3/P` -> active power total + per phase words.
  - Values may be positive or negative.
- `CERBO_PHASE_POWER_SOURCE` controls phase-power words (`0x5B16..0x5B1B`):
  - `activein` -> phase power from Cerbo `Ac/ActiveIn` (signed).
  - `acout` -> phase power derived from ABB phase voltages and Cerbo `Ac/Out` currents.
  - `abb` -> phase power passthrough from ABB (no rewrite on phase words).
- `CERBO_FORCE_NONNEGATIVE_PHASE_POWER=1` clamps rewritten phase-power values to `>=0`.
- Current rewrite source:
  - `Ac/Out/L1|L2|L3/I` -> phase current words.
  - `Ac/Out/N/I` -> neutral current word when present.
  - AC-out currents are clamped to `>= 0` before encoding.
- `CERBO_COHERENT_PHASE_FRAMES=1` requires full 3-phase refresh for both power and current
  inputs before publishing a new rewrite snapshot.
- `CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS` bounds acceptable timestamp skew across
  phase updates to reduce mixed-time phase combinations.
- The ABB target register block is `instantaneous_values`.
- Rewritten words in that block are:
  - `0x5B0C/0x5B0D`, `0x5B0E/0x5B0F`, `0x5B10/0x5B11`, `0x5B12/0x5B13`
    current L1/L2/L3/N from Cerbo `Ac/Out`.
  - `0x5B14/0x5B15`, `0x5B16/0x5B17`, `0x5B18/0x5B19`, `0x5B1A/0x5B1B`
    active power total/L1/L2/L3 from Cerbo `Ac/ActiveIn`.
- All other words in `instantaneous_values` and all other Maxem register blocks
  are mirrored unchanged from the ABB source.
- Preview logs should stay short and verifiable:
  - `ABB source: X W`
  - `Cerbo Usage to Maxem: Y W`
  - `Cerbo Phase Watts to Maxem: L1=..., L2=..., L3=...`
  - `Cerbo Phase Currents to Maxem: L1=..., L2=..., L3=..., N=...`
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
- Cerbo MQTT subscriptions must remain read-only; do not publish/control from
  this runtime.
- MQTT callbacks must stay lightweight and non-blocking; RTU serving path must
  remain timing-safe.
- Startup should log effective Cerbo MQTT source settings and source precedence
  (`env` vs `.env` vs defaults).
- `CERBO_MQTT_PROTOCOL_DEBUG=0` should remain default so DEBUG logs stay operator-readable; enable only during MQTT wire troubleshooting.
- `CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS=0` should remain default to prevent per-message snapshot log flooding.
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
