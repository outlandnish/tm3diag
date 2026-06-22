# can_live.py — Live CAN Signal Viewer

Web-based live viewer that decodes CAN frames in real time. Select an ECU node from the sidebar to see all its signals updating live, with age indicators and enum labels.

By default it loads signal definitions from the firmware dump (`TM3_ROOT`). Pass `--dbc` to use any DBC file instead — no firmware dump needed.

```bash
python can_live.py
python can_live.py --channel can0
python can_live.py --dbc Model3_ETH.2020.8.1-9-ae1963092f.dbc
```

Then open **http://localhost:8765** in your browser. On Linux (non-WSL) the browser opens automatically; on WSL you need to navigate there manually.

## Options

| Flag | Default | Description |
|---|---|---|
| `--channel` | from `.env` or `vcan0` | CAN interface name |
| `--interface` | from `.env` or `socketcan` | python-can interface type |
| `--bitrate` | from `.env` | CAN bitrate (optional) |
| `--port` | `8765` | HTTP and WebSocket port |
| `--no-browser` | off | Don't auto-open browser on start |
| `--dbc` | — | Load a DBC file instead of the firmware compact JSON |

## Signal database

Without `--dbc`, signals are loaded from the firmware compact JSON (`TM3_ROOT` must be set in `.env`). This covers all ~39 ETH bus nodes with full signal names, units, and enum labels.

With `--dbc`, pass any DBC file — including the ones shipped in this repo:

```bash
python can_live.py --dbc Model3_ETH.2020.8.1-9-ae1963092f.dbc
python can_live.py --dbc Model3_ETH.develop-2026.8.3-11442-4af4ce1574.dbc
```

This works without a firmware dump and is useful for quick signal inspection or when working with a specific known firmware version.

## UI features

- **Node sidebar** — all ETH bus nodes listed; click one to subscribe and populate the signal table
- **Live signal table** — per-message sections with signal name, raw value, unit, and enum label
- **Value flash** — cells briefly highlight when a value changes
- **Age indicator** — time since last frame per message; turns red when stale (>1 s)
- **Signal filter** — type to narrow signals by name within the selected node
- **FPS counter** — decoded frames/second shown in the top bar

## WSL note

Browser auto-open is disabled when running under WSL. Navigate to **http://localhost:8765** manually after starting the server. If the port isn't reachable from your Windows browser, check that your WSL distro's firewall isn't blocking it, or use `--port` to try a different port.
