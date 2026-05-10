# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Use at your own risk**
> This is unofficial, open-source software with no affiliation to Tesla. Flashing ECU firmware carries real risk — a failed or interrupted flash can leave an ECU in an unrecoverable state, potentially disabling safety-critical vehicle systems. By using these tools you accept full responsibility for any damage to your vehicle, its components, or any third parties. The authors provide no warranty and assume no liability.

## Tools

| Tool                                          | Description                                                                              |
| --------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [`tm3diag.py`](docs/tm3diag.md)               | Interactive diagnostic terminal — read DIDs, run routines, trigger firmware updates      |
| [`tm3uds.py`](docs/tm3uds.md)                 | General-purpose UDS CLI for reading/writing DIDs, routines, and session management       |
| [`dfu.py`](docs/dfu.md)                       | Firmware flash CLI — identity discovery, file selection, and ECU-specific flash sequence |
| [`bhx.py`](docs/bhx.md)                       | BHX firmware image parser and builder                                                    |
| [`compact_to_dbc.py`](docs/compact_to_dbc.md) | Convert `Model3_ETH.compact.json` to DBC                                                 |
| [`can_live.py`](docs/can_live.md)             | Web-based live CAN signal viewer                                                         |

## Configuration

All tools read defaults from a `.env` file in the project root. Copy the example and edit:

```
cp .env.example .env
```

You'll need to point it to your Tesla firmware squashfs root directory. If your ODJ and Model3_ETH.compact.json file are encrypted as bin files, follow the instructions in `.env.example` to obtain your key.

## Reference

- [FIRMWARE_UPDATE.md](docs/FIRMWARE_UPDATE.md) — UDS flash protocol, script map, frame-by-frame reference

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
