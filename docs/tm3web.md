# tm3web.py — Local web console

Browser console for tm3diag on `http://localhost:8765`. Decodes the live CAN bus in real
time and presents it as tabs: **Live** (per-node signal tables), **Alerts** (decoded faults +
alert log), **DB Explorer**, **Raw Frames**, **ODIN**, and **DID**. A driver HUD lives at
`/dash`.

```bash
python tm3web.py                          # signals from the firmware compact JSON (TM3_ROOT)
python tm3web.py --channel can0
python tm3web.py --dbc Model3_ETH.dbc     # use a DBC instead — no firmware dump needed
```

The browser opens automatically except under WSL — there, open the URL manually.

## Options

| Flag | Default | Description |
|---|---|---|
| `--channel` / `--interface` / `--bitrate` | from `.env` | primary CAN bus |
| `--channel2` / `--interface2` / `--bitrate2` | — | optional 2nd bus (e.g. party/private) |
| `--dbc` | — | load a DBC instead of the firmware compact JSON |
| `--port` | `8765` | HTTP / WebSocket port |
| `--no-browser` | off | don't auto-open the browser |
| `--ui-rate` | Hz | UI refresh rate; frames coalesced per ID between ticks |
| `--tx-ids` / `--show-tx` | — | mark IDs our tools transmit (hidden unless `--show-tx`) |
| `--ignore-ids` / `--hide-unknown` / `--hide-host-tx` | — | drop frames from the view |
| `--control` / `--sim-url` | — | enable `/dash` controls, forwarded to `vehicle_sim` (run it alongside) |

## Signal database

Without `--dbc`, signals come from the firmware compact JSON (`TM3_ROOT` in `.env`) — all
~39 ETH-bus nodes with names, units, and enum labels. Pass `--dbc` with any DBC (e.g. one
produced by [compact_to_dbc.py](compact_to_dbc.md)) to run without a firmware dump.

## Live tab

Click a node in the sidebar to populate its signal table (name, raw value, unit, enum label;
cells flash on change). The per-message age indicator turns red when stale (>1 s), a filter
narrows signals by name, and an FPS counter shows decoded frames/second.

## Alerts tab

Turns raw alert bits into readable faults using the MCU alert catalog from `libQtCarAlerts.so`
+ `libQtCarCANData.so` (see [alerts.md](alerts.md), `alert_log.py`). Needs `TM3_ROOT`; without
it the panels show only the name-derived title.

- **Active faults** — every alert-matrix bit currently set, as a card with title, cause,
  clears-when, effect, and the raw `NODE_aNNN_name`. Bench hardware faults (no motor/HV/
  resolver/LIN wired) are dimmed and tagged `hw`. The tab badge shows the active count.
- **Alert log** — the ECUs' own `<NODE>_alertLog` broadcasts, decoded; identical payloads fold
  into one row with first/last-seen + count. **Clear log** drops the log and latched bits.

Log decode resolves, in order: every field of an alert with a recovered bit layout (e.g.
`DI_a162_shiftDenied` → `SYS_STATE_NOT_ENABLED`, gear N→D); CAN-rationality alerts (offending
message + both bad values); any alert whose leading log signal is an enum (the *reason* it
fired); otherwise name + description with the raw payload listed.

Bit layouts aren't in any shipped artifact — they're recovered from DU firmware and supplied
separately per rev (point `TM3_ALERTLOG_LAYOUTS` at a layouts file). Without one the first three
tiers still work — it's enrichment, never a dependency.

> **Active faults** needs alert-matrix messages in the loaded DB. A per-rev `compact.json` is
> partial (2022.4.15 carries only `DIR_alertMatrix3`), so use `--dbc` with a year DBC to
> populate it. **Alert log** doesn't need the signal DB at all.
