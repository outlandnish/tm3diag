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

# structural types handled outside the _ctrl_/_data_ dispatch, or non-executable
# (networks.Output is materialized by Engine._collect_outputs, not a _ctrl_/_data_ method)
_STRUCTURAL = {"networks.Enter", "comments.TaskInfo", "networks.Output"}
_SUBNET_TYPES = {
    "networks.RunReferencedSubnetwork",
    "networks.ReferencedSubnetwork",
    "networks.DynamicallyReferencedSubnetwork",
    "scripts.RunScriptTest",
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
    """Node types odin_runner.Engine can currently execute."""
    out = set(_STRUCTURAL)
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


def collect(bundle: Path, relbase: str, visited: set, types: collections.Counter,
            missing_files: set, dynamic: list, *, visit=None) -> None:
    """Union node types of a graph and everything it (statically) references.

    `visit`, if given, is called `visit(name, node, relbase)` for every node
    reached by the transitive walk (including INLINE-subnetwork inner nodes), so a
    caller can gather per-node facts (e.g. CAN reads) over the same descent the
    coverage counter uses -- no parallel walker needed.
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
    if not isinstance(net, dict):
        dynamic.append((relbase, f"network-not-dict: {type(net).__name__}"))
        return
    _walk_nodes(net, bundle, relbase, visited, types, missing_files, dynamic, visit=visit)


def _walk_nodes(nodes: dict, bundle: Path, relbase: str, visited: set,
                types: collections.Counter, missing_files: set, dynamic: list,
                *, visit=None) -> None:
    """Count node types, recursing into referenced files (subnet basenames) and into
    INLINE networks.Subnetwork inner nodes (which live under the node's own keys)."""
    for name, node in nodes.items():
        if not isinstance(node, dict) or "type" not in node:
            continue
        t = node["type"]
        types[t] += 1
        if visit is not None:
            visit(name, node, relbase)
        if t in _SUBNET_TYPES:
            base = _basename(node)
            if base is None:
                dynamic.append((relbase, name))
            else:
                collect(bundle, base, visited, types, missing_files, dynamic, visit=visit)
        elif t == _INLINE_SUBNET_TYPE:
            inner = {k: v for k, v in node.items()
                     if k not in _RESERVED_SUBNET_KEYS
                     and isinstance(v, dict) and "type" in v}
            _walk_nodes(inner, bundle, relbase, visited, types,
                        missing_files, dynamic, visit=visit)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", type=Path, default=None,
                   help="…/networks dir (default: config.ODIN_BUNDLE from .env)")
    p.add_argument("--entries", default="Model3/tasks",
                   help="entry-procedure dir relative to bundle (default Model3/tasks)")
    p.add_argument("--top", type=int, default=25, help="worklist length")
    args = p.parse_args()

    import config as _cfg  # odin_runner import already put the repo root on sys.path
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
