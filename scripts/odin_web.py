#!/usr/bin/env python3
"""odin_web.py -- aiohttp route glue exposing the ODIN runner + DID read/write over
HTTP + WebSocket, for the tm3web.

All the logic lives here; tm3web wires it in with a single setup_routes(app, ...)
call, so the interface gets:
  * GET  /api/odin/procedures[?all=1]  -> the runnable (or full) procedure list
  * GET  /api/odin/requirements?procedure=<basename>
                                       -> what the proc expects on the bus (the CAN
                                          signals it reads, alert buses, UDS target
                                          nodes, preconditions) -- see
                                          odin_service.procedure_requirements.
  * POST /api/odin/run {procedure}     -> run one proc; returns the RunResult dict.
                                          Progress (trace/metric/done/error events)
                                          is broadcast to /ws/odin while it runs.
  * GET  /ws/odin                      -> server->client progress stream
  * GET  /api/did/{node}               -> the node's readable/writable DIDs
  * POST /api/did/read  {node, did}    -> read + decode a DID
  * POST /api/did/write {node, did, values} -> write a DID
  * GET  /api/uds/nodes                -> UDS-addressable nodes (name + tx/rx ids),
                                          so the UI can offer a picker instead of a
                                          free-text box
  * GET  /api/uds/ops                  -> the low-level UDS operation catalog
                                          (id, args, danger flag) -- the UI builds
                                          its form from this.
  * POST /api/uds/op {node, op, args}  -> run one low-level UDS operation

The Engine is SYNCHRONOUS (real sleeps on the bench), so a run goes through
loop.run_in_executor; the runner's on_event callback (called from the executor
thread) hands events back to the loop via call_soon_threadsafe -> an asyncio.Queue
-> the /ws/odin broadcast. One run at a time (a lock; a second run gets 409).

Backends/sessions are injected so this is testable with no bus:
  * backend_factory() -> a odin_runner.Backend (or 'mock'/'bench'); default 'mock'.
  * node_provider(node) -> (NodeConfig, session) for DID ops; without it, the DID
    read/write endpoints report 503 (no bench).
"""

from __future__ import annotations

import asyncio
import functools
import json

import odin_service
from aiohttp import web


def _json(obj, status: int = 200) -> web.Response:
    """JSON response tolerant of non-serializable values (bytes, etc. -> str)."""
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(o, default=str))


class OdinWeb:
    """Holds the run lock, the /ws/odin client set, and the injected backend/node
    sources; one instance per aiohttp app (stored at app['odin_web'])."""

    def __init__(self, *, bundle=None, backend_factory=None, node_provider=None):
        self.bundle = bundle
        self._backend_factory = backend_factory  # () -> Backend | 'mock'/'bench'
        self._node_provider = node_provider  # (node) -> (NodeConfig, session)
        self._proc_cache: list | None = None
        self._req_cache: dict[str, dict] = {}  # basename -> procedure_requirements (static)
        self._run_lock = asyncio.Lock()  # one ODIN run at a time
        self.clients: set[web.WebSocketResponse] = set()

    # -- ODIN: discovery ---------------------------------------------------------
    async def list_procedures(self, *, runnable_only: bool = True) -> list:
        # list_procedures walks the whole bundle (blocking) -> executor + cache.
        if self._proc_cache is None:
            loop = asyncio.get_running_loop()
            self._proc_cache = await loop.run_in_executor(
                None,
                functools.partial(
                    odin_service.list_procedures, bundle=self.bundle, runnable_only=False
                ),
            )
        if runnable_only:
            return [p for p in self._proc_cache if p["runnable"]]
        return self._proc_cache

    async def _h_procedures(self, request: web.Request) -> web.Response:
        runnable = request.query.get("all", "") not in ("1", "true", "yes")
        try:
            procs = await self.list_procedures(runnable_only=runnable)
        except ValueError as e:  # no bundle configured
            return _json({"error": str(e)}, status=503)
        return _json(procs)

    async def requirements(self, basename: str) -> dict:
        # procedure_requirements walks the proc's whole graph (blocking file I/O) ->
        # executor + per-basename cache (the bundle is static at runtime).
        if basename not in self._req_cache:
            loop = asyncio.get_running_loop()
            self._req_cache[basename] = await loop.run_in_executor(
                None,
                functools.partial(
                    odin_service.procedure_requirements, basename, bundle=self.bundle
                ),
            )
        return self._req_cache[basename]

    async def _h_requirements(self, request: web.Request) -> web.Response:
        proc = request.query.get("procedure")
        if not proc:
            return _json({"error": "missing 'procedure'"}, status=400)
        try:
            return _json(await self.requirements(proc))
        except FileNotFoundError as e:  # unknown procedure basename
            return _json({"error": str(e)}, status=404)
        except ValueError as e:  # no bundle configured
            return _json({"error": str(e)}, status=503)

    # -- ODIN: run + progress ----------------------------------------------------
    async def _h_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json({"error": "invalid JSON"}, status=400)
        proc = body.get("procedure")
        if not proc:
            return _json({"error": "missing 'procedure'"}, status=400)
        if self._run_lock.locked():
            return _json({"error": "a run is already in progress"}, status=409)
        async with self._run_lock:
            try:
                result = await self._run(proc)
            except Exception as e:  # noqa: BLE001  (surface a run failure as 500)
                return _json({"error": str(e), "procedure": proc}, status=500)
        return _json(result)

    async def _run(self, proc: str) -> dict:
        loop = asyncio.get_running_loop()
        # Build the backend FIRST: if backend_factory raises (e.g. no CAN channel),
        # fail before the pump task starts so nothing leaks.
        backend = self._backend_factory() if self._backend_factory else "mock"
        q: asyncio.Queue = asyncio.Queue()

        def emit(kind, payload):  # called from the executor thread
            loop.call_soon_threadsafe(q.put_nowait, (kind, payload))

        pump = asyncio.create_task(self._pump(q))
        try:
            return await loop.run_in_executor(
                None,
                functools.partial(
                    odin_service.run_procedure,
                    proc,
                    backend=backend,
                    bundle=self.bundle,
                    on_event=emit,
                ),
            )
        finally:
            await q.put(None)  # sentinel: all events already queued (FIFO) -> stop pump
            await pump

    async def _pump(self, q: asyncio.Queue) -> None:
        while True:
            item = await q.get()
            if item is None:
                return
            kind, payload = item
            msg = (
                {"type": kind, **payload}
                if isinstance(payload, dict)
                else {"type": kind, "value": payload}
            )
            await self._broadcast(msg)

    async def _broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(msg, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except Exception:  # noqa: BLE001  (drop a dead client)
                self.clients.discard(ws)

    async def _h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            async for _msg in ws:  # progress is server->client only; ignore inbound
                pass
        finally:
            self.clients.discard(ws)
        return ws

    # -- DID read/write ----------------------------------------------------------
    async def _node(self, node: str):
        if self._node_provider is None:
            raise web.HTTPServiceUnavailable(
                text="DID access needs a bench backend (no node provider configured)"
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._node_provider, node)

    async def _h_did_list(self, request: web.Request) -> web.Response:
        try:
            cfg, _sess = await self._node(request.match_info["node"])
        except web.HTTPException as e:
            return _json({"error": e.text}, status=e.status)
        except Exception as e:  # noqa: BLE001  (unknown node, load failure)
            return _json({"error": str(e)}, status=400)
        return _json(odin_service.list_dids(cfg))

    async def _h_did_read(self, request: web.Request) -> web.Response:
        return await self._did_op(request, write=False)

    async def _h_did_write(self, request: web.Request) -> web.Response:
        return await self._did_op(request, write=True)

    async def _did_op(self, request: web.Request, *, write: bool) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json({"error": "invalid JSON"}, status=400)
        node, did = body.get("node"), body.get("did")
        if not node or did is None:
            return _json({"error": "missing 'node' or 'did'"}, status=400)
        try:
            cfg, sess = await self._node(node)
        except web.HTTPException as e:
            return _json({"error": e.text}, status=e.status)
        except Exception as e:  # noqa: BLE001
            return _json({"error": str(e)}, status=400)
        loop = asyncio.get_running_loop()
        try:
            if write:
                call = functools.partial(odin_service.write_did, sess, cfg, did, body.get("values"))
            else:
                call = functools.partial(
                    odin_service.read_did, sess, cfg, did, parsed=body.get("parsed", True)
                )
            res = await loop.run_in_executor(None, call)
        except Exception as e:  # noqa: BLE001  (UDS/decode/unknown-DID error)
            return _json({"error": str(e)}, status=400)
        return _json(res)

    # -- low-level UDS ops -------------------------------------------------------
    async def _h_uds_nodes(self, request: web.Request) -> web.Response:
        """Every node with a UDS request/response pair in nodes.json. Static config,
        not bus state, so it needs no backend -- the DID and low-level tabs both
        populate their node picker from it."""
        try:
            nodes = await asyncio.get_running_loop().run_in_executor(None, _list_uds_nodes)
        except Exception as e:  # noqa: BLE001  (missing/!readable config)
            return _json({"error": str(e)}, status=503)
        return _json(nodes)

    async def _h_uds_ops(self, request: web.Request) -> web.Response:
        return _json(UDS_OPS)

    async def _h_uds_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json({"error": "invalid JSON"}, status=400)
        node, op = body.get("node"), body.get("op")
        if not node or not op:
            return _json({"error": "missing 'node' or 'op'"}, status=400)
        if op not in _UDS_OPS_BY_ID:
            return _json({"error": f"unknown operation: {op}"}, status=400)
        try:
            _cfg, sess = await self._node(node)
        except web.HTTPException as e:
            return _json({"error": e.text}, status=e.status)
        except Exception as e:  # noqa: BLE001  (unknown node, load failure)
            return _json({"error": str(e)}, status=400)
        # A reset or bootloader handover must not land mid-procedure.
        if self._run_lock.locked():
            return _json({"error": "a procedure run is in progress"}, status=409)
        backend = self._backend_factory() if self._backend_factory else None
        loop = asyncio.get_running_loop()
        async with self._run_lock:
            try:
                res = await loop.run_in_executor(
                    None,
                    functools.partial(_run_uds_op, backend, sess, node, op, body.get("args") or {}),
                )
            except Exception as e:  # noqa: BLE001  (UDS/NRC/timeout/bad arg)
                return _json({"error": f"{type(e).__name__}: {e}", "op": op}, status=400)
        return _json({"op": op, "node": node, "result": res})


# ---------------------------------------------------------------------------
# Low-level UDS operations
# ---------------------------------------------------------------------------
# The primitives the ODIN procedures are built out of, exposed on their own: when
# you are bringing a bench ECU up, no packaged procedure covers "put this node in
# its bootloader and tell me what it thinks it is". Every op maps to a method that
# already exists on UdsSession (or, for the bootloader pair, to the backend's
# ensure_application_state -- the same reset + TesterPresent-flood handover the
# flasher uses, so the state tracking stays consistent with a subsequent run).
#
# The catalog is data: the UI renders a form per op from `fields` and refuses to
# fire a `danger` op without a confirm. Keep the two in sync by adding here only.

_SESSION_MODES = {"default": 0x01, "programming": 0x02, "extended": 0x03, "safety": 0x04}

_RESET_TYPES = {"hard": 0x01, "key-off-on": 0x02, "soft": 0x03}

UDS_OPS: list[dict] = [
    {
        "op": "probe_state",
        "title": "Probe state",
        "group": "state",
        "help": "Read 0xF180 (fw_type byte) and probe 0xF181 to tell bootloader from app.",
    },
    {
        "op": "enter_bootloader",
        "title": "Enter bootloader",
        "group": "state",
        "danger": True,
        "help": "ECUReset, then flood TesterPresent through the reboot so the "
                "bootloader holds instead of booting on into the app.",
    },
    {
        "op": "enter_application",
        "title": "Enter application",
        "group": "state",
        "danger": True,
        "help": "ECUReset with no keep-alive flood: the bootloader boots on into the app.",
    },
    {
        "op": "ecu_reset",
        "title": "ECU reset",
        "group": "state",
        "danger": True,
        "help": "Raw ECUReset (0x11). 'no wait' sends 11 81 fire-and-forget.",
        "fields": [
            {"name": "type", "label": "reset type", "type": "select",
             "options": list(_RESET_TYPES), "default": "hard"},
            {"name": "no_wait", "label": "fire and forget", "type": "bool", "default": False},
        ],
    },
    {
        "op": "session",
        "title": "Diagnostic session",
        "group": "session",
        "help": "DiagnosticSessionControl (0x10).",
        "fields": [
            {"name": "mode", "label": "mode", "type": "select",
             "options": list(_SESSION_MODES), "default": "extended"},
        ],
    },
    {
        "op": "security_access",
        "title": "Security access",
        "group": "session",
        "help": "Seed/key exchange (0x27) using the node's configured algorithm. "
                "Most nodes want a programming session first.",
        "fields": [
            {"name": "level_idx", "label": "level index", "type": "int", "default": 0},
        ],
    },
    {
        "op": "tester_present",
        "title": "TesterPresent keepalive",
        "group": "session",
        "help": "Start or stop the background 0x3E keep-alive on this node's session.",
        "fields": [
            {"name": "on", "label": "running", "type": "bool", "default": True},
        ],
    },
    {
        "op": "read_did_raw",
        "title": "Read DID (raw)",
        "group": "diagnostics",
        "help": "ReadDataByIdentifier (0x22) by number, returning raw bytes -- no ODJ "
                "decode, so it reaches DIDs the DID tab does not list.",
        "fields": [
            {"name": "did", "label": "DID (hex)", "type": "hex16", "default": "F180"},
        ],
    },
    {
        "op": "routine_control",
        "title": "Routine control",
        "group": "diagnostics",
        "danger": True,
        "help": "RoutineControl (0x31). Subtype 1=start, 2=stop, 3=request results.",
        "fields": [
            {"name": "routine_id", "label": "routine (hex)", "type": "hex16", "default": ""},
            {"name": "subtype", "label": "subtype", "type": "int", "default": 1},
            {"name": "arg", "label": "argument (hex bytes)", "type": "hexbytes", "default": ""},
        ],
    },
    {
        "op": "read_dtcs",
        "title": "Read DTCs",
        "group": "diagnostics",
        "help": "ReadDTCInformation (0x19) by status mask.",
        "fields": [
            {"name": "status_mask", "label": "status mask", "type": "int", "default": 255},
        ],
    },
    {
        "op": "clear_dtc",
        "title": "Clear DTCs",
        "group": "diagnostics",
        "danger": True,
        "help": "ClearDiagnosticInformation (0x14), group 0xFFFFFF by default.",
        "fields": [
            {"name": "group", "label": "group (hex)", "type": "hex24", "default": "FFFFFF"},
        ],
    },
]

_UDS_OPS_BY_ID = {o["op"]: o for o in UDS_OPS}

_uds_node_cache: list[dict] | None = None


def _list_uds_nodes() -> list[dict]:
    """(name, tx, rx) for every UDS-addressable node, sorted and cached -- the file
    is static for the life of the process."""
    global _uds_node_cache
    if _uds_node_cache is None:
        import config as _cfg
        from uds_local.node_config import load_all_nodes

        _uds_node_cache = [
            {"node": name, "tx": f"0x{tx:03X}", "rx": f"0x{rx:03X}"}
            for name, tx, rx in sorted(load_all_nodes(_cfg.NODES_JSON, _cfg.ETH_COMPACT))
        ]
    return _uds_node_cache


def _as_hex_int(val, default: int = 0) -> int:
    """Parse a hex-typed field: 0xF180, F180, "f180" or an already-int value.
    HEX, not decimal -- every caller is a DID / routine / DTC-group field, where
    the user types hex without thinking about it."""
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    text = str(val).strip().replace(" ", "")
    return int(text, 16) if text.lower().startswith("0x") else int(text, 16)


def _as_bytes(val) -> bytes:
    if not val:
        return b""
    return bytes.fromhex(str(val).replace(" ", "").removeprefix("0x"))


def _probe_state(sess) -> dict:
    """Mirror flash_scripts._steps.step_probe_bootloader_state: fw_type from 0xF180
    byte 8, cross-checked against whether 0xF181 (app-only) answers."""
    out: dict = {}
    try:
        f180 = sess.read_did(0xF180)
        out["f180"] = f180.hex()
        if len(f180) >= 9:
            fw_type = f180[8]
            out["fw_type"] = fw_type
            out["state"] = "BOOTLOADER" if fw_type == 0 else "APPLICATION"
    except Exception as e:  # noqa: BLE001
        out["f180_error"] = f"{type(e).__name__}: {e}"
    try:
        sess.read_did(0xF181)
        out["f181"] = "present (app)"
    except Exception:  # noqa: BLE001
        out["f181"] = "NRC (bootloader)"
    return out


def _run_uds_op(backend, sess, node: str, op: str, args: dict) -> dict:
    """Blocking; called in an executor. Returns a JSON-able result dict."""
    spec = _UDS_OPS_BY_ID.get(op)
    if spec is None:
        raise ValueError(f"unknown operation: {op!r}")

    if op == "probe_state":
        return _probe_state(sess)

    if op in ("enter_bootloader", "enter_application"):
        state = "BOOTLOADER" if op == "enter_bootloader" else "APPLICATION"
        if not hasattr(backend, "ensure_application_state"):
            raise RuntimeError(
                "bootloader handover needs the bench backend (none configured)"
            )
        backend.ensure_application_state(node, state)
        return {"state": state, "probe": _probe_state(sess)}

    if op == "ecu_reset":
        rtype = _RESET_TYPES.get(str(args.get("type", "hard")), 0x01)
        if args.get("no_wait"):
            sess.ecu_reset_no_wait(rtype)
            return {"sent": f"11 8{rtype:X}", "waited": False}
        sess.ecu_reset(rtype)
        return {"sent": f"11 0{rtype:X}", "waited": True}

    if op == "session":
        mode = _SESSION_MODES.get(str(args.get("mode", "extended")))
        if mode is None:
            mode = _as_hex_int(args.get("mode"), 0x03)
        sess.diagnostic_session(mode)
        return {"session": f"0x{mode:02X}"}

    if op == "security_access":
        sess.security_access(int(args.get("level_idx") or 0))
        return {"granted": True, "level_idx": int(args.get("level_idx") or 0)}

    if op == "tester_present":
        if args.get("on", True):
            sess.start_tester_present()
            return {"tester_present": "started"}
        sess.stop_tester_present()
        return {"tester_present": "stopped"}

    if op == "read_did_raw":
        did = _as_hex_int(args.get("did"))
        data = sess.read_did(did)
        return {"did": f"0x{did:04X}", "raw": data.hex(), "length": len(data)}

    if op == "routine_control":
        rid = _as_hex_int(args.get("routine_id"))
        result = sess.routine_control(rid, _as_bytes(args.get("arg")),
                                      int(args.get("subtype") or 1))
        return {"routine": f"0x{rid:04X}", "result": result.hex() if result else ""}

    if op == "read_dtcs":
        mask = int(args.get("status_mask") or 0xFF)
        dtcs = sess.read_dtcs(mask)
        return {"count": len(dtcs),
                "dtcs": [{"dtc": f"0x{k:06X}", "status": f"0x{v:02X}"} for k, v in dtcs.items()]}

    if op == "clear_dtc":
        group = _as_hex_int(args.get("group"), 0xFFFFFF)
        sess.clear_dtc(group)
        return {"cleared": f"0x{group:06X}"}

    raise ValueError(f"operation not implemented: {op!r}")  # pragma: no cover

# Typed app key (aiohttp warns on bare-string keys); tm3web can read the instance
# back via app[odin_web.ODIN_WEB], though setup_routes also returns it.
ODIN_WEB = web.AppKey("odin_web", OdinWeb)


def setup_routes(
    app: web.Application, *, bundle=None, backend_factory=None, node_provider=None, prefix: str = ""
) -> OdinWeb:
    """Register the ODIN + DID routes on `app` and return the OdinWeb instance.
    tm3web calls this once from _build_app. See the module docstring for the
    backend_factory / node_provider seams."""
    svc = OdinWeb(bundle=bundle, backend_factory=backend_factory, node_provider=node_provider)
    app[ODIN_WEB] = svc
    app.router.add_get(prefix + "/api/odin/procedures", svc._h_procedures)
    app.router.add_get(prefix + "/api/odin/requirements", svc._h_requirements)
    app.router.add_post(prefix + "/api/odin/run", svc._h_run)
    app.router.add_get(prefix + "/ws/odin", svc._h_ws)
    app.router.add_get(prefix + "/api/did/{node}", svc._h_did_list)
    app.router.add_post(prefix + "/api/did/read", svc._h_did_read)
    app.router.add_post(prefix + "/api/did/write", svc._h_did_write)
    app.router.add_get(prefix + "/api/uds/nodes", svc._h_uds_nodes)
    app.router.add_get(prefix + "/api/uds/ops", svc._h_uds_ops)
    app.router.add_post(prefix + "/api/uds/op", svc._h_uds_run)
    return svc
