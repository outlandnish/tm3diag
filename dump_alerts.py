#!/usr/bin/env python3
"""Dump the alert catalogue a firmware build exposes on each vehicle bus.

Every build ships ``opt/odin/data/<model>/bus-alerts-map.json`` -- a per-model
map of which alert IDs are reachable on which vehicle bus.  The IDs are stored
as salted hashes, not in the clear: the map hash of an alert is
``sha256(alert_str + salt)`` and a bus bucket is ``sha256(bus_name + salt)``,
where the per-file salt is read from the map itself.  Because the alert strings
are also present in the clear in the firmware (the alertd binary and
libQtCarAlerts.so), the tool reverses the hashes by running known strings
through the same recipe and matching.

Given a firmware root (or the ``TM3_ROOT`` in .env), the tool:
  * loads each ``bus-alerts-map.json`` and resolves its bucket(s) to bus names;
  * reverses the alert hashes against a plaintext catalogue it maintains
    (``--catalog``) by running each known alert string through the recipe
    and matching;
  * for anything unresolved, scrapes the firmware image for alert-shaped
    strings (``--scrape``) and folds newly-confirmed strings back into the
    catalogue, so coverage improves every time a new revision is processed;
  * merges severity / UI panel info from service_ui/alerts.json (2025+);
  * writes ``<rev>-<model>-alerts.json`` per model and prints a summary.

Firmware root, output dir and catalogue all default from .env / the repo, so a
bare ``python dump_alerts.py`` works once .env is configured.

Usage:
  python dump_alerts.py [firmware_root] [--out DIR] [--catalog FILE]
                        [--scrape/--no-scrape] [--quiet]

Example:
  python dump_alerts.py                      # uses TM3_ROOT from .env
  python dump_alerts.py /path/to/2026.8.3.ice.extracted
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import config  # loads .env; provides ROOT and path defaults
import so_alerts  # descriptions + logged-signal map from libQtCarAlerts.so

_REPO = Path(__file__).parent

# Full alert string:  NODE_<type><code>_camelCaseName  (type a/w/d/..., 3-4 digit code)
_ALERT_RE = re.compile(r"[A-Z][A-Z0-9]{1,15}_[a-z][0-9]{3,4}_[A-Za-z0-9_]{1,96}")
_ID_RE = re.compile(r"^([A-Z][A-Z0-9]{1,15}_[a-z][0-9]{3,4})")
_NAMECHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)

# Generated data (the growing catalogue + the per-model JSON dumps) lands here;
# this dir is gitignored so extracted alert data is never committed by accident.
_DEFAULT_OUT = _REPO / "alerts"
_DEFAULT_CATALOG = _DEFAULT_OUT / "alert_catalog.txt"

# Candidate bus names for resolving a bucket hash -> bus label. Tesla's Bus enum
# only defines ETH for these platforms; the rest are kept so the tool keeps
# working if bridged CAN buses are added to the map later.
BUS_NAMES = [
    "ETH", "eth", "VEHICLE", "vehicle", "PARTY", "party", "CHASSIS", "chassis",
    "PT", "pt", "BODY", "body", "POWERTRAIN", "powertrain", "CANV", "CAN",
]


def alert_hash(alert_str: str, salt: str) -> str:
    """Map hash of a full alert string under a map's salt (lowercase hex)."""
    return hashlib.sha256((alert_str + salt).encode()).hexdigest()


def bus_bucket(bus_name: str, salt: str) -> str:
    """Bucket hash of a bus name under a map's salt (lowercase hex)."""
    return hashlib.sha256((bus_name + salt).encode()).hexdigest()


def find_odin_dir(root: Path) -> Path:
    """Accept a firmware root or an opt/odin dir; return the opt/odin dir."""
    if (root / "data").is_dir() and (root / "alertd").exists():
        return root
    cand = root / "opt" / "odin"
    if cand.is_dir():
        return cand
    hits = list(root.glob("**/opt/odin"))
    if hits:
        return hits[0]
    sys.exit(f"could not find opt/odin under {root}")


def load_catalog(path: Path) -> set:
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}


def save_catalog(path: Path, catalog: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(catalog)) + "\n")


def harvest_alertd(odin: Path) -> set:
    """Segment the concatenated alert strings inside the alertd Go binary.

    Strings are stored back-to-back with no separators, so a name that ends in
    an upper-case run merges into the next alert's prefix.  We slice from each
    id anchor and keep every end position -- the caller validates against a
    real hash, so over-captured variants are simply discarded.
    """
    out = set()
    binp = odin / "alertd"
    if not binp.exists():
        return out
    blob = binp.read_bytes().decode("latin-1")
    for m in re.finditer(r"[A-Z][A-Z0-9]{1,15}_[a-z][0-9]{3,4}", blob):
        ide = m.end()
        if ide >= len(blob) or blob[ide] != "_":
            continue
        e, lim = ide + 1, min(ide + 96, len(blob))
        while e < lim and blob[e] in _NAMECHARS:
            e += 1
            out.add(blob[m.start():e])
    return out


def scrape_image(root: Path) -> set:
    """grep the whole firmware image for alert-shaped strings (binary-safe)."""
    try:
        res = subprocess.run(
            ["grep", "-rhoaE", _ALERT_RE.pattern, str(root)],
            capture_output=True, text=True, timeout=1200, check=False,
        )
    except FileNotFoundError:
        sys.exit("grep not available; run under a POSIX shell")
    return {ln for ln in res.stdout.splitlines() if ln}


def reverse_map(inner: set, salt: str, candidates: set) -> dict:
    """Return {alert-hash: alert_str} for every inner hash we can explain.

    For each candidate, try every end-trim so concatenated/over-captured raw
    strings still yield their embedded real alert string.
    """
    matched = {}
    for c in candidates:
        m = _ID_RE.match(c)
        if not m:
            continue
        ide = m.end()
        # exact form first (fast path for clean catalogue entries)
        h = alert_hash(c, salt)
        if h in inner:
            matched.setdefault(h, c)
        # end-trim variants (only needed for raw scraped/binary candidates)
        for e in range(ide + 2, len(c) + 1):
            if c[e - 1] not in _NAMECHARS:
                break
            h = alert_hash(c[:e], salt)
            if h in inner and h not in matched:
                matched[h] = c[:e]
    return matched


def load_ui_alerts(odin: Path) -> dict:
    """service_ui alerts.json: id-only -> {panels, severity}. Present 2025+."""
    for p in (
        odin / "service_ui/static/assets/alerts.json",
        odin / "service-ui/static/assets/alerts.json",
    ):
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return {}


def load_so_alert_descs(img_root: Path) -> dict:
    """name -> record from libQtCarAlerts.so (description, cause/clear/effect,
    audience, log_signals). Returns {} when the lib isn't in this build."""
    for rel in ("usr/tesla/UI/lib/libQtCarAlerts.so",
                "usr/tesla/UI/lib/libQtCarAlerts.so.1.0.0"):
        p = img_root / rel
        if p.exists():
            try:
                return so_alerts.extract_alerts(p).alerts
            except Exception as exc:  # noqa: BLE001 - best-effort enrichment
                print(f"    (libQtCarAlerts parse failed: {exc})", file=sys.stderr)
                return {}
    return {}


def dump_one(map_path: Path, odin: Path, root: Path, catalog: set,
             ui: dict, do_scrape: bool, quiet: bool, descs: dict | None = None) -> dict:
    data = json.loads(map_path.read_text())
    salt = data["s"]
    buckets = data["c"]
    model = map_path.parent.name

    # resolve bucket hashes -> bus names
    known = {bus_bucket(b, salt): b for b in BUS_NAMES}
    bus_of = {bh: known.get(bh, f"?{bh[:8]}") for bh in buckets}

    inner_all = set()
    for v in buckets.values():
        inner_all.update(v)

    matched = reverse_map(inner_all, salt, catalog)
    if len(matched) < len(inner_all):
        matched.update(reverse_map(inner_all, salt, harvest_alertd(odin)))
    if do_scrape and len(matched) < len(inner_all):
        if not quiet:
            print(f"    scraping image for {len(inner_all) - len(matched)} "
                  f"unresolved (this takes a few minutes)...", file=sys.stderr)
        found = reverse_map(inner_all, salt, scrape_image(root))
        for h, s in found.items():
            matched.setdefault(h, s)
    # fold clean confirmed strings back into the catalogue
    catalog.update(matched.values())

    # build per-bus alert records
    result = {"model": model, "salt": salt, "buses": {}}
    for bh, hashes in buckets.items():
        bus = bus_of[bh]
        alerts = []
        unresolved = []
        for h in hashes:
            full = matched.get(h)
            if not full:
                unresolved.append(h)
                continue
            mid = _ID_RE.match(full)
            aid = mid.group(1)
            name = full[len(aid) + 1:]
            rec = {"id": aid, "name": name, "node": aid.split("_")[0]}
            if descs and full in descs:
                d = descs[full]
                for k in ("description", "cause", "clear", "effect", "audience"):
                    if d.get(k):
                        rec[k] = d[k]
                if d.get("log_signals"):
                    rec["log_signals"] = d["log_signals"]
            if aid in ui:
                meta = ui[aid]
                sev = sorted({p.get("severityLevel")
                              for p in meta.get("panels", {}).values()
                              if p.get("severityLevel") is not None})
                if sev:
                    rec["severity"] = sev
                rec["panels"] = sorted(meta.get("panels", {}))
            alerts.append(rec)
        alerts.sort(key=lambda r: r["id"])
        result["buses"][bus] = {
            "total": len(hashes),
            "resolved": len(alerts),
            "unresolved": len(unresolved),
            "nodes": dict(Counter(a["node"] for a in alerts).most_common()),
            "alerts": alerts,
            "unresolved_hashes": unresolved,
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("firmware_root", type=Path, nargs="?", default=None,
                    help="squashfs extraction root or opt/odin dir "
                         "(default: TM3_ROOT from .env)")
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT,
                    help=f"output dir for JSON dumps (default {_DEFAULT_OUT})")
    ap.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG,
                    help="plaintext alert catalogue, grows over time "
                         f"(default {_DEFAULT_CATALOG})")
    ap.add_argument("--scrape", dest="scrape", action="store_true", default=True,
                    help="scrape the image for unresolved alerts (default on)")
    ap.add_argument("--no-scrape", dest="scrape", action="store_false")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = args.firmware_root or config.ROOT
    if root is None:
        sys.exit("no firmware root: pass one, or set TM3_ROOT in .env")
    root = Path(root)
    odin = find_odin_dir(root)
    # the image root is the tree we scrape; if given opt/odin, climb to it
    img_root = root
    if odin == root:
        img_root = root.parents[1] if len(root.parents) >= 2 else root

    catalog = load_catalog(args.catalog)
    ui = load_ui_alerts(odin)
    so_descs = load_so_alert_descs(img_root)
    if so_descs and not args.quiet:
        print(f"    libQtCarAlerts.so: {len(so_descs)} alert descriptions",
              file=sys.stderr)
    maps = sorted(odin.glob("data/*/bus-alerts-map.json"))
    if not maps:
        sys.exit(f"no bus-alerts-map.json under {odin}/data")

    rev = next((p for p in (root, *root.parents) if ".ice" in p.name
                or ".model3" in p.name), root).name

    args.out.mkdir(parents=True, exist_ok=True)
    summary = []
    for mp in maps:
        res = dump_one(mp, odin, img_root, catalog, ui,
                       args.scrape, args.quiet, so_descs)
        outp = args.out / f"{rev}-{res['model']}-alerts.json"
        outp.write_text(json.dumps(res, indent=2))
        for bus, b in res["buses"].items():
            summary.append((rev, res["model"], bus, b["total"],
                            b["resolved"], b["unresolved"], outp))

    save_catalog(args.catalog, catalog)

    print(f"\ncatalogue: {len(catalog)} alert strings  ({args.catalog})")
    print(f"{'revision':30} {'model':7} {'bus':6} {'total':>6} "
          f"{'resolved':>9} {'miss':>5}")
    for rev, model, bus, tot, res_, miss, _outp in summary:
        pct = 100 * res_ / tot if tot else 0
        print(f"{rev:30} {model:7} {bus:6} {tot:6} {res_:9} ({pct:4.0f}%) {miss:5}")
    print(f"\ndumps written to {args.out}/")


if __name__ == "__main__":
    main()
