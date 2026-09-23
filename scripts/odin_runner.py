#!/usr/bin/env python3
"""odin_runner.py -- minimal interpreter for ODIN node-graph procedures.

Runs the ODIN diagnostic graphs from the firmware bundle
(.../networks/Model3/{tasks,lib}/*.py) directly. Of 181 node types across the bundle,
only 5 modules touch hardware (uds / odx / can / cid / vehiclecontrols); the rest is
pure compute.

Hardware interop is behind a Backend seam:
  * MockBackend  -- scripts the choreography so the graph runs with no hardware.
  * BenchBackend -- uds/odx -> uds_local.UdsSession per ECU node; can -> live-bus
                    decode; cid -> a bench data provider.

Tesla's ODIN graph files are NOT vendored into this (public) repo. The bundle path and
CAN channel/interface resolve from .env via config.py (TM3_ROOT, TM3_ODIN_BUNDLE,
TM3_VEHICLE_CHANNEL, TM3_INTERFACE); --bundle / --channel override them.

Usage:
  python scripts/odin_runner.py --scenario success -v      # bundle from .env
  python scripts/odin_runner.py --bundle <…/networks> --scenario not-dyno
  python scripts/odin_runner.py --procedure Model3/tasks/PROC_DI_X_RESOLVER-LEARN
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime
import hashlib
import inspect
import json
import os
import re
import string
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import odin_script_api  # noqa: E402  (needs the sys.path line above)

DEFAULT_PROC = "Model3/tasks/PROC_DI_X_RESOLVER-LEARN"


# graph loading
def load_graph(bundle: Path, relbase: str) -> dict | str:
    """Load a bundle graph by its basename (e.g. 'Model3/lib/DI_RESOLVER_LEARNING').

    Returns the file's `network`, which is EITHER a dict of wired nodes (1,801 of
    1,955 files on 2022.45.15) or a Python source string defining an
    `async def odin_script_test(api, ...)` -- a native script test, run by
    Engine._run_script via odin_script_api. Callers must handle both.
    """
    path = bundle / (relbase + ".py")
    ns: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)  # noqa: S102
    if "network" not in ns:
        raise ValueError(f"{path} has no top-level `network` dict")
    return ns["network"]


def run_coroutine(coro):
    """Run `coro` to completion from synchronous code, loop or no loop.

    A native script is a coroutine, but a script may reach another script through
    a WIRED graph in between (script -> subnetwork -> task wrapper -> script), and
    that middle hop is synchronous. Re-entering asyncio.run on a thread that
    already has a running loop is an error, so the nested case gets its own loop
    on a worker thread; the outer loop blocks on it, which is what the
    synchronous graph engine expects anyway.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}
    def _run():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001  (re-raised on the caller's thread)
            box["error"] = e
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class GraphExit(Exception):
    """networks.Exit -- unwinds the current graph run with an exit code."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"exit {code}")


class ProcedureError(Exception):
    """Unsupported node type or malformed graph."""


class _BreakLoop(Exception):
    """control.Break -- unwinds to the nearest enclosing loop."""


@dataclass
class Frame:
    graph: dict
    inputs: dict
    depth: int = 0
    vars: dict = field(default_factory=dict)
    metrics: list = field(default_factory=list)
    scratch: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)  # this graph's named outputs (SetOutput / networks.Output)
    threads: list = field(default_factory=list)
    # `cancel` ends THIS graph's background branches when it finishes (run_graph
    # sets it in its finally, then joins). `run_cancel` is the whole RUN's stop
    # flag, shared by every frame, set by an operator asking to stop. They are
    # separate because one graph completing must not stop the others -- check
    # both through stopping()/wait().
    cancel: threading.Event = field(default_factory=threading.Event)
    run_cancel: threading.Event = field(default_factory=threading.Event)

    def stopping(self) -> bool:
        """This frame is done, or the operator stopped the run."""
        return self.cancel.is_set() or self.run_cancel.is_set()

    def is_set(self) -> bool:
        # Event-shaped, so a frame can be handed to a backend adapter as its
        # `cancel` (odx start_and_wait) and cover both flags.
        return self.stopping()

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep up to `timeout`, returning early (True) if either flag is set."""
        if self.cancel.wait(timeout):
            return True
        return self.run_cancel.is_set()


@dataclass
class RunResult:
    exit_code: object
    metrics: list
    outputs: dict


# CID emulation (cid.* nodes): a read-your-writes data-value store + a read-only view
# of a firmware-dump rootfs.
# Bench defaults for CID data-values; values are strings (the CID wire type).
_CID_DEFAULTS = {
    "GUI_factoryMode": "false", "GUI_developerMode": "false",
    "GUI_diagnosticMode": "false", "GUI_tdsMode": "false",
    "GUI_serviceMode": "false", "GUI_isDelivered": "true",
    # Operator-declared on a bench (cid.IsFused); the web UI's bench-state toggle
    # writes it. Default: a production, fused gateway.
    "GUI_isFused": "true",
    "VAPI_isLocked": "false", "VAPI_driverPresent": "true",
    "VAPI_countryCode": "US", "VAPI_europeVehicle": "false",
    "VAPI_odometer": "0", "VAPI_doorState": "closed",
}


class CidStore:
    """Read-your-writes CID data-value store + in-memory SaveData/LoadData blobs.

    `derive(name)` (optional) supplies live/derived values (e.g. VAPI_shiftState
    from the bus) and wins over stored values when it returns non-None.
    """

    def __init__(self, seed: dict | None = None, derive=None):
        self.values = dict(_CID_DEFAULTS)
        if seed:
            self.values.update(seed)
        self.blobs: dict = {}
        self._derive = derive

    def get(self, name):
        if self._derive is not None:
            v = self._derive(name)
            if v is not None:
                return v
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def list(self, names):
        return {n: self.get(n) for n in (names or [])}

    def save(self, filename, data):
        self.blobs[filename] = data

    def load(self, filename):
        return self.blobs.get(filename)


class CidFilesystem:
    """Read-only view of a firmware-dump rootfs for CID filesystem ops.

    Maps a CID absolute path onto <root>/<path>, jailed to the root; never writes.
    Paths absent from the dump read as empty / not-found.
    """

    def __init__(self, root: Path | str | None):
        self.root = Path(root).expanduser().resolve() if root else None

    def _resolve(self, cid_path) -> Path | None:
        if self.root is None:
            return None
        full = (self.root / str(cid_path or "").lstrip("/")).resolve()
        if full != self.root and self.root not in full.parents:
            return None  # path-jail: escaped the firmware root
        return full

    def hash_file(self, path, algorithm="sha256"):
        full = self._resolve(path)
        if full is None or not full.is_file():
            return None
        algo = str(algorithm or "sha256").lower().replace("-", "")
        try:
            h = hashlib.new(algo)
        except (ValueError, TypeError):
            return None
        h.update(full.read_bytes())
        return h.hexdigest()

    def list_dir(self, directory, show_hidden=False, details=False):
        full = self._resolve(directory)
        if full is None or not full.is_dir():
            return []
        entries = sorted(p for p in full.iterdir()
                         if show_hidden or not p.name.startswith("."))
        if not details:
            return [p.name for p in entries]
        return [{"name": p.name, "is_dir": p.is_dir(),
                 "size": p.stat().st_size if p.is_file() else 0} for p in entries]

    def grep(self, pattern, file_location):
        full = self._resolve(file_location)
        if full is None or not full.is_file():
            return []
        with contextlib.suppress(OSError, re.error):
            return [ln for ln in full.read_text(errors="replace").splitlines()
                    if re.search(str(pattern), ln)]
        return []

    def read_text(self, path):
        full = self._resolve(path)
        if full is None or not full.is_file():
            return None
        with contextlib.suppress(OSError):
            return full.read_text(errors="replace")
        return None

    def read_bytes(self, path):
        full = self._resolve(path)
        if full is None or not full.is_file():
            return None
        with contextlib.suppress(OSError):
            return full.read_bytes()
        return None


# backends (the hardware-interop seam)
# /sbin/smashclicker -- the MCU's per-ECU flasher, which every UPDATE_* procedure
# reaches through Gen3/scripts/UPDATE_MODULE:
#
#     /sbin/smashclicker -h <hwidacq list> -u <update list> -j <job id> [-t '+^=']
#
# `-h` names the ECU whose identity is read, `-u` the images to write:
# UPDATE_PMR is `-h pmr -u pmr,dir`. UPDATE_MODULE reads exit_status and scrapes
# stdout for `garage-log` lines -- a quoted module name plus a `code`, where code
# 1 or 2 is a pass -- so a substitute has to answer in that shape or the
# procedure's user-facing message comes out empty.
_SMASHCLICKER = "/sbin/smashclicker"


def _is_smashclicker(path) -> bool:
    return bool(path) and str(path).strip().endswith("smashclicker")


def _parse_smashclicker(args) -> dict:
    """The flag pairs UPDATE_MODULE passes, as keywords. Lists are comma-joined
    by the caller; `-t` is the updater-mode string, present only when can_quiet."""
    argv = [str(a) for a in (args or [])]
    out = {"update": (), "hwidacq": (), "job_id": "", "can_quiet": False}
    flags = {"-u": "update", "-h": "hwidacq", "-j": "job_id"}
    i = 0
    while i < len(argv):
        key = flags.get(argv[i])
        if key and i + 1 < len(argv):
            value = argv[i + 1]
            out[key] = value if key == "job_id" else tuple(
                p for p in value.split(",") if p)
            i += 2
            continue
        if argv[i] == "-t":
            out["can_quiet"] = True
            i += 2
            continue
        i += 1
    return out


def _scrub_reason(reason) -> str:
    """parse_log decides pass/fail by looking for the literal 'code 1'/'code 2'
    inside the quoted piece, so a failure reason must not contain one."""
    return str(reason).replace("code ", "cd ")


def _smashclicker_result(passed, failed=(), error=None, log=()) -> dict:
    """A smashclicker-shaped result UPDATE_MODULE's parse_log can read.

    parse_log splits each `garage-log` line on '"': element [1] is the module and
    a later element carrying 'code 1'/'code 2' means that module passed, while
    any other 'code ...' is a failure whose text becomes the user-facing reason.
    So both verdicts carry their module name AND their code inside quotes.
    """
    lines = [f'garage-log "{c}" update finished "code 1"' for c in passed]
    lines += [f'garage-log "{c}" update aborted "code 99 {_scrub_reason(r)}"'
              for c, r in failed]
    if error:
        lines.append(
            f'garage-log "smashclicker" update aborted "code 99 {_scrub_reason(error)}"')
    ok = bool(passed) and not failed and not error
    return {"stdout": "\r\n".join([*log, *lines]),
            "stderr": "" if ok else (error or "flash failed"),
            "exit_status": 0 if ok else 1}


class Backend:
    """Interface the interop node handlers call. One impl per environment."""

    # Set by Engine.__init__ to its own _emit, so a long-running backend
    # operation (a flash, a bus capture) can report progress on the same stream
    # the node trace goes out on instead of going quiet for minutes.
    on_event = None

    def procedure_session(self):
        """Context held around one whole procedure run (see BenchBackend)."""
        return contextlib.nullcontext()

    def uds(self, node_name: str):
        raise NotImplementedError

    def odx(self, node_name: str):
        raise NotImplementedError

    def cid_get(self, name: str):
        return None

    def cid_set(self, name: str, value) -> None:
        pass

    def can_read(self, signal: str, bus: str | None = None):
        return None  # override to decode a live bus; None => signal unseen

    def can_active_alerts(self, bus=None, prefix=None, audience=None):
        # can.ActiveAlerts: alerts asserted on a bus; a bench returns none.
        return []

    def isotp_send(self, to_controller, from_controller, data, bus=None) -> bool:
        # isotp.Send: raw ISO-TP with explicit tx/rx CAN IDs (not UDS). Bench: no-op.
        return True

    # vehiclecontrols.* power/app-state orchestration; default no-op (bench assumes the state holds).
    def ensure_power_state(self, state) -> None:
        pass

    def ensure_application_state(self, node_name: str, state) -> None:
        pass

    def store_outputs(self, outputs) -> None:
        # Persist a procedure's outputs, keyed by board id. Default no-op; BenchBackend overrides.
        pass

    # -- cid.* emulation (default: empty/stub; MockBackend + BenchBackend override) --
    def cid_list_values(self, names):
        return {n: self.cid_get(n) for n in (names or [])}

    def cid_save(self, filename, data) -> None:
        pass

    def cid_load(self, filename):
        return None

    def cid_hash_file(self, path, algorithm="sha256"):
        return None

    def cid_list_dir(self, directory, **opts):
        return []

    def cid_grep(self, pattern, file_location, args=None):
        return []

    def cid_disk_free(self, mountpoint):
        return 0

    def cid_vin(self, in_hex=False):
        return None

    def cid_vitals(self):
        return {}

    def cid_execute(self, **kwargs):
        # Stubbed shell execution (ExecuteApplication / CidCommand / ExecuteScript):
        # the firmware dump's binaries can't be run, so return canned success --
        # EXCEPT the ECU flasher, which must never fabricate a successful flash.
        if _is_smashclicker(kwargs.get("path")):
            return self.flash_module(**_parse_smashclicker(kwargs.get("args")))
        return {"stdout": "", "stderr": "", "exit_status": 0}

    def flash_preview(self, update=(), hwidacq=()) -> dict:
        """What flash_module WOULD write, resolved but not written.

        Lets an operator see the images and the car config that chose them
        before starting the run, instead of after the first one is on the wire.
        A backend that cannot flash says so here too, rather than letting the
        operator find out at the point of no return.
        """
        return {"plan": [], "conditions": {}, "choices": [],
                "blocked": [str(c) for c in update],
                "error": "this backend cannot flash"}

    def flash_module(self, update=(), hwidacq=(), job_id="", can_quiet=False,
                     timeout=None):
        """Flash ECU firmware in place of the MCU's /sbin/smashclicker.

        The 55 UPDATE_* procedures reach the drive unit, PCS, VCSEC and the rest
        through that one binary. A Backend that cannot flash must NOT answer it
        with cid_execute's canned success: UPDATE_MODULE reads exit_status and
        scrapes stdout, so a stub makes every one of them report a flash that
        never happened. Refuse instead; BenchBackend overrides with the real one.
        """
        return _smashclicker_result(
            [], error="no flasher wired to /sbin/smashclicker on this backend")

    def cid_read_file(self, path, mode="r"):
        # proto.ReadFile: served from the firmware dump on a bench (None otherwise).
        return None

    # -- high-rate gateway logging (cid.StartHRL / StopHRL / StartHrlUploadService).
    # On a car the gateway records a high-rate bus trace and the upload service
    # ships it to Tesla. A bench has no gateway and nowhere to ship to, so the
    # capture is written locally and `hrl_upload` reports where it landed rather
    # than pretending an upload happened. (A user-specified upload endpoint is the
    # natural next step; nothing here assumes the file is the end of the road.)
    def hrl_start(self, timeout=None, path=None):
        """Begin a capture; returns its path, or None with no bus to capture."""
        return None

    def hrl_stop(self):
        """End the capture; returns its path, or None if none was running."""
        return None

    def hrl_upload(self, hrl_type=None):
        """Hand off the finished capture. Returns {'path': ..., 'uploaded': bool}."""
        return {"path": None, "uploaded": False}


class MockBackend(Backend):
    """Scripts the resolver-learn choreography so the graph runs with no hardware.

    Scenarios: success | not-dyno | speed-fail | learn-fail. Advances VAPI_shiftState
    D->N when the ESP dyno routine (0xf00a) starts.
    """

    def __init__(self, scenario: str = "success"):
        self.scenario = scenario
        self.gear = "D"
        self.traction = "Normal" if scenario == "not-dyno" else "Dyno"
        self.axle_speed = 100 if scenario == "speed-fail" else 600
        self.learn = "SPEED_RANGE" if scenario == "learn-fail" else "LEARN_SUCCESS"
        self._cid = CidStore(derive=self._derive_cid)

    def _derive_cid(self, name):
        if name == "GUI_tractionControlModeRequest":
            return self.traction
        if name == "VAPI_shiftState":
            return self.gear
        return None

    # -- interop --
    def uds(self, node_name):
        return _MockUds(self, node_name)

    def odx(self, node_name):
        return _MockOdx(self, node_name)

    def cid_get(self, name):
        return self._cid.get(name)

    def cid_set(self, name, value):
        self._cid.set(name, value)

    def cid_list_values(self, names):
        return self._cid.list(names)

    def cid_save(self, filename, data):
        self._cid.save(filename, data)

    def cid_load(self, filename):
        return self._cid.load(filename)

    def can_read(self, signal, bus=None):
        if signal and signal.endswith("axleSpeed"):
            return self.axle_speed
        return 0


class _MockUds:
    def __init__(self, backend: MockBackend, node: str):
        self.backend = backend
        self.node = node

    def tester_present(self):
        pass

    def diagnostic_session(self, session_type):
        pass

    def routine_control(self, routine_id, payload=None, routine_type=None):
        # ESP 0xf00a routine = dyno enable; simulate the shift to Neutral.
        if self.node == "ESP":
            self.backend.gear = "N"
        return b""

    def security_access(self, *_a, **_k): pass
    def read_data(self, *_a, **_k): return b""
    def write_data(self, *_a, **_k): pass
    def io_control(self, *_a, **_k): return b""
    def ecu_reset(self, *_a, **_k): pass
    def clear_dtcs(self, *_a, **_k): pass
    def read_dtcs(self, *_a, **_k): return {}


class _MockOdx:
    def __init__(self, backend: MockBackend, node: str):
        self.backend = backend
        self.node = node

    def start_and_wait(self, routine, status_param, in_progress, timeout, **_):
        if routine == "RESOLVER_LEARNING":
            return {"LEARN_RESULT": self.backend.learn, "RUNNING": False, "RMSERROR": 0.5}
        return {}

    def start_routine(self, *_a, **_k): return {}
    def stop_routine(self, *_a, **_k): return b""
    def request_results(self, *_a, **_k): return {}
    def read_data(self, *_a, **_k): return {}
    def write_data(self, *_a, **_k): pass
    def get_value(self, routine, param_name, param_value, parsed): return param_value


def _to_int(v, default=0):
    """Coerce a routine/DID id from ODIN (hex string '0xfd40' or int) to int."""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 0)


def _trailing_int(s, default=0):
    """'LEVEL_5' -> 5; falls back to `default` if no trailing digits."""
    m = re.search(r"(\d+)\s*$", str(s or ""))
    return int(m.group(1)) if m else default


class _CanRxCache:
    """python-can Listener that decodes every inbound frame into a {signal: value}
    cache via a can_decoder.CanDatabase. `can_read` reads the latest value."""

    def __init__(self, db):
        self._db = db
        self._signals: dict = {}
        self._lock = threading.Lock()

    def on_message_received(self, msg) -> None:
        if msg.is_error_frame or msg.is_remote_frame:
            return
        decoded = self._db.decode_frame(msg.arbitration_id, bytes(msg.data))
        if decoded:
            with self._lock:
                for s in decoded:
                    self._signals[s["signal"]] = s["value"]

    def get(self, name):
        with self._lock:
            return self._signals.get(name)

    def stop(self) -> None:
        pass


class BenchBackend(Backend):
    """Real bench: uds/odx map to one uds_local.UdsSession per ECU node (cached, with a
    TesterPresent keep-alive), driven off the ODJ (NodeConfig) + odj_codec. uds('ESP')/
    odx('ESP') return no-op stubs (no ESP module on a conversion). can_read decodes a
    live-bus RX cache; the CID store is read-your-writes over a live-bus derive
    (`_derive_cid`); the CID filesystem is read-only.
    """

    # CID data-values the drive-unit graphs gate on are published by the MCU, which
    # a bench does not have -- so answer them from the same CAN the MCU reads and
    # fall back to the store when the bus is quiet. Simple aliases of one signal
    # (VAPI_shiftState <- DI_gear, and ~300 more) come from the firmware's OWN
    # registration table -- see vapi_registry, consulted in _derive_cid. Only the
    # values the MCU COMPUTES in code are reproduced by hand here:
    #   * VAPI_{drive,acc,hvac}RailOn from VCFRONT_vehiclePowerState (0x221)
    #   * GUI_tractionControlModeRequest from DI_tractionControlMode (a bench proxy;
    #     the real one is a touchscreen request the MCU owns, not a CAN signal)
    # (Rear unit: the aliases resolve DI_*/DIR_* off 0x118 etc.)
    # The MCU sets each rail on from VCFRONT_vehiclePowerState: off=0 conditioning=1
    # accessory=2 drive=3, each state powering every rail below it.
    _RAIL_MIN_POWER_STATE = {"VAPI_hvacRailOn": 1, "VAPI_accRailOn": 2,
                             "VAPI_driveRailOn": 3}

    def __init__(self, channel: str, interface: str = "socketcan",
                 cid_values: dict | None = None, firmware_root=None, datastore=None,
                 frame_source=None, db=None, hrl_dir=None, allow_flash=False,
                 artifacts_dir=None, conditions=None, include_bootloaders=True,
                 ramapps="include", sim_url=None, vapi_registry=None):
        self.channel = channel
        self.interface = interface
        # vehicle_sim's control server (tm3web --sim-url), for procedure_session;
        # None => no service mode on the bus.
        self.sim_url = sim_url
        # Where cid.StartHRL writes its capture (default config.HRL_DIR).
        self._hrl_dir = hrl_dir
        # Flashing is destructive and a UPDATE_* procedure asks for it with no
        # further confirmation, so it is ARMED explicitly (the web UI's bench
        # state, or allow_flash=True here) rather than on by default.
        self.allow_flash = bool(allow_flash)
        self._artifacts_dir = artifacts_dir
        # Operator-declared car config, overlaid on GTW_carConfig; see
        # vehicle_conditions. Mutable so the web UI can update it per run.
        self.conditions: dict = dict(conditions or {})
        # Whether a procedure's bu/bl entries are written. Default True: a
        # -WITH-BOOTLOADER procedure asked for them by name, and quietly
        # downgrading it to an app update would be a silent no-op. The preflight
        # dialog is where it gets confirmed. See flash_scripts.bootloader_choice.
        self.include_bootloaders = bool(include_bootloaders)
        # 'include' (as the procedure names) / 'skip' / 'only'. A RAM app is not
        # part of a normal app update, so pushing one WITHOUT rewriting the app
        # it rides on is a real request -- hence three states, not a checkbox.
        self.ramapps = ramapps
        self._hrl = None         # (logger, path, channel) while a capture is running
        self._hrl_last = None    # path of the most recent finished capture
        self._nodes: dict = {}   # NODE (upper) -> (NodeConfig, UdsSession)
        self._can: dict = {}     # channel -> (bus, notifier, _CanRxCache), lazy
        self._isotp: dict = {}   # (channel, tx, rx) -> (bus, notifier, transport), lazy
        # (channel) -> {can_id: (data, ts)} of a host's latest retained frames, or
        # None to listen on our own. See can_read.
        self._frame_source = frame_source
        # CanDatabase for can_read, lazy. A host that hands over frames should hand
        # over ITS database too: the default ETH_COMPACT is a 2022 compact.json whose
        # 0x118 carries only DI_gear/DI_brakePedalState, so decoding a viewer's frames
        # with it would silently lose signals the viewer itself displays.
        self._db_cache = db
        self._sig_index = None   # signal name -> can id, lazy
        self._datastore = datastore  # interop DataStore for stored outputs (lazy)
        self._bootloader: set = set()  # NODES currently held in their bootloader
        # firmware_root defaults to TM3_ROOT (config.ROOT); None => FS ops stub empty.
        self._cid = CidStore(seed=cid_values, derive=self._derive_cid)
        if firmware_root is None:
            import config as _cfg
            firmware_root = _cfg.ROOT
        self._fs = CidFilesystem(firmware_root)
        # The firmware's VAPI alias table (VAPI_shiftState <- DI_gear, ...). A given
        # Registry is used as-is (tests inject one); None builds it lazily from the
        # firmware under `firmware_root` on first use, and an empty one derives no
        # aliases -- so a bench without the MCU libs simply falls back to seeds.
        self._firmware_root = firmware_root
        self._vapi_reg = vapi_registry

    def _node(self, node_name):
        key = node_name.upper()
        if key not in self._nodes:
            import config as _cfg
            from uds_local.client import UdsSession
            from uds_local.node_config import load_node_config
            cfg = load_node_config(node_name, _cfg.NODES_JSON, _cfg.ETH_COMPACT,
                                   _cfg.ODJ_DIR)
            sess = UdsSession(cfg, self.channel, interface=self.interface)
            sess.start_tester_present()
            self._nodes[key] = (cfg, sess)
        return self._nodes[key]

    def open_node(self, node_name):
        """(NodeConfig, UdsSession) for a node; reuses the cached per-node session."""
        return self._node(node_name)

    def uds(self, node_name):
        if node_name.upper() == "ESP":
            return _StubUds()  # no ESP on a conversion bench
        _cfg, sess = self._node(node_name)
        return _UdsAdapter(sess)

    def odx(self, node_name):
        if node_name.upper() == "ESP":
            return _StubOdx()
        cfg, sess = self._node(node_name)
        return _OdxAdapter(sess, cfg)

    # -- cid data-value store (live-bus derive, else read-your-writes) --
    def _derive_cid(self, name):
        """Live values for the MCU-published CID names the drive-unit graphs gate on.

        Returns None for every other name, and for a signal the bus has not carried
        yet -- CidStore then falls back to its stored/seeded value, so a cid_set (or
        a `cid_values` seed) still works when nothing is transmitting.
        """
        if name == "GUI_tractionControlModeRequest":
            # Only the 'Dyno' string is load-bearing (DI_RESOLVER_LEARNING compares
            # against it verbatim); the other modes just have to not equal it.
            tcm = self._can_signal_int("DI_tractionControlMode")
            return None if tcm is None else ("Dyno" if tcm == 5 else "Normal")
        if name in self._RAIL_MIN_POWER_STATE:
            vps = self._can_signal_int("VCFRONT_vehiclePowerState")
            if vps is None:
                return None
            return "true" if vps >= self._RAIL_MIN_POWER_STATE[name] else "false"
        # Everything else the drive-unit graphs read is a plain alias of one CAN
        # signal, rendered the way the MCU renders it (an enum to its label, a bool
        # to "true"/"false"): let the firmware's own table answer. None => not an
        # alias, or its source is off the bus -- the store/seed then serves it.
        return self._registry().value(name, self._can_signal)

    def _registry(self):
        """The firmware's VAPI alias table (lazily built from `firmware_root`, then
        cached beside the DBC). An empty table when the MCU libs are absent."""
        if self._vapi_reg is None:
            import config as _cfg
            import vapi_registry
            libs = _cfg.vapi_libs(self._firmware_root)
            cache = (_cfg.ETH_DBC.with_name(_cfg.ETH_DBC.stem + ".vapi_registry.json")
                     if _cfg.ETH_DBC else None)
            self._vapi_reg = vapi_registry.load_or_build(libs[0] if libs else None, cache)
        return self._vapi_reg

    def _can_signal(self, signal):
        """Latest value of `signal` off the vehicle bus as a float, or None if it is
        absent/unparseable (see _can_signal_int)."""
        return self._coerce_signal(signal, float)

    def _can_signal_int(self, signal):
        """Latest value of `signal` off the vehicle bus as an int, or None if it is
        absent/unparseable. Never raises: a bus that will not open just reads as
        absent, so CID lookups keep working without CAN."""
        return self._coerce_signal(signal, int)

    def _coerce_signal(self, signal, cast):
        try:
            v = self.can_read(signal, bus="ETH")
        except Exception:
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    def cid_get(self, name):
        return self._cid.get(name)

    # MCU GUI_* data-values a procedure sets that reach the DU only as vehicle_sim UI state
    # (the MCU puts them on 0x284 / 0x334). GUI_factoryModeLimitOverride has no CAN bit: it
    # turns UI_limitMode SERVICE into NORMAL (see tesla_frames.ui_limit_mode).
    _CID_TO_SIM_UI = {"GUI_serviceMode": "service_mode",
                      "GUI_factoryModeLimitOverride": "factory_mode_limit_override"}

    def cid_set(self, name, value):
        self._cid.set(name, value)
        field = self._CID_TO_SIM_UI.get(name)
        if field is None or not self.sim_url:
            return
        on = 1 if str(value).strip().lower() in ("1", "true", "on", "yes") else 0
        try:
            self._set_sim_ui(field, on)
            self._status(f"vehicle_sim: {field}={on} ({name})")
        except Exception as e:  # noqa: BLE001  (a bench without the sim still runs)
            self._status(f"{name} not on the bus: {e}")

    # -- the ODIN session's MCU state, held on the bus for a whole run --
    @contextlib.contextmanager
    def procedure_session(self):
        """A tech runs ODIN with the car in service mode, and the DI refuses
        ROTOR/RESOLVER_LEARNING without UI_serviceMode on 0x284 -- which the MCU
        sends and a bench does not have. So turn vehicle_sim's service mode on for
        the run; every sim UI field a procedure changes (cid_set) is restored after.
        Never fails the run: without it the DI's own refusal says what is missing,
        and the status line says why."""
        saved = {}
        try:
            if not self.sim_url:
                raise RuntimeError("no vehicle_sim control URL (tm3web --control)")
            ui = self._sim_request("/state")["ui"]
            saved = {f: ui[f] for f in self._CID_TO_SIM_UI.values() if f in ui}
            if not ui["service_mode"]:
                self._set_sim_ui("service_mode", 1)
                self._status("vehicle_sim: service mode on for this run")
        except Exception as e:  # noqa: BLE001
            self._status(f"UI_serviceMode not on the bus: {e}")
        try:
            yield
        finally:
            for field, value in saved.items():
                with contextlib.suppress(Exception):
                    self._set_sim_ui(field, value)

    def _set_sim_ui(self, field, value):
        self._sim_request("/cmd", {"type": "ui", "field": field, "value": value})

    def _set_sim_service_mode(self, value):
        self._set_sim_ui("service_mode", value)

    def _sim_request(self, path, payload=None):
        """JSON request to vehicle_sim's control server: GET, or POST `payload`."""
        import urllib.request
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.sim_url.rstrip("/") + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read() or b"{}")

    def _status(self, message):
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event("status", {"status": message})

    def cid_list_values(self, names):
        return self._cid.list(names)

    def cid_save(self, filename, data):
        self._cid.save(filename, data)

    def cid_load(self, filename):
        return self._cid.load(filename)

    # -- cid filesystem (real data from the firmware dump, read-only) --
    def cid_hash_file(self, path, algorithm="sha256"):
        return self._fs.hash_file(path, algorithm)

    def cid_list_dir(self, directory, **opts):
        return self._fs.list_dir(directory, show_hidden=opts.get("show_hidden", False),
                                 details=opts.get("details", False))

    def cid_grep(self, pattern, file_location, args=None):
        return self._fs.grep(pattern, file_location)

    def cid_disk_free(self, mountpoint):
        return 8 * 1024 * 1024 * 1024  # placeholder 8 GiB (dump has no live free space)

    def cid_vin(self, in_hex=False):
        return self._cid.get("VAPI_vin") or "5YJ3E1EA0LF000000"  # placeholder VIN

    def cid_vitals(self):
        osr = self._fs.read_text("/etc/os-release") or ""
        return {"os_release": osr, "vin": self.cid_vin()}

    def cid_read_file(self, path, mode="r"):
        return self._fs.read_bytes(path) if "b" in str(mode) else self._fs.read_text(path)

    # -- live CAN: can_read routes bus -> channel, then reads either a host's
    # retained frames (frame_source) or this backend's own RX cache --
    def _db(self):
        if self._db_cache is None:
            from can_decoder import default_db
            self._db_cache = default_db()
        return self._db_cache

    def _can_cache(self, channel):
        if channel not in self._can:
            import can
            bus = can.Bus(interface=self.interface, channel=channel)
            notifier = can.Notifier(bus, [])
            cache = _CanRxCache(self._db())
            notifier.add_listener(cache)
            self._can[channel] = (bus, notifier, cache)
        return self._can[channel][2]

    # Which firmware row applies is decided by the car's own configuration --
    # the signed metadata carries a row per condition combination
    # (chassisType/drivetrainType/vdcType/…). Every condition key is a signal on
    # GTW_carConfig (0x7FF), named GTW_<key>, so the gateway's broadcast IS the
    # answer where there is one. On a drive-unit bench there is no gateway (and
    # vehicle_sim is the thing transmitting 0x7FF), so the operator declares it
    # instead; declared values win, since they are the deliberate statement.
    _CONDITION_KEYS = (
        "chassisType", "drivetrainType", "vdcType", "packEnergy",
        "performancePackage", "packPerformanceDeviation", "brakeHWType",
        "steeringColumnUJointType", "rcmLocation", "headlamps",
        "cabinPTCHeaterType", "espValveType", "twelveVBatteryType",
        "restraintsHardwareType", "lumbarECUType", "numberHVILNodes",
        "blowerMotorType", "towPackage", "rightHandDrive",
    )

    def vehicle_conditions(self, declared=None) -> dict:
        """The car config the firmware rows are keyed on: {key: '<int>'}.

        Read off GTW_carConfig where the bus carries it, overlaid with whatever
        the operator declared. A key neither source provides is simply absent,
        which find_firmware treats as "no constraint" -- an honest unknown rather
        than a guessed default.
        """
        out = {}
        for key in self._CONDITION_KEYS:
            v = self._can_signal_int(f"GTW_{key}")
            if v is not None:
                out[key] = str(v)
        for key, value in (declared or {}).items():
            if value is not None and value != "":
                out[str(key)] = str(value)
        return out

    def _flash_setup(self, update, hwidacq):
        """(artifacts_dir, ecu, node, conditions) or an error string."""
        import config as _cfg
        artifacts = Path(self._artifacts_dir or _cfg.ARTIFACTS_DIR or "")
        if not artifacts.is_dir():
            return (f"no firmware artifacts directory ({artifacts or 'unset'}); "
                    "set TM3_ROOT or pass artifacts_dir")
        ecu = (list(hwidacq) or list(update))[0]
        return (artifacts, ecu,
                __import__("flash_scripts").uds_node_for(update[0], default=ecu.upper()),
                self.vehicle_conditions(self.conditions))

    def flash_preview(self, update=(), hwidacq=()) -> dict:
        """Resolve the flash without writing -- see Backend.flash_preview."""
        import flash_scripts

        if not update:
            return {"plan": [], "conditions": self.vehicle_conditions(self.conditions),
                    "blocked": [], "error": "no components requested"}
        setup = self._flash_setup(update, hwidacq)
        if isinstance(setup, str):
            return {"plan": [], "conditions": {}, "blocked": list(update),
                    "error": setup}
        artifacts, ecu, node, conditions = setup
        try:
            import config as _cfg
            from uds_local.condition_labels import load_condition_labels
            _cfg_node, sess = self._node(node)
            res = flash_scripts.flash_components(
                sess, artifacts, ecu, update, conditions=conditions, dry_run=True,
                label_map=load_condition_labels(_cfg.ETH_COMPACT, _cfg.ETH_DBC),
                include_bootloaders=self.include_bootloaders,
                ramapps=self.ramapps)
        except Exception as e:  # noqa: BLE001  (a preview must not raise at the UI)
            return {"plan": [], "conditions": conditions, "choices": [],
                    "blocked": list(update), "error": f"{type(e).__name__}: {e}"}
        return {"plan": res["plan"], "conditions": res["conditions"],
                "choices": res["choices"], "identity": res["identity"],
                "blocked": res["unplaced"] + res["ambiguous"] + res["no_script"],
                "excluded": res["excluded"], "bootloaders": res["bootloaders"],
                "ramapps": res["ramapps"],
                "armed": self.allow_flash,
                "error": None if res["ok"] else "not every component resolved"}

    # -- /sbin/smashclicker's job, done with dfu's flash scripts --
    def flash_module(self, update=(), hwidacq=(), job_id="", can_quiet=False,
                     timeout=None):
        """Flash the requested components, the way UPDATE_MODULE asks the MCU to.

        `hwidacq` names the ECU whose identity picks the firmware and `update`
        the images to write (UPDATE_PMR: hwidacq=('pmr',), update=('pmr','dir')),
        which are FirmwareEntry.component names -- so dfu's signed-metadata
        selection takes them directly. The UDS node is the one the component is
        flashed THROUGH: a bootloader image goes via its parent app's node, a
        subcomponent via the ECU that gateways it (lumbarl -> VCLEFT).
        """
        import flash_scripts

        if not self.allow_flash:
            return _smashclicker_result([], error=(
                "flashing is not armed on this bench (set allow_flash / the "
                "web UI's 'allow ECU flashing')"))
        if not update:
            return _smashclicker_result([], error="no components requested")

        setup = self._flash_setup(update, hwidacq)
        if isinstance(setup, str):
            return _smashclicker_result([], error=setup)
        artifacts, ecu, node, conditions = setup
        try:
            _cfg_node, sess = self._node(node)
            res = flash_scripts.flash_components(
                sess, artifacts, ecu, update,
                channel=self.channel, interface=self.interface,
                on_event=self.on_event, conditions=conditions,
                include_bootloaders=self.include_bootloaders,
                ramapps=self.ramapps)
        except Exception as e:  # noqa: BLE001  (a flash failure is the procedure's)
            return _smashclicker_result([], error=f"{type(e).__name__}: {e}")

        failed = [(c, "no firmware for this ECU identity") for c in res["unplaced"]]
        failed += [(c, "more than one image matches; pick it with dfu.py")
                   for c in res.get("ambiguous", ())]
        failed += [(c, "no validated flash sequence") for c in res["no_script"]]
        # An excluded bootloader is a narrowing the operator chose, not a
        # failure -- but the run's record has to say the procedure did less than
        # its name promises, or nothing ever shows that it was skipped.
        log = list(res["log"])
        if res.get("excluded"):
            log.append(f"  excluded by the operator: {', '.join(res['excluded'])}")
        return _smashclicker_result(res["flashed"], failed, log=log)

    # -- high-rate logging: attach a python-can Logger to the vehicle channel's
    # EXISTING notifier, so the capture rides the socket can_read already has open
    # instead of contending for a second one. --
    def hrl_start(self, timeout=None, path=None):
        if self._hrl is not None:
            return self._hrl[1]          # already capturing; one at a time
        import can
        if path is None:
            import config as _cfg
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            directory = Path(self._hrl_dir) if self._hrl_dir else _cfg.HRL_DIR
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"hrl-{self.channel}-{stamp}.asc"
        path = Path(path)
        self._can_cache(self.channel)    # ensure the bus+notifier exist
        _bus, notifier, _cache = self._can[self.channel]
        logger = can.Logger(str(path))
        notifier.add_listener(logger)
        self._hrl = (logger, path, self.channel)
        return path

    def hrl_stop(self):
        if self._hrl is None:
            return None
        logger, path, channel = self._hrl
        self._hrl = None
        entry = self._can.get(channel)
        if entry is not None:
            with contextlib.suppress(Exception):
                entry[1].remove_listener(logger)
        with contextlib.suppress(Exception):
            logger.stop()
        self._hrl_last = path
        return path

    def hrl_upload(self, hrl_type=None):
        # No Tesla service network from a bench: the capture stays on disk and the
        # caller is told where, rather than being handed a fabricated upload id.
        path = self.hrl_stop() or self._hrl_last
        return {"path": str(path) if path else None, "uploaded": False,
                "hrl_type": hrl_type}

    def _msg_id_for(self, signal):
        """Message id carrying `signal`, or None. Built once from the DB; a name in
        several messages resolves to the first, which is deterministic (the private
        RX cache instead let the last frame to arrive win)."""
        if self._sig_index is None:
            self._sig_index = {}
            for mid, m in self._db().messages.items():
                for sname in m["signals"]:
                    self._sig_index.setdefault(sname, mid)
        return self._sig_index.get(signal)

    def _read_retained(self, channel, signal):
        """Decode `signal` out of the host's latest retained frame for `channel`.
        None when the host has not seen that id -- i.e. it is not on the bus."""
        frames = self._frame_source(channel)
        if not frames:
            return None
        mid = self._msg_id_for(signal)
        cell = frames.get(mid) if mid is not None else None
        if cell is None:
            return None
        for s in self._db().decode_frame(mid, cell[0]) or []:
            if s["signal"] == signal:
                return s["value"]
        return None

    def can_read(self, signal, bus=None):
        # Resolve ODIN bus_name (ETH/VEH/PARTY/CH) to a channel; unconfigured -> this backend's channel.
        import config as _cfg
        channel = _cfg.can_channel(bus) or self.channel
        if channel is None:
            return None
        # A host already listening on this bus (tm3web) hands over its retained
        # frames, so ODIN reads anything the host has seen without opening a second
        # socket. Standalone, fall back to this backend's own listener.
        if self._frame_source is not None:
            return self._read_retained(channel, signal)
        return self._can_cache(channel).get(signal)

    # -- raw ISO-TP send (isotp.Send): own transport per (channel, tx, rx), explicit tx/rx CAN IDs --
    def _isotp_transport(self, channel, tx_id, rx_id):
        key = (channel, tx_id, rx_id)
        if key not in self._isotp:
            import can
            from uds.can.addressing import NormalCanAddressingInformation
            from uds.can.transport_interface import PyCanTransportInterface
            bus = can.Bus(interface=self.interface, channel=channel)
            notifier = can.Notifier(bus, [])
            addr = NormalCanAddressingInformation(
                rx_physical_params={"can_id": rx_id},
                tx_physical_params={"can_id": tx_id},
                rx_functional_params={"can_id": 0x7E8},
                tx_functional_params={"can_id": 0x7DF},
            )
            tp = PyCanTransportInterface(
                network_manager=bus, addressing_information=addr, notifier=notifier)
            self._isotp[key] = (bus, notifier, tp)
        return self._isotp[key][2]

    def isotp_send(self, to_controller, from_controller, data, bus=None):
        from uds.addressing import AddressingType
        from uds.message import UdsMessage

        import config as _cfg
        channel = _cfg.can_channel(bus) or self.channel
        if channel is None:
            return False
        if isinstance(data, str):
            data = bytes.fromhex(data)
        elif data is None:
            data = b""
        else:
            data = bytes(data)
        tp = self._isotp_transport(channel, to_controller, from_controller)
        tp.send_message(UdsMessage(payload=bytearray(data),
                                   addressing_type=AddressingType.PHYSICAL))
        return True

    # -- interop data store: persist a procedure's outputs keyed by board id --
    def _store(self):
        if self._datastore is None:
            from uds_local.datastore import DataStore
            self._datastore = DataStore()
        return self._datastore

    def _primary_board_id(self):
        """Board serial (DID 0xF013) of the first node this run reached; None if unread."""
        for _cfg, sess in self._nodes.values():
            with contextlib.suppress(Exception):
                raw = bytes(sess.read_did(0xF013))  # BOARD_SERIAL_NUMBER
                sn = raw.decode("ascii", "replace").rstrip("\x00").strip()
                if sn:
                    return sn
        return None

    def store_outputs(self, outputs):
        if not outputs:
            return
        board_id = self._primary_board_id()
        if not board_id:
            return
        from uds_local.datastore import json_safe
        self._store().update(board_id, "outputs", json_safe(outputs))

    def ensure_application_state(self, node_name, state):
        """Drive a node into its BOOTLOADER or APPLICATION state
        (vehiclecontrols.EnsureApplicationState). BOOTLOADER: ecu_reset then
        wait_for_bootloader floods TesterPresent through the reboot so the bootloader
        holds; APPLICATION: reset with no flood so it boots on into the app. Tracked so
        an already-in-state node isn't reset; None / ESP are no-ops."""
        if not state or node_name.upper() == "ESP":
            return
        want_bl = "BOOT" in str(state).upper()
        key = node_name.upper()
        if want_bl == (key in self._bootloader):
            return  # already in the requested state
        _cfg, sess = self._node(node_name)
        sess.ecu_reset_no_wait(0x01)
        if want_bl:
            sess.wait_for_bootloader()
            self._bootloader.add(key)
        else:
            self._bootloader.discard(key)

    def close(self):
        for _cfg, sess in self._nodes.values():
            with contextlib.suppress(Exception):
                sess.stop_tester_present()
            with contextlib.suppress(Exception):
                sess.__exit__()
        self._nodes.clear()
        for bus, notifier, _cache in self._can.values():
            with contextlib.suppress(Exception):
                notifier.stop()
            with contextlib.suppress(Exception):
                bus.shutdown()
        self._can.clear()
        for bus, notifier, _tp in self._isotp.values():
            with contextlib.suppress(Exception):
                notifier.stop()
            with contextlib.suppress(Exception):
                bus.shutdown()
        self._isotp.clear()


class _StubUds:
    """No-op UDS node (ESP on a conversion bench) -- every service is a nop."""
    def tester_present(self): pass
    def diagnostic_session(self, *_a, **_k): pass
    def security_access(self, *_a, **_k): pass
    def routine_control(self, *_a, **_k): return b""
    def read_data(self, *_a, **_k): return b""
    def write_data(self, *_a, **_k): pass
    def io_control(self, *_a, **_k): return b""
    def ecu_reset(self, *_a, **_k): pass
    def clear_dtcs(self, *_a, **_k): pass
    def read_dtcs(self, *_a, **_k): return {}


class _StubOdx:
    """No-op odx node (ESP)."""
    def start_routine(self, *_a, **_k): return {}
    def stop_routine(self, *_a, **_k): return b""
    def request_results(self, *_a, **_k): return {}
    def start_and_wait(self, *_a, **_k): return {}
    def read_data(self, *_a, **_k): return {}
    def write_data(self, *_a, **_k): pass
    def get_value(self, routine, param_name, param_value, parsed): return param_value


class _UdsAdapter:
    """Maps ODIN uds.* (raw payloads) onto uds_local.UdsSession."""
    _RTYPE = {"START_ROUTINE": 0x01, "STOP_ROUTINE": 0x02,
              "REQUEST_RESULTS": 0x03, "REQUEST_ROUTINE_RESULTS": 0x03}
    _SESSION = {"DEFAULT_SESSION": 0x01, "PROGRAMMING_SESSION": 0x02,
                "EXTENDED_DIAGNOSTIC_SESSION": 0x03,
                "SAFETY_SYSTEM_DIAGNOSTIC_SESSION": 0x04}
    _RESET = {"HARD_RESET": 0x01, "KEY_OFF_ON_RESET": 0x02,
              "KEY_OFF_ON": 0x02, "SOFT_RESET": 0x03}
    _IOCP = {"RETURN_CONTROL_TO_ECU": 0x00, "RESET_TO_DEFAULT": 0x01,
             "FREEZE_CURRENT_STATE": 0x02, "SHORT_TERM_ADJUST": 0x03,
             "SHORT_TERM_ADJUSTMENT": 0x03}

    def __init__(self, sess):
        self.sess = sess

    def tester_present(self):
        # One now; UdsSession's keep-alive thread repeats it. An ECU reset stops that thread
        # (so it can't fight the reset) and nothing else restarts it in a long-lived bench
        # session -- start it again (a no-op while it runs).
        start = getattr(self.sess, "start_tester_present", None)
        if start:
            start()
        send = getattr(self.sess, "send_tester_present", None)
        if send:
            send()

    def tester_present_interval(self, seconds):
        """Set the keep-alive period; returns the previous one (None if unsupported)."""
        setter = getattr(self.sess, "set_tester_present_interval", None)
        return setter(seconds) if setter else None

    def diagnostic_session(self, session_type):
        s = str(session_type).upper()
        mode = self._SESSION.get(s, 0x03 if "EXTENDED" in s else 0x01)
        self.sess.diagnostic_session(mode)

    def security_access(self, security_level):
        self.sess.security_access(level_idx=0, seed_level=_trailing_int(security_level, 5))

    def routine_control(self, routine_id, payload=None, routine_type=None):
        arg = bytes.fromhex(payload) if isinstance(payload, str) and payload else b""
        sub = self._RTYPE.get(str(routine_type).upper(), 0x01)
        return self.sess.routine_control(_to_int(routine_id), arg=arg, subtype=sub)

    def read_data(self, data_id):
        return self.sess.read_did(_to_int(data_id))

    def write_data(self, data_id, payload):
        data = bytes.fromhex(payload) if isinstance(payload, str) else bytes(payload or b"")
        self.sess.write_did(_to_int(data_id), data)

    def io_control(self, control_id, control_type=None, payload=None):
        cp = self._IOCP.get(str(control_type).upper(), 0x03)
        data = (bytes.fromhex(payload) if isinstance(payload, str) and payload
                else bytes(payload or b""))
        return self.sess.io_control(_to_int(control_id), cp, data)

    def ecu_reset(self, reset_type, response_required=True):
        rt = self._RESET.get(str(reset_type).upper(), 0x01)
        if response_required is False:
            self.sess.ecu_reset_no_wait(rt)
        else:
            self.sess.ecu_reset(rt)

    def clear_dtcs(self, dtc_mask=None):
        # dtc_mask is often a status name; clear-all group defaults to 0xFFFFFF.
        try:
            group = _to_int(dtc_mask) if dtc_mask not in (None, "") else 0xFFFFFF
        except ValueError:
            group = 0xFFFFFF
        self.sess.clear_dtc(group)

    def read_dtcs(self, dtc_mask=None):
        try:
            mask = _to_int(dtc_mask) if dtc_mask not in (None, "") else 0xFF
        except ValueError:
            mask = 0xFF
        return self.sess.read_dtcs(mask)


class _OdxAdapter:
    """Maps ODIN odx.* (NAMED params) onto UdsSession using the node's ODJ
    (NodeConfig.routines/.dids) + odj_codec to resolve name->id, encode requests,
    and decode responses per the ODJ FieldSpecs. Generalizes resolver_cal.py."""

    def __init__(self, sess, cfg):
        self.sess = sess
        self.cfg = cfg

    def _routine(self, name):
        rt = self.cfg.routines.get(name)
        if rt is None:
            raise ProcedureError(f"odx routine {name!r} not in {self.cfg.name} ODJ")
        return rt

    def _did(self, name):
        d = self.cfg.dids.get(name)
        if d is None:
            raise ProcedureError(f"odx DID {name!r} not in {self.cfg.name} ODJ")
        return d

    def _auth(self, sub) -> None:
        """Run SecurityAccess for a routine/DID subspec's ODJ-declared security level (the
        graphs carry no explicit uds.UdsSecurityAccess node, so a gated routine returns
        NRC 0x33 without it). Level comes from the ODJ (e.g. PMR CAN_COMM_SELF_TEST = 5);
        uses the EXTENDED diagnostic session (0x03). Idempotent (re-request -> NRC 0x35)."""
        level = getattr(sub, "security_level", 0) if sub else 0
        if level:
            self.sess.diagnostic_session(0x03)  # extended diagnostic session
            self.sess.security_access(seed_level=level)

    def start_routine(self, routine, params=None):
        from uds_local.odj_codec import decode_response, encode_request
        rt = self._routine(routine)
        self._auth(rt.start)
        raw = self.sess.routine_control(
            rt.hex_id, arg=encode_request(rt.start, params or {}), subtype=0x01)
        # The StartRoutine positive response carries the routineStatusRecord the
        # scripts branch on -- ROTOR/RESOLVER_LEARNING put START_ROUTINE_RESULTS /
        # START_RESULTS_REASON here. Decode it per the START subspec's OUTPUT
        # fields; it is NOT the RequestRoutineResults (0x31 03) record, which has
        # different fields (ROUTINE_STATUS / LEARN_RESULT).
        return decode_response(rt.start, raw, parsed=True)

    def stop_routine(self, routine, params=None):
        from uds_local.odj_codec import encode_request
        rt = self._routine(routine)
        self._auth(rt.stop)
        return self.sess.routine_control(
            rt.hex_id, arg=encode_request(rt.stop, params or {}), subtype=0x02)

    def request_results(self, routine, params=None):
        from uds_local.odj_codec import decode_response, encode_request
        rt = self._routine(routine)
        self._auth(rt.results)
        raw = self.sess.routine_control(
            rt.hex_id, arg=encode_request(rt.results, params or {}), subtype=0x03)
        return decode_response(rt.results, raw, parsed=True)

    def start_and_wait(self, routine, status_param, in_progress, timeout,
                       input_parameters=None, stop_routine=False,
                       cancel=None, time_scale=1.0):
        from uds_local.odj_codec import decode_response, encode_request
        rt = self._routine(routine)
        self._auth(rt.start)
        self.sess.routine_control(
            rt.hex_id, arg=encode_request(rt.start, input_parameters or {}), subtype=0x01)
        in_prog = in_progress if isinstance(in_progress, (list, tuple, set)) else [in_progress]
        deadline = time.monotonic() + (timeout or 0) * (time_scale or 0.0)
        results: dict = {}
        first = True
        while first or time.monotonic() < deadline:
            first = False
            raw = self.sess.routine_control(rt.hex_id, subtype=0x03)
            results = decode_response(rt.results, raw, parsed=True)
            if status_param is None or results.get(status_param) not in in_prog:
                break
            if cancel is not None and cancel.is_set():
                break
            time.sleep(0.2 * (time_scale or 0.0))
        if stop_routine:
            with contextlib.suppress(Exception):
                self.sess.routine_control(rt.hex_id, subtype=0x02)
        return results

    def read_data(self, data_name):
        from uds_local.odj_codec import decode_response
        d = self._did(data_name)
        self._auth(d.read)
        return decode_response(d.read, self.sess.read_did(d.hex_id), parsed=True)

    def write_data(self, data_name, data):
        from uds_local.odj_codec import encode_request
        d = self._did(data_name)
        self._auth(d.write)
        self.sess.write_did(d.hex_id, encode_request(d.write, data or {}))

    def get_value(self, routine, param_name, param_value, parsed):
        """Re-derive one results param's parsed/raw form from its ODJ FieldSpec."""
        from uds_local.odj_codec import decode_field  # noqa: F401
        rt = self.cfg.routines.get(routine)
        fs = rt.results.output.get(param_name) if rt and rt.results else None
        if fs is None or not fs.enum_map:
            return param_value
        if parsed:
            if {k.upper() for k in fs.enum_map} == {"TRUE", "FALSE"}:
                return bool(param_value)
            inverse = {v: k for k, v in fs.enum_map.items()}
            return inverse.get(param_value, param_value)
        return fs.enum_map.get(param_value, param_value)  # name -> raw number


# the engine
class Engine:
    def __init__(self, backend: Backend, bundle: Path, verbose: bool = False,
                 time_scale: float = 0.0, loop_pause: float = 0.01, max_loops: int = 50,
                 on_event=None, principals=odin_script_api.DEFAULT_PRINCIPALS):
        self.backend = backend
        self.bundle = bundle
        self.verbose = verbose
        # Who a native script sees as the caller (api.odin.get_caller_principals);
        # some procedures gate their inputs on it. Bench default: tbx-internal.
        self.principals = tuple(principals)
        self._time_scale = time_scale   # 0 => don't actually sleep (fast mock)
        self._loop_pause = loop_pause
        self._max_loops = max_loops
        # Optional run-progress listener: on_event(kind, payload) fires for 'trace' and
        # 'metric'. See scripts/odin_service.py.
        self._on_event = on_event
        # Let the backend report on that same stream -- a flash takes minutes and
        # must not go quiet while it runs.
        with contextlib.suppress(AttributeError):
            backend.on_event = self._emit
        # One cancel flag for the whole RUN, shared by every frame it creates, so
        # an operator's "stop" reaches the poll loops in a nested subnetwork too.
        # It is checked between operations -- what it can end is a wait (a CID
        # poll, a routine's result poll, a timeout loop), not an operation already
        # in flight. A flash in particular does not check it: interrupting a
        # transfer mid-write is how an ECU gets bricked.
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        """Ask the run to stop at its next check. Safe to call from any thread."""
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # Bare task-wrapper entry types (no networks.Enter): a single subnet-call node.
    _WRAPPER_ENTRY_TYPES = (
        "networks.RunReferencedSubnetwork",
        "scripts.RunScriptTest",
        "scripts.ScriptTest",     # same node, older name (always inline in 2022.45.15)
        "networks.DynamicallyReferencedSubnetwork",
    )

    def run_procedure(self, relbase: str) -> RunResult:
        session = getattr(self.backend, "procedure_session", contextlib.nullcontext)
        with session():
            result = self._run_child_graph(load_graph(self.bundle, relbase), {}, depth=0)
        with contextlib.suppress(Exception):
            self.backend.store_outputs(result.outputs)  # persist keyed by board id
        return result

    def _run_child_graph(self, graph: dict | str, inputs: dict, depth: int) -> RunResult:
        """Run a loaded graph regardless of form: a NATIVE SCRIPT (`network` is a
        Python source string) runs as a coroutine; a wired graph (has
        networks.Enter) runs directly; a bare task-wrapper (a single
        RunReferencedSubnetwork / RunScriptTest / DynamicallyReferencedSubnetwork
        node, no Enter) invokes its subnet in a wrapper frame so `<Input>.value`
        connections resolve."""
        if isinstance(graph, str):
            return run_coroutine(self._script_coro(graph, inputs, depth))
        if self._has(graph, "networks.Enter"):
            return self.run_graph(graph, inputs, depth=depth)
        tname = next((self._find_opt(graph, t) for t in self._WRAPPER_ENTRY_TYPES
                      if self._find_opt(graph, t)), None)
        if tname is None:
            return self.run_graph(graph, inputs, depth=depth)  # no Enter -> clear error
        frame = Frame(graph=graph, inputs=inputs, depth=depth, run_cancel=self._cancel)
        node = graph[tname]
        self._log(depth, f"task {tname} -> {self._resolve_basename(frame, node)}")
        return self._invoke_subnet(frame, tname, node)

    def run_graph(self, graph: dict, inputs: dict, depth: int = 0) -> RunResult:
        frame = Frame(graph=graph, inputs=inputs, depth=depth, run_cancel=self._cancel)
        code = 1
        try:
            self._fire(frame, self._graph_start(graph))
        except GraphExit as ge:
            code = ge.code
        finally:
            frame.cancel.set()
            for t in frame.threads:
                t.join(timeout=0.5)
        self._collect_outputs(frame)
        return RunResult(code, frame.metrics, frame.outputs)

    def _graph_start(self, graph: dict):
        """The graph's entry control field. Wired graphs (incl. inline Enter/Exit
        subnets) start at networks.Enter.start; inline Slot/Signal subnets start at
        the networks.Slot node, which relays into the inner graph via its `signal`."""
        enter = self._find_opt(graph, "networks.Enter")
        if enter is not None:
            return graph[enter].get("start")
        slot = self._find_opt(graph, "networks.Slot")
        if slot is not None:
            return graph[slot].get("signal")
        raise ProcedureError("graph has no networks.Enter or networks.Slot node")

    def _collect_outputs(self, frame: Frame) -> None:
        """Materialize a graph's networks.Output ports into frame.outputs (SetOutput nodes
        have already written theirs; setdefault lets an imperative SetOutput win).
        Best-effort: an early exit may leave some source nodes uncomputed."""
        for oname, onode in frame.graph.items():
            if isinstance(onode, dict) and onode.get("type") == "networks.Output":
                with contextlib.suppress(Exception):
                    frame.outputs.setdefault(oname, self._pull(frame, onode.get("port")))

    def _invoke_subnet(self, frame: Frame, name: str, node: dict) -> RunResult:
        """Run a referenced/script subnetwork: resolve basename, bind inputs (pulled in the
        caller's frame), run the child in its own frame, stash the RunResult. Child metrics
        bubble up; child GraphExit does not unwind the caller."""
        base = self._resolve_basename(frame, node)
        child = load_graph(self.bundle, base)
        child_inputs = {k: self._pull(frame, v)
                        for k, v in (node.get("inputs") or {}).items()}
        self._log(frame.depth, f"call {name} -> {base} inputs={child_inputs}")
        result = self._run_child_graph(child, child_inputs, depth=frame.depth + 1)
        frame.scratch[name] = result
        frame.metrics.extend(result.metrics)
        self._log(frame.depth, f"     {base} exit={result.exit_code} outputs={result.outputs}")
        return result

    def _resolve_basename(self, frame: Frame, node: dict):
        """The referenced graph basename, however this subnet-call node names it:
        `basename` (Referenced/RunReferencedSubnetwork), `script_name` (RunScriptTest,
        a bare string), or `name` (DynamicallyReferencedSubnetwork). A dict value is a
        data field pulled in the caller frame (a runtime-resolved basename)."""
        b = None
        for key in ("basename", "script_name", "name"):
            if node.get(key) is not None:
                b = node[key]
                break
        return self._pull(frame, b) if isinstance(b, dict) else b

    # -- native script tests: `network` is a Python SOURCE STRING defining
    # `async def odin_script_test(api, ...)`. Same interop as the graph form, but
    # awaited on a facade instead of wired through ports. See odin_script_api.
    async def _script_coro(self, source: str, inputs: dict, depth: int) -> RunResult:
        frame = Frame(graph={}, inputs=inputs, depth=depth, run_cancel=self._cancel)
        api = odin_script_api.ScriptApi(self, frame, self.principals)
        fn = odin_script_api.compile_script(source, api.clock)
        bound = self._bind_script_inputs(fn, inputs, depth)
        self._log(depth, f"script {fn.__name__}({', '.join(sorted(bound))})")
        try:
            returned = await fn(api, **bound)
        except GraphExit as ge:                     # a script may exit the run
            returned = ge.code
        finally:
            frame.cancel.set()
        code, extra = self._script_result(returned)
        return RunResult(code, frame.metrics, {**frame.outputs, **extra})

    def _bind_script_inputs(self, fn, inputs: dict, depth: int = 0) -> dict:
        """Bind a caller's inputs onto the script's declared parameters.

        A parameter the caller left unset -- absent, or bound to None, which is
        how a task wrapper spells "unset" (see _data_networks_Input) -- falls back
        to its declared default. A parameter with NEITHER is passed as None, which
        is what ODIN itself does: an unbound networks.Input reads as None too, and
        the scripts are written for it (UPDATE_MODULE takes a `job_id: str` with
        no default and normalises a non-numeric one to ''). Rejecting it here
        would refuse procedures the real tool runs, so it is logged instead.
        """
        params = list(inspect.signature(fn).parameters.values())[1:]  # [0] is `api`
        bound, unset = {}, []
        for p in params:
            v = inputs.get(p.name)
            if v is not None:
                bound[p.name] = v
            elif p.default is inspect.Parameter.empty:
                bound[p.name] = None
                unset.append(p.name)
        if unset:
            self._log(depth, f"     unbound script inputs -> None: {', '.join(unset)}")
        return bound

    @staticmethod
    def _script_result(returned) -> tuple[object, dict]:
        """A script's return -> (exit_code, extra outputs).

        Scripts end either with a ServiceOutput (the documented form: a verdict
        plus a user-facing message) or a bare int exit code; both reduce to the
        integer RunResult.exit_code that odin_service reports `passed` from.
        """
        if isinstance(returned, odin_script_api.ServiceOutput):
            return int(returned.exit_code), {"service_output": returned.as_dict()}
        if returned is None:
            return 0, {}
        if isinstance(returned, int):
            return int(returned), {}
        return 0, {"result": returned}

    async def _run_child_async(self, basename: str, inputs: dict,
                               depth: int) -> RunResult:
        """Run another bundle graph from inside a running script (the awaited
        api.subnetwork.run_reference / api.scripts.script_test). A script child is
        awaited directly; a wired child runs synchronously on this thread, as it
        does everywhere else in the engine."""
        child = load_graph(self.bundle, basename)
        self._log(depth - 1, f"call -> {basename} inputs={inputs}")
        if isinstance(child, str):
            return await self._script_coro(child, inputs, depth)
        return self._run_child_graph(child, inputs, depth)

    # -- networks.Subnetwork: an INLINE-nested subgraph. Unlike ReferencedSubnetwork
    # (which loads another file), the node's own dict IS the child graph: every non-
    # reserved key is an inner node, and inner connections are prefixed with this node's
    # name (`<subnet>.<inner>.<port>`). Lift the inner nodes into a child graph, strip
    # the prefix, and reuse the same execution path as a referenced subnetwork.
    _RESERVED_SUBNET_KEYS = frozenset(
        {"type", "position", "slots", "signals", "inputs", "outputs", "comment"})

    def _ctrl_networks_Subnetwork(self, frame, name, node, in_port):
        self._invoke_inline_subnet(frame, name, node)
        sig = (node.get("signals") or {}).get("exit")
        if sig:
            self._fire(frame, sig)

    def _invoke_inline_subnet(self, frame: Frame, name: str, node: dict) -> RunResult:
        child = {k: self._deprefix(v, name + ".")
                 for k, v in node.items()
                 if k not in self._RESERVED_SUBNET_KEYS
                 and isinstance(v, dict) and "type" in v}
        child_inputs = {k: self._pull(frame, v)
                        for k, v in (node.get("inputs") or {}).items()}
        self._log(frame.depth, f"inline-subnet {name} inputs={child_inputs}")
        result = self.run_graph(child, child_inputs, depth=frame.depth + 1)
        frame.scratch[name] = result
        frame.metrics.extend(result.metrics)
        self._log(frame.depth, f"     {name} exit={result.exit_code} outputs={result.outputs}")
        return result

    @classmethod
    def _deprefix(cls, obj, prefix):
        """Deep-copy a node/field, stripping `prefix` from every 'connection' string so
        lifted inner nodes reference each other by bare `<inner>.<port>`. Parent-scope
        connections (no prefix) are left untouched."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k == "connection" and isinstance(v, str) and v.startswith(prefix):
                    out[k] = v[len(prefix):]
                else:
                    out[k] = cls._deprefix(v, prefix)
            return out
        if isinstance(obj, list):
            return [cls._deprefix(x, prefix) for x in obj]
        return obj

    # inline-subnet Slot/Signal plumbing (alternative to Enter/Exit): Slot relays the
    # parent's slot into the inner graph via `signal`; Signal is the exit relay leaf.
    def _ctrl_networks_Slot(self, frame, name, node, in_port):
        self._fire(frame, node.get("signal"))

    def _ctrl_networks_Signal(self, frame, name, node, in_port):
        pass  # exit relay -- see _ctrl_networks_Subnetwork

    def _ctrl_networks_Cancelled(self, frame, name, node, in_port):
        # Operator-cancellation handler; never raised here -> inert.
        pass

    # -- control/data plumbing --
    def _fire(self, frame: Frame, ctrl_field) -> None:
        if not ctrl_field or "connection" not in ctrl_field:
            return
        # Split on the FIRST dot: the remainder is the full port path (e.g. 'run', 'slots.enter').
        node_name, _, in_port = ctrl_field["connection"].partition(".")
        node = frame.graph[node_name]
        handler = getattr(self, "_ctrl_" + node["type"].replace(".", "_"), None)
        if handler is None:
            raise ProcedureError(f"no control handler for {node['type']} ({node_name})")
        self._log(frame.depth, f"run  {node_name} [{node['type']}] :{in_port}")
        handler(frame, node_name, node, in_port)

    def _pull(self, frame: Frame, fld):
        if not isinstance(fld, dict):
            return fld
        if "connection" in fld:
            node_name, _, port = fld["connection"].partition(".")  # first dot: see _fire
            node = frame.graph[node_name]
            handler = getattr(self, "_data_" + node["type"].replace(".", "_"), None)
            if handler is None:
                raise ProcedureError(f"no data handler for {node['type']} ({node_name})")
            return handler(frame, node_name, node, port)
        if "value" in fld:
            return fld["value"]
        return None

    @staticmethod
    def _find_opt(graph, ntype):
        for name, node in graph.items():
            if isinstance(node, dict) and node.get("type") == ntype:
                return name
        return None

    @staticmethod
    def _has(graph, ntype):
        return any(isinstance(n, dict) and n.get("type") == ntype for n in graph.values())

    # comparator/operator enum: 0:== 1:!= 2:< 3:<= 4:> 5:>=
    @staticmethod
    def _cmp(op, a, b) -> bool:
        if a is None:
            return False
        try:
            return [lambda: a == b, lambda: a != b, lambda: a < b,
                    lambda: a <= b, lambda: a > b, lambda: a >= b][int(op or 0)]()
        except TypeError:
            return a == b

    def _log(self, depth, msg):
        if self.verbose:
            print("  " * (depth + 1) + msg)
        if self._on_event is not None:
            self._emit("trace", {"depth": depth, "message": msg})

    def _emit(self, kind, payload) -> None:
        """Push a run event (kind in {'trace','metric'}) to the on_event listener."""
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(kind, payload)

    def _sleep(self, seconds):
        if self._time_scale and seconds:
            time.sleep(seconds * self._time_scale)

    # ---------------- control handlers ----------------
    def _ctrl_networks_Exit(self, frame, name, node, in_port):
        code = self._pull(frame, node.get("exit_code"))
        with contextlib.suppress(TypeError, ValueError):
            code = int(code)
        raise GraphExit(code)

    def _ctrl_networks_Set(self, frame, name, node, in_port):
        var = self._pull(frame, node["variable"])
        frame.vars[var] = self._pull(frame, node["value"])
        self._log(frame.depth, f"     set {var} = {frame.vars[var]!r}")
        self._fire(frame, node.get("saved"))

    def _ctrl_networks_SetOutput(self, frame, name, node, in_port):
        frame.outputs[node["key"]] = self._pull(frame, node.get("value"))
        self._log(frame.depth, f"     output[{node['key']!r}] = {frame.outputs[node['key']]!r}")
        self._fire(frame, node.get("finished"))

    def _ctrl_networks_AppendOutput(self, frame, name, node, in_port):
        # Like SetOutput but accumulates into a LIST-valued output (fired repeatedly).
        key = node["key"]
        bucket = frame.outputs.setdefault(key, [])
        if not isinstance(bucket, list):
            bucket = frame.outputs[key] = [bucket]
        bucket.append(self._pull(frame, node.get("value")))
        self._fire(frame, node.get("finished"))

    # referenced subnetworks: entered via a <node>.slots.<slot> control connection; on
    # child exit fire the `exit` signal. Child outputs read as data via <node>.outputs.<name>.
    def _ctrl_networks_ReferencedSubnetwork(self, frame, name, node, in_port):
        self._invoke_subnet(frame, name, node)
        sig = (node.get("signals") or {}).get("exit")
        if sig:
            self._fire(frame, sig)

    _ctrl_networks_RunReferencedSubnetwork = _ctrl_networks_ReferencedSubnetwork

    def _data_networks_ReferencedSubnetwork(self, frame, name, node, port):
        result = frame.scratch.get(name)
        if result is None:
            return None
        oname = port.split(".", 1)[1] if port.startswith("outputs.") else port
        if oname == "exit_code":
            return result.exit_code
        return result.outputs.get(oname)

    _data_networks_RunReferencedSubnetwork = _data_networks_ReferencedSubnetwork
    # networks.Subnetwork outputs are read the same way (<node>.outputs.<name>).
    _data_networks_Subnetwork = _data_networks_ReferencedSubnetwork

    # scripts.RunScriptTest: run a scripts/ graph (basename = `script_name`); continues via `done`.
    def _ctrl_scripts_RunScriptTest(self, frame, name, node, in_port):
        self._invoke_subnet(frame, name, node)
        sig = (node.get("signals") or {}).get("exit")
        if sig:
            self._fire(frame, sig)
        self._fire(frame, node.get("done"))

    _data_scripts_RunScriptTest = _data_networks_ReferencedSubnetwork
    # scripts.ScriptTest is the same node under its older name (script_name +
    # inputs + done); PROC_DIR_X_RESTORE-DATA-APP and the immobilizer/odometer
    # pairing task both use it.
    _ctrl_scripts_ScriptTest = _ctrl_scripts_RunScriptTest
    _data_scripts_ScriptTest = _data_networks_ReferencedSubnetwork

    # networks.DynamicallyReferencedSubnetwork: basename resolved at run time from `name`; continues via `done`.
    def _ctrl_networks_DynamicallyReferencedSubnetwork(self, frame, name, node, in_port):
        self._invoke_subnet(frame, name, node)
        sig = (node.get("signals") or {}).get("exit")
        if sig:
            self._fire(frame, sig)
        self._fire(frame, node.get("done"))

    _data_networks_DynamicallyReferencedSubnetwork = _data_networks_ReferencedSubnetwork

    def _ctrl_control_IfThen(self, frame, name, node, in_port):
        branch = "if_true" if self._pull(frame, node["expr"]) else "if_false"
        self._fire(frame, node.get(branch))

    def _ctrl_control_Split(self, frame, name, node, in_port):
        t = threading.Thread(target=self._branch, args=(frame, node.get("a")), daemon=True)
        frame.threads.append(t)
        t.start()
        self._fire(frame, node.get("b"))

    def _ctrl_control_ConcurrentSplit(self, frame, name, node, in_port):
        # N named branches instead of Split's two. Every branch but the last runs
        # on its own thread (joined by run_graph's finally); the last runs inline,
        # so a single-branch node behaves like a plain call.
        branches = [b for _k, b in sorted((node.get("branches") or {}).items(),
                                          key=lambda kv: kv[1].get("index", 0)
                                          if isinstance(kv[1], dict) else 0)]
        for branch in branches[:-1]:
            t = threading.Thread(target=self._branch, args=(frame, branch), daemon=True)
            frame.threads.append(t)
            t.start()
        if branches:
            self._fire(frame, branches[-1])

    def _branch(self, frame, ctrl_field):
        try:
            self._fire(frame, ctrl_field)
        except GraphExit:
            pass
        except _BreakLoop:
            pass
        except Exception as e:  # noqa: BLE001  (a concurrent branch dying must not crash the run)
            self._log(frame.depth, f"     [split branch error] {e}")

    def _run_body(self, frame, ctrl_field) -> bool:
        """Fire a loop body; return True if a control.Break asked the loop to stop."""
        try:
            self._fire(frame, ctrl_field)
            return False
        except _BreakLoop:
            return True

    def _ctrl_control_Break(self, frame, name, node, in_port):
        raise _BreakLoop

    def _ctrl_control_WhileLoop(self, frame, name, node, in_port):
        cond = node.get("condition", {}).get("value", "True")
        i = 0
        while not frame.stopping():
            if cond != "True" and not self._pull(frame, node["condition"]):
                break
            if self._run_body(frame, node.get("run_body")):
                break
            i += 1
            if self._max_loops and i >= self._max_loops:
                break
            if frame.wait(self._loop_pause):
                break

    def _ctrl_control_TryExceptAll(self, frame, name, node, in_port):
        try:
            self._fire(frame, node.get("try_body"))
            self._fire(frame, node.get("else_body"))
        except GraphExit:
            raise
        except Exception as e:  # noqa: BLE001
            frame.scratch.setdefault(name, {})["exception"] = e
            self._fire(frame, node.get("except_body"))
        finally:
            self._fire(frame, node.get("finally_body"))

    def _ctrl_control_TryExcept(self, frame, name, node, in_port):
        # Typed catch: Tesla's exception_class names don't map here, so catch broadly. GraphExit still propagates.
        try:
            self._fire(frame, node.get("try_body"))
            self._fire(frame, node.get("else_body"))
        except GraphExit:
            raise
        except Exception as e:  # noqa: BLE001
            frame.scratch.setdefault(name, {})["exception"] = e
            self._fire(frame, node.get("except_body"))
        finally:
            self._fire(frame, node.get("finally_body"))

    def _data_control_TryExcept(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("exception")

    # -- vehiclecontrols.* (power / app-state orchestration; bench = assume state) --
    def _ctrl_vehiclecontrols_PowerContext(self, frame, name, node, in_port):
        # Ensure power state, run the protected body, continue via `done`.
        self.backend.ensure_power_state(self._pull(frame, node.get("power_state")))
        self._fire(frame, node.get("body"))
        self._fire(frame, node.get("done"))

    def _ctrl_vehiclecontrols_EnsureApplicationState(self, frame, name, node, in_port):
        self.backend.ensure_application_state(
            self._pull(frame, node.get("node_name")),
            self._pull(frame, node.get("application_state")))
        self._fire(frame, node.get("done"))

    def _ctrl_vehiclecontrols_EnsurePowerState(self, frame, name, node, in_port):
        self.backend.ensure_power_state(self._pull(frame, node.get("power_state")))
        self._fire(frame, node.get("done"))

    def _ctrl_vehiclecontrols_McuScreenOn(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_debug_Sleep(self, frame, name, node, in_port):
        self._sleep(self._pull(frame, node.get("seconds")) or 0)
        self._fire(frame, node.get("done"))

    def _ctrl_debug_Print(self, frame, name, node, in_port):
        self._log(frame.depth, f"     print {self._pull(frame, node.get('value'))!r}")
        self._fire(frame, node.get("done"))

    def _ctrl_reporting_CaptureMetric(self, frame, name, node, in_port):
        metric = {
            "metric": self._pull(frame, node.get("metric_name")),
            "value": self._pull(frame, node.get("value")),
            "result_code": self._pull(frame, node.get("result_code")),
            "expected": self._pull(frame, node.get("expected_value")),
        }
        frame.metrics.append(metric)
        self._log(frame.depth,
                  f"     metric {metric['metric']} = {metric['value']!r} rc={metric['result_code']}")
        self._emit("metric", metric)
        self._fire(frame, node.get("done"))

    # -- reporting.ServiceOutput: the graph-form twin of a native script's returned
    # ServiceOutput -- a user-facing message, an exit reason and optional named data
    # blocks. It is TERMINAL (no `done` on any of the 9 instances in the bundle), so
    # it ends its graph the way networks.Exit does, carrying its verdict out as the
    # RunResult exit code. In a lib/ subnetwork that stops the child only, which is
    # what its callers read back via <node>.outputs / exit_code.
    def _ctrl_reporting_ServiceOutput(self, frame, name, node, in_port):
        out = odin_script_api.ServiceOutput(
            self._pull(frame, node.get("user_facing_msg")) or "",
            self._pull(frame, node.get("exit_code")))
        for block, fld in (node.get("data") or {}).items():
            out.add_data(name=block, data_type=None, data=self._pull(frame, fld))
        frame.outputs["service_output"] = out.as_dict()
        self._log(frame.depth, f"     service_output {out.exit_code!r}: "
                               f"{out.user_facing_msg!r}")
        code = out.exit_code
        with contextlib.suppress(TypeError, ValueError):
            code = int(code)
        raise GraphExit(code)

    def _data_reporting_ServiceOutputExitReason(self, frame, name, node, port):
        # Read as <node>.<MEMBER> (e.g. ServiceOutputExitReason.INVALID_INPUT).
        try:
            return odin_script_api.ExitReason[port]
        except KeyError:
            return odin_script_api.ExitReason.UNKNOWN

    def _ctrl_reporting_FileOutput(self, frame, name, node, in_port):
        # Attach a file to the run report. There is no report service on a bench,
        # so the blob goes to the CID store under its file name and the run's
        # `files` output records what was attached.
        fname = self._pull(frame, node.get("file_name"))
        self.backend.cid_save(fname, self._pull(frame, node.get("data")))
        frame.outputs.setdefault("files", []).append({
            "file_name": fname,
            "mime_type": self._pull(frame, node.get("mime_type")),
            "encoding": self._pull(frame, node.get("encoding")),
        })
        self._log(frame.depth, f"     file output {fname!r}")
        self._fire(frame, node.get("finished"))

    def _ctrl_testing_PingOutcome(self, frame, name, node, in_port):
        # Terminal node of the PING tasks: the outcome IS the procedure's verdict.
        outcome = self._pull(frame, node.get("outcome"))
        frame.metrics.append({"metric": "PingOutcome", "value": outcome,
                              "result_code": 0 if outcome else 1, "expected": True})
        self._emit("metric", frame.metrics[-1])
        self._fire(frame, node.get("done"))

    def _data_testing_PingOutcome(self, frame, name, node, port):
        return self._pull(frame, node.get("outcome"))

    def _ctrl_reporting_CaptureConnectorInfoLookup(self, frame, name, node, in_port):
        # Ties a connector-info file to the exit_code; records it as a metric (no connector DB on a bench).
        code = self._pull(frame, node.get("exit_code"))
        frame.metrics.append({
            "metric": "ConnectorInfoLookup",
            "value": self._pull(frame, node.get("file_name")),
            "result_code": code,
            "expected": None,
        })
        self._emit("metric", frame.metrics[-1])
        self._fire(frame, node.get("done"))

    def _ctrl_cid_GetDataValueUntil(self, frame, name, node, in_port):
        dn = self._pull(frame, node["data_name"])
        want = self._pull(frame, node["pass_value"])
        op = self._pull(frame, node.get("operator")) or 0
        timeout = self._pull(frame, node.get("timeout")) or 10
        poll = self._pull(frame, node.get("sleep")) or 0.25
        deadline = time.monotonic() + timeout * (self._time_scale or 0.0)
        v = None
        first = True
        while first or time.monotonic() < deadline:
            first = False
            v = self.backend.cid_get(dn)
            if self._cmp(op, v, want):
                self._log(frame.depth, f"     cid {dn}={v!r} == {want!r} -> passed")
                return self._fire(frame, node.get("passed"))
            if frame.stopping():
                break
            self._sleep(poll)
        self._log(frame.depth, f"     cid {dn}={v!r} != {want!r} -> timed_out")
        self._fire(frame, node.get("timed_out"))

    # ---- cid.* data-value store (read-your-writes) ----
    def _ctrl_cid_SetDataValue(self, frame, name, node, in_port):
        self.backend.cid_set(self._pull(frame, node["data_name"]),
                             self._pull(frame, node.get("value")))
        self._fire(frame, node.get("done"))

    def _ctrl_cid_SaveData(self, frame, name, node, in_port):
        self.backend.cid_save(self._pull(frame, node.get("filename")),
                             self._pull(frame, node.get("data")))
        self._fire(frame, node.get("done"))

    # ---- cid.* filesystem ops (real data from the firmware dump, read-only) ----
    def _ctrl_cid_GetDirectoryContents(self, frame, name, node, in_port):
        listing = self.backend.cid_list_dir(
            self._pull(frame, node.get("directory")),
            show_hidden=bool(self._pull(frame, node.get("show_hidden"))),
            details=bool(self._pull(frame, node.get("details"))))
        frame.scratch[name] = {"result": listing, "error": ""}
        self._fire(frame, node.get("done"))

    def _ctrl_cid_Grep(self, frame, name, node, in_port):
        matches = self.backend.cid_grep(self._pull(frame, node.get("pattern")),
                                        self._pull(frame, node.get("file_location")),
                                        self._pull(frame, node.get("args")))
        frame.scratch[name] = {"stdout": "\n".join(matches), "stderr": "",
                               "matches": matches}
        self._fire(frame, node.get("done"))

    def _ctrl_cid_GetDiskFree(self, frame, name, node, in_port):
        frame.scratch[name] = self.backend.cid_disk_free(
            self._pull(frame, node.get("mountpoint")))
        self._fire(frame, node.get("done"))

    # ---- cid.* shell execution (stubbed: dump binaries can't run) ----
    def _cid_exec(self, frame, name, node, kind):
        frame.scratch[name] = self.backend.cid_execute(
            kind=kind,
            path=self._pull(frame, node.get("path")),
            command=self._pull(frame, node.get("command")),
            args=self._pull(frame, node.get("args")),
            user=self._pull(frame, node.get("user")))
        self._fire(frame, node.get("done"))

    def _ctrl_cid_ExecuteApplication(self, frame, name, node, in_port):
        self._cid_exec(frame, name, node, "application")

    def _ctrl_cid_ExecuteScript(self, frame, name, node, in_port):
        self._cid_exec(frame, name, node, "script")

    def _ctrl_cid_CidCommand(self, frame, name, node, in_port):
        self._cid_exec(frame, name, node, "command")

    # ---- cid.* no-op control (service/reboot/process; nothing to do on a bench) ----
    def _ctrl_cid_SvCommand(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_cid_CheckProcess(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_cid_RebootCid(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_cid_ClearCache(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_cid_EmitRebootGateway(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    # High-rate gateway logging. On a car the gateway records the bus and the
    # upload service ships the trace to Tesla; here the capture is written to a
    # local CAN log (config.HRL_DIR) and the upload step reports the path instead
    # of claiming an upload. The path is exposed as an output so a caller can pick
    # the file up -- and, later, POST it somewhere the operator chooses.
    def _ctrl_cid_StartHRL(self, frame, name, node, in_port):
        path = self.backend.hrl_start(self._pull(frame, node.get("timeout")))
        frame.scratch[name] = {"path": str(path) if path else None}
        self._log(frame.depth, f"     hrl start -> {path}")
        self._fire(frame, node.get("done"))

    def _data_cid_StartHRL(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("path")

    def _ctrl_cid_StopHRL(self, frame, name, node, in_port):
        path = self.backend.hrl_stop()
        frame.scratch[name] = {"path": str(path) if path else None}
        self._log(frame.depth, f"     hrl stop -> {path}")
        self._fire(frame, node.get("done"))

    def _data_cid_StopHRL(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("path")

    def _ctrl_cid_StartHrlUploadService(self, frame, name, node, in_port):
        res = self.backend.hrl_upload(self._pull(frame, node.get("hrl_type"))) or {}
        frame.scratch[name] = res
        if res.get("path"):
            frame.outputs.setdefault("hrl_logs", []).append(res["path"])
        self._log(frame.depth,
                  f"     hrl upload -> {res.get('path')} (uploaded={res.get('uploaded')})")
        self._fire(frame, node.get("done"))

    def _data_cid_StartHrlUploadService(self, frame, name, node, port):
        res = frame.scratch.get(name, {})
        return res.get(port, res)

    def _ctrl_cid_SetVehicleConfig(self, frame, name, node, in_port):
        # A gateway vehicle-config write. No gateway on a bench: record it in the
        # CID store (read-your-writes) so a later read sees what was set.
        cid = self._pull(frame, node.get("configid"))
        self.backend.cid_set(f"config_{cid}", self._pull(frame, node.get("data")))
        frame.scratch[name] = {"success": True}
        self._fire(frame, node.get("done"))

    def _data_cid_SetVehicleConfig(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("success", True)

    # cid.IsFused: whether the gateway is fused (a production car) or unfused (a
    # factory/service unit). Several graphs gate factory-mode writes on it, so it
    # is an OPERATOR-DECLARED fact on a bench, not something to infer: it lives in
    # the CID store as GUI_isFused, which the web UI's bench-state toggle sets.
    def _cid_is_fused(self) -> bool:
        return str(self.backend.cid_get("GUI_isFused")).lower() == "true"

    def _ctrl_cid_IsFused(self, frame, name, node, in_port):
        frame.scratch[name] = {"is_fused": self._cid_is_fused()}
        self._fire(frame, node.get("done"))

    def _data_cid_IsFused(self, frame, name, node, port):
        return self._cid_is_fused()

    def _ctrl_cid_SetFactoryMode(self, frame, name, node, in_port):
        state = self._pull(frame, node.get("factory_mode_state"))
        self.backend.cid_set("GUI_factoryMode",
                             "true" if state in (True, "true", 1, "1") else "false")
        self._fire(frame, node.get("done"))

    def _ctrl_cid_GetPlatform(self, frame, name, node, in_port):
        frame.scratch[name] = self._cid_platform()
        self._fire(frame, node.get("done"))

    def _data_cid_GetPlatform(self, frame, name, node, port):
        info = frame.scratch.get(name) or self._cid_platform()
        return info.get(port) if port in info else info

    def _cid_platform(self) -> dict:
        """The MCU generation graphs/scripts branch on ('infoz', …), from vitals."""
        vitals = self.backend.cid_vitals() or {}
        return {"info": {"info_hw": vitals.get("info_hw"),
                         "info_sw": vitals.get("info_sw")}}

    def _ctrl_cidupdater_Command(self, frame, name, node, in_port):
        # The CID's updater daemon (handshake / schedule-update / …). Nothing to
        # drive on a bench; reported like cid.SvCommand so control flow advances.
        self._log(frame.depth,
                  f"     cidupdater {self._pull(frame, node.get('command'))!r} (no-op)")
        frame.scratch[name] = {"stdout": "", "stderr": "", "exit_status": 0}
        self._fire(frame, node.get("done"))

    def _data_cidupdater_Command(self, frame, name, node, port):
        res = frame.scratch.get(name, {"stdout": "", "stderr": "", "exit_status": 0})
        return res.get(port, res)

    def _ctrl_odin_GetMetadataPath(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _data_odin_GetMetadataPath(self, frame, name, node, port):
        # Where ODIN keeps a run's metadata on the MCU. The graphs concatenate a
        # filename onto it and hand the result to cid.SaveData/LoadData, whose
        # bench store is keyed by that string -- so it only has to be consistent.
        return "/home/odin/metadata"

    def _ctrl_cid_SaveAuthoredPopup(self, frame, name, node, in_port):
        # Persist an authored popup blob keyed by identifier; record success.
        self.backend.cid_save(self._pull(frame, node.get("identifier")),
                              self._pull(frame, node.get("data")))
        frame.scratch[name] = {"success": True}
        self._fire(frame, node.get("done"))

    def _data_cid_SaveAuthoredPopup(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("success", True)

    def _ctrl_cid_ShowAuthoredPopup(self, frame, name, node, in_port):
        # Display a saved popup on the MCU screen. There is no screen on a bench,
        # so record which one was shown and continue.
        ident = self._pull(frame, node.get("identifier"))
        frame.scratch[name] = {"success": True}
        self._log(frame.depth, f"     popup {ident!r} (no screen on a bench)")
        self._fire(frame, node.get("done"))

    def _data_cid_ShowAuthoredPopup(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("success", True)

    def _ctrl_cid_GetGatewayFile(self, frame, name, node, in_port):
        # Pull a file off the gateway. No gateway on a bench, but the firmware
        # dump's read-only FS view serves the static ones (CidFilesystem); a path
        # it does not carry reads as absent, which is what a fresh unit has.
        src = self._pull(frame, node.get("source"))
        frame.scratch[name] = {"contents": self.backend.cid_read_file(src, "r"),
                               "source": src}
        self._fire(frame, node.get("done"))

    def _data_cid_GetGatewayFile(self, frame, name, node, port):
        res = frame.scratch.get(name, {})
        return res.get(port, res.get("contents"))

    def _ctrl_apupdater_ClearCache(self, frame, name, node, in_port):
        # Clear the Autopilot updater's download cache on the MCU. Nothing to
        # clear on a bench; continue so the surrounding procedure still runs.
        self._fire(frame, node.get("done"))

    # ---- messages.* (ODIN framework UI/IPC messages; bench = non-blocking) ----
    # ODIN procedures narrate themselves through these two. On the MCU they drive
    # the service UI; here they go out on the run's event stream so a caller can
    # show the same thing -- a percentage where the procedure gives one, the step
    # name where it does not.
    def _ctrl_messages_ProgressUpdate(self, frame, name, node, in_port):
        value = self._pull(frame, node.get("value"))
        self._log(frame.depth, f"     progress {value}%")
        self._emit("progress", {"value": value, "total": 100, "current": value,
                                "units": "percent", "source": "procedure"})
        self._fire(frame, node.get("done"))

    def _ctrl_messages_StatusUpdate(self, frame, name, node, in_port):
        status = self._pull(frame, node.get("status"))
        self._log(frame.depth, f"     status: {status!r}")
        self._emit("status", {"status": status, "source": "procedure"})
        self._fire(frame, node.get("done"))

    def _ctrl_messages_Listen(self, frame, name, node, in_port):
        # No message framework on a bench -> fire done (or timed_out if no done port).
        self._fire(frame, node.get("done") or node.get("timed_out"))

    def _ctrl_messages_Broadcast(self, frame, name, node, in_port):
        # Publish to a Hermes topic (Tesla's manufacturing message bus). No bus on
        # a bench -> fire-and-forget, logged so the choreography is still visible.
        self._log(frame.depth,
                  f"     msg.broadcast [{self._pull(frame, node.get('message_type'))}] "
                  f"-> {self._pull(frame, node.get('hermes_topic'))!r}")
        self._fire(frame, node.get("done"))

    def _ctrl_messages_Send(self, frame, name, node, in_port):
        # Publish to the ODIN message bus; no such bus on a bench -> fire-and-forget, continue via `done`.
        self._log(frame.depth, f"     msg.send {self._pull(frame, node.get('payload'))!r}")
        self._fire(frame, node.get("done"))

    # ---- proto.ReadFile (served from the firmware dump via CidFilesystem) ----
    def _ctrl_proto_ReadFile(self, frame, name, node, in_port):
        frame.scratch[name] = self.backend.cid_read_file(
            self._pull(frame, node.get("filepath")),
            self._pull(frame, node.get("mode")) or "r")
        self._fire(frame, node.get("done"))

    # ---- can.CANSignalMonitor (fire value_changed on a change; else timed_out) ----
    def _ctrl_can_CANSignalMonitor(self, frame, name, node, in_port):
        if node.get("enabled") is not None and not self._pull(frame, node["enabled"]):
            return self._fire(frame, node.get("done") or node.get("value_changed"))
        sig = self._pull(frame, node.get("signal_name"))
        bus = self._pull(frame, node.get("bus_name"))
        timeout = self._pull(frame, node.get("timeout")) or 10
        deadline = time.monotonic() + timeout * (self._time_scale or 0.0)
        initial = self.backend.can_read(sig, bus)
        frame.scratch[name] = {"current": initial}
        first = True
        while first or time.monotonic() < deadline:
            first = False
            v = self.backend.can_read(sig, bus)
            frame.scratch[name] = {"current": v}
            if v is not None and v != initial:
                return self._fire(frame, node.get("value_changed"))
            if frame.stopping():
                break
            self._sleep(0.05)
        self._fire(frame, node.get("timed_out") or node.get("done"))

    def _ctrl_can_CANSignalValueComparison(self, frame, name, node, in_port):
        sig = self._pull(frame, node["signal_name"])
        bus = self._pull(frame, node.get("bus_name"))
        target = self._pull(frame, node["target"])
        comp = self._pull(frame, node["comparator"])
        timeout = self._pull(frame, node.get("timeout")) or 10
        deadline = time.monotonic() + timeout * (self._time_scale or 0.0)
        v = None
        first = True
        while first or time.monotonic() < deadline:
            first = False
            v = self.backend.can_read(sig, bus)
            if self._cmp(comp, v, target):
                self._log(frame.depth, f"     can {sig}={v} (cmp {comp} {target}) -> true")
                return self._fire(frame, node.get("true"))
            if frame.stopping():
                break
            self._sleep(0.05)
        self._log(frame.depth, f"     can {sig}={v} (cmp {comp} {target}) -> false")
        self._fire(frame, node.get("false"))

    def _ctrl_uds_UdsTesterPresent(self, frame, name, node, in_port):
        self.backend.uds(self._pull(frame, node["node_name"])).tester_present()
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsDiagnosticSession(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).diagnostic_session(self._pull(frame, node.get("session_type")))
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsRoutineControl(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).routine_control(
            self._pull(frame, node.get("routine_id")),
            self._pull(frame, node.get("input_payload")),
            self._pull(frame, node.get("routine_type")))
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxStartAndWaitResults(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        routine = self._pull(frame, node["routine_name"])
        results = self.backend.odx(nn).start_and_wait(
            routine,
            self._pull(frame, node.get("status_parameter")),
            self._pull(frame, node.get("in_progress_statuses")) or [True],
            self._pull(frame, node.get("timeout")) or 1,
            input_parameters=self._pull(frame, node.get("input_parameters")),
            stop_routine=self._pull(frame, node.get("stop_routine")) or False,
            cancel=frame, time_scale=self._time_scale)
        frame.scratch.setdefault(name, {})["results"] = results
        self._log(frame.depth, f"     odx {routine}@{nn} -> {results}")
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxStartAndWaitResults_V2(self, frame, name, node, in_port):
        # V2: explicit diagnostic_session, max_runtime (s), success/failed ports. Leaving
        # the in-progress set before the deadline = success; timeout/error = failure.
        nn = self._pull(frame, node["node_name"])
        routine = self._pull(frame, node["routine_name"])
        status_param = self._pull(frame, node.get("status_parameter"))
        in_prog = self._pull(frame, node.get("in_progress_statuses")) or [True]
        sess = self._pull(frame, node.get("diagnostic_session"))
        ok = True
        try:
            if sess:
                self.backend.odx(nn)  # session is applied by the adapter as needed
            results = self.backend.odx(nn).start_and_wait(
                routine, status_param, in_prog,
                self._pull(frame, node.get("max_runtime")) or 1,
                stop_routine=self._pull(frame, node.get("should_stop")) or False,
                cancel=frame, time_scale=self._time_scale)
            final = results.get(status_param) if status_param else None
            ok = final not in in_prog  # left the in-progress set -> completed
        except Exception as e:  # noqa: BLE001  (a routine/transport error is a fail)
            results, ok = {"error": str(e)}, False
        frame.scratch.setdefault(name, {})["results"] = results
        self._log(frame.depth, f"     odx-v2 {routine}@{nn} ok={ok} -> {results}")
        self._fire(frame, node.get("success") if ok else node.get("failed"))

    def _data_odx_OdxStartAndWaitResults_V2(self, frame, name, node, port):
        if port == "results_control_type":
            return "REQUEST_ROUTINE_RESULTS"
        return frame.scratch.get(name, {}).get("results", {})

    # ---- odx.* routines/DIDs (named params via the ODJ codec) ----
    def _ctrl_odx_OdxStartRoutine(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.odx(nn).start_routine(
            self._pull(frame, node["routine_name"]),
            self._pull(frame, node.get("params")))
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxStopRoutine(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.odx(nn).stop_routine(
            self._pull(frame, node["routine_name"]),
            self._pull(frame, node.get("params")))
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxRequestResults(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        results = self.backend.odx(nn).request_results(
            self._pull(frame, node["routine_name"]),
            self._pull(frame, node.get("params")))
        frame.scratch.setdefault(name, {})["results"] = results
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxReadData(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        data = self.backend.odx(nn).read_data(self._pull(frame, node["data_name"]))
        frame.scratch.setdefault(name, {})["data"] = data
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxWriteData(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.odx(nn).write_data(
            self._pull(frame, node["data_name"]),
            self._pull(frame, node.get("data")))
        self._fire(frame, node.get("done"))

    def _odx_get_value(self, frame, node, parsed):
        nn = self._pull(frame, node["node_name"])
        return self.backend.odx(nn).get_value(
            self._pull(frame, node.get("routine_name")),
            self._pull(frame, node.get("param_name")),
            self._pull(frame, node.get("param_value")),
            parsed)

    def _ctrl_odx_OdxGetParsedValue(self, frame, name, node, in_port):
        frame.scratch[name] = self._odx_get_value(frame, node, parsed=True)
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxGetRawValue(self, frame, name, node, in_port):
        frame.scratch[name] = self._odx_get_value(frame, node, parsed=False)
        self._fire(frame, node.get("done"))

    # ---- uds.* raw services ----
    def _ctrl_uds_UdsReadData(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        data = self.backend.uds(nn).read_data(self._pull(frame, node["data_id"]))
        frame.scratch.setdefault(name, {})["data"] = data
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsWriteData(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).write_data(
            self._pull(frame, node["data_id"]),
            self._pull(frame, node.get("input_payload")))
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsSecurityAccess(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).security_access(self._pull(frame, node.get("security_level")))
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsIOControl(self, frame, name, node, in_port):
        # InputOutputControlByIdentifier (0x2F): control_id=DID, control_type=IOCBI
        # controlParameter name, input_payload=controlState bytes.
        nn = self._pull(frame, node["node_name"])
        data = self.backend.uds(nn).io_control(
            self._pull(frame, node.get("control_id")),
            self._pull(frame, node.get("control_type")),
            self._pull(frame, node.get("input_payload")))
        frame.scratch.setdefault(name, {})["data"] = data
        self._fire(frame, node.get("done"))

    def _data_uds_UdsIOControl(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("data", b"")

    # ---- isotp.* raw transport (explicit tx/rx CAN IDs, not a UDS service) ----
    def _ctrl_isotp_Send(self, frame, name, node, in_port):
        ok = self.backend.isotp_send(
            _to_int(self._pull(frame, node.get("to_controller"))),
            _to_int(self._pull(frame, node.get("from_controller"))),
            self._pull(frame, node.get("data")),
            bus=self._pull(frame, node.get("bus_name")))
        frame.scratch[name] = {"success": bool(ok)}
        self._fire(frame, node.get("done"))

    def _data_isotp_Send(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("success", True)

    def _ctrl_uds_UdsEcuReset(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).ecu_reset(
            self._pull(frame, node.get("reset_type")),
            self._pull(frame, node.get("response_required")))
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsClearDtcs(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        self.backend.uds(nn).clear_dtcs(self._pull(frame, node.get("dtc_mask")))
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsReadDtcs(self, frame, name, node, in_port):
        nn = self._pull(frame, node["node_name"])
        dtcs = self.backend.uds(nn).read_dtcs(self._pull(frame, node.get("dtc_mask")))
        # {dtc_code: status} dict (empty on a healthy ECU).
        frame.scratch[name] = {"dtcs": dtcs}
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsTesterPresentContext(self, frame, name, node, in_port):
        # UdsSession keeps its own TesterPresent thread alive; just run the body.
        self.backend.uds(self._pull(frame, node["node_name"])).tester_present()
        self._fire(frame, node.get("body"))
        self._fire(frame, node.get("done"))

    # ---------------- data handlers ----------------
    def _data_networks_Input(self, frame, name, node, port):
        # Bind to None means "unset" -> fall back to the Input node's declared default.
        v = frame.inputs.get(name)
        if v is not None:
            return v
        return self._pull(frame, node.get("default"))

    def _data_enum_EnumInput(self, frame, name, node, port):
        # A networks.Input restricted to `options`. Same None->default rule; the
        # options list itself is readable as <node>.options.
        if port == "options":
            return list(node.get("options") or [])
        v = frame.inputs.get(name)
        return v if v is not None else self._pull(frame, node.get("default"))

    def _data_networks_Get(self, frame, name, node, port):
        var = self._pull(frame, node["variable"])
        if var in frame.vars:
            return frame.vars[var]
        return self._pull(frame, node.get("default"))

    def _data_constant_Constant(self, frame, name, node, port):
        return self._pull(frame, node.get("value"))

    def _data_logic_Compare(self, frame, name, node, port):
        # Compare carries an `operator` (0:== 1:!= 2:< 3:<= 4:> 5:>=); default ==.
        return self._cmp(self._pull(frame, node.get("operator")),
                         self._pull(frame, node["a"]), self._pull(frame, node["b"]))

    def _data_collections_GetItem(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        key = self._pull(frame, node["key"])
        default = self._pull(frame, node.get("default"))
        if isinstance(data, dict):
            return data.get(key, default)
        if isinstance(data, (list, tuple, str)):
            try:
                return data[key]
            except (IndexError, KeyError, TypeError):
                return default
        return default

    _data_dicts_GetItem = _data_collections_GetItem

    def _data_reporting_BoolToResultCode(self, frame, name, node, port):
        return 0 if self._pull(frame, node["input"]) else 1

    def _data_strings_Concat(self, frame, name, node, port):
        return str(self._pull(frame, node["a"])) + str(self._pull(frame, node["b"]))

    def _data_cid_GetDataValue(self, frame, name, node, port):
        return self.backend.cid_get(self._pull(frame, node["data_name"]))

    def _data_cid_ListDataValues(self, frame, name, node, port):
        return self.backend.cid_list_values(self._pull(frame, node.get("dv")))

    def _data_cid_LoadData(self, frame, name, node, port):
        return self.backend.cid_load(self._pull(frame, node.get("filename")))

    def _data_cid_HashFile(self, frame, name, node, port):
        return self.backend.cid_hash_file(
            self._pull(frame, node.get("filepath")),
            self._pull(frame, node.get("algorithm")) or "sha256")

    def _data_cid_GetVin(self, frame, name, node, port):
        return self.backend.cid_vin(bool(self._pull(frame, node.get("in_hex"))))

    def _data_cid_GetVitals(self, frame, name, node, port):
        return self.backend.cid_vitals()

    def _data_cid_GetDirectoryContents(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get(port or "result")

    def _data_cid_Grep(self, frame, name, node, port):
        res = frame.scratch.get(name, {})
        return res.get(port, res.get("stdout", ""))

    def _data_cid_GetDiskFree(self, frame, name, node, port):
        return frame.scratch.get(name, 0)

    def _data_cid_ExecuteApplication(self, frame, name, node, port):
        res = frame.scratch.get(name, {})
        return res.get(port, res)

    _data_cid_ExecuteScript = _data_cid_ExecuteApplication
    _data_cid_CidCommand = _data_cid_ExecuteApplication

    # ---- cheap-logic + live-CAN data handlers ----
    def _data_dicts_FromInputs(self, frame, name, node, port):
        # Gather single-letter input ports (a, b, c, ...) into a list, ordered by letter.
        keys = sorted(k for k in node if len(k) == 1 and k.isalpha())
        return [self._pull(frame, node[k]) for k in keys]

    def _data_bytes_Base64Decode(self, frame, name, node, port):
        v = self._pull(frame, node["value"])
        return base64.b64decode(v) if v is not None else b""

    def _data_bytes_Base64Encode(self, frame, name, node, port):
        return base64.b64encode(self._as_bytes(self._pull(frame, node["value"])))

    def _data_bytes_RandomBytes(self, frame, name, node, port):
        return os.urandom(int(self._pull(frame, node.get("n")) or 0))

    def _data_can_CANSignalRead(self, frame, name, node, port):
        return self.backend.can_read(self._pull(frame, node.get("signal_name")),
                                     self._pull(frame, node.get("bus_name")))

    def _data_can_CANSignalMonitor(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get(port or "current")

    def _data_can_ActiveAlerts(self, frame, name, node, port):
        return self.backend.can_active_alerts(
            self._pull(frame, node.get("bus_name")),
            self._pull(frame, node.get("prefix")),
            self._pull(frame, node.get("audience")))

    def _data_can_BytesToInt(self, frame, name, node, port):
        b = self._pull(frame, node.get("bytes"))
        if isinstance(b, (bytes, bytearray)):
            return int.from_bytes(b, "big")
        if isinstance(b, str):
            return int.from_bytes(bytes.fromhex(b), "big") if b else 0
        return b  # already an int (e.g. from GetChunkFromBytes)

    def _data_proto_ReadFile(self, frame, name, node, port):
        return frame.scratch.get(name)

    def _data_odx_OdxStartAndWaitResults(self, frame, name, node, port):
        if port == "results_control_type":
            return "REQUEST_ROUTINE_RESULTS"
        return frame.scratch.get(name, {}).get("results", {})

    def _data_odx_OdxRequestResults(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("results", {})

    def _data_odx_OdxReadData(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("data", {})

    def _data_odx_OdxGetParsedValue(self, frame, name, node, port):
        return frame.scratch.get(name)

    def _data_odx_OdxGetRawValue(self, frame, name, node, port):
        return frame.scratch.get(name)

    def _data_uds_UdsReadData(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("data", b"")

    def _data_uds_UdsReadDtcs(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("dtcs", {})

    def _data_uds_UdsDTCMaskRepr(self, frame, name, node, port):
        # Render a DTC status-mask byte as a hex string (cosmetic; used in reports).
        v = self._pull(frame, node.get("dtc_mask_value"))
        return f"0x{int(v):02X}" if isinstance(v, int) else str(v)

    def _data_control_TryExceptAll(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("exception")

    # Data nodes return one value (the `port` arg is ignored unless a node exposes several
    # named outputs -- the iteration nodes below use it).

    # ---- logic ----
    def _data_logic_IsIn(self, frame, name, node, port):
        b = self._pull(frame, node["b"])
        return self._pull(frame, node["a"]) in b if b is not None else False

    def _data_logic_IsNotIn(self, frame, name, node, port):
        b = self._pull(frame, node["b"])
        return self._pull(frame, node["a"]) not in b if b is not None else True

    def _data_logic_IsEmpty(self, frame, name, node, port):
        a = self._pull(frame, node["a"])
        if a is None:
            return True
        try:
            return len(a) == 0
        except TypeError:
            return not a

    def _data_logic_Or(self, frame, name, node, port):
        return self._pull(frame, node["a"]) or self._pull(frame, node["b"])

    def _data_logic_And(self, frame, name, node, port):
        return self._pull(frame, node["a"]) and self._pull(frame, node["b"])

    def _data_logic_Not(self, frame, name, node, port):
        return not self._pull(frame, node["a"])

    def _data_logic_IsNone(self, frame, name, node, port):
        return self._pull(frame, node["a"]) is None

    def _data_logic_IsNonZero(self, frame, name, node, port):
        return bool(self._pull(frame, node["a"]))

    def _data_logic_Between(self, frame, name, node, port):
        a = self._pull(frame, node["a"])
        lo = self._pull(frame, node["low"])
        hi = self._pull(frame, node["high"])
        return lo <= a <= hi if None not in (a, lo, hi) else False

    def _data_logic_LessThan(self, frame, name, node, port):
        return self._cmp(2, self._pull(frame, node["a"]), self._pull(frame, node["b"]))

    def _data_logic_MultiAndOr(self, frame, name, node, port):
        op = self._pull(frame, node.get("operator"))
        vals = [self._pull(frame, v) for v in (node.get("inputs") or {}).values()]
        return all(vals) if op == "and" else any(vals)

    # ---- dicts (immutable: return a new dict) ----
    def _data_dicts_SetItem(self, frame, name, node, port):
        data = self._pull(frame, node.get("data"))
        out = dict(data) if isinstance(data, dict) else {}
        out[self._pull(frame, node["key"])] = self._pull(frame, node["value"])
        return out

    def _data_dicts_Keys(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        return list(data.keys()) if isinstance(data, dict) else []

    def _data_dicts_Values(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        return list(data.values()) if isinstance(data, dict) else []

    def _data_dicts_Merge(self, frame, name, node, port):
        return {**(self._pull(frame, node["a"]) or {}),
                **(self._pull(frame, node["b"]) or {})}

    def _data_dicts_HasKey(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        return isinstance(data, dict) and self._pull(frame, node["key"]) in data

    # ---- collections / lists ----
    def _data_collections_Len(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        return len(data) if data is not None else 0

    def _data_collections_Sort(self, frame, name, node, port):
        return sorted(self._pull(frame, node["data"]) or [])

    def _data_lists_Append(self, frame, name, node, port):
        out = list(self._pull(frame, node.get("items")) or [])
        out.append(self._pull(frame, node["value"]))
        return out

    def _data_lists_Extend(self, frame, name, node, port):
        return [*(self._pull(frame, node["a"]) or []),
                *(self._pull(frame, node["b"]) or [])]

    def _data_lists_ExtendMulti(self, frame, name, node, port):
        # Extend, but over an indexed `lists` map instead of a fixed a/b pair.
        # A non-list entry contributes itself (the sole instance in the bundle
        # feeds it a single string).
        out: list = []
        for _k, fld in sorted((node.get("lists") or {}).items(),
                              key=lambda kv: kv[1].get("index", 0)
                              if isinstance(kv[1], dict) else 0):
            v = self._pull(frame, fld)
            if isinstance(v, (list, tuple)):
                out.extend(v)
            elif v is not None:
                out.append(v)
        return out

    def _data_lists_Any(self, frame, name, node, port):
        return any(self._pull(frame, node["items"]) or [])

    def _data_lists_Splice(self, frame, name, node, port):
        items = self._pull(frame, node["items"]) or []
        start = self._pull(frame, node.get("start")) or 0
        return items[start:self._pull(frame, node.get("end"))]

    # ---- math ----
    def _data_math_Add(self, frame, name, node, port):
        return self._pull(frame, node["a"]) + self._pull(frame, node["b"])

    def _data_math_Subtract(self, frame, name, node, port):
        return self._pull(frame, node["a"]) - self._pull(frame, node["b"])

    def _data_math_Multiply(self, frame, name, node, port):
        return self._pull(frame, node["a"]) * self._pull(frame, node["b"])

    def _data_math_Divide(self, frame, name, node, port):
        return self._pull(frame, node["a"]) / self._pull(frame, node["b"])

    def _data_math_Mod(self, frame, name, node, port):
        return self._pull(frame, node["x"]) % self._pull(frame, node["divisor"])

    def _data_math_SeriesSum(self, frame, name, node, port):
        return sum(self._pull(frame, node.get("series")) or [])

    def _data_math_Abs(self, frame, name, node, port):
        return abs(self._pull(frame, node["value"]))

    # ---- strings ----
    def _data_strings_Format(self, frame, name, node, port):
        text = self._pull(frame, node["text"]) or ""
        opts = self._pull(frame, node.get("options"))
        return text.format(**opts) if isinstance(opts, dict) else text

    def _data_strings_Split(self, frame, name, node, port):
        return str(self._pull(frame, node["text"])).split(
            self._pull(frame, node.get("separator")))

    def _data_strings_Join(self, frame, name, node, port):
        joiner = self._pull(frame, node.get("joiner")) or ""
        return joiner.join(str(x) for x in (self._pull(frame, node["items"]) or []))

    def _data_strings_Substring(self, frame, name, node, port):
        s = str(self._pull(frame, node["string"]))
        start = self._pull(frame, node.get("start")) or 0
        return s[start:self._pull(frame, node.get("end"))]

    def _data_strings_Rstrip(self, frame, name, node, port):
        return str(self._pull(frame, node["str"])).rstrip()

    def _data_strings_Strip(self, frame, name, node, port):
        return str(self._pull(frame, node["str"])).strip()

    def _data_strings_Splitlines(self, frame, name, node, port):
        return str(self._pull(frame, node["text"])).splitlines()

    def _data_strings_Case(self, frame, name, node, port):
        # case enum: 0:lower 1:upper 2:title 3:capitalize
        text = str(self._pull(frame, node["text"]))
        case = int(self._pull(frame, node.get("case")) or 0)
        funcs = [text.lower, text.upper, text.title, text.capitalize]
        return funcs[case]() if 0 <= case < len(funcs) else text

    # ---- control: data ternary + iteration + misc ----
    def _data_control_Switch(self, frame, name, node, port):
        branch = "if_true" if self._pull(frame, node["expr"]) else "if_false"
        return self._pull(frame, node.get(branch))

    def _ctrl_control_ForEach(self, frame, name, node, in_port):
        for item in self._pull(frame, node.get("items")) or []:
            if frame.stopping():
                break
            frame.scratch[name] = {"item": item}
            if self._run_body(frame, node.get("run_each")):
                break
        self._fire(frame, node.get("done"))

    def _data_control_ForEach(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get(port or "item")

    def _ctrl_control_ForEachEntry(self, frame, name, node, in_port):
        data = self._pull(frame, node.get("data"))
        for key, value in (data.items() if isinstance(data, dict) else []):
            if frame.stopping():
                break
            frame.scratch[name] = {"key": key, "value": value}
            if self._run_body(frame, node.get("run_each")):
                break
        self._fire(frame, node.get("done"))

    def _data_control_ForEachEntry(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get(port)

    def _ctrl_control_ForLoop(self, frame, name, node, in_port):
        start = int(self._pull(frame, node.get("start")) or 0)
        end = int(self._pull(frame, node.get("end")) or 0)
        for i in range(start, end):
            if frame.stopping():
                break
            frame.scratch[name] = {"i": i}
            if self._run_body(frame, node.get("run_each")):
                break
        self._fire(frame, node.get("done"))

    def _data_control_ForLoop(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get(port or "i")

    def _ctrl_control_TimeoutLoop(self, frame, name, node, in_port):
        timeout = self._pull(frame, node.get("timeout")) or 0
        poll = self._pull(frame, node.get("sleep")) or 0
        deadline = time.monotonic() + (timeout / 1000.0) * (self._time_scale or 0.0)
        first = True
        while first or time.monotonic() < deadline:
            first = False
            if self._run_body(frame, node.get("run_body")):
                break
            if "condition" in node and self._pull(frame, node["condition"]):
                self._fire(frame, node.get("passed") or node.get("done"))
                return
            if frame.stopping():
                break
            self._sleep(poll / 1000.0)
        self._fire(frame, node.get("timed_out") or node.get("done"))

    def _ctrl_control_Delay(self, frame, name, node, in_port):
        self._sleep(self._pull(frame, node.get("seconds")) or 0)
        self._fire(frame, node.get("done"))

    def _ctrl_control_Either(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_control_Sync(self, frame, name, node, in_port):
        self._fire(frame, node.get("done"))

    def _ctrl_control_Counter(self, frame, name, node, in_port):
        frame.scratch[name] = frame.scratch.get(name, 0) + 1
        self._fire(frame, node.get("updated"))

    def _data_control_Counter(self, frame, name, node, port):
        return frame.scratch.get(name, 0)

    def _ctrl_control_Case(self, frame, name, node, in_port):
        sel = self._pull(frame, node.get("selector"))
        runs = node.get("run_cases") or {}
        for cname, cval in (node.get("cases") or {}).items():
            if self._pull(frame, cval) == sel:
                self._fire(frame, runs.get(cname))
                return
        self._fire(frame, node.get("default") or runs.get("default"))

    # fan-out / join: MultiSplit fires each branch (index order); MultiMerge fires `done`
    # once every dependency has arrived.
    def _ctrl_control_MultiSplit(self, frame, name, node, in_port):
        branches = node.get("branches") or {}
        for _bn, fld in sorted(
                branches.items(),
                key=lambda kv: kv[1].get("index", 0) if isinstance(kv[1], dict) else 0):
            self._fire(frame, fld)

    def _ctrl_control_MultiMerge(self, frame, name, node, in_port):
        deps = node.get("dependencies") or {}
        arrived = frame.scratch.setdefault(name, set())
        arrived.add(in_port.split(".", 1)[1] if in_port.startswith("dependencies.")
                    else in_port)
        if arrived.issuperset(deps.keys()):
            self._fire(frame, node.get("done"))

    def _ctrl_control_Merge(self, frame, name, node, in_port):
        # OR-join: whichever incoming branch arrives continues via `done`.
        self._fire(frame, node.get("done"))

    # -- control.ForAccumulate: fired once per enclosing-loop iteration; appends the
    # pulled `value` to an accumulator read back (post-loop) via its `results` port.
    def _ctrl_control_ForAccumulate(self, frame, name, node, in_port):
        frame.scratch.setdefault(name, []).append(self._pull(frame, node.get("value")))
        self._fire(frame, node.get("done"))  # usually absent -> terminal accumulate

    def _data_control_ForAccumulate(self, frame, name, node, port):
        return frame.scratch.get(name, [])

    # ---- bytes ----
    @staticmethod
    def _as_bytes(v):
        if v is None:
            return b""
        if isinstance(v, bytes):
            return v
        if isinstance(v, (bytearray, list)):
            return bytes(v)
        if isinstance(v, str):
            return bytes.fromhex(v)
        raise TypeError(f"cannot coerce {type(v).__name__} to bytes")

    def _ctrl_bytes_AppendBytes(self, frame, name, node, in_port):
        base = self._as_bytes(self._pull(frame, node.get("input_bytes")))
        add = self._as_bytes(self._pull(frame, node.get("append_bytes")))
        frame.scratch[name] = base + add
        self._fire(frame, node.get("done"))

    def _data_bytes_AppendBytes(self, frame, name, node, port):
        return frame.scratch.get(name, b"")

    def _ctrl_bytes_GetChunkFromBytes(self, frame, name, node, in_port):
        data = self._as_bytes(self._pull(frame, node.get("input_bytes")))
        byte0 = int(self._pull(frame, node.get("byte_start_position")) or 0)
        bit0 = int(self._pull(frame, node.get("bit_start_position")) or 0)
        blen = int(self._pull(frame, node.get("bit_length")) or 0)
        # MSB-first big-endian bit extraction.
        val = 0
        for i in range(blen):
            ab = bit0 + i
            bi = byte0 + ab // 8
            bit = ((data[bi] >> (7 - ab % 8)) & 1) if bi < len(data) else 0
            val = (val << 1) | bit
        frame.scratch[name] = val
        self._fire(frame, node.get("done"))

    def _data_bytes_GetChunkFromBytes(self, frame, name, node, port):
        return frame.scratch.get(name, 0)

    def _data_bytes_EncodeToBytes(self, frame, name, node, port):
        return str(self._pull(frame, node["utf8_chars"])).encode("utf-8")

    def _data_bytes_DecodeToString(self, frame, name, node, port):
        return self._as_bytes(self._pull(frame, node["value"])).decode("utf-8", "replace")

    # ---- json / regex / sets / misc / types ----
    def _data_json_Dumps(self, frame, name, node, port):
        return json.dumps(self._pull(frame, node["json"]))

    def _data_json_Loads(self, frame, name, node, port):
        return json.loads(self._pull(frame, node["string"]))

    def _data_regex_Findall(self, frame, name, node, port):
        data = self._pull(frame, node["data"])
        return re.findall(self._pull(frame, node["pattern"]),
                          data if data is not None else "")

    def _data_sets_Intersection(self, frame, name, node, port):
        a = set(self._pull(frame, node["a"]) or [])
        return list(a & set(self._pull(frame, node["b"]) or []))

    def _data_sets_Difference(self, frame, name, node, port):
        a = set(self._pull(frame, node["a"]) or [])
        return list(a - set(self._pull(frame, node["b"]) or []))

    def _data_misc_SanitizeString(self, frame, name, node, port):
        allowed: set = set()
        if self._pull(frame, node.get("digits")):
            allowed |= set(string.digits)
        if self._pull(frame, node.get("ascii_letters")):
            allowed |= set(string.ascii_letters)
        if self._pull(frame, node.get("whitespace")):
            allowed |= set(string.whitespace)
        extra = self._pull(frame, node.get("allowed_chars"))
        if extra:
            allowed |= set(extra)
        text = str(self._pull(frame, node.get("input_text")) or "")
        if all(c in allowed for c in text):
            return text
        raise ValueError(
            self._pull(frame, node.get("value_error_message")) or "invalid string")

    # misc.WhitelistDict: keep only the keys a whitelist permits. Both a control
    # node (continues via `done`) and the data source for the filtered dict.
    def _whitelisted(self, frame, node) -> dict:
        data = self._pull(frame, node.get("input_data"))
        allowed = self._pull(frame, node.get("whitelist"))
        if not isinstance(data, dict):
            return {}
        keys = set(allowed if isinstance(allowed, (list, tuple, set, dict)) else ())
        return {k: v for k, v in data.items() if k in keys}

    def _ctrl_misc_WhitelistDict(self, frame, name, node, in_port):
        frame.scratch[name] = self._whitelisted(frame, node)
        self._fire(frame, node.get("done"))

    def _data_misc_WhitelistDict(self, frame, name, node, port):
        if name in frame.scratch:
            return frame.scratch[name]
        return self._whitelisted(frame, node)

    def _data_types_VariantToNumber(self, frame, name, node, port):
        v = self._pull(frame, node["value"])
        try:
            return int(v)
        except (TypeError, ValueError):
            return float(v)

    def _data_misc_DateTime(self, frame, name, node, port):
        # Output port `now`: an ISO-8601 timestamp string.
        return datetime.datetime.now().isoformat()

    def _data_misc_Uuid(self, frame, name, node, port):
        return str(uuid.uuid4())


def _print_proc_table(procs) -> None:
    """Human-readable --list output: a runnable flag, name, title, valid_states."""
    n_runnable = sum(1 for x in procs if x["runnable"])
    print(f"procedures: {len(procs)}  runnable-now: {n_runnable}")
    for x in procs:
        flag = "OK" if x["runnable"] else "--"
        title = x["title"] or ""
        line = f"  [{flag}] {x['name']:<44} {title}"
        if x["valid_states"]:
            line += f"  <{'|'.join(x['valid_states'])}>"
        print(line)
        if not x["runnable"] and x["missing_types"]:
            print(f"         missing: {', '.join(x['missing_types'])}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", type=Path, default=None,
                   help="bundle networks/ dir (default: config.ODIN_BUNDLE from "
                        "TM3_ROOT / TM3_ODIN_BUNDLE in .env)")
    p.add_argument("--procedure", default=DEFAULT_PROC,
                   help=f"graph basename to run (default {DEFAULT_PROC})")
    p.add_argument("--backend", choices=["mock", "bench"], default="mock")
    p.add_argument("--scenario", default="success",
                   choices=["success", "not-dyno", "speed-fail", "learn-fail"],
                   help="(mock) which choreography to script")
    p.add_argument("--channel", default=None, help="(bench) CAN channel (default: TM3_VEHICLE_CHANNEL)")
    p.add_argument("--interface", default=None,
                   help="(bench) python-can interface (default: TM3_INTERFACE)")
    p.add_argument("--list", action="store_true",
                   help="list entry procedures (with runnable status) instead of running one")
    p.add_argument("--runnable", action="store_true",
                   help="(with --list) list only procedures runnable now")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="print the node execution trace")

    import config as _cfg
    _cfg.apply_defaults(p)  # channel/interface from .env (TM3_VEHICLE_CHANNEL/TM3_INTERFACE)
    args = p.parse_args()

    import odin_service

    bundle = args.bundle or _cfg.ODIN_BUNDLE
    if bundle is None:
        p.error("no bundle: pass --bundle or set TM3_ROOT (or TM3_ODIN_BUNDLE) in .env")

    if args.list:
        try:
            procs = odin_service.list_procedures(bundle=bundle, runnable_only=args.runnable)
        except ValueError as e:
            p.error(str(e))
        if args.json:
            print(json.dumps(procs, indent=2, default=str))
        else:
            _print_proc_table(procs)
        return 0

    if args.backend == "bench" and not args.channel:
        p.error("--channel required for --backend bench")

    if not args.json:
        print(f"[odin_runner] {args.procedure}  backend={args.backend}"
              + (f" scenario={args.scenario}" if args.backend == "mock" else ""))
    try:
        result = odin_service.run_procedure(
            args.procedure, backend=args.backend, bundle=bundle,
            channel=args.channel, interface=args.interface,
            scenario=args.scenario, verbose=args.verbose)
    except ValueError as e:
        p.error(str(e))

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\nexit_code = {result['exit_code']}")
        print("metrics:")
        for m in result["metrics"]:
            print(f"  {m['metric']}: value={m['value']!r} rc={m['result_code']} "
                  f"expected={m['expected']!r}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
