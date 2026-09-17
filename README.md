# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Use at your own risk**
> This is unofficial, open-source software with no affiliation to Tesla and is a vibe-coded rapid prototype. By using these tools you accept full responsibility for any damage to your vehicle, its components, or any third parties. The authors provide no warranty and assume no liability.

> Where a security-access or immobilizer is required, you'll need to supply it through a provider you are lawfully entitled to use — see [docs/SECURITY_PROVIDER.md](docs/SECURITY_PROVIDER.md).

## Requirements

- Python 3.10 or later
- python-can compatible CAN interface(s) connected to the Tesla ECUs. Check your ECU config for Vehicle and Party CAN interfaces 

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/outlandnish/tm3diag.git
cd tm3diag
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure your CAN interface

Copy the example config and open it in a text editor:

```bash
cp .env.example .env
```

```bash
TM3_VEHICLE_CHANNEL=can0
#TM3_PARTY_CHANNEL=can1 # optional
#TM3_CHARGE_CHANNEL=can2 # optional
TM3_INTERFACE=socketcan

# Optional path to extracted Tesla firmware squashfs root. use `unsquashfs_firmware.py` to extract a SquashFS image
TM3_ROOT=/path/to/squashfs-root

```

Make sure you've brought up the relevant interfaces:

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
```

### 3. Firmware dump (optional)

Some tools (`tm3cli.py`, `dfu.py`, `tm3uds.py`) can decode signal names and validate routines when pointed at an extracted Tesla firmware squashfs.

If you have a firmware image, extract it with `unsquash_firmware.py`, then set `TM3_ROOT` in `.env` to the resulting `squashfs-root` directory:

```bash
TM3_ROOT=/path/to/squashfs-root
```

If your firmware's `.compact.json` and ODJ files are encrypted `.bin` files, you'll also need the decryption key in your `. env` as `TM3_BIN_KEY`

### 4. Security / immobilizer provider (optional)

For UDS SecurityAccess as well as the immobilizer, an interface is provided for you to implement to access the relevant features. See [docs/SECURITY_PROVIDER.md](docs/SECURITY_PROVIDER.md). 

The ODIN diagnostic graphs ship as a zip. Unzip it it for access to diagnostics scripts:

```bash
cd "$TM3_ROOT/opt/odin" && unzip -q odin_bundle.zip     # -> opt/odin/odin_bundle/networks
```

### 4. Signal database

With `TM3_ROOT` set, CAN frames are automatically decoded and converted into readable signals. Alternatively, you can provide a DBC file or generate one from the firmware.

`default_db()` resolves three sources, best first:

| | Source | Covers |
|---|---|---|
| 1 | **The firmware's own decoder** (`vapi_emu`) | the whole catalog, exactly as the car decodes it |
| 2 | A generated DBC (`candata_to_dbc.py`) | the whole catalog, from bit layouts recovered out of that same decoder |
| 3 | `Model3_ETH.compact.json` | only the subset Tesla ships to the diagnostic tool, and it shrinks every release |

To generate a DBC:

```bash
python candata_to_dbc.py dbc        # writes Model3_ETH.<rev>.dbc, ~1-2 min
```

## Tools

| Tool | Description |
|---|---|
| [`tm3web.py`](docs/tm3web.md) | Web-based live CAN signal viewer, ODIN interface, dashboard |
| [`tm3cli.py`](docs/tm3cli.md) | Interactive diagnostic terminal — read DIDs, run routines, trigger firmware updates |
| [`tm3uds.py`](docs/tm3uds.md) | General-purpose UDS CLI for reading/writing DIDs, routines, and session management |
| [`dfu.py`](docs/dfu.md) | Firmware flash CLI — identity discovery, file selection, and ECU-specific flash sequence |
| [`bhx.py`](docs/bhx.md) | BHX firmware image parser and builder |
| [`ihex.py`](docs/ihex.md) | Intel HEX / `.hgz` parser — decode dual-bank gateway images to canonical Intel HEX |
| [`clog.py`](docs/clog.md) | Gateway cluster-log parser — decode `CL/DATA/*.CLH`+`*.CLB` signal logs |
| [`compact_to_dbc.py`](docs/compact_to_dbc.md) | Convert `Model3_ETH.compact.json` to DBC |
| [`dump_odin.py`](docs/dump_odin.md) | Extract + decompile the odin PyInstaller binary from a firmware squashfs |
| [`unsquash_firmware.py`](docs/unsquash_firmware.md) | Unsquash a firmware image and expand its nested `.dirsquashed` parts |

## Reference

- [unsquash_firmware.md](docs/unsquash_firmware.md) — How to extract a firmware blob to a `squashfs-root` directory
- [SECURITY_PROVIDER.md](docs/SECURITY_PROVIDER.md) — The security-access / key-derivation provider interface (signatures only)
- [FIRMWARE_UPDATE.md](docs/FIRMWARE_UPDATE.md) — UDS flash protocol, script map, frame-by-frame reference
- [ghidra_c28x_loading.md](docs/ghidra_c28x_loading.md) — Load a TMS320 firmware image (inverter DIR/PMR, PCS) into Ghidra for reverse engineering

## Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

## License

Licensed under the GNU General Public License v3.0 — see [LICENSE](LICENSE).
