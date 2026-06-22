# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Use at your own risk**
> This is unofficial, open-source software with no affiliation to Tesla. Flashing ECU firmware carries real risk — a failed or interrupted flash can leave an ECU in an unrecoverable state, potentially disabling safety-critical vehicle systems. By using these tools you accept full responsibility for any damage to your vehicle, its components, or any third parties. The authors provide no warranty and assume no liability.

## Requirements

- Python 3.10 or later
- A CAN interface connected to any of the Tesla ECUs — either a real USB adapter (e.g. PEAK, Kvaser, CANable) or a virtual interface (`vcan`) for offline testing
- Linux is recommended; SocketCAN is the default interface driver

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/outlandnish/tm3diag.git
cd tm3diag
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The `source` line activates the virtual environment. You'll need to run it again in each new terminal session before using any of the tools:

```bash
source .venv/bin/activate
```

### 2. Configure your CAN interface

Copy the example config and open it in a text editor:

```bash
cp .env.example .env
```

Set `TM3_CHANNEL` to your CAN interface name and `TM3_INTERFACE` to your adapter's driver. The defaults work for a standard Linux SocketCAN setup:

```bash
TM3_CHANNEL=can0       # your interface name — check with: ip link show type can
TM3_INTERFACE=socketcan
```

Bring the interface up before running any tool (replace `can0` and `500000` with your interface and bitrate):

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
```

To use a virtual interface for testing without hardware:

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
# then set TM3_CHANNEL=vcan0 in .env
```

### 3. Firmware dump (optional)

Some tools (`tm3diag.py`, `dfu.py`, `tm3uds.py`) can decode signal names and validate routines when pointed at an extracted Tesla firmware squashfs. This is **not required** for the immobilizer handshake script.

If you have a firmware image, extract it with `unsquash_firmware.py`, then set `TM3_ROOT` in `.env` to the resulting `squashfs-root` directory:

```bash
TM3_ROOT=/path/to/squashfs-root
```

If your firmware's `.compact.json` and ODJ files are encrypted `.bin` files, you'll also need the decryption key — see `.env.example` for how to extract it.

## Tools

| Tool | Description |
|---|---|
| [`tm3diag.py`](docs/tm3diag.md) | Interactive diagnostic terminal — read DIDs, run routines, trigger firmware updates |
| [`tm3uds.py`](docs/tm3uds.md) | General-purpose UDS CLI for reading/writing DIDs, routines, and session management |
| [`dfu.py`](docs/dfu.md) | Firmware flash CLI — identity discovery, file selection, and ECU-specific flash sequence |
| [`scripts/di/di.py`](docs/di.md) | Drive Inverter bench emulator — gear/system control + immobilizer responder |
| [`scripts/di/immobilizer_handshake.py`](docs/immobilizer_handshake.md) | Pair a KEY/SALT with the Drive Inverter and run the runtime 0x276/0x3D9 responder |
| [`scripts/pcs/pcs.py`](docs/pcs.md) | PCS bench emulator — operating modes, precharge, DC-DC and charge control |
| [`bhx.py`](docs/bhx.md) | BHX firmware image parser and builder |
| [`ihex.py`](docs/ihex.md) | Intel HEX / `.hgz` parser — decode dual-bank gateway images to canonical Intel HEX |
| [`clog.py`](docs/clog.md) | Gateway cluster-log parser — decode `CL/DATA/*.CLH`+`*.CLB` signal logs |
| [`compact_to_dbc.py`](docs/compact_to_dbc.md) | Convert `Model3_ETH.compact.json` to DBC |
| [`dump_odin.py`](docs/dump_odin.md) | Extract + decompile the odin PyInstaller binary from a firmware squashfs |
| [`unsquash_firmware.py`](docs/unsquash_firmware.md) | Unsquash a firmware image and expand its nested `.dirsquashed` parts |
| [`can_live.py`](docs/can_live.md) | Web-based live CAN signal viewer |

## Reference

- [unsquash_firmware.md](docs/unsquash_firmware.md) — How to extract a firmware blob to a `squashfs-root` directory
- [immobilizer_handshake.md](docs/immobilizer_handshake.md) — Immobilizer pairing and runtime handshake guide
- [FIRMWARE_UPDATE.md](docs/FIRMWARE_UPDATE.md) — UDS flash protocol, script map, frame-by-frame reference

## Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```
