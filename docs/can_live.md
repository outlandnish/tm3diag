# can_live.py — Live CAN signal viewer

Web-based live viewer that decodes CAN frames in real time using `Model3_ETH.compact.json`. Select an ECU node from the sidebar to see all its signals updating live, with age indicators and enum labels.

```
python can_live.py --channel vcan0
python can_live.py --channel vcan0 --interface socketcan --port 8765
```

Opens `http://localhost:8765` automatically in your browser.

## Options

| Flag           | Default         | Description                      |
| -------------- | --------------- | -------------------------------- |
| `--channel`    | `TM3_CHANNEL`   | CAN interface                    |
| `--interface`  | `TM3_INTERFACE` | python-can interface type        |
| `--bitrate`    | `TM3_BITRATE`   | CAN bitrate (optional)           |
| `--port`       | `8765`          | HTTP/WebSocket port              |
| `--no-browser` | —               | Don't auto-open browser on start |

## Features

- Node sidebar — all 39 ECU nodes listed; click to subscribe
- Live signal table — per-message sections with signal name, value, unit, and enum label
- Value flash — cells briefly highlight when a value changes
- Age indicator — shows time since last frame per message; turns red when stale (>1 s)
- Signal filter — type to narrow signals by name
- FPS counter — shows decoded frames/second in the top bar

Signal decoding handles little-endian and big-endian bit packing, multiplexed signals (`mux_id`), `value_description` enum mappings, and signed/unsigned scale+offset.
