# Modbus-softsplit
Modbus software based splitter/slave replicator allowing multiple modbus masters read access on a single bus
#### (Note: This is very early POC code and certainly not polished or ready for any form of "production" deployment)


## The Problem
The Modbus protocol, by design, permits multiple slave devices on a single bus but only a single master device. (This is legacy 
terminology which will be referred to as "server and client devices" in the rest of this documentation). 

What this means in practice is that if a situation arises where you have 2 server devices wishing to communicate with the same client devices, 
you will experience conflicts in the bus and an inability for either server devices to reliably communicate with the 
client devices.  This is usually not a common scenario and the most likely encountered scenario would be during the integration 
of systems not specifically designed to be interoperable with each other.  In my case, I encountered this issue while 
attempting to integrate a Victron Energy CerboGX device and a Maxem.io Home Electric Vehicle Charge controller with
ABB B23/B24 kWh Meters.  Both the Victron CerboGX and the Maxem device act as server devices on the bus and and interfere
with each other and cause bus conflicts if connected together on the same bus.  

## The Solution
How this project managed to address the problem was with an approach of creating 2 "virtual" client devices and a 
"virtual" server device which continously polls the physical client devices and continously updates the "virtual" 
clients with that polled data, mirroring the state and holding registers of the "virtual" clients with the state and 
holding registers of the real physical client devices. The "virtual" Master/Server lives on the "actual" data bus while
the two "virtual" Slave/Client devices, in essence, create 2 new data buses (one for each physical Master/Server device)
and allow each of the physical Master/Server devices to have exclusive communication to the "virtual" slaves on these 2 
new "virtual" buses.

This architecture is intentionally generic and can be adapted for other Modbus device families; ABB meter mapping is the
current implementation profile, not a hard project limitation.

![screenshot](/layout.png?raw=true)

## Notes on this implementation:
- The implementation "as is" uses an Elfin EW-11 serial server device as the physical Master/Server device. The software
in this repository then polls it with modbus-tcp in order to keep the virtual slaves/clients up to date. 
- The two virtual slaves in this implementation differ in that one exposes itself via modbus-tcp to the Victron 
Energy hardware while the other exposes itself via modbus-rtu by way of a USB to RS-485 dongle.  The Maxem device
mentioned earlier does not support modbus-tcp so it was necessary to wire it directly and provide access to the 
virtual client devices via modbus-rtu using the USB to RS-485 converter.  
- This code should be easily read and converted in the situation where you want to have multiple RTU based Server devices
or all Server devices support modbus-tcp.  The easy to use modbus-tk project makes it quite easy to quickly adjust to
your specific situation. 
- The Maxem Home rewrite path is opt-in via `--dry-run-maxem-home`. When that flag is set, the service leaves the RTU
  serial adapter unopened and logs the instantaneous-power rewrite it would apply to `instantaneous_values` instead of
  writing values into the RTU slave. This keeps the RTU serving path quiet while we inspect the intended rewrite
  behavior.
- You can also set `DRY_RUN_MAXEM_HOME=1` in `.env` to make dry-run the default without changing service/unit args.
- In dry-run mode the Maxem preview is intentionally short and easy to compare against dashboards:
  `ABB source: X W`, `Cerbo Usage to Maxem: Y W`, and `Cerbo Phase Watts to Maxem: L1=..., L2=..., L3=...`.
  When PV slave emulation is enabled, dry-run also logs `Cerbo PV to Maxem (slave 001): ...`.
  These lines are emitted at `DEBUG` level (set `LOG_LEVEL=DEBUG` when you want them).
- The active rewrite story now reads directly from Victron CerboGX MQTT (read-only) and rewrites selected words in
  ABB `instantaneous_values` while mirroring all other words verbatim.
  - `active_power_total` (`0x5B14/0x5B15`) is sourced from `Ac/ActiveIn` total watts.
  - `active_power_l1/l2/l3` (`0x5B16..0x5B1B`) follow `CERBO_PHASE_POWER_SOURCE`.
  - `current_l1/l2/l3/n` (`0x5B0C..0x5B13`) are sourced from `Ac/Out` phase currents.
  - `Ac/ActiveIn` values may be positive or negative; `Ac/Out` currents are clamped to non-negative values.
  - `CERBO_PHASE_POWER_SOURCE` can pivot phase-power behavior without code edits:
    - `activein` uses Cerbo `Ac/ActiveIn` per-phase watts.
      When `CERBO_FORCE_NONNEGATIVE_PHASE_POWER=1`, exports are first netted against imports across phases, then clamped to `>=0`.
    - `acout` derives phase watts from ABB phase voltages and Cerbo `Ac/Out` phase currents.
    - `abb` leaves phase-power words unchanged from ABB passthrough.
  - `CERBO_COHERENT_PHASE_FRAMES=1` publishes snapshots only after complete 3-phase updates for both
    `Ac/ActiveIn` and `Ac/Out`, reducing mixed-time phase combinations.
  - Optional virtual PV meter emulation for Maxem slave `001` is available via:
    - `CERBO_ENABLE_PV_SLAVE=1`
    - `CERBO_PV_TARGET_SLAVE=1`
    - `CERBO_PV_TOPICS=<comma-separated topic list>`
    The default topic is `N/48e7da878d35/system/0/Dc/Pv/Power`.
    The PV total is summed from configured topic(s) and written to the `instantaneous_values` active-power words on slave `001`.
    Phase power/current words for slave `001` are synthesized coherently from that total (equal split by phase, amps from ABB phase voltage).
    For this test model, non-instantaneous blocks on slave `001` are not mirrored from slave `100`.
  This keeps Maxem home/grid power semantics aligned with grid import/export while preserving AC-out current safety inputs
  used for EV phase protection.
- The dry-run logger prints one semantic line before the preview values so it is obvious that the preview is the
  ABB instantaneous register block being rewritten from Cerbo MQTT.
- Register bundle captures and replay previews live under `tools/`. The dump helper defaults to `instantaneous_values`
  only, and the replay helper prints the same preview lines without touching the RTU adapter.
- Preview semantics are simple:
  - `ABB source` is the decoded instantaneous active-power total from ABB.
  - `Cerbo Usage to Maxem` is the rewrite total watts from `Ac/ActiveIn`.
  - `Cerbo Phase Watts to Maxem` are per-phase rewrite watts from `Ac/ActiveIn`.
  - `Cerbo Phase Currents to Maxem` are per-phase + neutral current amps from `Ac/Out`.
- Core application modules now live under `lib/` so the repo root stays focused on the entrypoint, tools,
  and docs.

## Development workflow

- Test files use numbered prefixes like `tests/10_test_maxem_home_usage.py` and `tests/20_test_dump_and_replay.py` so
  related suites can grow in a predictable order.
- `pytest.ini` is configured to collect only numbered test files from `tests/`.
- The quickest local checks are `python3 -m pytest` and `python3 -m py_compile` on the touched modules.
- For the register tooling, run `python3 tools/dump_register_block.py --help` and
  `python3 tools/replay_maxem_preview.py --help` before using real captures.
- `python3 tools/dump_register_block.py` defaults to the ABB `instantaneous_values` block; pass `--register` to add
  extra blocks only when you truly need them.
- `python3 tools/replay_maxem_preview.py --bundle <file>` prints the same short preview line format the dry-run
  runtime uses (`ABB source` plus rewrite lines) from a captured bundle.
- `python3 tools/inspect_instantaneous_payload.py --bundle <file>` unpacks the key ABB instantaneous fields and shows
  source vs rewritten values plus the exact word addresses that changed.
- `python3 main.py --trace-instantaneous-payload` enables the same field-level diff in live runtime logs so we can
  verify exactly what is being written without changing default behavior.
- Cerbo MQTT poller is read-only and subscribes to:
  - `CERBO_AC_OUT_TOPIC` (default `N/48e7da878d35/vebus/276/Ac/Out`)
  - `CERBO_AC_ACTIVEIN_TOPIC` (default `N/48e7da878d35/vebus/276/Ac/ActiveIn`)
  - `CERBO_PV_TOPICS` (default: `N/48e7da878d35/system/0/Dc/Pv/Power`; summed for virtual PV meter power)
- At `DEBUG` level the runtime logs snapshot updates from MQTT and preview lines (`ABB source` / `Cerbo ... to Maxem`).
- To keep DEBUG readable by default:
  - `CERBO_MQTT_PROTOCOL_DEBUG=0` suppresses raw paho wire logs (`Sending CONNECT`, `Received PUBLISH`, etc).
  - `CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS=0` suppresses per-message snapshot debug spam.
  - Set `CERBO_MQTT_PROTOCOL_DEBUG=1` and/or `CERBO_MQTT_SNAPSHOT_DEBUG_INTERVAL_SECONDS=<seconds>` only for deep MQTT troubleshooting.
- Startup logs print effective Cerbo source settings and where values came from (`env`, `.env`, or defaults).
- The main loop now handles `Ctrl-C` cleanly in one interrupt and stops the poller and servers without a traceback.
- Canonical project-specific runtime rules and durable decisions live in
  `docs/decisions/project-conventions.md`.
- Long-run logging/observability tuning is controlled with:
  - `SUPPRESS_SHORT_RTU_REQUEST_LOGS=1` (default) to suppress only `invalid request: Request length is invalid 1`.
  - `STATUS_LOG_INTERVAL_SECONDS=30` (default) to emit periodic heartbeat summaries instead of per-loop update spam.

## Tested Hardware
This has been tested with the Exar USB to RS-485 adapter and with the Waveshare CAN Hat (CANbus and RS-485 add-on) for
RPi hardware.

#### Exar Notes:
- use the included driver which will build on linux kernels 5.15 and newer
- look at the rc.local file in the ```etc``` directory for tips how to load and start the driver 

#### Waveshare CAN Hat notes:
- The serial port of RPi hardware is used by default by the console.  We need to disable this using the 
```raspi-config``` tool.  Turn off the console but turn on the port itself.  
- Then, add the following to /boot/config.txt after fitting the unit to your board. 

 ```
 # Enable waveshare can hat -jd
dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=2000000
enable_uart=1
dtoverlay=pi3-miniuart-bt
```
This enables the canbus interface, UART, and allows use of /dev/ttyAMA0 as an RS-485 serial port.  
Finally, reboot the RPi. 

## Acknowledgements
This project, with gratitude, depends on and appreciates the following third party projects
- Modbus-TK (https://github.com/ljean/modbus-tk)
- pyserial (for RTU/serial communication) (https://github.com/pyserial/pyserial)
