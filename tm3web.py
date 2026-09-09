#!/usr/bin/env python3
"""Live CAN viewer — aiohttp backend with WebSocket streaming.

Pipeline (sized for a full vehicle bus, not just a quiet bench):

    bus.recv() in a dedicated OS thread per bus
        -> per-ID coalescing buffer (latest frame wins, arrivals counted)
        -> asyncio flusher at --ui-rate Hz
        -> ONE batched JSON message per tick
        -> per-client writer task with a latest-wins outbox

Three rules keep it from saturating:
  * The CAN reader never touches asyncio, JSON, or a socket — it only buffers,
    so nothing on the browser side can stall bus ingest and overflow the RX queue.
  * Within a flush tick only the newest frame per (bus, ID) survives; the frames
    coalesced away are still counted, so the displayed rate stays truthful even
    though a 100 Hz message only paints 20 times a second.
  * A slow client drops stale batches instead of applying backpressure.

The read-only viewer lives at ``/``. A separate driver-HUD dashboard (speed,
gear control + reported gear, immobilizer, telltales, faults, LV/pedal/car-config)
lives at ``/dash``. tm3web is the command SURFACE + viewer: with ``--control``,
gear/pedal/LV/car-config actions from the dashboard are FORWARDED to vehicle_sim's
control server (``--sim-url``). vehicle_sim is the single bus owner — it transmits
everything and runs the closed-loop responses (e.g. EPB) — so there's no two-writer
collision and no ``--exclude`` juggling.

Usage:
  python tm3web.py --channel vcan0
  python tm3web.py --channel can0 --channel2 can1 --ui-rate 30
  # driver HUD with gear/pedal/car-config/LV control (open http://localhost:8765/dash):
  #   1) the sim owns the bus + serves its control server on :8770:
  python scripts/vehicle_sim.py --channel can0 --party-channel can1
  #   2) the viewer/HUD, forwarding /dash commands to the sim:
  python tm3web.py --channel can0 --control
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import aiohttp
import can
from aiohttp import web

import alert_log
import config as _cfg
from can_decoder import CanDatabase

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
# Reuse di.py's DI-report decode maps (for the RX side / dashboard visualization).
# Import via the `di` PACKAGE (scripts/di/) -> di.di module; putting scripts/di
# itself on sys.path would make di.py shadow the package and break sim_registry's
# `from di.di_node import NODE` (di.py deliberately strips scripts/di from the path).
# All command TX + the shared frame builders live in vehicle_sim + tesla_frames now;
# tm3web forwards commands to vehicle_sim's control server (--sim-url) and views.
from di.di import (  # noqa: E402
    _DI_0X118_RECOVERED,
    DI_GEAR_LABELS,
    DI_IMMO_LABELS,
    DI_SYS_LABELS,
)

_STATIC_DIR = Path(__file__).parent / "tm3web_ui"
_DB: CanDatabase | None = None

log = logging.getLogger("tm3web")

# --- dashboard: DI signals we surface as a driver HUD (2020 Model3_ETH DB) -----
# Decoded every flush tick regardless of viewer subscriptions so the dashboard
# always has fresh state. 0x118 DI_systemStatus, 0x257 DI_speed, 0x256 DIR_status,
# 0x2B6 DI_chassisControlStatus (the real telltale lamps: VDC / TC / vehicle-hold).
_DASH_IDS = (0x118, 0x257, 0x256, 0x2B6)

# Active alert-matrix bits are surfaced as a faults panel. Names matching these
# hints are bench HARDWARE faults (no motor / HV / resolver wired) — flagged so
# they read as expected, not as something CAN liveness can clear.
_HW_FAULT_HINTS = (
    "statorsensor",
    "nostatorsensor",
    "busvsensor",
    "resolver",
    "oilpump",
    "linerror",
)
_ALERT_RE = re.compile(r"_a\d")  # alert bit signals, e.g. DIR_a050_noStatorSensor

# --- alert catalog + alert log ------------------------------------------------
# Alert-matrix bits and <NODE>_alertLog frames both identify an alert as
# NODE_aNNN_*. The MCU alert catalog (libQtCarAlerts.so, via alert_log)
# turns that into a human description / cause / clear / effect. It's OPTIONAL:
# without a firmware extraction (config.ROOT) the alert panels still render,
# they just carry the name-derived title and no prose.
_ALERT_CAT = None
_ALERT_CAT_ERR: str | None = None
_ALERT_CAT_TRIED = False
# Distinct alertLog payloads kept per bus session. An ECU re-broadcasts the same
# logged alert continuously, so identical payloads are folded into one entry with
# a first/last-seen instead of flooding a stream.
_ALERT_LOG_CAP = 200

# --- ODIN live cross-reference ------------------------------------------------
# A signal counts as "on the bus now" (green in the ODIN "requires on bus" panel)
# if the CAN frame carrying it arrived within this many seconds. Frame ARRIVAL, not
# a decode, is the presence test -- the ODIN page holds no decoded-stream client, so
# only arrival is reliably observed for every id (see _flusher / _api_seen_signals).
_SEEN_WINDOW_S = 5.0
# Only surface faults from the drive-unit ECUs. The DU also emits frames at other
# ECUs' alert-matrix IDs (e.g. ESP 0x3D5, OCS1P 0x395); decoding those with the
# full-car DB's ESP/OCS layouts yields PHANTOM alerts (there's no ESP/OCS on a DU
# bench), so we ignore any alert matrix whose ECU prefix isn't a drive-unit one.
_FAULT_ECUS = {"DIR", "DI", "PMR", "PMF", "DIF"}

_DEFAULT_UI_RATE = 20.0  # flush ticks per second

# Frames whose arbitration id is absent from the signal DB are forwarded to the
# decoded view under this synthetic node rather than dropped -- see _flusher.
# Capped so a real vehicle bus (far more ids than the DB carries) can't spray
# hundreds of sections into the table.
_UNKNOWN_NODE = "unknown"
_UNKNOWN_ID_CAP = 64
_RCVBUF_BYTES = 1 << 20  # best-effort SocketCAN receive buffer (burst headroom)
_RECV_TIMEOUT = 0.2  # bus.recv() timeout, only bounds shutdown latency


# ---------------------------------------------------------------------------
# Frame filtering (our own bench traffic vs. the vehicle's)
# ---------------------------------------------------------------------------


class _TxFilter:
    """Drops frames at the reader thread, before any buffering or decoding.

    ``tx_ids`` are the IDs *our* tooling transmits (e.g. vehicle_sim.py, di.py).
    Hidden by default when supplied, toggleable live from the UI. ``ignore_ids``
    is the permanent version (never re-enabled).

    ``hide_host_tx`` drops every frame that originated **on this host**, from any
    local process. Note what that is not: it is not "our own socket's echo".
    python-can derives ``is_rx`` from ``MSG_DONTROUTE``, which SocketCAN sets for
    anything a local socket sent (``MSG_CONFIRM`` is the this-socket marker), so
    with it on, everything vehicle_sim broadcasts disappears from the viewer
    alongside our own sends. It is off by default for that reason, and the
    this-socket case needs no filtering anyway: the reader never asks for
    ``receive_own_messages``, so the kernel already withholds its own echoes.
    """

    def __init__(
        self,
        tx_ids: set[int],
        ignore_ids: set[int],
        hide_host_tx: bool,
        tx_hidden: bool,
    ) -> None:
        self.tx_ids = frozenset(tx_ids)
        self.ignore = frozenset(ignore_ids)
        self.hide_host_tx = hide_host_tx
        self.tx_hidden = tx_hidden
        # Rebound (never mutated) so a reader thread always sees a consistent set.
        self._muted: frozenset[int] = self.tx_ids if tx_hidden else frozenset()

    def drop(self, msg: can.Message) -> bool:
        if self.hide_host_tx and msg.is_rx is False:
            return True  # created on this host (any local process), not just us
        aid = msg.arbitration_id
        return aid in self.ignore or aid in self._muted

    def set_tx_hidden(self, hidden: bool) -> None:
        self.tx_hidden = hidden
        self._muted = self.tx_ids if hidden else frozenset()


def parse_can_ids(spec: str | None) -> set[int]:
    """Parse ``132,0x212,300-31F`` into a set of IDs. Bare values are hex."""
    ids: set[int] = set()
    for tok in (spec or "").replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        lo, _, hi = tok.partition("-")
        start = int(lo, 16)
        ids.update(range(start, int(hi, 16) + 1) if hi else (start,))
    return ids


# ---------------------------------------------------------------------------
# Command forwarding -- tm3web is the command surface; vehicle_sim is the single
# bus owner. Gear/pedal/LV/car-config are POSTed to vehicle_sim's control server
# (--sim-url); it mutates its VehicleController and transmits. tm3web only reads
# the bus (below) for visualization + pulls commanded state from the sim.
# ---------------------------------------------------------------------------
_SIM_TIMEOUT = aiohttp.ClientTimeout(total=1.5)


async def _sim_post(app: web.Application, payload: dict) -> tuple[int, dict]:
    """Forward a command to vehicle_sim's control server. Returns (status, json)."""
    url = app.get("sim_url")
    if not url:
        return 409, {"error": "control disabled; start with --control (+ --sim-url)"}
    try:
        async with app["http"].post(url + "/cmd", json=payload, timeout=_SIM_TIMEOUT) as r:
            return r.status, await r.json()
    except Exception as e:  # noqa: BLE001
        return 502, {"error": f"vehicle_sim unreachable at {url}: {e}"}


async def _sim_get(app: web.Application, path: str) -> dict | None:
    """GET JSON from vehicle_sim's control server (/state or /carconfig); None on failure."""
    url = app.get("sim_url")
    if not url:
        return None
    try:
        async with app["http"].get(url + path, timeout=_SIM_TIMEOUT) as r:
            if r.status == 200:
                return await r.json()
    except Exception:  # noqa: BLE001
        return None
    return None


def _num(v: object) -> object:
    """Round floats for JSON; pass other types through."""
    return round(v, 2) if isinstance(v, float) else v


def _overlay_0x118(sig: dict[str, object], data: bytes) -> None:
    """Recover DI_immobilizerState/systemState/accelPedalPos straight from the
    0x118 bytes at their 2020 positions — needed when a 2022+ compact.json is
    loaded that stripped them from the DB. Reuses di.py's ``_DI_0X118_RECOVERED``.
    Harmless when the DB already decodes them (identical raw bits)."""
    if len(data) < 8:
        return
    val = int.from_bytes(bytes(data[:8]), "little")
    for name, (sb, w) in _DI_0X118_RECOVERED.items():
        raw = (val >> sb) & ((1 << w) - 1)
        if name == "DI_accelPedalPos":
            sig[name] = None if raw == 255 else raw * 0.4
        else:
            sig[name] = raw


def _alert_catalog():
    """The MCU alert catalog decoder, or None when the firmware libs are absent.

    Loaded once, lazily (parsing the two .so files costs ~0.7 s), and shared with
    can_decoder's alertLog overlay through alert_log's process-wide cache.
    """
    global _ALERT_CAT, _ALERT_CAT_ERR, _ALERT_CAT_TRIED
    if _ALERT_CAT_TRIED:
        return _ALERT_CAT
    _ALERT_CAT_TRIED = True
    try:
        _ALERT_CAT = alert_log.get_decoder()
    except Exception as e:  # noqa: BLE001 — no firmware root, missing libs, ...
        _ALERT_CAT_ERR = str(e)
        log.info("alert catalog unavailable (%s) — alert panels show names only", e)
    return _ALERT_CAT


def _faults_list(faults: dict[str, bool] | None) -> list[dict]:
    """Active alert-matrix bits as human-readable alerts.

    Each entry is a :func:`alert_log.alert_view` (title / description /
    cause / clear / effect, or just the name-derived title with no catalog) plus
    the bench-local ``ecu`` and ``hardware`` flags.
    """
    cat = _alert_catalog()
    out = []
    for name in sorted(k for k, v in (faults or {}).items() if v):
        view = cat.view(name) if cat is not None else alert_log.alert_view(name)
        view["ecu"] = view.get("node") or name.split("_", 1)[0]
        view["hardware"] = any(h in name.lower() for h in _HW_FAULT_HINTS)
        out.append(view)
    return out


def _record_alertlog(store: dict, dec, can_id: int, data: bytes, ts: float) -> None:
    """Fold one ``<NODE>_alertLog`` frame into the distinct-payload store.

    An all-zero payload is the idle "nothing logged" broadcast every ECU emits,
    so it's skipped. ``count`` is how many flush ticks saw this exact payload as
    the newest frame for its id — a liveness measure, not a frame count (frames
    are coalesced per tick before we get here).
    """
    if not any(data):
        return
    key = (can_id, bytes(data))
    ent = store.get(key)
    if ent is None:
        r = dec.decode(can_id, bytes(data))
        if r is None:
            return
        ent = r.to_dict()
        ent.update(key=f"{can_id:03X}:{bytes(data).hex()}", data=list(data),
                   first=ts, last=ts, count=0)
        store[key] = ent
        if len(store) > _ALERT_LOG_CAP:
            del store[min(store, key=lambda k: store[k]["last"])]
    ent["last"] = ts
    ent["count"] += 1


def _build_dash_snapshot(
    sig: dict[str, object], sim_state: dict | None, faults: dict[str, bool] | None = None
) -> dict:
    """DU state from the decoded bus (sig) + commanded state from vehicle_sim (sim_state)."""

    def g(name: str) -> object:
        return sig.get(name)

    def as_int(name: str) -> int | None:
        v = sig.get(name)
        return int(v) if v is not None else None

    gear_raw = as_int("DI_gear")
    immo_raw = as_int("DI_immobilizerState")
    sys_raw = as_int("DI_systemState")
    limp = as_int("DIR_sysLimpRequest")
    drive_blocked = as_int("DI_driveBlocked")
    brake = as_int("DI_brakePedalState")
    epb = as_int("DI_epbRequest")
    regen = as_int("DI_regenLight")
    # DI_chassisControlStatus 0x2B6 — the actual telltale lamps.
    vdc_on = as_int("DI_vdcTelltaleOn")
    vdc_flash = as_int("DI_vdcTelltaleFlash")
    tc_on = as_int("DI_tcTelltaleOn")
    tc_flash = as_int("DI_tcTelltaleFlash")
    hold_on = as_int("DI_vehicleHoldTelltaleOn")
    ptc = as_int("DI_ptcStateUI")  # 0=FAULTED 1=BACKUP 2=ON 3=SNA

    telltales = [
        {"id": "vdc", "label": "STABILITY", "level": "warn", "lit": vdc_on == 1 or vdc_flash == 1},
        {"id": "tc", "label": "TRACTION", "level": "warn", "lit": tc_on == 1 or tc_flash == 1},
        {"id": "hold", "label": "VEHICLE HOLD", "level": "info", "lit": hold_on == 1},
        {"id": "ptc", "label": "PTC HEATER", "level": "danger", "lit": ptc == 0},
        {
            "id": "driveBlocked",
            "label": "DRIVE BLOCKED",
            "level": "danger",
            "lit": bool(drive_blocked),
        },
        {"id": "limp", "label": "LIMP MODE", "level": "danger", "lit": limp == 1},
        {"id": "brake", "label": "BRAKE", "level": "warn", "lit": brake in (2, 3)},
        {"id": "parkBrake", "label": "PARK BRAKE", "level": "warn", "lit": bool(epb)},
        {"id": "regen", "label": "REGEN", "level": "info", "lit": regen == 1},
    ]

    st = sim_state or {}
    return {
        "connected": any(v is not None for v in (gear_raw, immo_raw, sys_raw)),
        "control": sim_state is not None,
        "commanded_gear": st.get("commanded_gear"),
        "speed_kph": _num(g("DI_vehicleSpeed")),
        "ui_speed": as_int("DI_uiSpeed"),
        "accel_pct": _num(g("DI_accelPedalPos")),
        "gear": DI_GEAR_LABELS.get(gear_raw) if gear_raw is not None else None,
        "gear_raw": gear_raw,
        "immobilizer": DI_IMMO_LABELS.get(immo_raw) if immo_raw is not None else None,
        "immobilizer_ok": immo_raw == 3,
        "system_state": DI_SYS_LABELS.get(sys_raw) if sys_raw is not None else None,
        "telltales": telltales,
        "faults": _faults_list(faults),
        # Commanded state comes from vehicle_sim's VehicleController (control server).
        "lv_state": st.get("lv_state"),
        "lv_options": st.get("lv_options", []),
        "ui": st.get("ui"),
        "ui_schema": st.get("ui_schema", {}),
    }


# ---------------------------------------------------------------------------
# Coalescing buffer: reader thread writes, flusher drains
# ---------------------------------------------------------------------------


class _BusHub:
    """Latest-frame-per-ID buffer for one bus.

    Memory is bounded by the number of distinct IDs on the bus, not by the
    frame rate, so a burst can never grow this without limit.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._lock = threading.Lock()
        self._latest: dict[int, tuple[bytes, float]] = {}
        self._counts: dict[int, int] = {}
        self.rx_total = 0
        self.coalesced = 0
        self.errors = 0
        # Last payload actually decoded/sent, so an unchanged frame costs a dict
        # lookup instead of a full signal decode. Flusher-thread only.
        self.last_sent: dict[int, bytes] = {}

    def push(self, msg: can.Message) -> None:
        aid = msg.arbitration_id
        with self._lock:
            if aid in self._latest:
                self.coalesced += 1
            self._latest[aid] = (bytes(msg.data), msg.timestamp)
            self._counts[aid] = self._counts.get(aid, 0) + 1
            self.rx_total += 1

    def drain(self) -> tuple[dict[int, tuple[bytes, float]], dict[int, int]]:
        with self._lock:
            latest, counts = self._latest, self._counts
            self._latest, self._counts = {}, {}
        return latest, counts


def _reader_thread(
    hub: _BusHub,
    bus: can.BusABC,
    filt: _TxFilter,
    stop: threading.Event,
) -> None:
    """Tight recv loop. No asyncio, no JSON, no I/O — just buffer and go back."""
    push = hub.push
    drop = filt.drop
    while not stop.is_set():
        try:
            msg = bus.recv(timeout=_RECV_TIMEOUT)
        except Exception:
            if stop.is_set():
                break
            hub.errors += 1
            log.debug("recv error on %s", hub.label, exc_info=True)
            time.sleep(0.05)
            continue
        if msg is None or drop(msg):
            continue
        push(msg)


# ---------------------------------------------------------------------------
# Client channel: latest-wins outbox, never blocks the flusher
# ---------------------------------------------------------------------------


class _Channel:
    """One WebSocket client.

    ``offer()`` is non-blocking: it parks the newest batch in a single slot and
    wakes the writer. If the browser can't keep up, the stale batch is simply
    replaced — dropping an already-superseded snapshot costs the user nothing,
    whereas awaiting a congested socket from the flusher would back the whole
    pipeline up to the CAN reader.
    """

    __slots__ = ("ws", "node", "mute", "paused", "dropped", "_pending", "_wake", "_task")

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.node = ""  # decoded subscription; "" = every node
        self.mute: frozenset[int] = frozenset()
        self.paused = False
        self.dropped = 0
        self._pending: str | None = None
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._pump())

    def offer(self, payload: str) -> None:
        if self._pending is not None:
            self.dropped += 1
        self._pending = payload
        self._wake.set()

    async def _pump(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                payload, self._pending = self._pending, None
                if payload is None:
                    continue
                await self.ws.send_str(payload)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            log.debug("client writer stopped", exc_info=True)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


def _reset_dedupe(app: web.Application) -> None:
    """Force a full resend on the next tick.

    A client that just connected, unmuted an ID, or resumed has no prior state,
    so the unchanged-payload shortcut would otherwise starve it of any message
    whose bytes happen to be static.
    """
    for hub in app["hubs"]:
        hub.last_sent.clear()


def _hello(app: web.Application) -> dict:
    filt: _TxFilter = app["filter"]
    return {
        "tx_ids": sorted(filt.tx_ids),
        "tx_hidden": filt.tx_hidden,
        "flush_hz": round(1.0 / app["flush_interval"], 2),
        "buses": [hub.label for hub in app["hubs"]],
    }


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    app = request.app
    chan = _Channel(ws)
    app["clients"].add(chan)
    _reset_dedupe(app)
    log.info("WebSocket client connected (%d total)", len(app["clients"]))

    # Full DB metadata so the client can populate the node list
    await ws.send_json({"type": "db", "nodes": _DB.nodes(), **_hello(app)})

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
                continue
            try:
                cmd = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = cmd.get("type")

            if kind == "subscribe":
                node = cmd.get("node", "")
                chan.node = node
                _reset_dedupe(app)
                # Tell the client which message IDs belong to this node so it
                # can pre-populate the signal table with empty rows.
                await ws.send_json(
                    {
                        "type": "schema",
                        "node": node,
                        "messages": [
                            {
                                "id": m["message_id"],
                                "name": m["name"],
                                "cycle_time": m.get("cycle_time", 0),
                                "signals": [
                                    {"signal": sname, "units": sig.get("units", "")}
                                    for sname, sig in m["signals"].items()
                                    if not sig.get("is_muxer")
                                ],
                            }
                            for m in _DB.messages_for_node(node)
                        ],
                    }
                )
            elif kind == "tx_filter":
                _set_tx_filter(app, bool(cmd.get("on", True)))
            elif kind == "pause":
                chan.paused = bool(cmd.get("on", False))
                if not chan.paused:
                    _reset_dedupe(app)
    finally:
        app["clients"].discard(chan)
        await chan.close()
        log.info("WebSocket client disconnected (%d remaining)", len(app["clients"]))
    return ws


async def _ws_raw_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    app = request.app
    chan = _Channel(ws)
    app["raw_clients"].add(chan)
    _reset_dedupe(app)
    log.info("Raw WebSocket client connected (%d total)", len(app["raw_clients"]))

    await ws.send_json({"type": "hello", **_hello(app)})

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
                continue
            try:
                cmd = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = cmd.get("type")

            if kind == "mute":
                # Per-client: the IDs unchecked in the raw sidebar are filtered
                # server-side so they cost no bandwidth or rendering at all.
                chan.mute = frozenset(int(i) for i in cmd.get("ids", []))
                _reset_dedupe(app)
            elif kind == "pause":
                chan.paused = bool(cmd.get("on", False))
                if not chan.paused:
                    _reset_dedupe(app)
            elif kind == "tx_filter":
                _set_tx_filter(app, bool(cmd.get("on", True)))
    finally:
        app["raw_clients"].discard(chan)
        await chan.close()
        log.info("Raw WebSocket client disconnected (%d remaining)", len(app["raw_clients"]))
    return ws


def _set_tx_filter(app: web.Application, hidden: bool) -> None:
    """Global (both tabs, all clients) — it's a statement about what the traffic
    *is*, not a per-view preference."""
    filt: _TxFilter = app["filter"]
    if filt.tx_hidden == hidden:
        return
    filt.set_tx_hidden(hidden)
    _reset_dedupe(app)
    log.info("TX filter %s (%d ids)", "on" if hidden else "off", len(filt.tx_ids))
    payload = json.dumps({"type": "tx_filter", "on": hidden, "tx_ids": sorted(filt.tx_ids)})
    for chan in (*app["clients"], *app["raw_clients"]):
        with contextlib.suppress(Exception):
            asyncio.create_task(chan.ws.send_str(payload))


# ---------------------------------------------------------------------------
# Flusher: drain -> decode (only what's subscribed) -> batch -> fan out
# ---------------------------------------------------------------------------


def _fanout_raw(clients: list[_Channel], frames: list[dict], rx: int) -> None:
    shared: str | None = None
    for chan in clients:
        if chan.mute:
            sel = [f for f in frames if f["id"] not in chan.mute]
            if not sel:
                continue
            chan.offer(json.dumps({"type": "frames", "frames": sel, "rx": rx}))
        else:
            if shared is None:
                shared = json.dumps({"type": "frames", "frames": frames, "rx": rx})
            chan.offer(shared)


def _fanout_decoded(clients: list[_Channel], frames: list[dict], rx: int) -> None:
    shared: str | None = None
    by_node: dict[str, str] = {}
    for chan in clients:
        if chan.node:
            payload = by_node.get(chan.node)
            if payload is None:
                sel = [f for f in frames if f["node"] == chan.node]
                if not sel:
                    continue
                payload = json.dumps({"type": "frames", "frames": sel, "rx": rx})
                by_node[chan.node] = payload
            chan.offer(payload)
        else:
            if shared is None:
                shared = json.dumps({"type": "frames", "frames": frames, "rx": rx})
            chan.offer(shared)


async def _flusher(app: web.Application) -> None:
    hubs: list[_BusHub] = app["hubs"]
    interval: float = app["flush_interval"]
    messages = _DB.messages
    decode = _DB.decode_frame

    dash_sig: dict[str, object] = app["dash_sig"]
    faults: dict[str, bool] = app["faults"]
    alert_ids: dict[int, str] = app["alert_ids"]
    alertlog_store: dict = app["alert_log"]  # not `alert_log` — that's the module
    alert_dec = app["alert_decoder"]
    alertlog_ids = app["alertlog_ids"]
    seen_msg: dict[int, float] = app["seen_msg"]
    show_unknown: bool = app["show_unknown"]
    unknown_ids: set[int] = set()   # ids not in the DB, already announced
    unknown_capped = False

    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()

        raw_clients = [c for c in app["raw_clients"] if not c.paused]
        dec_clients = [c for c in app["clients"] if not c.paused]
        want_raw = bool(raw_clients)
        want_dec = bool(dec_clients)
        # "" subscription means every node; otherwise decode only what's wanted.
        wants_all = want_dec and any(not c.node for c in dec_clients)
        wanted = {c.node for c in dec_clients if c.node}

        raw_frames: list[dict] = []
        dec_frames: list[dict] = []
        rx = 0

        for hub in hubs:
            latest, counts = hub.drain()  # always drain, even with no clients
            # ODIN live cross-reference: stamp every arriving id, regardless of viewer
            # subscriptions, so the "requires on bus" panel can mark signals present.
            for aid in latest:
                seen_msg[aid] = now
            # Dashboard: decode the handful of DI status IDs every tick, regardless
            # of viewer subscriptions, so the HUD stays live even with no WS client.
            for aid in _DASH_IDS:
                cell = latest.get(aid)
                if cell is None:
                    continue
                dec = decode(aid, cell[0])
                if dec:
                    for s in dec:
                        dash_sig[s["signal"]] = s["value"]
                if aid == 0x118:
                    _overlay_0x118(dash_sig, cell[0])
            # Faults: accumulate active alert-matrix bits (muxed -> update per page
            # as it cycles). Runs every tick regardless of viewer subscriptions.
            for aid, cell in latest.items():
                if aid in alert_ids:
                    for s in decode(aid, cell[0]) or []:
                        if _ALERT_RE.search(s["signal"]):
                            faults[s["signal"]] = bool(s["value"])
                # <NODE>_alertLog: the ECU's own log of WHY an alert was raised
                # (for CAN-rationality alerts, the offending id + error type).
                # No sim node transmits these, so anything seen here is a real ECU.
                elif aid in alertlog_ids:
                    _record_alertlog(alertlog_store, alert_dec, aid, cell[0], cell[1])
            if not (want_raw or want_dec):
                continue
            label = hub.label
            last_sent = hub.last_sent
            for aid, (data, ts) in latest.items():
                n = counts[aid]
                rx += n
                # A byte-identical payload cannot change any displayed value, so
                # it's forwarded as a bare liveness tick: no decode, no signal
                # list, no byte array. Keeps age/rate honest for the many frames
                # that only repeat a static status.
                unchanged = last_sent.get(aid) == data
                if not unchanged:
                    last_sent[aid] = data

                if want_raw:
                    frame = {"id": aid, "ts": ts, "bus": label, "n": n}
                    if not unchanged:
                        frame["data"] = list(data)
                    raw_frames.append(frame)

                if not want_dec:
                    continue
                db_msg = messages.get(aid)
                if db_msg is None:
                    # Not in the DB. Forwarded undecoded rather than dropped: a
                    # silently hidden frame is exactly how a node that IS
                    # transmitting comes to look dead in the viewer (the sim
                    # gained ids the compact JSON doesn't carry, and its nodes
                    # went dark). No originNode to match a subscription against,
                    # so these only go to unfiltered clients.
                    if show_unknown and wants_all:
                        if aid not in unknown_ids:
                            if len(unknown_ids) >= _UNKNOWN_ID_CAP:
                                if not unknown_capped:
                                    log.warning(
                                        "unknown-id cap reached (%d): further ids absent from "
                                        "the DB are dropped from the decoded view. Raw Frames "
                                        "still shows everything.", _UNKNOWN_ID_CAP,
                                    )
                                    unknown_capped = True
                                continue
                            unknown_ids.add(aid)
                            log.info("id 0x%03X on %s is not in the signal DB -- "
                                     "showing it undecoded", aid, label)
                        frame = {
                            "node": _UNKNOWN_NODE, "msg_id": aid, "timestamp": ts,
                            "bus": label, "n": n, "unknown": 1,
                        }
                        if unchanged:
                            frame["same"] = 1
                        else:
                            frame["data"] = list(data)
                        dec_frames.append(frame)
                    continue
                node = db_msg.get("originNode", "")
                if not wants_all and node not in wanted:
                    continue  # skip the decode entirely — the expensive part
                if unchanged:
                    dec_frames.append(
                        {
                            "node": node,
                            "msg_id": aid,
                            "timestamp": ts,
                            "bus": label,
                            "n": n,
                            "same": 1,
                        }
                    )
                    continue
                decoded = decode(aid, data)
                if decoded is None:
                    continue
                dec_frames.append(
                    {
                        "node": node,
                        "msg_id": aid,
                        "msg_name": db_msg["name"],
                        "timestamp": ts,
                        "signals": decoded,
                        "bus": label,
                        "n": n,
                    }
                )

        if raw_frames:
            _fanout_raw(raw_clients, raw_frames, rx)
        if dec_frames:
            _fanout_decoded(dec_clients, dec_frames, rx)


# ---------------------------------------------------------------------------
# Startup / cleanup
# ---------------------------------------------------------------------------


async def _start_reader(app: web.Application) -> None:
    app["clients"] = set()
    app["raw_clients"] = set()
    app["http"] = aiohttp.ClientSession()  # for forwarding commands to vehicle_sim
    app["dash_sig"] = {}  # latest decoded DI signals for the driver HUD
    app["faults"] = {}  # alert-matrix bit name -> currently-set bool
    app["seen_msg"] = {}  # CAN id -> last-arrival monotonic (ODIN live cross-ref)
    # CAN id -> the signal names it carries; a signal is "seen" when its id arrives.
    app["msg_signals"] = {mid: tuple(m["signals"].keys()) for mid, m in _DB.messages.items()}
    app["alert_ids"] = {
        mid: m["name"]
        for mid, m in _DB.messages.items()
        if "alertmatrix" in m["name"].lower() and m["name"].split("_", 1)[0] in _FAULT_ECUS
    }
    # Alert log: distinct decoded payloads, keyed (can_id, bytes). The catalog is
    # loaded here (once, at startup) rather than on the first HTTP poll so the
    # ~0.7 s .so parse never lands inside a request.
    app["alert_log"] = {}
    app["alert_decoder"] = _alert_catalog()
    app["alertlog_ids"] = frozenset(
        app["alert_decoder"].alertlog_node) if app["alert_decoder"] else frozenset()
    app["stop"] = threading.Event()
    app["threads"] = [
        threading.Thread(
            target=_reader_thread,
            args=(hub, bus, app["filter"], app["stop"]),
            name=f"can-rx-{hub.label}",
            daemon=True,
        )
        for bus, hub in app["sources"]
    ]
    for t in app["threads"]:
        t.start()
    app["flusher"] = asyncio.create_task(_flusher(app))
    app["stats"] = asyncio.create_task(_stats_logger(app))


async def _stats_logger(app: web.Application) -> None:
    hubs: list[_BusHub] = app["hubs"]
    prev = {hub.label: (0, 0) for hub in hubs}
    while True:
        await asyncio.sleep(10.0)
        for hub in hubs:
            p_rx, p_co = prev[hub.label]
            rx, co = hub.rx_total, hub.coalesced
            prev[hub.label] = (rx, co)
            if rx != p_rx:
                log.info(
                    "%s: %.0f frame/s in, %.0f%% coalesced for display",
                    hub.label,
                    (rx - p_rx) / 10.0,
                    100.0 * (co - p_co) / max(1, rx - p_rx),
                )


async def _stop_reader(app: web.Application) -> None:
    app["stop"].set()
    for task_key in ("flusher", "stats"):
        task = app.get(task_key)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    if app.get("http") is not None:
        await app["http"].close()
    if app.get("odin_bench") is not None:
        with contextlib.suppress(Exception):
            app["odin_bench"].close()  # stop ODIN's UDS TesterPresent + shut its sockets
    for chan in (*app["clients"], *app["raw_clients"]):
        await chan.close()
    for t in app["threads"]:
        t.join(timeout=_RECV_TIMEOUT * 5)
    for bus, _hub in app["sources"]:
        with contextlib.suppress(Exception):
            bus.shutdown()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC_DIR / "index.html")


async def _alerts_js(request: web.Request) -> web.FileResponse:
    """The alert renderer shared by the viewer and the HUD (tm3web_ui/alerts.js)."""
    return web.FileResponse(_STATIC_DIR / "alerts.js")


# ---------------------------------------------------------------------------
# Dashboard (driver HUD) — separate page, same backend
# ---------------------------------------------------------------------------


async def _dash_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC_DIR / "dash.html")


# ---------------------------------------------------------------------------
# ODIN (procedure runner + DID read/write) — endpoints only; the ODIN and DID
# panes are tabs on the main viewer page (tm3web_ui/index.html).
# ---------------------------------------------------------------------------


def _odin_backend(app: web.Application):
    """Lazily build the shared ODIN bench backend on the configured VEHICLE bus
    (config.VEHICLE_CHANNEL / TM3_VEHICLE_CHANNEL), falling back to tm3web's own
    --channel. ODIN transmits UDS, so it needs a CONCRETE interface: tm3web can
    read on the socketcan 'any' interface (an empty channel), but UDS TX cannot.
    Shared by procedure runs (backend_factory) + DID ops (node_provider); closed in
    _stop_reader. Lazy import so a view-only tm3web pays nothing."""
    be = app.get("odin_bench")
    if be is None:
        import odin_runner

        channel = _cfg.VEHICLE_CHANNEL or app.get("can_channel")
        if not channel:
            raise RuntimeError(
                "ODIN needs a concrete vehicle CAN channel to transmit UDS. Set the "
                "config bus TM3_VEHICLE_CHANNEL, or start tm3web with --channel "
                "<iface> (tm3web reads on the 'any' interface, but UDS TX cannot)."
            )
        be = odin_runner.BenchBackend(channel, app.get("can_interface") or "socketcan")
        app["odin_bench"] = be
    return be


async def _api_dash(request: web.Request) -> web.Response:
    """DI HUD snapshot: DU state from the bus + commanded state from vehicle_sim."""
    app = request.app
    sim_state = await _sim_get(app, "/state")
    return web.json_response(_build_dash_snapshot(app["dash_sig"], sim_state, app.get("faults")))


async def _api_alerts(request: web.Request) -> web.Response:
    """Human-readable alerts: active alert-matrix bits + the decoded alert log.

    ``faults`` are the alert bits currently SET on the bus; ``log`` is what the
    ECUs logged about them (newest first). Both carry the MCU catalog's
    description / cause / clear / effect when a firmware root is configured --
    ``catalog`` says whether that text is available at all.
    """
    app = request.app
    log_entries = sorted(app.get("alert_log", {}).values(),
                         key=lambda e: e["last"], reverse=True)
    return web.json_response({
        "catalog": app.get("alert_decoder") is not None,
        "catalog_error": _ALERT_CAT_ERR,
        "faults": _faults_list(app.get("faults")),
        "log": log_entries,
    })


async def _api_alerts_clear(request: web.Request) -> web.Response:
    """POST — drop the accumulated alert log (and the latched fault bits).

    Alert bits only clear when the ECU clears them, and a logged payload stays in
    the store until evicted, so a bench run that fixed the cause needs a way to
    start the picture over rather than reading stale history.
    """
    app = request.app
    app.get("alert_log", {}).clear()
    app.get("faults", {}).clear()
    return web.json_response({"ok": True})


async def _api_seen_signals(request: web.Request) -> web.Response:
    """Signal names whose carrying CAN frame arrived within the freshness window --
    the live cross-reference behind the ODIN 'requires on bus' green/red badges.
    Optional ?window=<seconds> overrides the default staleness cutoff."""
    app = request.app
    seen_msg: dict[int, float] = app.get("seen_msg", {})
    msg_signals: dict[int, tuple] = app.get("msg_signals", {})
    try:
        window = float(request.query.get("window", _SEEN_WINDOW_S))
    except (TypeError, ValueError):
        window = _SEEN_WINDOW_S
    now = time.monotonic()
    names: set[str] = set()
    for aid, ts in seen_msg.items():
        if now - ts <= window:
            names.update(msg_signals.get(aid, ()))
    return web.json_response({"signals": sorted(names), "window": window})


async def _forward_cmd(request: web.Request, cmd_type: str) -> web.Response:
    """Read the POST body, tag it with the command type, forward to vehicle_sim."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)
    status, resp = await _sim_post(request.app, {"type": cmd_type, **body})
    return web.json_response(resp, status=status)


async def _api_gear(request: web.Request) -> web.Response:
    """POST {"value":"P|R|N|D"} — actuate the SCCM stalk gesture (via vehicle_sim)."""
    return await _forward_cmd(request, "gear")


async def _api_ui(request: web.Request) -> web.Response:
    """POST {"field":"pedal_map","value":"sport"} — set a UiConfig setting (via sim)."""
    return await _forward_cmd(request, "ui")


async def _api_lv(request: web.Request) -> web.Response:
    """POST {"value":"off|conditioning|accessory|drive"} — set LV power (via sim)."""
    return await _forward_cmd(request, "lv")


async def _api_carconfig(request: web.Request) -> web.Response:
    """GET the GTW_carConfig schema (from vehicle_sim); POST {"signal","value"} to set."""
    if request.method == "GET":
        sch = await _sim_get(request.app, "/carconfig")
        if sch is None:
            return web.json_response({"error": "vehicle_sim unreachable"}, status=502)
        return web.json_response(sch)
    return await _forward_cmd(request, "carconfig")


async def _api_db(request: web.Request) -> web.Response:
    """Return the full DB schema: nodes → messages → signals."""
    payload: dict[str, list] = {}
    for node in _DB.nodes():
        msgs = []
        for m in _DB.messages_for_node(node):
            signals = [
                {
                    "name": sname,
                    "units": sig.get("units", ""),
                    "values": list(sig["value_description"].keys())
                    if sig.get("value_description")
                    else [],
                }
                for sname, sig in m["signals"].items()
                if not sig.get("is_muxer")
            ]
            msgs.append(
                {
                    "id": m["message_id"],
                    "name": m["name"],
                    "cycle_time": m.get("cycle_time", 0),
                    "signals": signals,
                }
            )
        payload[node] = msgs
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_app(
    buses: list[tuple[can.BusABC, str]],
    filt: _TxFilter,
    ui_rate: float,
    sim_url: str | None = None,
    channel: str | None = None,
    interface: str = "socketcan",
    show_unknown: bool = True,
) -> web.Application:
    app = web.Application()
    sources = [(bus, _BusHub(label)) for bus, label in buses]
    app["sources"] = sources
    app["hubs"] = [hub for _bus, hub in sources]
    app["filter"] = filt
    app["sim_url"] = sim_url  # vehicle_sim control server, or None (view-only)
    app["can_channel"] = channel  # for the ODIN's bench backend
    app["can_interface"] = interface
    app["flush_interval"] = 1.0 / max(1.0, ui_rate)
    app["show_unknown"] = show_unknown
    app.on_startup.append(_start_reader)
    app.on_cleanup.append(_stop_reader)
    app.router.add_get("/", _index)
    app.router.add_get("/dash", _dash_index)
    app.router.add_get("/alerts.js", _alerts_js)
    app.router.add_get("/api/db", _api_db)
    app.router.add_get("/api/dash", _api_dash)
    app.router.add_get("/api/alerts", _api_alerts)
    app.router.add_post("/api/alerts/clear", _api_alerts_clear)
    app.router.add_get("/api/seen-signals", _api_seen_signals)
    app.router.add_post("/api/gear", _api_gear)
    app.router.add_post("/api/ui", _api_ui)
    app.router.add_post("/api/lv", _api_lv)
    app.router.add_get("/api/carconfig", _api_carconfig)
    app.router.add_post("/api/carconfig", _api_carconfig)
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/ws-raw", _ws_raw_handler)
    # ODIN: procedure list/run (+ /ws/odin progress), DID read/write, and the
    # low-level UDS ops -- all driven from the ODIN/DID tabs on the viewer page.
    # Runs against a lazily-built bench backend (this app's CAN channel); see
    # scripts/odin_web.py. tm3web keeps only the viewer/HUD.
    import odin_web

    odin_web.setup_routes(
        app,
        bundle=_cfg.ODIN_BUNDLE,
        backend_factory=lambda: _odin_backend(app),
        node_provider=lambda node: _odin_backend(app).open_node(node),
    )
    return app


def _tune_rx_buffer(bus: can.BusABC) -> None:
    """Give SocketCAN burst headroom so a scheduling hiccup doesn't drop frames."""
    sock = getattr(bus, "socket", None)
    if sock is None:
        return
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF_BYTES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live CAN signal viewer")
    parser.add_argument("--channel", help="CAN channel")
    parser.add_argument("--interface", help="python-can interface")
    parser.add_argument("--bitrate", type=int, default=None, help="CAN bitrate (optional)")
    parser.add_argument(
        "--channel2",
        default=None,
        help="optional 2nd CAN channel to also read (e.g. party/private bus)",
    )
    parser.add_argument(
        "--interface2", default=None, help="interface for --channel2 (defaults to --interface)"
    )
    parser.add_argument(
        "--bitrate2", type=int, default=None, help="bitrate for --channel2 (defaults to --bitrate)"
    )
    _cfg.apply_defaults(parser)
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument(
        "--dbc", type=Path, default=None, help="Load a DBC file instead of the default compact JSON"
    )
    parser.add_argument(
        "--ui-rate",
        type=float,
        default=_DEFAULT_UI_RATE,
        help=f"UI update rate in Hz (default: {_DEFAULT_UI_RATE:.0f}); "
        "frames are coalesced per ID between ticks",
    )
    parser.add_argument(
        "--tx-ids",
        default=None,
        help="comma-separated IDs our own tools transmit (hex, e.g. "
        "132,212,25D,221,3A1) — hidden by default, toggleable in the UI",
    )
    parser.add_argument(
        "--ignore-ids",
        default=None,
        help="comma-separated IDs to drop permanently (hex; ranges like 700-7FF ok)",
    )
    parser.add_argument(
        "--show-tx",
        action="store_true",
        help="start with --tx-ids traffic visible instead of hidden",
    )
    parser.add_argument(
        "--hide-unknown",
        action="store_true",
        help="drop frames whose id is not in the signal DB instead of showing "
        "them undecoded under an 'unknown' node",
    )
    parser.add_argument(
        "--hide-host-tx",
        action="store_true",
        help="drop every frame originated on this host — including everything "
        "vehicle_sim broadcasts, not just this process's sends",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="enable the driver HUD's controls: gear/pedal/LV/car-config commands "
        "from /dash are FORWARDED to vehicle_sim's control server (--sim-url), "
        "which owns the bus and transmits. Run vehicle_sim.py alongside.",
    )
    parser.add_argument(
        "--sim-url",
        default="http://localhost:8770",
        help="vehicle_sim control server URL for --control (default localhost:8770)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    global _DB
    if args.dbc:
        _DB = CanDatabase.from_dbc(args.dbc)
        log.info("Loaded DBC: %s (%d messages)", args.dbc, len(_DB.messages))
    else:
        _DB = CanDatabase()

    def _open(channel: str, interface: str, bitrate: int | None) -> can.BusABC:
        kw: dict = {"channel": channel, "interface": interface}
        if bitrate:
            kw["bitrate"] = bitrate
        b = can.Bus(**kw)
        _tune_rx_buffer(b)
        log.info("Opened CAN bus: %s on %s", channel, interface)
        return b

    # Buses come from the config vehicle/party channels (config.CAN_CHANNELS) unless
    # overridden by --channel/--channel2, so the viewer + the ODIN agree on one
    # concrete bus (an empty channel binds socketcan to 'any' -- reads work, UDS TX
    # doesn't). label each bus by its channel name so the UI can tell them apart.
    channel = args.channel or _cfg.VEHICLE_CHANNEL
    interface = args.interface or "socketcan"
    if not channel:
        parser.error("no CAN channel: pass --channel or set TM3_VEHICLE_CHANNEL (config bus)")
    buses = [(_open(channel, interface, args.bitrate), channel)]
    channel2 = args.channel2 or _cfg.PARTY_CHANNEL
    if channel2:
        buses.append(
            (
                _open(channel2, args.interface2 or interface, args.bitrate2 or args.bitrate),
                channel2,
            )
        )

    tx_ids = parse_can_ids(args.tx_ids)
    sim_url = None
    if args.control:
        sim_url = args.sim_url.rstrip("/")
        log.info(
            "Control ENABLED: /dash gear/pedal/LV/car-config -> vehicle_sim at %s "
            "(it owns the bus + transmits). Drive from %s/dash.",
            sim_url,
            f"http://localhost:{args.port}",
        )
    filt = _TxFilter(
        tx_ids=tx_ids,
        ignore_ids=parse_can_ids(args.ignore_ids),
        hide_host_tx=args.hide_host_tx,
        tx_hidden=bool(tx_ids) and not args.show_tx,
    )
    if tx_ids:
        log.info(
            "TX filter: %d ids %s",
            len(tx_ids),
            "hidden" if filt.tx_hidden else "visible (--show-tx)",
        )

    app = _build_app(buses, filt, args.ui_rate, sim_url, channel=channel,
                     interface=interface, show_unknown=not args.hide_unknown)

    url = f"http://localhost:{args.port}"
    in_wsl = (
        Path("/proc/version").exists() and "microsoft" in Path("/proc/version").read_text().lower()
    )
    if not args.no_browser and not in_wsl:

        def _open_browser() -> None:
            if not webbrowser.open(url):
                log.info("Could not open browser automatically — visit %s", url)

        threading.Timer(0.5, _open_browser).start()

    log.info("Serving on %s (UI %.0f Hz)", url, args.ui_rate)
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
