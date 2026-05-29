# Maxem Live Validation Runbook

## Goal

Validate that Maxem sees:

- Home/grid power driven by Cerbo `Ac/ActiveIn` rewrite values.
- Phase current safety behavior driven by Cerbo `Ac/Out` current signals.

## Preconditions

- Upstream ABB source is reachable via Modbus TCP gateway.
- Cerbo MQTT broker is reachable.
- Maxem RTU link is stable.
- Environment variables are configured (`MOSQUITTO_*`, `CERBO_*`, `MODBUS_TCP_GW_*`, `SERIAL_PORT`).
- If phase sign behavior is under investigation, explicitly record:
  - `CERBO_PHASE_POWER_SOURCE`
  - `CERBO_FORCE_NONNEGATIVE_PHASE_POWER`
  - `CERBO_COHERENT_PHASE_FRAMES`
  - `CERBO_COHERENT_PHASE_FRAME_MAX_SKEW_SECONDS`

## Step 1: Offline Sanity Capture

Capture current source + rewrite-source values:

```bash
python3 tools/dump_register_block.py --output /tmp/maxem-bundle-live.json
```

Inspect source vs rewrite:

```bash
python3 tools/inspect_instantaneous_payload.py --bundle /tmp/maxem-bundle-live.json
```

Expected:

- `changed_words` includes:
  - `0x5B0C..0x5B13` (current L1/L2/L3/N from Cerbo `Ac/Out`)
  - `0x5B14, 0x5B15` (total power)
  - `0x5B16..0x5B1B` (phase L1/L2/L3 power)
- Voltage fields remain unchanged unless source changed.
- Power rewrite values can be positive or negative (Cerbo `Ac/ActiveIn`).
- Current rewrite values are clamped to non-negative (Cerbo `Ac/Out`).

## Step 2: Live Runtime with Trace

Run live with field-level trace:

```bash
LOG_LEVEL=DEBUG python3 -u main.py --trace-instantaneous-payload
```

Watch for:

- Preview lines:
  - `ABB source: ...`
  - `Cerbo Usage to Maxem: ...`
  - `Cerbo Phase Watts to Maxem: ...`
  - `Cerbo Phase Currents to Maxem: ...`
- Trace lines indicating only intended words changed in `instantaneous_values`.
- Optional debug line `Cerbo MQTT snapshot updated...` should show coherent
  ActiveIn phase power + Out phase current snapshots.
- Startup logs should print effective Cerbo broker/topic settings and source precedence.

## Step 3: UI Cross-Check

In Maxem UI/app, compare:

- Home usage tile/summary.
- Grid phase usage chart/values.
- Charger power.

During known scenarios (for example: charging from battery/solar with low net grid import), verify Home and Grid values align with desired interpretation.

## Step 4: Long-Run Observation

Run for an extended window (for example 2-8 hours) and monitor:

- Stability of RTU updates.
- No runaway exception loops.
- No unexpected expansion of rewritten words.
- Behavior during MQTT transient failures (warnings should not stop serving loop).

## Known Observability Notes

- `invalid request: Request length is invalid 1` messages may originate from malformed external Modbus client traffic and are not automatically a rewrite fault.
- By default the runtime suppresses only that exact 1-byte RTU noise line
  (`SUPPRESS_SHORT_RTU_REQUEST_LOGS=1`). Set `SUPPRESS_SHORT_RTU_REQUEST_LOGS=0`
  when you need raw-wire troubleshooting logs.
- Loop status logs are emitted as heartbeat summaries (default every 30s). Tune
  with `STATUS_LOG_INTERVAL_SECONDS`.
- ABB sentinels such as `0xFFFF` may decode as `n/a` in diagnostics.

## Exit Criteria

Validation pass is considered successful when:

- Rewritten words match design (`0x5B0C..0x5B1B` for current+active-power fields only).
- Maxem dashboard behavior aligns with intended Home/Grid semantics across multiple load conditions.
- Service remains stable over long-running periods.
