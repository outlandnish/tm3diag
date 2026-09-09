# tm3cli.py — Interactive diagnostic terminal

Interactive terminal for exploring ECU state. When run without `--node`, opens a pre-connection menu where you can scan the bus for live nodes before connecting. Once connected, reads identity on startup then lets you read DIDs by name (with tab completion), run routines by name or hex ID, switch sessions, and trigger a firmware update — all in one session.

```
python tm3cli.py --channel vcan0                          # opens pre-connection menu (scan / connect)
python tm3cli.py --node PCS --channel vcan0
python tm3cli.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
```

## Options

| Flag                | Default             | Description                                                       |
| ------------------- | ------------------- | ----------------------------------------------------------------- |
| `--node`, `-n`      | —                   | ECU node name. Opens pre-connection menu if omitted.              |
| `--channel`, `-c`   | `TM3_VEHICLE_CHANNEL` | CAN interface                                                   |
| `--interface`, `-i` | `TM3_INTERFACE`     | python-can interface type                                         |
| `--artifacts`, `-a` | `TM3_ARTIFACTS_DIR` | Path to `seed_artifacts_v2` (needed for DFU; prompted if missing) |

## Pre-connection commands

Shown when `--node` is omitted.

| Command          | Description                                        |
| ---------------- | -------------------------------------------------- |
| `scan`           | Probe all known nodes on the bus for TesterPresent |
| `connect <node>` | Connect to a node by name                          |
| `quit`           | Exit                                               |

## Connected commands

| Command       | Description                                                                        |
| ------------- | ---------------------------------------------------------------------------------- |
| `dids`        | Read DIDs by name (tab complete) or hex ID; auto-decodes fields from ODJ           |
| `routine`     | Run a routine by name or hex ID (see named routines below)                         |
| `board-parts` | Read board part/serial DIDs `0xF012`–`0xF015`, `0xF030`/`0xF031`                  |
| `clear-dtc`   | ClearDiagnosticInformation group `0xFFFFFF` (UDS 0x14)                             |
| `dfu`         | Full firmware update using `dfu.py` phases (identity → select → preflight → flash) |
| `session`     | Switch diagnostic session                                                          |
| `reset`       | ECU hard reset                                                                     |
| `quit`        | Disconnect and exit                                                                |

## Named routines

| Name                       | Routine ID | Description                                                      |
| -------------------------- | ---------- | ---------------------------------------------------------------- |
| `erase`                    | `0xFF00`   | `initializeEraseModule` — EraseMemory (requires security access) |
| `verify-crc`               | `0x0201`   | `checkModuleProgrammedCorrectly` — CRC verify                    |
| `check-component`          | `0x0202`   | `checkCorrectComponentAndRev`                                    |
| `ota-wait`                 | `0x0540`   | `vcWaitForOTAMode` / `otaStateRoutineControl`                    |
| `ibst-power`               | `0x0543`   | `ibstPowerControl` (requires security access)                    |
| `bms-contactor-close`      | `0x0204`   | `bmsContactorControl` — close contactor                          |
| `bms-contactor-open`       | `0x0304`   | `bmsContactorControl` — open contactor                           |
| `disable-intrusion-sensor` | `0x0601`   | `disableIntrusionSensor`                                         |
