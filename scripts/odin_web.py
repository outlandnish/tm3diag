#!/usr/bin/env python3
"""odin_web.py -- aiohttp route glue exposing the ODIN runner + DID read/write over
HTTP + WebSocket, for the tm3web.

All the logic lives here; tm3web wires it in with a single setup_routes(app, ...)
call, so the interface gets:
  * GET  /api/odin/vehicles            -> the cars this bundle covers + the default
  * GET  /api/odin/procedures[?all=1][&vehicle=Model3]
                                       -> the runnable (or full) procedure list for
                                          one car: its own tree plus Gen3/Common
  * GET  /api/odin/requirements?procedure=<basename>
                                       -> what the proc expects on the bus (CAN signals
                                          read, alert buses, UDS target nodes,
                                          preconditions); see
                                          odin_service.procedure_requirements.
  * POST /api/odin/run {procedure}     -> run one proc; returns the RunResult dict.
                                          Progress events broadcast to /ws/odin.
  * GET  /ws/odin                      -> server->client progress stream
  * GET  /api/did/{node}               -> the node's readable/writable DIDs
  * POST /api/did/read  {node, did}    -> read + decode a DID
  * POST /api/did/write {node, did, values} -> write a DID
  * GET  /api/uds/nodes                -> UDS-addressable nodes (name + tx/rx ids)
  * GET  /api/uds/ops                  -> the low-level UDS operation catalog
                                          (id, args, danger flag)
  * POST /api/uds/op {node, op, args}  -> run one low-level UDS operation

The Engine is synchronous, so a run goes through loop.run_in_executor; on_event events
reach /ws/odin via an asyncio.Queue. One run at a time (a lock; a second run gets 409).

Backends/sessions are injected so this is testable with no bus:
  * backend_factory() -> a odin_runner.Backend (or 'mock'/'bench'); default 'mock'.
  * node_provider(node) -> (NodeConfig, session) for DID ops; without it, the DID
    read/write endpoints report 503.
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


# Facts about the unit on the bench that no bus reading can establish, so the
# OPERATOR declares them and procedures gate on them. Most map to a CID data
# value the runner's cid.* nodes read (BENCH_STATE_CID); `allow_flash` is not a
# vehicle fact but an arming switch -- the 55 UPDATE_* procedures flash an ECU
# with no confirmation of their own, so it stays off until asked for.
# `include_bootloaders` defaults ON: a -WITH-BOOTLOADER procedure names its bu/bl
# by hand, so defaulting it off would silently downgrade the run the operator
# picked. The preflight dialog is where it is confirmed or turned off.
DEFAULT_BENCH_STATE: dict = {"is_fused": True, "allow_flash": False,
                             "include_bootloaders": True, "ramapps": "include"}
# Keys that take one of a fixed set of values rather than a flag. `ramapps` needs
# three states because "just the RAM app, without rewriting the app it rides on"
# is a real request a checkbox cannot express.
BENCH_STATE_ENUMS: dict = {"ramapps": ("include", "skip", "only")}
BENCH_STATE_CID: dict = {"is_fused": "GUI_isFused"}
# Car config the firmware rows are keyed on. Read off GTW_carConfig where the
# bus carries it; what the operator declares here wins, because on a drive-unit
# bench vehicle_sim is the thing transmitting 0x7FF in the first place.
BENCH_STATE_DICTS: tuple = ("conditions",)


def _bench_state_cid(state: dict) -> dict:
    """Bench state -> the CID data values it sets ('true'/'false' strings, the
    CID wire type). Keys with no CID mapping (allow_flash) are backend settings,
    applied in _run instead."""
    return {cid: ("true" if state.get(key) else "false")
            for key, cid in BENCH_STATE_CID.items() if key in state}


class OdinWeb:
    """Holds the run lock, the /ws/odin client set, and the injected backend/node
    sources; one instance per aiohttp app (stored at app['odin_web'])."""

    def __init__(self, *, bundle=None, backend_factory=None, node_provider=None):
        self.bundle = bundle
        self._backend_factory = backend_factory  # () -> Backend | 'mock'/'bench'
        self._node_provider = node_provider  # (node) -> (NodeConfig, session)
        self._proc_cache: dict[str, list] = {}  # vehicle -> procedures
        self._default_vehicle: str | None = None
        self._req_cache: dict[str, dict] = {}  # basename -> procedure_requirements (static)
        self._run_lock = asyncio.Lock()  # one ODIN run at a time
        self.clients: set[web.WebSocketResponse] = set()
        self.bench_state: dict = {**DEFAULT_BENCH_STATE,
                                  **{k: {} for k in BENCH_STATE_DICTS}}
        self._engine = None  # the live Engine while a run is in flight (cancel handle)

    # -- ODIN: discovery ---------------------------------------------------------
    async def list_procedures(self, *, runnable_only: bool = True,
                              vehicle: str | None = None) -> list:
        # Walks several trees (blocking) -> executor + per-vehicle cache. A
        # procedure's basename already includes its tree (Gen3/tasks/PROC_...),
        # so requirements and run need no vehicle of their own.
        if vehicle is None:
            vehicle = await self.default_vehicle()
        if vehicle not in self._proc_cache:
            loop = asyncio.get_running_loop()
            self._proc_cache[vehicle] = await loop.run_in_executor(
                None,
                functools.partial(
                    odin_service.list_procedures_for, vehicle, bundle=self.bundle,
                    runnable_only=False
                ),
            )
        procs = self._proc_cache[vehicle]
        if runnable_only:
            return [p for p in procs if p["runnable"]]
        return procs

    async def default_vehicle(self) -> str:
        if self._default_vehicle is None:
            loop = asyncio.get_running_loop()
            self._default_vehicle = await loop.run_in_executor(
                None, functools.partial(odin_service.default_vehicle, bundle=self.bundle))
        return self._default_vehicle

    async def _h_vehicles(self, request: web.Request) -> web.Response:
        """The cars this bundle covers, and which to show first."""
        try:
            loop = asyncio.get_running_loop()
            found = await loop.run_in_executor(
                None, functools.partial(odin_service.list_vehicles, bundle=self.bundle))
            return _json({"vehicles": found, "default": await self.default_vehicle()})
        except ValueError as e:  # no bundle configured
            return _json({"error": str(e)}, status=503)

    async def _h_procedures(self, request: web.Request) -> web.Response:
        runnable = request.query.get("all", "") not in ("1", "true", "yes")
        try:
            procs = await self.list_procedures(
                runnable_only=runnable, vehicle=request.query.get("vehicle") or None)
        except ValueError as e:  # no bundle configured
            return _json({"error": str(e)}, status=503)
        except (FileNotFoundError, NotADirectoryError):
            return _json({"error": "no such vehicle in this bundle"}, status=404)
        return _json(procs)

    async def requirements(self, basename: str) -> dict:
        # blocking graph walk -> executor + per-basename cache.
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

    # -- ODIN: operator-declared bench state -------------------------------------
    async def _h_bench_state(self, request: web.Request) -> web.Response:
        """GET the declared state; POST a partial update (only known keys)."""
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return _json({"error": "invalid JSON"}, status=400)
            if not isinstance(body, dict):
                return _json({"error": "expected an object"}, status=400)
            known = set(DEFAULT_BENCH_STATE) | set(BENCH_STATE_DICTS)
            unknown = sorted(set(body) - known)
            if unknown:
                return _json({"error": f"unknown bench state: {', '.join(unknown)}"},
                             status=400)
            for key, value in body.items():
                if key in BENCH_STATE_DICTS:
                    if not isinstance(value, dict):
                        return _json({"error": f"{key} must be an object"}, status=400)
                    # str-valued: a condition compares against the metadata's
                    # text, so 0 and "0" must not be different answers.
                    self.bench_state[key] = {str(k): str(v) for k, v in value.items()
                                             if v is not None and v != ""}
                elif key in BENCH_STATE_ENUMS:
                    allowed = BENCH_STATE_ENUMS[key]
                    if str(value) not in allowed:
                        return _json({"error": f"{key} must be one of "
                                               f"{', '.join(allowed)}"}, status=400)
                    self.bench_state[key] = str(value)
                else:
                    self.bench_state[key] = bool(value)
        return _json({"state": self.bench_state,
                      "cid_values": _bench_state_cid(self.bench_state)})

    def _apply_bench_state(self, backend):
        """Put the operator's declaration onto a freshly built backend.

        Arming is per-run and explicit: a backend built armed by its factory
        still honours an operator who has since turned flashing off. Preflight
        and the run MUST agree -- when only the run applied this, preflight
        reported an unarmed bench for an armed one and the confirm button was
        never enabled.
        """
        if hasattr(backend, "allow_flash"):
            backend.allow_flash = bool(self.bench_state.get("allow_flash"))
        if hasattr(backend, "conditions"):
            backend.conditions = dict(self.bench_state.get("conditions") or {})
        if hasattr(backend, "include_bootloaders"):
            backend.include_bootloaders = bool(
                self.bench_state.get("include_bootloaders", True))
        if hasattr(backend, "ramapps"):
            backend.ramapps = str(self.bench_state.get("ramapps", "include"))
        return backend

    async def _h_flash_preflight(self, request: web.Request) -> web.Response:
        """What a procedure would flash, resolved but not written.

        The UI calls this before a run so the operator confirms the actual
        images -- and can correct the car config that chose them -- at the point
        of decision, instead of watching the first one already go down the wire.
        `flashes: false` means nothing to confirm; run it straight.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        proc = body.get("procedure") or request.query.get("procedure")
        if not proc:
            return _json({"error": "missing 'procedure'"}, status=400)
        if self._run_lock.locked():
            return _json({"error": "a run is already in progress"}, status=409)
        backend = self._apply_bench_state(
            self._backend_factory() if self._backend_factory else "mock")
        loop = asyncio.get_running_loop()
        try:
            return _json(await loop.run_in_executor(None, functools.partial(
                odin_service.flash_preflight, proc, backend=backend,
                bundle=self.bundle,
                allow_flash=bool(self.bench_state.get("allow_flash")),
                include_bootloaders=bool(
                    self.bench_state.get("include_bootloaders", True)),
                ramapps=str(self.bench_state.get("ramapps", "include")),
                conditions=self.bench_state.get("conditions") or {})))
        except FileNotFoundError as e:
            return _json({"error": str(e)}, status=404)
        except ValueError as e:
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
        routine_params = body.get("routine_params") or {}
        if not isinstance(routine_params, dict):
            return _json({"error": "'routine_params' must be {routine: {param: value}}"},
                         status=400)
        async with self._run_lock:
            try:
                result = await self._run(proc, routine_params)
            except Exception as e:  # noqa: BLE001  (surface a run failure as 500)
                return _json({"error": str(e), "procedure": proc}, status=500)
            finally:
                self._engine = None
        return _json(result)

    def _track_engine(self, engine) -> None:
        """Hold the running Engine so /api/odin/cancel can reach it."""
        self._engine = engine

    async def _h_cancel(self, request: web.Request) -> web.Response:
        """Ask the in-flight run to stop at its next check.

        This ends a WAIT -- a CID poll, a routine's result poll, a timeout loop --
        which is what a procedure spends its time in. It does NOT interrupt an
        operation already in flight, and a flash deliberately does not check it:
        stopping a transfer mid-write is how an ECU gets bricked. The response
        says which of those the caller is getting.
        """
        engine = self._engine
        if engine is None or not self._run_lock.locked():
            return _json({"error": "no run in progress"}, status=409)
        engine.request_cancel()
        return _json({"cancelling": True,
                      "note": "stops at the next wait; a flash in progress "
                              "finishes its current image"})

    async def _run(self, proc: str, routine_params: dict | None = None) -> dict:
        loop = asyncio.get_running_loop()
        # Build the backend FIRST: if backend_factory raises (e.g. no CAN channel),
        # fail before the pump task starts so nothing leaks.
        backend = self._apply_bench_state(
            self._backend_factory() if self._backend_factory else "mock")
        # START-param overrides for this run only (the bench backend is shared across runs).
        if not isinstance(backend, str):
            backend.routine_param_overrides = dict(routine_params or {})
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
                    # What the operator declared about the unit (cid.IsFused, …),
                    # seeded into the CID store before the procedure reads it.
                    cid_values=_bench_state_cid(self.bench_state),
                    on_engine=self._track_engine,
                ),
            )
        finally:
            if not isinstance(backend, str):
                backend.routine_param_overrides = {}
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
        """Every node with a UDS request/response pair in nodes.json (static config, no backend)."""
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


# Low-level UDS operations: primitives the ODIN procedures are built from, exposed on
# their own. Each op maps to a UdsSession method (or, for the bootloader pair, the
# backend's ensure_application_state). The catalog is data: the UI renders a form per op
# from `fields` and refuses a `danger` op without a confirm.

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
    """(name, tx, rx) for every UDS-addressable node, sorted and cached."""
    global _uds_node_cache
    if _uds_node_cache is None:
        import config as _cfg
        from uds_local.node_config import load_all_nodes

        _uds_node_cache = [
            {"node": name, "tx": f"0x{tx:03X}", "rx": f"0x{rx:03X}"}
            for name, tx, rx in sorted(load_all_nodes(_cfg.NODES_JSON, _cfg.ETH_DBC or _cfg.ETH_COMPACT))
        ]
    return _uds_node_cache


def _as_hex_int(val, default: int = 0) -> int:
    """Parse a hex-typed field: 0xF180, F180, "f180", or an already-int value (always hex)."""
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
    """fw_type from 0xF180 byte 8, cross-checked against whether 0xF181 (app-only) answers."""
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

# Typed app key; tm3web reads the instance back via app[odin_web.ODIN_WEB].
ODIN_WEB = web.AppKey("odin_web", OdinWeb)


def setup_routes(
    app: web.Application, *, bundle=None, backend_factory=None, node_provider=None, prefix: str = ""
) -> OdinWeb:
    """Register the ODIN + DID routes on `app` and return the OdinWeb instance."""
    svc = OdinWeb(bundle=bundle, backend_factory=backend_factory, node_provider=node_provider)
    app[ODIN_WEB] = svc
    app.router.add_get(prefix + "/api/odin/vehicles", svc._h_vehicles)
    app.router.add_get(prefix + "/api/odin/procedures", svc._h_procedures)
    app.router.add_get(prefix + "/api/odin/requirements", svc._h_requirements)
    app.router.add_get(prefix + "/api/odin/bench-state", svc._h_bench_state)
    app.router.add_post(prefix + "/api/odin/bench-state", svc._h_bench_state)
    app.router.add_post(prefix + "/api/odin/run", svc._h_run)
    app.router.add_post(prefix + "/api/odin/cancel", svc._h_cancel)
    app.router.add_post(prefix + "/api/odin/flash-preflight", svc._h_flash_preflight)
    app.router.add_get(prefix + "/ws/odin", svc._h_ws)
    app.router.add_get(prefix + "/api/did/{node}", svc._h_did_list)
    app.router.add_post(prefix + "/api/did/read", svc._h_did_read)
    app.router.add_post(prefix + "/api/did/write", svc._h_did_write)
    app.router.add_get(prefix + "/api/uds/nodes", svc._h_uds_nodes)
    app.router.add_get(prefix + "/api/uds/ops", svc._h_uds_ops)
    app.router.add_post(prefix + "/api/uds/op", svc._h_uds_run)
    return svc
