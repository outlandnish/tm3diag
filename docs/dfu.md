# dfu.py — Firmware flash CLI

Flashes firmware to an ECU using the correct ECU-specific UDS programming sequence: identity discovery → firmware selection → pre-flight verification → flash. The flash sequence is selected automatically based on the ECU type reported in `signed_metadata_map.tsv`, covering all script variants reverse-engineered from `hashpicker_sim`.

For PCS-family ECUs where both a primary and secondary CPU file are present, the tool automatically uses the dual-CPU prog-1 sequence (single authenticated session, both CPUs in order). See [FIRMWARE_UPDATE.md](FIRMWARE_UPDATE.md) for protocol details.

```
python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
```

## Options

| Flag                | Default             | Description                             |
| ------------------- | ------------------- | --------------------------------------- |
| `--node`, `-n`      | —                   | ECU node name                           |
| `--channel`, `-c`   | `TM3_VEHICLE_CHANNEL` | CAN interface                         |
| `--interface`, `-i` | `TM3_INTERFACE`     | python-can interface type               |
| `--artifacts`, `-a` | `TM3_ARTIFACTS_DIR` | Path to `seed_artifacts_v2` directory   |
| `--force`           | —                   | Proceed despite BHX identity mismatches |

The tool reads `signed_metadata_map.tsv` from the artifacts directory to select the correct firmware file for the connected ECU's identity.
