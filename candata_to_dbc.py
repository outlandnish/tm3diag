"""Build a rich Model 3 ETH DBC from the MCU UI CAN catalog.

The infotainment ``libQtCarCANData.so`` embeds the *complete* vehicle signal
catalog -- message IDs, DLCs, cycle times, signal names, units and enum value
tables -- but not the numeric bit-layout (start bit / width / scale / offset).
The per-revision ``Model3_ETH.compact.json`` has the bit-layout, but Tesla
strips it down every release (2022.45.15: 140 messages in compact.json vs 446
in the .so).

This tool marries the two:

    catalog (topology + units + enums)   <-  libQtCarCANData.so   (rich, full)
    bit-layout (start/width/scale/off)    <-  compact.json donor(s) (authoritative)

Signals present in the .so but with no layout donor can't be decoded, so they
land in a coverage report rather than the DBC.

Usage:
    # just dump the .so catalog as compact-schema JSON
    python candata_to_dbc.py extract libQtCarCANData.so -o catalog.json

    # rich DBC: .so catalog overlaid with compact.json layout
    python candata_to_dbc.py dbc libQtCarCANData.so \
        --compact Model3_ETH.compact.json[.bin] [--compact older.json ...] \
        -o Model3_ETH.rich.dbc --report gaps.txt

Donors are consulted in the order given (first match wins), so list the
same-revision compact.json first, then older/newer ones to recover layout for
signals the current revision dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import compact_to_dbc
import so_candata
from decode_bin import load_json

_LAYOUT_KEYS = ("start_position", "width")

# Extraction-dir suffixes to strip when deriving a rev from the firmware root.
_ROOT_SUFFIXES = (".ice.extracted", ".extracted", ".model3", ".modely")


def _rev_from_lib(lib: Path, root: Path | None = None) -> str:
    """Derive a firmware revision token from the firmware root directory name.

    The lib lives at ``<root>/usr/tesla/UI/lib/libQtCarCANData.so*`` so the root
    is the path component just above ``usr/``. The extraction suffix (e.g.
    ``.ice.extracted`` or ``.model3``) is stripped, yielding tokens like
    ``2022.45.15`` or ``2020.8.1-9-ae1963092f``.
    """
    if root is None:
        parts = Path(lib).resolve().parts
        cut = next((parts.index(m) for m in ("usr", "opt") if m in parts), None)
        root = Path(*parts[:cut]) if cut else Path(lib).resolve().parent
    name = Path(root).name
    for suf in _ROOT_SUFFIXES:
        if name.endswith(suf):
            name = name[:-len(suf)]
            break
    return name or "unknown"


def _has_layout(sig: dict) -> bool:
    return all(k in sig for k in _LAYOUT_KEYS)


def _alert_signal_index(alerts: dict) -> dict[str, list[str]]:
    """Map each logged CAN signal (ETH_ stripped) -> [alert names that log it]."""
    idx: dict[str, list[str]] = {}
    for name, a in alerts.items():
        for s in a.get("log_signals", []):
            key = s[4:] if s.startswith("ETH_") else s
            idx.setdefault(key, []).append(name)
    return idx


def _annotate_alerts(db: dict, sigidx: dict[str, list[str]], alerts: dict) -> int:
    """Attach a ``comment`` to each DBC signal an alert logs (-> CM_ SG_)."""
    n = 0
    for m in db["messages"].values():
        for sname, sig in m["signals"].items():
            names = sigidx.get(sname)
            if not names:
                continue
            names = sorted(set(names))
            comment = "alert: " + "; ".join(names)
            if len(names) == 1:
                desc = alerts.get(names[0], {}).get("description")
                if desc:
                    comment += f" -- {desc}"
            sig["comment"] = comment[:480]
            n += 1
    return n


def _merge_signal(so_sig: dict, layout: dict) -> dict:
    """Overlay .so enrichment (units + enums) onto a donor layout signal."""
    out = dict(layout)
    if so_sig.get("units"):
        out["units"] = so_sig["units"]
    vd = dict(layout.get("value_description") or {})
    vd.update(so_sig.get("value_description") or {})
    if vd:
        out["value_description"] = vd
    if so_sig.get("value_table_name"):
        out["value_table_name"] = so_sig["value_table_name"]
    return out


def _layout_signals(donor_msg: dict) -> dict[str, dict]:
    """Signals of a donor message that carry a usable bit-layout."""
    return {n: s for n, s in donor_msg.get("signals", {}).items()
            if _has_layout(s)}


def _index_donor_messages(donors: list[dict]):
    """Index donor messages by name and by id, each in donor-priority order.

    Values are lists of (donor_index, donor_msg) so we can pick a single
    self-consistent donor message per CAN message (mixing bit-layouts from
    different firmware revisions inside one message causes overlaps -- Tesla
    repurposes bits across releases).
    """
    by_name: dict[str, list[tuple[int, dict]]] = {}
    by_id: dict[int, list[tuple[int, dict]]] = {}
    for idx, db in enumerate(donors):
        for mname, m in db.get("messages", {}).items():
            by_name.setdefault(mname, []).append((idx, m))
            mid = m.get("message_id")
            if mid is not None:
                by_id.setdefault(mid, []).append((idx, m))
    return by_name, by_id


def _choose_donor(cands: list[tuple[int, dict]], prefer_order: bool):
    """Pick one donor message: richest layout (default) or first (prefer_order).

    ``cands`` is in donor-priority order, so ``max`` breaks ties toward the
    earliest (highest-priority) donor.
    """
    usable = [(i, m) for i, m in cands if _layout_signals(m)]
    if not usable:
        return None
    if prefer_order:
        return usable[0]
    return max(usable, key=lambda im: len(_layout_signals(im[1])))


def enrich(cat: so_candata.SoCatalog, donors: list[dict], *,
           prefer_order: bool = False):
    """Merge a .so catalog with compact.json donors.

    Layout for each message is taken from a *single* donor (the richest, or the
    highest-priority when ``prefer_order``) so the result stays internally
    consistent. The .so overlays units and enum tables. Returns
    ``(enriched_db, report)`` in compact-schema form.
    """
    by_name, by_id = _index_donor_messages(donors)
    donor_labels = [db.get("_label") or db.get("version") or f"donor{idx}"
                    for idx, db in enumerate(donors)]

    messages: dict[str, dict] = {}
    gaps: list[dict] = []
    sources: list[dict] = []
    n_enriched = 0

    def build(mname: str, m_so: dict | None, cands: list[tuple[int, dict]]):
        nonlocal n_enriched
        chosen = _choose_donor(cands, prefer_order)
        out_sigs: dict[str, dict] = {}
        donor_len = 0
        so_sigs = (m_so or {}).get("signals", {})
        if chosen:
            didx, dmsg = chosen
            donor_len = dmsg.get("length_bytes", 0)
            for sname, layout in _layout_signals(dmsg).items():
                so_sig = so_sigs.get(sname, {})
                out_sigs[sname] = _merge_signal(so_sig, layout)
                if sname in so_sigs:
                    n_enriched += 1
            sources.append({"message": mname,
                            "donor": donor_labels[didx],
                            "signals": len(out_sigs)})
        # .so-only signals with no layout anywhere -> reported as a gap.
        for sname in so_sigs:
            if sname not in out_sigs:
                gaps.append({"message": mname,
                             "message_id": (m_so or {}).get("message_id", 0),
                             "signal": sname})
        mid = (m_so or {}).get("message_id")
        if mid is None:
            mid = chosen[1].get("message_id", 0) if chosen else 0
        so_len = (m_so or {}).get("length_bytes") or 0
        length = max(so_len, donor_len, 8)
        node = (m_so or {}).get("originNode") \
            or (chosen[1].get("originNode") if chosen else None) \
            or mname.split("_", 1)[0]
        return {
            "message_id": mid,
            "length_bytes": length,
            "cycle_time": (m_so or {}).get("cycle_time")
            or (chosen[1].get("cycle_time", 0) if chosen else 0),
            "originNode": node,
            "senders": [node],
            "signals": out_sigs,
        }

    # Spine: every message in the .so catalog.
    for mname, m_so in cat.messages.items():
        cands = by_name.get(mname) or by_id.get(m_so["message_id"]) or []
        messages[mname] = build(mname, m_so, cands)

    # Messages a donor knows but the .so does not (only if layout exists).
    donor_only = 0
    for mname, cands in by_name.items():
        if mname in messages or _choose_donor(cands, prefer_order) is None:
            continue
        messages[mname] = build(mname, None, cands)
        donor_only += 1

    enriched_db = {
        "product": "Model3",
        "version": f"rich:{cat.lib}",
        "busMetadata": {cat.bus: {"messageCount": len(messages)}},
        "messages": messages,
    }
    report = {
        "lib": cat.lib,
        "bus": cat.bus,
        "donors": donor_labels,
        "so_messages": len(cat.messages),
        "so_signals": cat.signal_count,
        "signals_enriched": n_enriched,
        "signals_gapped": len(gaps),
        "donor_only_messages": donor_only,
        "sources": sources,
        "gaps": gaps,
    }
    return enriched_db, report


def _write_report(report: dict, path: Path) -> None:
    lines = [
        f"# ETH DBC coverage report -- {report['lib']} (bus {report['bus']})",
        f"# donors (priority order): {', '.join(report['donors'])}",
        "",
        f"messages in .so catalog : {report['so_messages']}",
        f"signals in .so catalog  : {report['so_signals']}",
        f"signals with layout     : {report['signals_enriched']}",
        f"signals WITHOUT layout  : {report['signals_gapped']}",
        f"donor-only messages     : {report['donor_only_messages']}",
        "",
        "## layout source per message (message -> donor : #signals):",
    ]
    primary = report["donors"][0] if report["donors"] else None
    borrowed = [s for s in report["sources"] if s["donor"] != primary]
    for s in sorted(report["sources"], key=lambda s: s["message"]):
        flag = "  <- non-primary" if s["donor"] != primary else ""
        lines.append(f"    {s['message']} -> {s['donor']} : {s['signals']}{flag}")
    lines.append("")
    lines.append(f"## {len(borrowed)} messages took layout from a non-primary "
                 "(cross-revision) donor")
    lines.append("")
    lines.append("## signals missing a layout donor (undecodable):")
    by_msg: dict[str, list[str]] = {}
    for g in report["gaps"]:
        by_msg.setdefault(f"{g['message']} (0x{g['message_id']:X})", []).append(
            g["signal"])
    for mkey in sorted(by_msg):
        lines.append(f"\n{mkey}")
        for s in sorted(by_msg[mkey]):
            lines.append(f"    {s}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_donors(paths: list[Path]) -> list[dict]:
    donors = []
    for p in paths:
        db = load_json(p)
        db["_label"] = _rev_from_lib(Path(p))
        n = len(db.get("messages", {}))
        print(f"  donor {db['_label']} ({p.name}): {n} messages",
              file=sys.stderr)
        donors.append(db)
    return donors


_DEFAULT_LIB_REL = "usr/tesla/UI/lib/libQtCarCANData.so"


def _resolve_lib(args) -> tuple[Path, Path | None]:
    """Return (lib_path, root_hint), defaulting the lib from config.ROOT."""
    import config as _cfg
    root = Path(args.root).expanduser() if args.root else _cfg.ROOT
    if args.lib:
        return Path(args.lib), root
    if root is None:
        sys.exit("no lib given and config.ROOT (TM3_ROOT) is unset "
                 "(pass the .so path, --root, or set TM3_ROOT in .env)")
    return root / _DEFAULT_LIB_REL, root


def _resolve_rev(args, lib: Path, root: Path | None) -> str:
    import config as _cfg
    return args.rev or _cfg.FW_VERSION or _rev_from_lib(lib, root)


def cmd_extract(args) -> None:
    lib, root = _resolve_lib(args)
    rev = _resolve_rev(args, lib, root)
    cat = so_candata.extract_catalog(lib, bus=args.bus)
    print(f"{cat.lib}: rev={rev} bus={cat.bus} messages={len(cat.messages)} "
          f"signals={cat.signal_count} value_tables={cat.value_table_count}",
          file=sys.stderr)
    db = so_candata.to_compact_dict(cat, product=args.product)
    db["version"] = rev
    out = args.out or Path(f"{args.product}_{cat.bus}.{rev}.catalog.json")
    out.write_text(json.dumps(db, indent=2, sort_keys=True))
    print(f"wrote {out}")


def cmd_dbc(args) -> None:
    lib, root = _resolve_lib(args)
    rev = _resolve_rev(args, lib, root)
    cat = so_candata.extract_catalog(lib, bus=args.bus)
    print(f"catalog: rev={rev} {len(cat.messages)} messages / "
          f"{cat.signal_count} signals", file=sys.stderr)
    if not args.compact:
        import config as _cfg
        if _cfg.ETH_COMPACT is None:
            sys.exit("no --compact given and config.ETH_COMPACT is unset "
                     "(set TM3_ROOT or pass --compact)")
        args.compact = [_cfg.ETH_COMPACT]
    donors = _load_donors(args.compact)
    enriched_db, report = enrich(cat, donors, prefer_order=args.prefer_order)
    enriched_db["dbc_version"] = rev

    # Cross-link alerts: annotate each logged signal with the alert(s) that log it.
    if not args.no_alerts:
        alib = args.alerts or (lib.parent / "libQtCarAlerts.so")
        if Path(alib).exists():
            import so_alerts
            acat = so_alerts.extract_alerts(alib)
            n = _annotate_alerts(enriched_db,
                                 _alert_signal_index(acat.alerts), acat.alerts)
            print(f"alerts: {Path(alib).name} -> annotated {n} signals",
                  file=sys.stderr)
        elif args.alerts:
            print(f"alerts: {alib} not found, skipping", file=sys.stderr)

    out = args.out or Path(f"{args.product}_{cat.bus}.{rev}.dbc")
    compact_to_dbc.convert_db(enriched_db, out)
    print(f"enriched: {report['signals_enriched']} signals with layout, "
          f"{report['signals_gapped']} without", file=sys.stderr)
    if args.report:
        _write_report(report, args.report)
        print(f"wrote report {args.report}")
    if args.json:
        args.json.write_text(json.dumps(enriched_db, indent=2, sort_keys=True))
        print(f"wrote enriched json {args.json}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", default="Model3", help="product name (default Model3)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="dump the .so catalog as compact JSON")
    pe.add_argument("lib", type=Path, nargs="?",
                    help="libQtCarCANData.so (default: from config.ROOT)")
    pe.add_argument("--root", help="firmware extraction root (default: TM3_ROOT)")
    pe.add_argument("--rev", help="firmware rev label (default: from root name)")
    pe.add_argument("--bus", help="override bus name (default from CANBusList)")
    pe.add_argument("-o", "--out", type=Path, help="output JSON path")
    pe.set_defaults(func=cmd_extract)

    pd = sub.add_parser("dbc", help="build a rich DBC (.so + compact.json)")
    pd.add_argument("lib", type=Path, nargs="?",
                    help="libQtCarCANData.so (default: from config.ROOT)")
    pd.add_argument("--root", help="firmware extraction root (default: TM3_ROOT)")
    pd.add_argument("--rev", help="firmware rev label (default: from root name)")
    pd.add_argument("--compact", type=Path, action="append", default=[],
                    help="compact.json donor (repeatable; first match wins)")
    pd.add_argument("--bus", help="override bus name")
    pd.add_argument("-o", "--out", type=Path, help="output DBC path")
    pd.add_argument("--report", type=Path, help="write coverage/gap report")
    pd.add_argument("--json", type=Path, help="also write enriched compact JSON")
    pd.add_argument("--prefer-order", action="store_true",
                    help="per message, take layout from the first donor that "
                         "has it (current-rev fidelity) instead of the richest "
                         "donor (max coverage, the default)")
    pd.add_argument("--alerts", type=Path,
                    help="libQtCarAlerts.so for alert<->signal CM_ annotations "
                         "(default: sibling of the CAN lib)")
    pd.add_argument("--no-alerts", action="store_true",
                    help="skip alert->signal CM_ annotations")
    pd.set_defaults(func=cmd_dbc)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
