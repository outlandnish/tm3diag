#!/usr/bin/env python3
"""odin_service.py -- one import surface for driving ODIN from a CLI or web app.

Thin layer over the engine (odin_runner) + coverage (odin_coverage) returning
JSON-friendly data, so the odin_runner CLI and tm3web share one core.

  * list_procedures(...)  -> [{basename, name, title, principals, valid_states,
                               description, user_facing_impact, additional_info,
                               gtw_diag_level, cancelable, post_fusing_allowed,
                               runnable, missing_types, has_dynamic}]
    Uses odin_coverage to decide runnable-now; pulls metadata off each entry proc's
    comments.TaskInfo node.

  * run_procedure(basename, *, backend, ..., on_event) -> RunResult-as-dict
    Wraps Engine.run_procedure. on_event(kind, payload) streams 'trace' / 'metric'
    events and a terminal 'done'/'error'.

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
import odin_script_api

DEFAULT_ENTRIES = "Model3/tasks"


# bundle / TaskInfo helpers
def _resolve_bundle(bundle) -> Path:
    """The networks/ bundle dir: the given `bundle`, else config.ODIN_BUNDLE."""
    if bundle is not None:
        return Path(bundle)
    import config

    if config.ODIN_BUNDLE is None:
        raise ValueError("no ODIN bundle: pass bundle= or set TM3_ROOT / TM3_ODIN_BUNDLE in .env")
    return Path(config.ODIN_BUNDLE)


def _load_network(path: Path) -> dict | None:
    """Exec a bundle graph file and return its top-level `network` dict (or None)."""
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
        return {"title": None, "principals": [], "valid_states": [], "description": None,
                "user_facing_impact": None, "additional_info": None,
                "gtw_diag_level": [], "cancelable": None, "post_fusing_allowed": None}
    title = _unwrap(info.get("title"))
    desc = _unwrap(info.get("description"))
    impact = _unwrap(info.get("user_facing_impact"))
    extra = _unwrap(info.get("additional_info"))
    cancelable = _unwrap(info.get("cancelable"))
    post_fusing = _unwrap(info.get("post_fusing_allowed"))
    return {
        "title": title if isinstance(title, str) and title else None,
        "principals": _as_list(_unwrap(info.get("principals"))),
        "valid_states": _as_list(_unwrap(info.get("valid_states"))),
        "description": desc if isinstance(desc, str) and desc else None,
        # The operator-facing "what will physically happen" warning (e.g. "Axles
        # and wheels will physically rotate"); shown in the picker before a run.
        "user_facing_impact": impact if isinstance(impact, str) and impact else None,
        # Free-form extra note some procedures carry (e.g. "Returns pass if target
        # is fused."); shown alongside the description.
        "additional_info": extra if isinstance(extra, str) and extra else None,
        # Bench preconditions/flags some procedures declare. `gtw_diag_level` is the
        # gateway diagnostic level(s) the proc needs; the two bools are None when the
        # proc doesn't declare them (so the UI can tell "not set" from "false").
        "gtw_diag_level": _as_list(_unwrap(info.get("gtw_diag_level"))),
        "cancelable": bool(cancelable) if cancelable is not None else None,
        "post_fusing_allowed": bool(post_fusing) if post_fusing is not None else None,
    }


# discovery
# Trees a vehicle LAYERS ON, rather than vehicles in their own right. Measured,
# not assumed: Model3's own tasks reference Gen3/ 432 times and Common/ 84, so a
# vehicle tree is a thin specialisation over these. Anything else carrying
# tasks/ is a vehicle. If a bundle has neither, a vehicle simply stands alone.
_SHARED_TREES = ("Gen3", "Common")
# Carries entries but is not a car and is not layered onto one: 6 sample graphs.
_NOT_A_CAR = (*_SHARED_TREES, "Tutorials")


def list_trees(*, bundle=None) -> list[str]:
    """Every tree in the bundle holding entry procedures.

    A tree qualifies by having a ``tasks/`` dir; one with only ``lib/`` is
    pulled in by reference and is never an entry point. On 2022.45.15:
    ``Common 212, Gen3 625, Model3 569, ModelY 49, Tutorials 6``.
    """
    bundle = _resolve_bundle(bundle)
    return sorted(p.name for p in bundle.iterdir()
                  if p.is_dir() and (p / "tasks").is_dir())


def list_vehicles(*, bundle=None) -> list[str]:
    """The trees that name a car, e.g. ``["Model3", "ModelY"]``."""
    return [t for t in list_trees(bundle=bundle) if t not in _NOT_A_CAR]


def trees_for(vehicle: str, *, bundle=None) -> list[str]:
    """The trees a vehicle draws procedures from, most specific first."""
    present = set(list_trees(bundle=bundle))
    return [vehicle] + [t for t in _SHARED_TREES if t in present]


def default_vehicle(*, bundle=None) -> str:
    """The vehicle to show first: TM3_PRODUCT when the bundle has it, else
    Model3, else whatever comes first."""
    try:
        found = list_vehicles(bundle=bundle)
    except (ValueError, OSError):
        return DEFAULT_ENTRIES.split("/")[0]
    import config
    for cand in (getattr(config, "PRODUCT", None), "Model3"):
        if cand and cand in found:
            return cand
    return found[0] if found else DEFAULT_ENTRIES.split("/")[0]


def list_procedures_for(vehicle: str, *, bundle=None,
                        runnable_only: bool = True) -> list[dict]:
    """Every procedure ``vehicle`` can run: its own tree plus the shared ones.

    A task NAME appearing in more than one tree resolves to the most specific --
    Model3 and Gen3 share 217 names on 2022.45.15 and 172 of them DIFFER, so
    which one wins is not cosmetic. Each entry carries the ``tree`` it came from.

    Runnability is filtered AFTER that choice, so a blocked procedure does not
    silently fall through to a different tree's version of the same name.
    """
    seen: dict[str, dict] = {}
    for i, tree in enumerate(trees_for(vehicle, bundle=bundle)):
        try:
            procs = list_procedures(bundle=bundle, entries=f"{tree}/tasks",
                                    runnable_only=False)
        except FileNotFoundError:
            # The SHARED trees are optional -- a bundle need not carry Gen3 or
            # Common. The car's own tree (first) is not: without this, an unknown
            # vehicle would quietly return the shared procedures instead of
            # saying it does not exist.
            if i == 0:
                raise
            continue
        for p in procs:
            seen.setdefault(p["name"], {**p, "tree": tree})
    out = sorted(seen.values(), key=lambda p: p["name"])
    return [p for p in out if p["runnable"]] if runnable_only else out


def list_procedures(
    *, bundle=None, entries: str = DEFAULT_ENTRIES, runnable_only: bool = True
) -> list[dict]:
    """Every entry procedure in <bundle>/<entries>, annotated with whether the engine can
    run it now (all node types handled) and its TaskInfo metadata.

    runnable_only=True (default) returns just the runnable set; False returns all procs
    with `runnable`/`missing_types`.
    """
    bundle = _resolve_bundle(bundle)
    handled = odin_coverage.handled_types()
    entry_dir = bundle / entries
    if not entry_dir.is_dir():
        # Globbing a missing dir yields nothing, so a typo (or a platform this
        # bundle does not carry) would read as "no procedures here" instead of
        # "wrong place".
        raise FileNotFoundError(f"no entries dir in the bundle: {entries}")
    out: list[dict] = []
    for f in sorted(entry_dir.glob("*.py")):
        relbase = f"{entries}/{f.stem}"
        types: collections.Counter = collections.Counter()
        missing_files: set = set()
        dynamic: list = []
        flashes = []

        def note_flash(source, _relbase, _inputs, _found=flashes):
            # Riding the same descent the coverage counter uses: a procedure
            # that reaches the MCU's flasher writes firmware, and the UI has to
            # know that BEFORE offering to run it.
            if "smashclicker" in source:
                _found.append(True)

        odin_coverage.collect(bundle, relbase, set(), types, missing_files, dynamic,
                              script_visit=note_flash)
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
                "flashes": bool(flashes),
                **meta,
            }
        )
    return out


# requirements: what must be on the bus before this proc will run
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
    """Static value of a node input field: the literal it carries, None if absent/empty,
    or _DYNAMIC if it is purely a {'connection': ...} with no static default.

    A field with both 'connection' and 'value' uses the 'value' as the static default
    (the networks.Input None->default fallback). Literals wrap as {'value': X}; bare
    fields are accepted as-is."""
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


def _resolved_str(graph: dict, node: dict, key: str, inputs: dict) -> str | None:
    """`_lit_str`, but following resolvable connections first (see
    odin_coverage.resolve_field, which the descent itself uses too)."""
    v = odin_coverage.resolve_field(graph, node.get(key), inputs)
    return v if isinstance(v, str) and v else None


def flash_targets(basename: str, *, bundle=None) -> dict | None:
    """What this procedure would FLASH, or None if it flashes nothing.

    The 55 UPDATE_* procedures all end at Gen3/scripts/UPDATE_MODULE, which
    shells out to the MCU's flasher with the component lists their task bound:
    UPDATE_PMR binds update_list ['pmr','dir'], hwidacq_list ['pmr'],
    node_to_lock 'PMR'. Reading them statically is what lets a caller show the
    operator what is about to be written BEFORE the run starts, rather than
    announcing it once the first image is already going down the wire.
    """
    bundle = _resolve_bundle(bundle)
    if not (bundle / (basename + ".py")).exists():
        raise FileNotFoundError(f"no such procedure: {basename}")
    found: dict = {}

    def script_visit(source, relbase, inputs):
        # UPDATE_MODULE is the only script that drives the flasher; it takes the
        # lists as parameters, so the caller's bindings ARE the answer.
        if "smashclicker" not in source or found:
            return
        update = inputs.get("update_component_list")
        if not update:
            return
        found.update({
            "script": relbase,
            "update": [str(c) for c in update],
            "hwidacq": [str(c) for c in (inputs.get("hwidacq_component_list") or [])],
            "node_to_lock": inputs.get("node_to_lock"),
            "power_state": inputs.get("power_state"),
            "bootloader_update": bool(inputs.get("bootloader_update")),
        })

    odin_coverage.collect(bundle, basename, set(), collections.Counter(), set(), [],
                          script_visit=script_visit)
    return found or None


def procedure_requirements(basename: str, *, bundle=None) -> dict:
    """Statically list what an ODIN procedure expects on the bus before it runs, transitive
    over the proc's whole graph:
      * signals -- CAN signals the proc READS, grouped by bus token, each with a kind
        (read/monitor/compare).
      * alerts  -- alert buses/prefixes it inspects (can.ActiveAlerts).
      * nodes   -- ECU node_name(s) it does UDS to (odx.*/uds.*/EnsureApplicationState).
      * preconditions -- valid_states (off the entry TaskInfo), the ensured
        application_state / power_state, and MCU data-value deps (cid_values).
      * dynamic_count -- CAN reads whose signal name is connection-sourced with no declared
        default, so the signal list is a LOWER BOUND.
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

    def visit(name, node, relbase, graph, inputs):
        t = node.get("type")

        def lit(key):
            """The field's static value, following a resolvable connection (and
            the literal the caller bound) before falling back to its default."""
            return _resolved_str(graph, node, key, inputs)

        if t in _CAN_READ_KINDS:
            sig = lit("signal_name")
            if sig is None:
                counters["dynamic"] += 1   # a loop item / run-time variable
                return
            bus = lit("bus_name") or default_bus
            key = (bus, sig, _CAN_READ_KINDS[t])
            if key not in seen_sig:
                seen_sig.add(key)
                signals.setdefault(bus, []).append({"signal": sig, "kind": _CAN_READ_KINDS[t]})
        elif t == "can.ActiveAlerts":
            key = (lit("bus_name"), lit("prefix"))
            if key not in seen_alert:
                seen_alert.add(key)
                alerts.append({"bus": key[0], "prefix": key[1]})
        elif t and (t.startswith("odx.") or t.startswith("uds.")):
            nn = lit("node_name")
            if nn:
                nodes.add(nn)
        elif t == "vehiclecontrols.EnsureApplicationState":
            nn = lit("node_name")
            if nn:
                nodes.add(nn)
            st = lit("application_state")
            if st and st not in app_states:
                app_states.append(st)
        elif t in ("vehiclecontrols.PowerContext", "vehiclecontrols.EnsurePowerState"):
            st = lit("power_state")
            if st and st not in power_states:
                power_states.append(st)
        elif t in _CID_READ_DATANAME:
            dn = lit("data_name")
            if dn:
                cid_values.add(dn)
        elif t == "cid.ListDataValues":
            dv = _field_value(node, "dv")
            for n in dv if isinstance(dv, (list, tuple)) else []:
                if isinstance(n, str) and n:
                    cid_values.add(n)

    def script_visit(source, relbase, inputs):
        """Same gathering for a NATIVE SCRIPT in the descent: it has no nodes, so
        its requirements come from the api calls it makes (odin_script_api),
        read against the literals its caller bound."""
        req = odin_script_api.script_requirements(source, inputs)
        for sig, bus, kind in req["signals"]:
            bus = bus or default_bus
            if (bus, sig, kind) not in seen_sig:
                seen_sig.add((bus, sig, kind))
                signals.setdefault(bus, []).append({"signal": sig, "kind": kind})
        for bus, prefix in req["alerts"]:
            if (bus, prefix) not in seen_alert:
                seen_alert.add((bus, prefix))
                alerts.append({"bus": bus, "prefix": prefix})
        nodes.update(req["nodes"])
        cid_values.update(req["cid_values"])
        for st in req["app_states"]:
            if st not in app_states:
                app_states.append(st)
        for st in req["power_states"]:
            if st not in power_states:
                power_states.append(st)
        counters["dynamic"] += req["dynamic"]

    odin_coverage.collect(bundle, basename, set(), collections.Counter(), set(), [],
                          visit=visit, script_visit=script_visit)

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


# run
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


def flash_preflight(
    basename: str, *, backend="mock", bundle=None, channel=None, interface=None,
    scenario: str = "success", conditions=None, allow_flash=None,
    include_bootloaders=None, ramapps=None,
) -> dict:
    """Resolve what `basename` would flash WITHOUT writing anything.

    Reads the ECU's identity and matches it against the signed metadata, so the
    operator sees the actual images -- and the car config that chose them --
    before the run starts. `flashes: False` means the procedure touches no
    firmware and needs no preflight at all.

    `allow_flash` declares whether this bench is armed. It is reported, never
    acted on -- nothing here writes -- but the preview must answer for the SAME
    arming the run will use, or it reports an unarmed bench for an armed one and
    the operator can never confirm. When `backend` is a name rather than an
    instance, this is the only place that arming can be applied at all.
    """
    bundle = _resolve_bundle(bundle)
    targets = flash_targets(basename, bundle=bundle)
    if targets is None:
        return {"basename": basename, "flashes": False}

    be, owns = _resolve_backend(backend, scenario=scenario, channel=channel,
                               interface=interface)
    try:
        if conditions is not None and hasattr(be, "conditions"):
            be.conditions = dict(conditions)
        if allow_flash is not None and hasattr(be, "allow_flash"):
            be.allow_flash = bool(allow_flash)
        if include_bootloaders is not None and hasattr(be, "include_bootloaders"):
            be.include_bootloaders = bool(include_bootloaders)
        if ramapps is not None and hasattr(be, "ramapps"):
            be.ramapps = str(ramapps)
        plan = be.flash_preview(update=targets["update"],
                                hwidacq=targets["hwidacq"])
    finally:
        if owns:
            with contextlib.suppress(Exception):
                be.close()
    return {"basename": basename, "flashes": True, **targets, **plan}


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
    cid_values=None,
    on_engine=None,
) -> dict:
    """Run one ODIN procedure and return its RunResult as a JSON-friendly dict
    ({basename, exit_code, passed, metrics, outputs}).

    backend: a odin_runner.Backend instance, or 'mock'/'bench' (bench needs channel).
    on_event(kind, payload) streams 'trace'/'metric' events and a final 'done'/'error'.
    time_scale defaults to real timings on the bench, instant otherwise.

    cid_values seeds the CID data-value store before the run -- what the bus cannot
    tell us (e.g. GUI_isFused for cid.IsFused).

    on_engine(engine) is called once with the live Engine before it starts, so a
    caller on another thread can stop it (engine.request_cancel).
    """
    bundle = _resolve_bundle(bundle)
    be, owns = _resolve_backend(backend, scenario=scenario, channel=channel, interface=interface)
    for key, value in (cid_values or {}).items():
        be.cid_set(key, value)
    if time_scale is None:
        time_scale = 1.0 if isinstance(be, odin_runner.BenchBackend) else 0.0
    eng = odin_runner.Engine(be, bundle, verbose=verbose, time_scale=time_scale, on_event=on_event)
    if on_engine is not None:
        on_engine(eng)          # hand the caller a cancel handle for this run
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


# DID read / write (0x22 / 0x2E)
# Resolve a DID name-or-id off the node's ODJ (NodeConfig.dids), decode/encode via
# odj_codec, and run SecurityAccess when the DID's subspec demands a level. Functions
# take an already-opened (sess, cfg): sess a uds_local.UdsSession (read_did/write_did/
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
    """Build the write payload for a DID -> (name, did_id, bytes), without sending."""
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
