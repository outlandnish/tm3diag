#!/usr/bin/env python3
"""odin_coverage.py -- how much of the ODIN test library odin_runner can execute.

Introspects odin_runner.Engine for its implemented node handlers, then does a
transitive pass over every entry procedure in the bundle (expanding referenced
subnetworks), and reports:
  * procedures runnable NOW (all node types handled)
  * the ranked worklist of missing node types (each -> #procedures it blocks),
    split into hardware-interop vs pure-logic handlers
  * a greedy cumulative projection: add the top-K handlers -> N procedures unlock

Usage:
  python scripts/odin_coverage.py --bundle <…/networks>
  python scripts/odin_coverage.py --bundle <…/networks> --entries Model3/tasks --top 20
"""
from __future__ import annotations

import argparse
import collections
from pathlib import Path

import odin_runner
import odin_script_api

# structural types handled outside the _ctrl_/_data_ dispatch, or non-executable
_STRUCTURAL = {"networks.Enter", "comments.TaskInfo", "networks.Output"}
_SUBNET_TYPES = {
    "networks.RunReferencedSubnetwork",
    "networks.ReferencedSubnetwork",
    "networks.DynamicallyReferencedSubnetwork",
    "scripts.RunScriptTest",
    "scripts.ScriptTest",
}
# networks.Subnetwork is an INLINE subgraph: its inner nodes live under non-reserved
# keys of the node dict (not as a separate file). These keys are structural, not nodes.
_INLINE_SUBNET_TYPE = "networks.Subnetwork"
_RESERVED_SUBNET_KEYS = frozenset(
    {"type", "position", "slots", "signals", "inputs", "outputs", "comment"})
# module prefixes that require a Backend shim (vs pure compute)
_INTEROP_MODS = {"uds", "odx", "cid", "can", "vehiclecontrols", "lin", "isotp",
                 "hardware", "flash", "cidupdater", "apupdater", "hermes", "http",
                 "cert", "proto", "odin"}


def handled_types() -> set[str]:
    """Node types odin_runner.Engine can currently execute, plus the `api.*`
    capabilities odin_script_api gives a native script."""
    out = set(_STRUCTURAL) | odin_script_api.supported()
    for attr in dir(odin_runner.Engine):
        for pre in ("_ctrl_", "_data_"):
            if attr.startswith(pre):
                mod, _, nm = attr[len(pre):].partition("_")
                if nm:
                    out.add(f"{mod}.{nm}")
    return out


def _basename(node: dict) -> str | None:
    """Static basename of a subnet-call node, however it names its target:
    `basename` (ReferencedSubnetwork), `script_name` (RunScriptTest, a bare string),
    or `name` (DynamicallyReferencedSubnetwork). A connection => dynamic (None)."""
    for key in ("basename", "script_name", "name"):
        b = node.get(key)
        if isinstance(b, str):
            return b
        if isinstance(b, dict):
            if "value" in b:
                return b["value"]  # a literal; a bare connection => dynamic
            return None
    return None


# A connection can still be statically knowable: these node types compute from
# their own fields, so following one back yields a value. Anything else -- a loop
# item, a graph variable set at run time -- stays DYNAMIC, which is what keeps a
# static readout an honest lower bound instead of a guess.
DYNAMIC = object()
_RESOLVE_DEPTH = 8


def resolve_field(graph: dict, field, inputs: dict, depth: int = 0):
    """Best static value of a node input field, following connections.

    The shared libs are written once and parameterised: DI_RESOLVER_LEARNING's
    UDS node_name is `{'connection': 'node_name.value', 'value': 'DIR'}` and its
    CAN signal_name is a strings.Concat of that input with '_axleSpeed'. Read
    field-locally, the front task reports the REAR unit (the lib's declared
    default). Following the connection through the caller's binding gives DIF.
    """
    if depth > _RESOLVE_DEPTH:
        return DYNAMIC
    if not isinstance(field, dict):
        return field
    if "connection" in field:
        target, _, _port = field["connection"].partition(".")
        node = graph.get(target)
        v = _resolve_node(graph, node, inputs, depth + 1) if isinstance(node, dict) \
            else DYNAMIC
        # A field carrying BOTH is a connection with a declared default: prefer
        # what the connection actually resolves to, fall back to the default.
        if v is DYNAMIC and "value" in field:
            return field["value"]
        return v
    if "value" in field:
        return field["value"]
    return None


def _resolve_node(graph: dict, node: dict, inputs: dict, depth: int):
    t = node.get("type")
    if t == "constant.Constant":
        return resolve_field(graph, node.get("value"), inputs, depth)
    if t == "networks.Input":
        # ODIN binds an input by its NODE NAME, and a None binding means "unset"
        # -> the declared default (matching Engine._data_networks_Input).
        for name, other in graph.items():
            if other is node:
                bound = inputs.get(name)
                if bound is not None:
                    return bound
                break
        return resolve_field(graph, node.get("default"), inputs, depth)
    if t == "strings.Concat":
        a = resolve_field(graph, node.get("a"), inputs, depth)
        b = resolve_field(graph, node.get("b"), inputs, depth)
        if DYNAMIC in (a, b) or a is None or b is None:
            return DYNAMIC
        return f"{a}{b}"
    return DYNAMIC


def _static_inputs(node: dict, graph: dict | None = None,
                   inputs: dict | None = None) -> dict:
    """What a subnet-call node binds to its child's inputs, as far as it is
    statically knowable.

    ODIN writes a bound literal either wrapped ({'value': 'DIF'}) or bare
    ('DIF'); PROC_DIF_X_RESOLVER-LEARN uses the bare form, so both count. A
    connection is followed through the CALLER's own bindings, which is what
    carries a value across a pass-through lib -- Gen3/lib/FIRMWARE_DOWNLOAD is
    nine Inputs relayed straight into UPDATE_MODULE, so without this the
    component list UPDATE_PMR binds would be invisible one hop later.
    """
    out = {}
    for key, fld in (node.get("inputs") or {}).items():
        if not isinstance(fld, dict):
            if fld is not None:
                out[key] = fld
            continue
        if "value" in fld and "connection" not in fld:
            out[key] = fld["value"]
            continue
        if graph is not None:
            v = resolve_field(graph, fld, inputs or {})
            if v is not DYNAMIC and v is not None:
                out[key] = v
    return out


def collect(bundle: Path, relbase: str, visited: set, types: collections.Counter,
            missing_files: set, dynamic: list, *, visit=None, script_visit=None,
            inputs: dict | None = None) -> None:
    """Union node types of a graph and everything it (statically) references.

    `visit`, if given, is called `visit(name, node, relbase, graph, inputs)` for
    every node reached by the transitive walk (including INLINE-subnetwork inner
    nodes), so a caller can gather per-node facts (e.g. CAN reads) over the same
    descent the coverage counter uses -- no parallel walker needed. `graph` is the
    node's own graph and `inputs` the literals its CALLER bound, which together
    let a visitor resolve a connection-sourced field statically.

    `script_visit(source, relbase, inputs)` is the same hook for a NATIVE SCRIPT
    reached by the walk, which has no nodes for `visit` to see; `inputs` are the
    literals its caller bound (the task wrapper's nodeName='DIR', say).
    """
    if relbase in visited:
        return
    visited.add(relbase)
    path = bundle / (relbase + ".py")
    if not path.exists():
        missing_files.add(relbase)
        return
    ns: dict = {}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)  # noqa: S102
    except Exception as e:  # noqa: BLE001
        dynamic.append((relbase, f"parse-error: {e}"))
        return
    net = ns.get("network", {})
    if isinstance(net, str):
        # A NATIVE SCRIPT: no nodes, so its "types" are the api.* calls it makes.
        # They are counted alongside node types, and odin_script_api.supported()
        # joins handled_types(), so a script needing api.http.* reports blocked
        # the same way a graph needing an unimplemented node type does.
        types.update(odin_script_api.required(net))
        if script_visit is not None:
            script_visit(net, relbase, inputs or {})
        for base in sorted(odin_script_api.referenced(net)):
            collect(bundle, base, visited, types, missing_files, dynamic,
                    visit=visit, script_visit=script_visit)
        return
    if not isinstance(net, dict):
        dynamic.append((relbase, f"network-not-dict: {type(net).__name__}"))
        return
    _walk_nodes(net, bundle, relbase, visited, types, missing_files, dynamic,
                visit=visit, script_visit=script_visit, inputs=inputs)


def _walk_nodes(nodes: dict, bundle: Path, relbase: str, visited: set,
                types: collections.Counter, missing_files: set, dynamic: list,
                *, visit=None, script_visit=None, inputs: dict | None = None) -> None:
    """Count node types, recursing into referenced files (subnet basenames) and into
    INLINE networks.Subnetwork inner nodes (which live under the node's own keys)."""
    for name, node in nodes.items():
        if not isinstance(node, dict) or "type" not in node:
            continue
        t = node["type"]
        types[t] += 1
        if visit is not None:
            visit(name, node, relbase, nodes, inputs or {})
        if t in _SUBNET_TYPES:
            base = _basename(node)
            if base is None:
                dynamic.append((relbase, name))
            else:
                collect(bundle, base, visited, types, missing_files, dynamic,
                        visit=visit, script_visit=script_visit,
                        inputs=_static_inputs(node, nodes, inputs))
        elif t == _INLINE_SUBNET_TYPE:
            inner = {k: v for k, v in node.items()
                     if k not in _RESERVED_SUBNET_KEYS
                     and isinstance(v, dict) and "type" in v}
            # An inline subnet's inner nodes see the same bindings the outer graph
            # does -- they are lifted out of this node, not a separate file.
            _walk_nodes(inner, bundle, relbase, visited, types, missing_files,
                        dynamic, visit=visit, script_visit=script_visit,
                        inputs=inputs)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", type=Path, default=None,
                   help="…/networks dir (default: config.ODIN_BUNDLE from .env)")
    p.add_argument("--entries", default="Model3/tasks",
                   help="entry-procedure dir relative to bundle (default Model3/tasks)")
    p.add_argument("--top", type=int, default=25, help="worklist length")
    args = p.parse_args()

    import config as _cfg
    bundle = args.bundle or _cfg.ODIN_BUNDLE
    if bundle is None:
        p.error("no bundle: pass --bundle or set TM3_ROOT (or TM3_ODIN_BUNDLE) in .env")

    handled = handled_types()
    entry_dir = bundle / args.entries
    procs = sorted(f for f in entry_dir.glob("*.py"))

    runnable, blocked = [], {}          # blocked: proc -> set(missing types)
    blocks = collections.Counter()      # missing type -> #procs it blocks
    proc_missing: dict[str, set] = {}
    has_dynamic = []

    for f in procs:
        rel = f"{args.entries}/{f.stem}"
        types: collections.Counter = collections.Counter()
        missing_files: set = set()
        dynamic: list = []
        collect(bundle, rel, set(), types, missing_files, dynamic)
        missing = {t for t in types if t not in handled}
        proc_missing[f.stem] = missing
        if dynamic:
            has_dynamic.append(f.stem)
        if not missing:
            runnable.append(f.stem)
        else:
            blocked[f.stem] = missing
            for t in missing:
                blocks[t] += 1

    print(f"bundle entries ({args.entries}): {len(procs)}")
    print(f"handler-implemented node types: {len(handled)}")
    print(f"RUNNABLE NOW (all node types handled): {len(runnable)}  "
          f"({100*len(runnable)//max(len(procs),1)}%)")
    for name in [x for x in runnable if "_DI" in x or "DIS" in x][:20]:
        print(f"    {name}")
    if len(runnable) > 20:
        print(f"    … and {len(runnable)-20} more")

    print(f"\nWORKLIST -- missing node types ranked by #procedures they block "
          f"(top {args.top}):")
    print(f"  {'#procs':>6}  {'kind':<8}  type")
    for t, n in blocks.most_common(args.top):
        kind = "interop" if t.split(".")[0] in _INTEROP_MODS else "logic"
        print(f"  {n:>6}  {kind:<8}  {t}")

    # greedy cumulative projection
    print("\nGREEDY UNLOCK -- add handlers in this order, procedures that become runnable:")
    remaining = dict(blocked)
    added: list[str] = []
    cum = len(runnable)
    for _ in range(args.top):
        # which single missing type, if handled, frees the most procedures?
        freed = collections.Counter()
        for miss in remaining.values():
            if len(miss) == 1:
                freed[next(iter(miss))] += 1
        if not freed:
            # nothing unlocks a whole proc alone; pick the most-blocking type to chip away
            nxt = collections.Counter(
                t for miss in remaining.values() for t in miss).most_common(1)
            if not nxt:
                break
            t = nxt[0][0]
            for miss in remaining.values():
                miss.discard(t)
            added.append(t)
            print(f"  + {t:<40} (partial; 0 fully unlocked)")
            continue
        t, k = freed.most_common(1)[0]
        cum += k
        added.append(t)
        for proc in [pr for pr, miss in remaining.items() if miss == {t}]:
            del remaining[proc]
        for miss in remaining.values():
            miss.discard(t)
        print(f"  + {t:<40} -> +{k} procs (cumulative {cum}/{len(procs)})")

    if has_dynamic:
        print(f"\nnote: {len(has_dynamic)} procedures use dynamic/unresolved subnetworks "
              f"(coverage is a lower bound for those)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
