#!/usr/bin/env python3
"""odin_runner.py -- minimal interpreter for ODIN node-graph procedures (Path B PoC).

Runs the REAL ODIN diagnostic graphs from the firmware bundle
(.../networks/Model3/{tasks,lib}/*.py) directly, instead of Tesla's frozen
odin-engine + web UI. Node-type inventory across the bundle is 181 types, but
only 5 modules touch hardware (uds / odx / can / cid / vehiclecontrols); the
other ~90% (networks / control / logic / dicts / reporting / ...) is pure
compute that runs unchanged. See docs/private/odin-resolver-cal-and-inverter-swap.md.

Execution model (as read out of the graphs):
  * CONTROL flow is push: a node's control-OUTPUT port (run/done/passed/if_true/
    save/capture/try_body/...) holds {'connection': 'target.inputport'}; firing it
    runs the target. networks.Enter.start is the entry; networks.Exit raises out
    of the run with an exit_code.
  * DATA flow is pull: an input field is {'connection': 'node.outputport'} (lazy)
    or {'value': X} (literal). Pulling a data port evaluates that node's data
    handler. 'connection' wins over a cached 'value'.
  * control.Split runs two branches concurrently (here: a thread for the infinite
    tester-present loop + the main sequence inline); Exit cancels the thread.

Hardware interop is behind a Backend seam:
  * MockBackend  -- scripts the choreography (dyno mode, gear D->N, axle speed,
                    RESOLVER_LEARNING result) so the graph runs with NO hardware.
                    This is what validates the interpreter.
  * BenchBackend -- (skeleton) uds/odx -> uds_local.UdsSession per ECU node;
                    can -> live-bus decode; cid -> a bench data provider. The
                    seams are marked; complete them on the bench.

Tesla's ODIN graph files are NOT vendored into this (public) repo. The bundle
path (and CAN channel/interface) are resolved from .env via config.py -- set
TM3_ROOT (the firmware extraction) and the bundle + TM3_VEHICLE_CHANNEL/TM3_INTERFACE are
derived automatically; --bundle / --channel override them.

Usage:
  python scripts/odin_runner.py --scenario success -v      # bundle from .env
  python scripts/odin_runner.py --bundle <…/networks> --scenario not-dyno
  python scripts/odin_runner.py --procedure Model3/tasks/PROC_DI_X_RESOLVER-LEARN
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import hashlib
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

DEFAULT_PROC = "Model3/tasks/PROC_DI_X_RESOLVER-LEARN"


# =====================================================================================
# graph loading
# =====================================================================================
def load_graph(bundle: Path, relbase: str) -> dict:
    """Load a bundle graph by its basename (e.g. 'Model3/lib/DI_RESOLVER_LEARNING')."""
    path = bundle / (relbase + ".py")
    ns: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)  # noqa: S102
    if "network" not in ns:
        raise ValueError(f"{path} has no top-level `network` dict")
    return ns["network"]


class GraphExit(Exception):
    """networks.Exit -- unwinds the current graph run with an exit code."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"exit {code}")


class ProcedureError(Exception):
    """Unsupported node type or malformed graph."""


class _BreakLoop(Exception):
    """control.Break -- unwinds to the nearest enclosing loop, which stops
    iterating and continues via its normal completion port."""


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
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class RunResult:
    exit_code: object
    metrics: list
    outputs: dict


# =====================================================================================
# CID emulation (cid.* nodes): a read-your-writes data-value store + a read-only
# view of a firmware-dump rootfs. See docs/private/odin-runner-plan.md (Step 5).
# =====================================================================================
# Sensible bench defaults for CID data-values graphs read back after (or without)
# a SetDataValue. Values are strings, matching the CID data-value wire type.
_CID_DEFAULTS = {
    "GUI_factoryMode": "false", "GUI_developerMode": "false",
    "GUI_diagnosticMode": "false", "GUI_tdsMode": "false",
    "GUI_serviceMode": "false", "GUI_isDelivered": "true",
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

    Maps a CID absolute path onto <root>/<path>, JAILED so it can never escape the
    root and never writes. Serves real data for static firmware content; paths
    absent from the dump (runtime state: logs, /var/etc, device nodes) read as
    empty / not-found -- which is what a freshly-flashed unit actually has.
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


# =====================================================================================
# backends (the hardware-interop seam)
# =====================================================================================
class Backend:
    """Interface the interop node handlers call. One impl per environment."""

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
        # can.ActiveAlerts: the alerts currently asserted on a bus. Decoding the
        # alert matrix off a live bus needs the alert DB; a bench returns none.
        return []

    def isotp_send(self, to_controller, from_controller, data, bus=None) -> bool:
        # isotp.Send: transmit a raw ISO-TP message with explicit tx/rx CAN IDs
        # (not a UDS service). No transport off a bench -> report success.
        return True

    # Power / application-state orchestration (vehiclecontrols.*). Default no-op:
    # a bench assumes the requested state already holds. Override to actually drive
    # it (e.g. vehicle_sim's LV/BMS via its HTTP control server).
    def ensure_power_state(self, state) -> None:
        pass

    def ensure_application_state(self, node_name: str, state) -> None:
        pass

    def store_outputs(self, outputs) -> None:
        # Persist a procedure's outputs to the interop data store, keyed by board id.
        # Default: no-op (offline / mock). BenchBackend overrides.
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
        # the firmware dump's binaries can't be run, so return canned success.
        return {"stdout": "", "stderr": "", "exit_status": 0}

    def cid_read_file(self, path, mode="r"):
        # proto.ReadFile: served from the firmware dump on a bench (None otherwise).
        return None


class MockBackend(Backend):
    """Scripts the resolver-learn choreography so the graph runs with no hardware.

    Scenarios: success | not-dyno | speed-fail | learn-fail. The mock advances
    VAPI_shiftState D->N when the ESP dyno routine (0xf00a) is started, mirroring
    the operator shifting during the real procedure.
    """

    def __init__(self, scenario: str = "success"):
        self.scenario = scenario
        self.gear = "D"
        self.traction = "Normal" if scenario == "not-dyno" else "Dyno"
        self.axle_speed = 100 if scenario == "speed-fail" else 600
        self.learn = "SPEED_RANGE" if scenario == "learn-fail" else "LEARN_SUCCESS"
        # read-your-writes store; gear/traction stay live via the derive callback.
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
        # The ESP 0xf00a routine is the dyno enable; simulate the operator having
        # shifted into Neutral by the time it starts.
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

    def start_routine(self, *_a, **_k): return b""
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
    """Real bench: uds/odx map to one uds_local.UdsSession per ECU node (cached,
    with a TesterPresent keep-alive), driven off the ODJ (NodeConfig) + odj_codec.

    NOTE: on a conversion there is no ESP module -- uds('ESP')/odx('ESP') return
    no-op stubs so the resolver-learn graph's brake/dyno choreography (routed via
    ESP) does not block. `can_read` decodes a live-bus RX cache (CanDatabase); the
    CID data-value store is read-your-writes; the CID filesystem is a read-only view
    of the firmware dump.
    """

    def __init__(self, channel: str, interface: str = "socketcan",
                 cid_values: dict | None = None, firmware_root=None, datastore=None):
        self.channel = channel
        self.interface = interface
        self._nodes: dict = {}   # NODE (upper) -> (NodeConfig, UdsSession)
        self._can: dict = {}     # channel -> (bus, notifier, _CanRxCache), lazy
        self._isotp: dict = {}   # (channel, tx, rx) -> (bus, notifier, transport), lazy
        self._datastore = datastore  # interop DataStore for stored outputs (lazy)
        self._bootloader: set = set()  # NODES currently held in their bootloader
        # CID data-value store (read-your-writes) + read-only firmware-dump FS.
        # firmware_root defaults to TM3_ROOT (config.ROOT); None => FS ops stub empty.
        self._cid = CidStore(seed=cid_values)
        if firmware_root is None:
            import config as _cfg
            firmware_root = _cfg.ROOT
        self._fs = CidFilesystem(firmware_root)

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
        """(NodeConfig, UdsSession) for a node -- the seam odin_service's DID helpers
        (list_dids/read_did/write_did) need. Reuses the cached per-node session."""
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

    # -- cid data-value store (read-your-writes; wire derive to the live bus later) --
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

    # -- live CAN: one RX cache per channel; can_read routes bus -> channel --
    def _can_cache(self, channel):
        if channel not in self._can:
            import can

            import config as _cfg
            from can_decoder import CanDatabase
            db = CanDatabase(str(_cfg.ETH_COMPACT)) if _cfg.ETH_COMPACT else CanDatabase()
            bus = can.Bus(interface=self.interface, channel=channel)
            notifier = can.Notifier(bus, [])
            cache = _CanRxCache(db)
            notifier.add_listener(cache)
            self._can[channel] = (bus, notifier, cache)
        return self._can[channel][2]

    def can_read(self, signal, bus=None):
        # Resolve the ODIN bus_name (ETH/VEH/PARTY/CH) to a configured channel;
        # unconfigured buses fall back to this backend's channel (vehicle bus).
        import config as _cfg
        channel = _cfg.can_channel(bus) or self.channel
        if channel is None:
            return None
        return self._can_cache(channel).get(signal)

    # -- raw ISO-TP send (isotp.Send). Reuses the py-uds transport the UDS stack is
    # built on, with explicit tx/rx CAN IDs (to_controller/from_controller) instead of
    # a node's configured pair -- so it needs its own transport (own bus+notifier, one
    # per (channel, tx, rx)) rather than a per-node UdsSession. --
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
        """Board serial (DID 0xF013) of the first node this run reached; None if
        unread. Keys stored outputs by the physical board (like the immobilizer)."""
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
        (vehiclecontrols.EnsureApplicationState). Reuses the SAME UdsSession bootloader
        handover as the flasher (dfu.py / flash_scripts step_ecu_reset +
        step_wait_for_bootloader): ecu_reset, then wait_for_bootloader floods
        TesterPresent through the reboot so the bootloader holds (update.img
        enter_bootloader_v0) -- that's what makes bootloader-context DIDs
        (STORE-DATA-BOOT package identity) readable. APPLICATION just resets: with no
        TP flood the bootloader boots on into the app. Tracked so an already-in-state
        node isn't needlessly reset; None / ESP are no-ops."""
        if not state or node_name.upper() == "ESP":
            return
        want_bl = "BOOT" in str(state).upper()
        key = node_name.upper()
        if want_bl == (key in self._bootloader):
            return  # already in the requested state
        _cfg, sess = self._node(node_name)
        sess.ecu_reset_no_wait(0x01)          # == flash step_ecu_reset
        if want_bl:
            sess.wait_for_bootloader()        # == flash step_wait_for_bootloader
            self._bootloader.add(key)
        else:
            self._bootloader.discard(key)     # reset boots on into the app

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
    def start_routine(self, *_a, **_k): return b""
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
        pass  # UdsSession runs its own keep-alive thread

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
        # dtc_mask is often a status name (e.g. 'TestFailed'); clear-all is the
        # safe generalization since the group defaults to 0xFFFFFF.
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
        """Run SecurityAccess for a routine/DID subspec's ODJ-declared security level.
        Tesla's odx layer does this IMPLICITLY -- these graphs carry no explicit
        uds.UdsSecurityAccess node before an odx routine, so a security-gated routine
        returns NRC 0x33 (securityAccessDenied) without it. The level comes straight
        from the ODJ (e.g. PMR CAN_COMM_SELF_TEST = 5); the ODJ carries no session, so
        we use the EXTENDED diagnostic session (0x03) -- where security-gated DIAGNOSTIC
        routines/DIDs run. The PROGRAMMING session (0x02) would push the ECU into a
        flash/bootloader mode that stops normal app comms -- which for a bus self-test
        reads as 'not connected' and starves the bus of ACKs (ENOBUFS). Flashing graphs
        set programming explicitly via uds.* nodes. Idempotent (re-request -> NRC 0x35)."""
        level = getattr(sub, "security_level", 0) if sub else 0
        if level:
            self.sess.diagnostic_session(0x03)  # extended diagnostic session
            self.sess.security_access(seed_level=level)

    def start_routine(self, routine, params=None):
        from uds_local.odj_codec import encode_request
        rt = self._routine(routine)
        self._auth(rt.start)
        return self.sess.routine_control(
            rt.hex_id, arg=encode_request(rt.start, params or {}), subtype=0x01)

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
        # Auth once before the start; the results polls reuse the unlocked session.
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
        from uds_local.odj_codec import decode_field  # noqa: F401  (kept for symmetry)
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


# =====================================================================================
# the engine
# =====================================================================================
class Engine:
    def __init__(self, backend: Backend, bundle: Path, verbose: bool = False,
                 time_scale: float = 0.0, loop_pause: float = 0.01, max_loops: int = 50,
                 on_event=None):
        self.backend = backend
        self.bundle = bundle
        self.verbose = verbose
        self._time_scale = time_scale   # 0 => don't actually sleep (fast mock)
        self._loop_pause = loop_pause
        self._max_loops = max_loops
        # Optional run-progress listener: on_event(kind, payload) is called for
        # 'trace' (node execution steps) and 'metric' (each captured metric) so a
        # CLI/web caller can stream progress instead of waiting silently. A listener
        # error never crashes the run. See scripts/odin_service.py.
        self._on_event = on_event

    # -- entry points --
    # A bare task-wrapper's entry node is one of these single subnet-call types (no
    # networks.Enter): its `inputs` bind to sibling networks.Input nodes in the wrapper
    # frame, then it invokes the referenced/script graph.
    _WRAPPER_ENTRY_TYPES = (
        "networks.RunReferencedSubnetwork",
        "scripts.RunScriptTest",
        "networks.DynamicallyReferencedSubnetwork",
    )

    def run_procedure(self, relbase: str) -> RunResult:
        result = self._run_child_graph(load_graph(self.bundle, relbase), {}, depth=0)
        with contextlib.suppress(Exception):
            self.backend.store_outputs(result.outputs)  # persist keyed by board id
        return result

    def _run_child_graph(self, graph: dict, inputs: dict, depth: int) -> RunResult:
        """Run a loaded graph regardless of form: a wired graph (has networks.Enter)
        runs directly; a bare task-wrapper (a single RunReferencedSubnetwork /
        RunScriptTest / DynamicallyReferencedSubnetwork node, no Enter) invokes its
        subnet in a wrapper frame so `<Input>.value` connections resolve."""
        if self._has(graph, "networks.Enter"):
            return self.run_graph(graph, inputs, depth=depth)
        tname = next((self._find_opt(graph, t) for t in self._WRAPPER_ENTRY_TYPES
                      if self._find_opt(graph, t)), None)
        if tname is None:
            return self.run_graph(graph, inputs, depth=depth)  # no Enter -> clear error
        frame = Frame(graph=graph, inputs=inputs, depth=depth)
        node = graph[tname]
        self._log(depth, f"task {tname} -> {self._resolve_basename(frame, node)}")
        return self._invoke_subnet(frame, tname, node)

    def run_graph(self, graph: dict, inputs: dict, depth: int = 0) -> RunResult:
        frame = Frame(graph=graph, inputs=inputs, depth=depth)
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
        """Materialize a graph's networks.Output ports (pulled from their source) into
        frame.outputs. SetOutput nodes have already written theirs during control flow;
        setdefault lets an imperative SetOutput win over a declarative Output of the
        same name. Each pull is best-effort: a graph that exited early may not have
        computed every source node."""
        for oname, onode in frame.graph.items():
            if isinstance(onode, dict) and onode.get("type") == "networks.Output":
                with contextlib.suppress(Exception):
                    frame.outputs.setdefault(oname, self._pull(frame, onode.get("port")))

    def _invoke_subnet(self, frame: Frame, name: str, node: dict) -> RunResult:
        """Run a referenced/script subnetwork: resolve its basename, bind its inputs by
        pulling each mapping (literal or connection) IN THE CALLER's frame, run the child
        graph in its own frame, and stash the RunResult. Child metrics bubble up so a
        single report spans the whole tree; child GraphExit does NOT unwind the caller.
        The child may itself be a wired graph or a bare task-wrapper."""
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
        the lifted inner nodes reference each other by bare `<inner>.<port>` (what
        run_graph expects). Parent-scope connections (which don't carry the prefix) are
        left untouched -- but inline subnets keep all inner refs prefixed."""
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

    # -- inline-subnet Slot/Signal plumbing (the alternative to Enter/Exit). Slot is the
    # inner entry: relays the parent's slot into the inner graph via `signal`. Signal is
    # the exit relay: a leaf whose firing ends its branch; the parent's `signals.exit` is
    # fired by _ctrl_networks_Subnetwork after the inner run completes.
    def _ctrl_networks_Slot(self, frame, name, node, in_port):
        self._fire(frame, node.get("signal"))

    def _ctrl_networks_Signal(self, frame, name, node, in_port):
        pass  # exit relay -- see _ctrl_networks_Subnetwork

    def _ctrl_networks_Cancelled(self, frame, name, node, in_port):
        # Registers a handler fired only on operator cancellation, which this runner
        # never raises mid-graph -> the `cancelled` branch is inert on a bench.
        pass

    # -- control/data plumbing --
    def _fire(self, frame: Frame, ctrl_field) -> None:
        if not ctrl_field or "connection" not in ctrl_field:
            return
        # Split on the FIRST dot: node names never contain '.', so the remainder is the
        # full port path -- 'run' for a normal node, 'slots.enter' into a subnetwork.
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

    # NOTE: comparator/operator enum is a best-guess (0:== 1:!= 2:< 3:<= 4:> 5:>=).
    # cid.GetDataValueUntil uses operator (0 seen = equals); can.* uses comparator
    # (4 seen on axleSpeed_>_560). Confirm on the bench if a compare misbehaves.
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
        """Push a run event (kind in {'trace','metric'}) to the optional on_event
        listener. A listener error must never crash the run."""
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

    # -- referenced subnetworks (THE keystone: 633/661 procs call another graph inline).
    # Entered via a <node>.slots.<slot> control connection; on child exit, fire the single
    # `exit` signal. Child outputs are read as data via <node>.outputs.<name>. Both the
    # inline ReferencedSubnetwork and RunReferencedSubnetwork forms share this shape.
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

    # -- scripts.RunScriptTest: run a networks/*/scripts/ graph. Same call shape as a
    # referenced subnetwork (basename = `script_name`, a bare string); as an inline node
    # it continues via `done`, as a bare-task entry it IS the wrapper's entry node.
    def _ctrl_scripts_RunScriptTest(self, frame, name, node, in_port):
        self._invoke_subnet(frame, name, node)
        sig = (node.get("signals") or {}).get("exit")
        if sig:
            self._fire(frame, sig)
        self._fire(frame, node.get("done"))

    _data_scripts_RunScriptTest = _data_networks_ReferencedSubnetwork

    # -- networks.DynamicallyReferencedSubnetwork: the target graph basename is resolved
    # at run time from `name` (a literal or a connection, e.g. ForEach.item). No slots;
    # continues via `done`. Child outputs (rare) read the same as a referenced subnet.
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
        while not frame.cancel.is_set():
            if cond != "True" and not self._pull(frame, node["condition"]):
                break
            if self._run_body(frame, node.get("run_body")):
                break
            i += 1
            if self._max_loops and i >= self._max_loops:
                break
            if frame.cancel.wait(self._loop_pause):
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
        # Typed catch (exception_class is an ODIN/Python class path). Tesla's names
        # (e.g. odin.core.uds.exceptions.UdsEcuError) don't map to our exception
        # classes, so we catch broadly -- matching the intent (swallow expected UDS/
        # ISO-TP errors during interop). GraphExit still propagates.
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
        # Ensure the power state (bench no-op by default), run the protected body,
        # then continue via `done`. `failure` is only for real power-acquisition
        # faults, which the bench never raises. Body may itself Exit the graph.
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

    def _ctrl_reporting_CaptureConnectorInfoLookup(self, frame, name, node, in_port):
        # The task-terminal node that ties a connector-info file to the procedure's
        # exit_code. The real connector-DB lookup needs Tesla's manufacturing data;
        # on a bench we record the exit_code + file as a metric and continue.
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
            if frame.cancel.is_set():
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

    def _ctrl_cid_SaveAuthoredPopup(self, frame, name, node, in_port):
        # Persist an authored popup blob into the CID data store (read-your-writes),
        # keyed by identifier; record success and continue.
        self.backend.cid_save(self._pull(frame, node.get("identifier")),
                              self._pull(frame, node.get("data")))
        frame.scratch[name] = {"success": True}
        self._fire(frame, node.get("done"))

    def _data_cid_SaveAuthoredPopup(self, frame, name, node, port):
        return frame.scratch.get(name, {}).get("success", True)

    # ---- messages.* (ODIN framework UI/IPC messages; bench = non-blocking) ----
    def _ctrl_messages_ProgressUpdate(self, frame, name, node, in_port):
        self._log(frame.depth, f"     progress {self._pull(frame, node.get('value'))}%")
        self._fire(frame, node.get("done"))

    def _ctrl_messages_StatusUpdate(self, frame, name, node, in_port):
        self._log(frame.depth, f"     status: {self._pull(frame, node.get('status'))!r}")
        self._fire(frame, node.get("done"))

    def _ctrl_messages_Listen(self, frame, name, node, in_port):
        # No ODIN message framework on a bench -> treat the awaited message as
        # received (fire done), non-blocking; fall back to timed_out if there's
        # no done port.
        self._fire(frame, node.get("done") or node.get("timed_out"))

    def _ctrl_messages_Send(self, frame, name, node, in_port):
        # Publish to the ODIN manufacturing-message bus (e.g. a brake-dyno controller).
        # No such bus on a bench -> fire-and-forget; continue via `done` when present
        # (some Send nodes are terminal, with the reply arriving via a later Listen).
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
            if frame.cancel.is_set():
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
            if frame.cancel.is_set():
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
            cancel=frame.cancel, time_scale=self._time_scale)
        frame.scratch.setdefault(name, {})["results"] = results
        self._log(frame.depth, f"     odx {routine}@{nn} -> {results}")
        self._fire(frame, node.get("done"))

    def _ctrl_odx_OdxStartAndWaitResults_V2(self, frame, name, node, in_port):
        # V2: takes an explicit diagnostic_session, max_runtime (seconds) and
        # success/failed control ports. Poll like the V1 node; the routine leaving
        # its in-progress set before the deadline is treated as success, a timeout
        # (or error) as failure. (The exact pass/fail status set isn't in the graph;
        # confirm on the bench for a routine whose terminal status can be "failed".)
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
                cancel=frame.cancel, time_scale=self._time_scale)
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
        # `dtcs` is a {dtc_code: status} dict so dicts.Keys / control.ForEachEntry
        # (the graph's downstream consumers) work; empty on a healthy ECU.
        frame.scratch[name] = {"dtcs": dtcs}
        self._fire(frame, node.get("done"))

    def _ctrl_uds_UdsTesterPresentContext(self, frame, name, node, in_port):
        # UdsSession keeps its own TesterPresent thread alive; just run the body.
        self.backend.uds(self._pull(frame, node["node_name"])).tester_present()
        self._fire(frame, node.get("body"))
        self._fire(frame, node.get("done"))

    # ---------------- data handlers ----------------
    def _data_networks_Input(self, frame, name, node, port):
        # A caller that binds this input to None means "unset" -> fall back to the
        # Input node's declared default (ODIN semantics; e.g. WRITE_DRIVE_TYPE's
        # pmr_power_ecu default 'VCLEFT' when the task passes None). Only a non-None
        # bound value overrides the default.
        v = frame.inputs.get(name)
        if v is not None:
            return v
        return self._pull(frame, node.get("default"))

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

    # ---- Step 6: cheap-logic + live-CAN data handlers ----
    def _data_dicts_FromInputs(self, frame, name, node, port):
        # Gather the single-letter input ports (a, b, c, ...) into a list, ordered
        # by letter. (Best-guess: the `out` port is a positional collection; the
        # only ambiguous case is all-dict inputs, which a merge would also fit.)
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

    # ================= Step 2: pure-logic + iteration handler batch =================
    # Data nodes return one value (the `port` arg is ignored unless a node exposes
    # several named outputs -- the iteration nodes below use it). Field/port names were
    # read out of the bundle (the inspection recipe), not guessed.

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
        # case enum best-guess 0:lower 1:upper 2:title 3:capitalize (confirm on bench).
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
            if frame.cancel.is_set():
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
            if frame.cancel.is_set():
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
            if frame.cancel.is_set():
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
            if frame.cancel.is_set():
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

    # -- fan-out / join. MultiSplit fires each named branch (index order); each branch
    # eventually fires MultiMerge.dependencies.<branch>. MultiMerge counts arrivals and
    # fires `done` once every dependency has arrived. Sequential execution is correct
    # here: the branches converge at the merge before the graph continues.
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
        # OR-join: whichever incoming branch (`first`/`second`) arrives continues via
        # `done`. In practice the branches are mutually exclusive (if/else convergence).
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
        # MSB-first big-endian bit extraction (endianness field is rare; big default --
        # confirm little-endian handling on the bench before trusting non-byte-aligned).
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

    def _data_types_VariantToNumber(self, frame, name, node, port):
        v = self._pull(frame, node["value"])
        try:
            return int(v)
        except (TypeError, ValueError):
            return float(v)

    def _data_misc_DateTime(self, frame, name, node, port):
        # Output port `now`: an ISO-8601 timestamp string (downstream consumers
        # format/concatenate it into reports).
        return datetime.datetime.now().isoformat()

    def _data_misc_Uuid(self, frame, name, node, port):
        return str(uuid.uuid4())


# =====================================================================================
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
