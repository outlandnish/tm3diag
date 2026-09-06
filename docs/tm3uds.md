# tm3uds.py — UDS diagnostic CLI

General-purpose UDS client for reading/writing DIDs, running routines, managing sessions, and scanning the network.

```
python tm3uds.py scan --channel vcan0
python tm3uds.py --node PCS --channel vcan0 read-did BOOTLOADER_VERSION
python tm3uds.py --node PCS --channel vcan0 read-did 0xF180
python tm3uds.py --node PCS --channel vcan0 write-did 0x0102 deadbeef
python tm3uds.py --node PCS --channel vcan0 routine 0xFF00
python tm3uds.py --node CP  --channel vcan0 security-access
python tm3uds.py --node PCS --channel vcan0 session programming
python tm3uds.py --node PCS --channel vcan0 reset
```

## Subcommands

| Command                 | Description                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------- |
| `scan`                  | Probe all nodes for a TesterPresent response                                          |
| `read-did <did>`        | Read a DID by name or hex ID (UDS 0x22)                                               |
| `write-did <did> <hex>` | Write a DID (UDS 0x2E)                                                                |
| `routine <id> [arg]`    | Execute a RoutineControl (UDS 0x31)                                                   |
| `security-access`       | Enter programming session and complete seed/key exchange                              |
| `session <mode>`        | Switch diagnostic session (`default`, `programming`, `extended`, `safety`, or `0xNN`) |
| `reset`                 | Send ECU hard reset (UDS 0x11 01)                                                     |
| `clear-dtc`             | ClearDiagnosticInformation group `0xFFFFFF` (UDS 0x14)                                |

## Options

| Flag                | Default         | Description                             |
| ------------------- | --------------- | --------------------------------------- |
| `--node`, `-n`      | —               | ECU node name (e.g. `PCS`, `CP`, `RCM`) |
| `--channel`, `-c`   | `TM3_VEHICLE_CHANNEL` | CAN interface                     |
| `--interface`, `-i` | `TM3_INTERFACE` | python-can interface type               |
