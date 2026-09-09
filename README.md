# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Use at your own risk**
> This is unofficial, open-source software with no affiliation to Tesla. Flashing ECU firmware carries real risk — a failed or interrupted flash can leave an ECU in an unrecoverable state, potentially disabling safety-critical vehicle systems. By using these tools you accept full responsibility for any damage to your vehicle, its components, or any third parties. The authors provide no warranty and assume no liability.

> **Scope**
> This tool ships **no** seed/key algorithms, immobilizer logic, or decryption keys. It is a CAN/UDS diagnostics and interoperability framework intended for use on hardware you own. Where a security-access or immobilizer computation is required, you supply it through a provider you are lawfully entitled to use — see [docs/SECURITY_PROVIDER.md](docs/SECURITY_PROVIDER.md).

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

Set `TM3_VEHICLE_CHANNEL` to your CAN interface name and `TM3_INTERFACE` to your adapter's driver. The defaults work for a standard Linux SocketCAN setup:

```bash
TM3_VEHICLE_CHANNEL=can0   # your interface name — check with: ip link show type can
TM3_INTERFACE=socketcan
```

`TM3_VEHICLE_CHANNEL` is the vehicle bus and the default channel for every tool. On a multi-bus bench you can also set `TM3_PARTY_CHANNEL` and `TM3_CHARGE_CHANNEL`; tools fall back to the vehicle bus when a bus has no channel configured.

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
# then set TM3_VEHICLE_CHANNEL=vcan0 in .env
```

### 3. Firmware dump (optional)

Some tools (`tm3cli.py`, `dfu.py`, `tm3uds.py`) can decode signal names and validate routines when pointed at an extracted Tesla firmware squashfs. This is **not required** for the CAN/bench tools.

If you have a firmware image, extract it with `unsquash_firmware.py`, then set `TM3_ROOT` in `.env` to the resulting `squashfs-root` directory:

```bash
TM3_ROOT=/path/to/squashfs-root
```

If your firmware's `.compact.json` and ODJ files are encrypted `.bin` files, you'll also need the decryption key, which you must supply for firmware you own — this project ships no key-extraction tooling. See `.env.example`.

### 4. Security / immobilizer provider (optional)

The framework contains no seed/key or immobilizer algorithms. Tools that need a UDS SecurityAccess key or an immobilizer response resolve it through a provider you supply. If none is configured they fail closed with a pointer to the docs. See [docs/SECURITY_PROVIDER.md](docs/SECURITY_PROVIDER.md) for the interface.

## Tools

| Tool | Description |
|---|---|
| [`tm3cli.py`](docs/tm3cli.md) | Interactive diagnostic terminal — read DIDs, run routines, trigger firmware updates |
| [`tm3uds.py`](docs/tm3uds.md) | General-purpose UDS CLI for reading/writing DIDs, routines, and session management |
| [`dfu.py`](docs/dfu.md) | Firmware flash CLI — identity discovery, file selection, and ECU-specific flash sequence |
| [`scripts/di/di.py`](docs/di.md) | Drive Inverter bench emulator — gear/system control + optional immobilizer responder (provider-supplied) |
| [`scripts/pcs/pcs.py`](docs/pcs.md) | PCS bench emulator — operating modes, precharge, DC-DC and charge control |
| [`bhx.py`](docs/bhx.md) | BHX firmware image parser and builder |
| [`ihex.py`](docs/ihex.md) | Intel HEX / `.hgz` parser — decode dual-bank gateway images to canonical Intel HEX |
| [`clog.py`](docs/clog.md) | Gateway cluster-log parser — decode `CL/DATA/*.CLH`+`*.CLB` signal logs |
| [`compact_to_dbc.py`](docs/compact_to_dbc.md) | Convert `Model3_ETH.compact.json` to DBC |
| [`dump_odin.py`](docs/dump_odin.md) | Extract + decompile the odin PyInstaller binary from a firmware squashfs |
| [`unsquash_firmware.py`](docs/unsquash_firmware.md) | Unsquash a firmware image and expand its nested `.dirsquashed` parts |
| [`tm3web.py`](docs/tm3web.md) | Web-based live CAN signal viewer |

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
