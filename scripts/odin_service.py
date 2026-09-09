#!/usr/bin/env python3
"""odin_service.py -- one import surface for driving ODIN from a CLI or web app.

Thin layer over the engine (odin_runner) + coverage (odin_coverage): it turns
"which procedures can I run and what are they" and "run this one, streaming
progress" into plain functions returning JSON-friendly data, so both the
odin_runner CLI and the tm3web call the SAME core.

  * list_procedures(...)  -> [{basename, name, title, principals, valid_states,
                               description, runnable, missing_types, has_dynamic}]
    Reuses odin_coverage's handled-set + transitive walk to decide runnable-now,
    and pulls the title/valid_states/principals off each entry proc's
    comments.TaskInfo node (bench preconditions + a human-readable picker).

  * run_procedure(basename, *, backend, ..., on_event) -> RunResult-as-dict
    Wraps Engine.run_procedure. on_event(kind, payload) streams the node trace
    ('trace'), each captured metric ('metric'), and a terminal 'done'/'error'
    event -- the runner's _log/CaptureMetric fed through a callback instead of
    stdout, so long procs report progress live.

The ODIN bundle is NOT vendored into this (public) repo; it resolves from
config.ODIN_BUNDLE (.env: TM3_ROOT / TM3_ODIN_BUNDLE) unless `bundle=` is passed.
"""

from __future__ import annotations

import collections
import contextlib
import os
from pathlib import Path

import odin_coverage
import odin_runner

DEFAULT_ENTRIES = "Model3/tasks"


# =====================================================================================
# bundle / TaskInfo helpers
# =====================================================================================
def _resolve_bundle(bundle) -> Path:
    """The networks/ bundle dir: the given `bundle`, else config.ODIN_BUNDLE."""
    if bundle is not None:
        return Path(bundle)
    import config

    if config.ODIN_BUNDLE is None:
        raise ValueError("no ODIN bundle: pass bundle= or set TM3_ROOT / TM3_ODIN_BUNDLE in .env")
    return Path(config.ODIN_BUNDLE)


def _load_network(path: Path) -> dict | None:
    """Exec a bundle graph file and return its top-level `network` dict (or None if
    it doesn't parse / has no dict network). Tolerant -- used only for TaskInfo."""
    ns: dict = {}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)  # noqa: S102
    except Exception:  # noqa: BLE001  (a bad entry file just has no TaskInfo)
        return None
    net = ns.get("network")
    return net if isinstance(net, dict) else None


def _task_info(network: dict) -> dict | None:
    """The graph's comments.TaskInfo node (title/valid_states/principals/...)."""
    for node in network.values():
        if isinstance(node, dict) and node.get("type") == "comments.TaskInfo":
            return node
    return None


def _unwrap(v):
    """TaskInfo fields are bare in the real bundle (title='...', valid_states=[...])
    but ODIN literals elsewhere wrap as {'value': X}; accept either."""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _proc_meta(info: dict | None) -> dict:
    """Extract the human/precondition fields off a comments.TaskInfo node."""
    if not info:
        return {"title": None, "principals": [], "valid_states": [], "description": None}
    title = _unwrap(info.get("title"))
    desc = _unwrap(info.get("description"))
    return {
        "title": title if isinstance(title, str) and title else None,
        "principals": _as_list(_unwrap(info.get("principals"))),
        "valid_states": _as_list(_unwrap(info.get("valid_states"))),
        "description": desc if isinstance(desc, str) and desc else None,
    }


# =====================================================================================
# discovery
# =====================================================================================
def list_procedures(
    *, bundle=None, entries: str = DEFAULT_ENTRIES, runnable_only: bool = True
) -> list[dict]:
    """Every entry procedure in <bundle>/<entries>, each annotated with whether the
    engine can run it now (all node types handled) and its TaskInfo metadata.

    runnable_only=True (default) returns just the runnable set -- the picker's
    happy path; False returns all procs with `runnable`/`missing_types` so a UI
    can show why a proc is blocked.
    """
    bundle = _resolve_bundle(bundle)
    handled = odin_coverage.handled_types()
    entry_dir = bundle / entries
    out: list[dict] = []
    for f in sorted(entry_dir.glob("*.py")):
        relbase = f"{entries}/{f.stem}"
        types: collections.Counter = collections.Counter()
        missing_files: set = set()
        dynamic: list = []
        odin_coverage.collect(bundle, relbase, set(), types, missing_files, dynamic)
        missing = sorted(t for t in types if t not in handled)
        runnable = not missing
        if runnable_only and not runnable:
            continue
        net = _load_network(f)
        meta = _proc_meta(_task_info(net) if net else None)
        out.append(
            {
                "basename": relbase,
                "name": f.stem,
                "runnable": runnable,
                "missing_types": missing,
                "has_dynamic": bool(dynamic),
                **meta,
            }
        )
    return out


# =====================================================================================
# requirements  ("what must be on the bus before this proc will run")
# =====================================================================================
_DYNAMIC = object()  # a connection-sourced field: its value is only known at run time

# CAN signal-read node types -> the "kind" label shown in the readout.
_CAN_READ_KINDS = {
    "can.CANSignalRead": "read",
    "can.CANSignalMonitor": "monitor",
    "can.CANSignalValueComparison": "compare",
}
# cid.* nodes that READ a named MCU data value (an environment dep, not the bus).
_CID_READ_DATANAME = ("cid.GetDataValue", "cid.GetDataValueUntil")


def _field_value(node: dict, key: str):
    """Static value of a node input field: the literal it carries, None if the field
    is absent/empty, or _DYNAMIC if it is purely a {'connection': ...} (runtime-sourced
    with no static default).

    Many ODIN fields carry BOTH a 'connection' and a 'value': the connection is the
    runtime source, 'value' the declared default used when that connection resolves to
    None (the networks.Input None->default fallback). We prefer that default -- it's a
    useful, usually-correct static hint (e.g. a UDS node_name defaulting to 'PMR') --
    so only a connection with NO default reads as _DYNAMIC. ODIN literals wrap as
    {'value': X}; a few fields are bare (accepted as-is, mirroring _unwrap/_pull)."""
    fld = node.get(key)
    if isinstance(fld, dict):
        if "value" in fld:
            return fld["value"]
        if "connection" in fld:
            return _DYNAMIC
        return None
    return fld  # bare literal (or None)


def _lit_str(node: dict, key: str) -> str | None:
    """The field's value only if it's a non-empty literal string, else None."""
    v = _field_value(node, key)
    return v if isinstance(v, str) and v else None


def procedure_requirements(basename: str, *, bundle=None) -> dict:
    """Statically list what an ODIN procedure expects to be present before it runs.

    Transitive over the proc's whole graph (the same referenced/inline-subnet descent
    odin_coverage.collect uses), it gathers:
      * signals -- CAN signals the proc READS, grouped by bus token, each with a
        kind (read/monitor/compare).
      * alerts  -- alert buses/prefixes it inspects (can.ActiveAlerts).
      * nodes   -- ECU node_name(s) it does UDS to (odx.*/uds.*/EnsureApplicationState).
      * preconditions -- valid_states (off the entry TaskInfo), the ensured
        application_state / power_state, and MCU data-value deps (cid_values).
      * dynamic_count -- CAN reads whose signal name is connection-sourced with no
        declared default (only known at run time), so the signal list is a LOWER BOUND.

    Bench use: a PMR/DIR bench has no gateway, so a proc that reads e.g.
    'GTW_drivetrainType' hangs unless the operator provides it (vehicle_sim or a real
    ECU). This readout says exactly which signals to put on the bus first.
    """
    import config

    bundle = _resolve_bundle(bundle)
    entry = bundle / (basename + ".py")
    if not entry.exists():
        raise FileNotFoundError(f"no such procedure: {basename}")
    default_bus = config.canonical_bus(None)  # absent/dynamic bus -> vehicle backbone

    signals: dict[str, list] = {}
    seen_sig: set = set()          # (bus, signal, kind) de-dupe
    alerts: list = []
    seen_alert: set = set()
    nodes: set = set()             # UDS target ECU node_names
    cid_values: set = set()
    app_states: list = []
    power_states: list = []
    counters = {"dynamic": 0}      # CAN reads with a connection-sourced signal name

    def visit(name, node, relbase):
        t = node.get("type")
        if t in _CAN_READ_KINDS:
            sig = _lit_str(node, "signal_name")
            if sig is None:
                counters["dynamic"] += 1   # connection-sourced (or missing) signal name
                return
            bus = _lit_str(node, "bus_name") or default_bus
            key = (bus, sig, _CAN_READ_KINDS[t])
            if key not in seen_sig:
                seen_sig.add(key)
                signals.setdefault(bus, []).append({"signal": sig, "kind": _CAN_READ_KINDS[t]})
        elif t == "can.ActiveAlerts":
            key = (_lit_str(node, "bus_name"), _lit_str(node, "prefix"))
            if key not in seen_alert:
                seen_alert.add(key)
                alerts.append({"bus": key[0], "prefix": key[1]})
        elif t and (t.startswith("odx.") or t.startswith("uds.")):
            nn = _lit_str(node, "node_name")
            if nn:
                nodes.add(nn)
        elif t == "vehiclecontrols.EnsureApplicationState":
            nn = _lit_str(node, "node_name")
            if nn:
                nodes.add(nn)
            st = _lit_str(node, "application_state")
            if st and st not in app_states:
                app_states.append(st)
        elif t in ("vehiclecontrols.PowerContext", "vehiclecontrols.EnsurePowerState"):
            st = _lit_str(node, "power_state")
            if st and st not in power_states:
                power_states.append(st)
        elif t in _CID_READ_DATANAME:
            dn = _lit_str(node, "data_name")
            if dn:
                cid_values.add(dn)
        elif t == "cid.ListDataValues":
            dv = _field_value(node, "dv")
            for n in dv if isinstance(dv, (list, tuple)) else []:
                if isinstance(n, str) and n:
                    cid_values.add(n)

    odin_coverage.collect(bundle, basename, set(), collections.Counter(), set(), [], visit=visit)

    net = _load_network(entry)
    valid_states = _proc_meta(_task_info(net) if net else None)["valid_states"]
    for lst in signals.values():
        lst.sort(key=lambda s: (s["signal"], s["kind"]))

    return {
        "basename": basename,
        "signals": signals,
        "alerts": alerts,
        "nodes": sorted(nodes),
        "preconditions": {
            "valid_states": valid_states,
            "application_state": app_states[0] if app_states else None,
            "power_state": power_states[0] if power_states else None,
            "cid_values": sorted(cid_values),
        },
        "dynamic_count": counters["dynamic"],
    }


# =====================================================================================
# run
# =====================================================================================
def _resolve_backend(backend, *, scenario, channel, interface):
    """Return (backend_instance, we_created_it). Accepts a Backend instance (used
    as-is, caller owns it) or a string 'mock'/'bench' (built with .env defaults)."""
    if isinstance(backend, odin_runner.Backend):
        return backend, False
    key = str(backend).lower()
    if key == "mock":
        return odin_runner.MockBackend(scenario), True
    if key == "bench":
        import config

        ch = channel or config.VEHICLE_CHANNEL
        if not ch:
            raise ValueError(
                "bench backend needs a CAN channel (pass channel= or set TM3_VEHICLE_CHANNEL)"
            )
        iface = interface or os.environ.get("TM3_INTERFACE") or "socketcan"
        return odin_runner.BenchBackend(ch, iface), True
    raise ValueError(f"unknown backend {backend!r}: use 'mock', 'bench', or a Backend instance")


def _result_dict(basename: str, result: odin_runner.RunResult) -> dict:
    return {
        "basename": basename,
        "exit_code": result.exit_code,
        "passed": result.exit_code == 0,
        "metrics": result.metrics,
        "outputs": result.outputs,
    }


def run_procedure(
    basename: str,
    *,
    backend="mock",
    bundle=None,
    channel=None,
    interface=None,
    scenario: str = "success",
    on_event=None,
    verbose: bool = False,
    time_scale=None,
) -> dict:
    """Run one ODIN procedure and return its RunResult as a JSON-friendly dict
    ({basename, exit_code, passed, metrics, outputs}).

    backend: a odin_runner.Backend instance, or 'mock'/'bench'. A bench backend
    talks real UDS/CAN (needs channel); mock scripts the resolver-learn
    choreography offline. on_event(kind, payload) streams 'trace'/'metric' events
    during the run and a final 'done' (or 'error') event. time_scale defaults to
    real timings on the bench and instant (no sleeps) otherwise.
    """
    bundle = _resolve_bundle(bundle)
    be, owns = _resolve_backend(backend, scenario=scenario, channel=channel, interface=interface)
    if time_scale is None:
        time_scale = 1.0 if isinstance(be, odin_runner.BenchBackend) else 0.0
    eng = odin_runner.Engine(be, bundle, verbose=verbose, time_scale=time_scale, on_event=on_event)
    try:
        result = eng.run_procedure(basename)
    except Exception as e:  # noqa: BLE001  (surface the failure to a streaming caller)
        if on_event is not None:
            with contextlib.suppress(Exception):
                on_event("error", {"basename": basename, "error": str(e)})
        raise
    finally:
        if owns:
            getattr(be, "close", lambda: None)()
    out = _result_dict(basename, result)
    if on_event is not None:
        with contextlib.suppress(Exception):
            on_event("done", out)
    return out


# =====================================================================================
# DID read / write (0x22 / 0x2E)
# =====================================================================================
# The non-interactive core of tm3diag's _did_menu / _did_write_menu: resolve a DID
# name-or-id off the node's ODJ (NodeConfig.dids), decode a read response and encode
# a write payload via odj_codec (the SAME table-driven codec the ODIN runner's odx.*
# nodes use), and run SecurityAccess when the DID's subspec demands a level. Both the
# terminal menus and the coming tm3web/CLI DID surface call these, so there is one
# encode/decode path (no hand-packed duplicate). Functions take an already-opened
# (sess, cfg): sess is a uds_local.UdsSession (or any object with read_did/write_did/
# diagnostic_session/security_access), cfg a NodeConfig.
def _resolve_did(cfg, name_or_id):
    """Resolve a DID name or id (int, '0xNNNN', or decimal str) to
    (name, OdjEntry|None, did_id). entry is None for a raw id not in the node's ODJ."""
    if isinstance(name_or_id, int):
        did_id = name_or_id
    else:
        s = str(name_or_id).strip()
        if s in cfg.dids:
            e = cfg.dids[s]
            return s, e, e.hex_id
        try:
            did_id = int(s, 16) if s.lower().startswith("0x") else int(s, 0)
        except ValueError as exc:
            raise KeyError(f"unknown DID {name_or_id!r} for node {cfg.name}") from exc
    for n, e in cfg.dids.items():
        if e.hex_id == did_id:
            return n, e, did_id
    return f"0x{did_id:04X}", None, did_id


def _apply_did_security(sess, level) -> None:
    """Enter programming session + run SecurityAccess for a DID's required level."""
    if level:
        from uds_local.client import _SESSION_PROGRAMMING

        sess.diagnostic_session(_SESSION_PROGRAMMING)
        sess.security_access(seed_level=level)


def _did_meta(name, entry, sub) -> dict:
    """Picker-friendly metadata for one DID subspec (read.output / write.input)."""
    return {
        "name": name,
        "id": entry.hex_id,
        "hex_id": f"0x{entry.hex_id:04X}",
        "size": (sub.output_size if sub is entry.read else sub.input_size),
        "security_level": sub.security_level,
        "fields": list((sub.output if sub is entry.read else sub.input).keys()),
    }


def list_dids(cfg) -> dict:
    """The node's readable + writable DIDs as {'read': [...], 'write': [...]} lists of
    metadata dicts (name, id, hex_id, size, security_level, fields), name-sorted."""
    read, write = [], []
    for name, e in sorted(cfg.dids.items()):
        if e.read is not None:
            read.append(_did_meta(name, e, e.read))
        if e.write is not None:
            write.append(_did_meta(name, e, e.write))
    return {"read": read, "write": write}


def read_did(sess, cfg, name_or_id, *, parsed: bool = True, security: bool = True) -> dict:
    """ReadDataByIdentifier (0x22): resolve the DID, run SecurityAccess if its read
    subspec needs a level, read, and decode per the ODJ FieldSpecs.

    Returns {name, id, hex_id, raw (hex str), fields ({field: value})}. `fields` is
    empty for a raw id with no ODJ read subspec (the caller still gets `raw`).
    parsed=True applies enum maps (raw number -> enum name); False keeps raw numbers.
    """
    from uds_local.odj_codec import decode_response

    name, entry, did_id = _resolve_did(cfg, name_or_id)
    sub = entry.read if entry else None
    if security and sub is not None:
        _apply_did_security(sess, sub.security_level)
    raw = bytes(sess.read_did(did_id))
    return {
        "name": name,
        "id": did_id,
        "hex_id": f"0x{did_id:04X}",
        "raw": raw.hex(),
        "fields": decode_response(sub, raw, parsed=parsed),
    }


def _encode_write(entry, values) -> bytes:
    """Encode a write payload from a {field: value} dict (via odj_codec, enum names
    accepted) or pass raw bytes / a hex string straight through."""
    from uds_local.odj_codec import encode_request

    if isinstance(values, (bytes, bytearray)):
        return bytes(values)
    if isinstance(values, str):
        return bytes.fromhex(values.replace(" ", ""))
    return encode_request(entry.write if entry else None, values or {})


def encode_did_write(cfg, name_or_id, values) -> tuple:
    """Build the write payload for a DID -> (name, did_id, bytes), without sending.
    Exposed so a caller can show/confirm the exact bytes before the write goes out."""
    name, entry, did_id = _resolve_did(cfg, name_or_id)
    return name, did_id, _encode_write(entry, values)


def write_did(sess, cfg, name_or_id, values, *, security: bool = True) -> dict:
    """WriteDataByIdentifier (0x2E): encode `values` (see encode_did_write), run
    SecurityAccess if the write subspec needs a level, and write. Returns
    {name, id, hex_id, bytes (hex str), size}."""
    name, entry, did_id = _resolve_did(cfg, name_or_id)
    data = _encode_write(entry, values)
    sub = entry.write if entry else None
    if security and sub is not None:
        _apply_did_security(sess, sub.security_level)
    sess.write_did(did_id, data)
    return {
        "name": name,
        "id": did_id,
        "hex_id": f"0x{did_id:04X}",
        "bytes": data.hex(),
        "size": len(data),
    }
