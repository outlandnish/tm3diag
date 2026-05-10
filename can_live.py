#!/usr/bin/env python3
"""Live CAN viewer — aiohttp backend with WebSocket streaming.

Usage:
  python can_live.py --channel vcan0
  python can_live.py --channel vcan0 --interface socketcan --port 8765
  python can_live.py --channel vcan0 --replay  # replay last 100 frames for demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import config as _cfg
import threading
import webbrowser
from pathlib import Path

import can
from aiohttp import web

from can_decoder import CanDatabase

_STATIC_DIR = Path(__file__).parent / "can_live_ui"
_DB: CanDatabase | None = None

log = logging.getLogger("can_live")


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    app = request.app
    app["clients"].add(ws)
    log.info("WebSocket client connected (%d total)", len(app["clients"]))

    # Send the full DB metadata so the client can populate the node list
    await ws.send_json({"type": "db", "nodes": _DB.nodes()})

    subscribed: set[str] = set()

    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                cmd = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if cmd.get("type") == "subscribe":
                node = cmd.get("node", "")
                subscribed = {node} if node else set()
                # Tell the client which message IDs belong to this node so it
                # can pre-populate the signal table with empty rows.
                msgs = _DB.messages_for_node(node)
                await ws.send_json({
                    "type": "schema",
                    "node": node,
                    "messages": [
                        {
                            "id": m["message_id"],
                            "name": m["name"],
                            "cycle_time": m.get("cycle_time", 0),
                            "signals": [
                                {
                                    "signal": sname,
                                    "units": sig.get("units", ""),
                                }
                                for sname, sig in m["signals"].items()
                                if not sig.get("is_muxer")
                            ],
                        }
                        for m in msgs
                    ],
                })
                # Attach subscription info to the ws object for the reader task
                ws["subscribed"] = subscribed
        elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
            break

    app["clients"].discard(ws)
    log.info("WebSocket client disconnected (%d remaining)", len(app["clients"]))
    return ws


# ---------------------------------------------------------------------------
# CAN reader task
# ---------------------------------------------------------------------------

async def _can_reader(app: web.Application, bus: can.BusABC) -> None:
    loop = asyncio.get_running_loop()

    def _read_one() -> can.Message | None:
        return bus.recv(timeout=0.1)

    while True:
        msg = await loop.run_in_executor(None, _read_one)
        if msg is None:
            continue

        decoded = _DB.decode_frame(msg.arbitration_id, bytes(msg.data))
        if decoded is None:
            continue

        db_msg = _DB.messages.get(msg.arbitration_id)
        if db_msg is None:
            continue

        node = db_msg.get("originNode", "")
        payload = json.dumps({
            "type": "frame",
            "node": node,
            "msg_id": msg.arbitration_id,
            "msg_name": db_msg["name"],
            "timestamp": msg.timestamp,
            "signals": decoded,
        })

        dead: set[web.WebSocketResponse] = set()
        for ws in list(app["clients"]):
            sub = getattr(ws, "__dict__", {}).get("subscribed") or ws.get("subscribed", set())
            if not sub or node in sub:
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.add(ws)

        app["clients"].difference_update(dead)


# ---------------------------------------------------------------------------
# Startup / cleanup
# ---------------------------------------------------------------------------

async def _start_reader(app: web.Application) -> None:
    app["clients"] = set()
    bus: can.BusABC = app["bus"]
    app["reader_task"] = asyncio.create_task(_can_reader(app, bus))


async def _stop_reader(app: web.Application) -> None:
    task: asyncio.Task = app["reader_task"]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app["bus"].shutdown()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC_DIR / "index.html")


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
                    "values": list(sig["value_description"].keys()) if sig.get("value_description") else [],
                }
                for sname, sig in m["signals"].items()
                if not sig.get("is_muxer")
            ]
            msgs.append({
                "id": m["message_id"],
                "name": m["name"],
                "cycle_time": m.get("cycle_time", 0),
                "signals": signals,
            })
        payload[node] = msgs
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_app(bus: can.BusABC) -> web.Application:
    app = web.Application()
    app["bus"] = bus
    app.on_startup.append(_start_reader)
    app.on_cleanup.append(_stop_reader)
    app.router.add_get("/", _index)
    app.router.add_get("/api/db", _api_db)
    app.router.add_get("/ws", _ws_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Live CAN signal viewer")
    parser.add_argument("--channel", help="CAN channel")
    parser.add_argument("--interface", help="python-can interface")
    parser.add_argument("--bitrate", type=int, default=None, help="CAN bitrate (optional)")
    _cfg.apply_defaults(parser)
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--dbc", type=Path, default=None, help="Load a DBC file instead of the default compact JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    global _DB
    if args.dbc:
        _DB = CanDatabase.from_dbc(args.dbc)
        log.info("Loaded DBC: %s (%d messages)", args.dbc, len(_DB.messages))
    else:
        _DB = CanDatabase()

    kwargs: dict = {"channel": args.channel, "interface": args.interface}
    if args.bitrate:
        kwargs["bitrate"] = args.bitrate

    bus = can.Bus(**kwargs)
    log.info("Opened CAN bus: %s on %s", args.channel, args.interface)

    app = _build_app(bus)

    url = f"http://localhost:{args.port}"
    in_wsl = "microsoft" in open("/proc/version").read().lower() if Path("/proc/version").exists() else False
    if not args.no_browser and not in_wsl:
        def _open_browser() -> None:
            if not webbrowser.open(url):
                log.info("Could not open browser automatically — visit %s", url)
        threading.Timer(0.5, _open_browser).start()

    log.info("Serving on %s", url)
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
