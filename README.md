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
- In dry-run mode the Maxem preview is intentionally short and easy to compare against dashboards:
  `ABB source: X W`, `DZ Usage to Maxem: Y W`, and `DZ Phase Watts to Maxem: L1=..., L2=..., L3=...`.
  These lines are emitted at `DEBUG` level (set `LOG_LEVEL=DEBUG` when you want them).
- The corrected v1 story is to source Domoticz IDX 20 `Usage` as the live grid-import watt reading, clamp any negative
  net value to zero, and encode the result into the ABB-compatible instantaneous active-power registers:
  `0x5B14/0x5B15` (total), `0x5B16/0x5B17` (L1), `0x5B18/0x5B19` (L2), and `0x5B1A/0x5B1B` (L3). Phase watt inputs come from Domoticz
  `result.Data` at `rid=26` (L1), `rid=25` (L2), and `rid=24` (L3). House load is not forwarded to Maxem.
  The earlier cumulative-counter preview was a prototype interpretation and is deprecated.
- The dry-run logger prints one semantic line before the preview values so it is obvious that the preview is the
  ABB instantaneous power register being rewritten from Domoticz `Usage`.
- Register bundle captures and replay previews live under `tools/`. The dump helper defaults to `instantaneous_values`
  only, and the replay helper prints the same preview lines without touching the RTU adapter.
- Preview semantics are simple:
  - `ABB source` is the decoded instantaneous active-power total from ABB.
  - `DZ Usage to Maxem` is the Domoticz IDX 20 grid-import watt reading after clamping negatives to zero and encoding
    it back into the ABB register format.
  - `DZ Phase Watts to Maxem` are the Domoticz per-phase watt readings (`rid 26/25/24`) encoded into
    `active_power_l1/l2/l3`.
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
- `python3 tools/replay_maxem_preview.py --bundle <file>` prints the same `ABB source`, `DZ Usage to Maxem`, and
  optional `DZ Phase Watts to Maxem` preview lines that the dry-run runtime uses.
- `python3 tools/inspect_instantaneous_payload.py --bundle <file>` unpacks the key ABB instantaneous fields and shows
  source vs rewritten values plus the exact word addresses that changed.
- `python3 main.py --trace-instantaneous-payload` enables the same field-level diff in live runtime logs so we can
  verify exactly what is being written without changing default behavior.
- `LOG_LEVEL` defaults to `INFO`; set `LOG_LEVEL=DEBUG` to show preview lines (`ABB source` / `DZ ... to Maxem`).
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
