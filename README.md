# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Use at your own risk**
> This is unofficial, open-source software with no affiliation to Tesla. Flashing ECU firmware carries real risk — a failed or interrupted flash can leave an ECU in an unrecoverable state, potentially disabling safety-critical vehicle systems. By using these tools you accept full responsibility for any damage to your vehicle, its components, or any third parties. The authors provide no warranty and assume no liability.

## Tools

| Tool | Description |
| ---- | ----------- |
| [`tm3diag.py`](docs/tm3diag.md) | Interactive diagnostic terminal — read DIDs, run routines, trigger firmware updates |
| [`tm3uds.py`](docs/tm3uds.md) | General-purpose UDS CLI for reading/writing DIDs, routines, and session management |
| [`dfu.py`](docs/dfu.md) | Firmware flash CLI — identity discovery, file selection, and ECU-specific flash sequence |
| [`bhx.py`](docs/bhx.md) | BHX firmware image parser and builder |
| [`pcs_send.py`](docs/pcs_send.md) | Interactive PCS CAN scripting shell |
| [`compact_to_dbc.py`](docs/compact_to_dbc.md) | Convert `Model3_ETH.compact.json` to DBC |
| [`can_live.py`](docs/can_live.md) | Web-based live CAN signal viewer |

## Configuration

All tools read defaults from a `.env` file in the project root. Copy the example and edit:

```
cp .env.example .env
```

```sh
# CAN interface
TM3_CHANNEL=can0
TM3_INTERFACE=socketcan
# TM3_BITRATE=500000

# Data files (defaults to data/ in the project root)
# TM3_NODES_JSON=/path/to/nodes.json
# TM3_ETH_COMPACT=/path/to/Model3_ETH.compact.json
# TM3_ODJ_DIR=/path/to/odj

# Firmware artifacts
# TM3_ARTIFACTS_DIR=/path/to/seed_artifacts_v2

# Odin .bin decryption key (base64-encoded, NOT committed to the repo)
# Found in opt/odin/odin from a firmware squashfs extraction.
# TM3_BIN_KEY=<base64-encoded value>
```

CLI flags always override `.env` values. `.env` is gitignored.

## Data files

Node configurations live in `data/`:

- `nodes.json` — CAN IDs, security parameters, and ODJ sources for each ECU
- `Model3_ETH.compact.json` — CAN message ID map
- `odj/` — Per-ECU DID definitions (name, hex ID, read/write sizes, security level)

## Reference

- [FIRMWARE_UPDATE.md](docs/FIRMWARE_UPDATE.md) — UDS flash protocol, script map, frame-by-frame reference
- [ECU_DIDS.md](docs/ECU_DIDS.md) — DID reference for all ECU nodes
- [UDS_VM_OPCODES.md](docs/UDS_VM_OPCODES.md) — VM opcode table from `hashpicker_sim`

## Requirements

```
pip install -r requirements.txt
```

Requires Python 3.10+, `python-can`, `python-dotenv`, `aiohttp`, and a SocketCAN interface (real or virtual via `vcan`).

## Tests

```
pytest tests/ -v
```

Tests require the `seed_artifacts_v2` firmware artifact directory. Tests that depend on it are automatically skipped when the path is absent.
