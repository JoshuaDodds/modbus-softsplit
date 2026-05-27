# Maxem Live Validation Runbook

## Goal

Validate that Maxem sees:

- Home usage driven by Domoticz rewrite values.
- Grid phase usage behavior matching expected phase signals.

## Preconditions

- Upstream ABB source is reachable via Modbus TCP gateway.
- Domoticz endpoints are reachable.
- Maxem RTU link is stable.
- Environment variables are configured (`DOMOTICZ_*`, `MODBUS_TCP_GW_*`, `SERIAL_PORT`).

## Step 1: Offline Sanity Capture

Capture current source + Domoticz values:

```bash
python3 tools/dump_register_block.py --output /tmp/maxem-bundle-live.json
```

Inspect source vs rewrite:

```bash
python3 tools/inspect_instantaneous_payload.py --bundle /tmp/maxem-bundle-live.json
```

Expected:

- `changed_words` includes:
  - `0x5B14, 0x5B15` (total power)
  - `0x5B16..0x5B1B` (phase L1/L2/L3 power)
- Voltage and current fields remain unchanged unless source changed.

## Step 2: Live Runtime with Trace

Run live with field-level trace:

```bash
python3 -u main.py --trace-instantaneous-payload
```

Watch for:

- Preview lines:
  - `ABB source: ...`
  - `DZ Usage to Maxem: ...`
  - `DZ Phase Watts to Maxem: ...`
- Trace lines indicating only intended words changed in `instantaneous_values`.

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
- Behavior during Domoticz transient failures (poll warnings should not stop serving loop).

## Known Observability Notes

- `invalid request: Request length is invalid 1` messages may originate from malformed external Modbus client traffic and are not automatically a rewrite fault.
- ABB sentinels such as `0xFFFF` may decode as `n/a` in diagnostics.

## Exit Criteria

Validation pass is considered successful when:

- Rewritten words match design (`0x5B14..0x5B1B` only in instantaneous active power fields).
- Maxem dashboard behavior aligns with intended Home/Grid semantics across multiple load conditions.
- Service remains stable over long-running periods.
