"""Extract Tesla's alert catalog from the MCU UI ``libQtCarAlerts.so``.

The infotainment alert library embeds, as exported relocated data, the full
per-build alert catalog -- names, human-readable descriptions and the CAN
signals each alert logs. This is richer than ``bus-alerts-map.json`` (which
stores only salted hashes; see :mod:`dump_alerts`): here the names and
descriptions are in the clear.

Tables (2022.45.15, x86-64 LE; reversed + validated):

    globalBasicAlertDataTable   88-byte records, 6308 alerts
      +0x00 char*  name              e.g. "BMS_a089_SW_VcFront_MIA"
      +0x08 u64    hash (name digest)
      +0x10 u32    index             (matrix/enum index)
      +0x18 char*  description        human-readable text
      +0x20 u64    flags
      +0x28 char*  note              (usually "")
      +0x30 char*  audience          e.g. "6.0.89"
      +0x38 char*  cause             set/trigger condition
      +0x40 char*  clear             clear condition
      +0x48 char*  effect            consequence / impact
      +0x50 u64

    globalAlertLogSignalData    24-byte records, 3027 entries
      +0x00 char*  node              e.g. "BMS"
      +0x08 u32    code              alert number (the NNN in aNNN)
      +0x10 char*  comma-joined ETH signal names logged for that alert

    Empty string fields all point at a single shared "" datum.

The two tables join on (node, code) -- code parsed from the alert name -- to
attach ``log_signals`` to each alert (3027/3027 join in 2022.45.15).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from so_candata import ElfImage

BASIC_STRIDE = 88
LOG_STRIDE = 24

# NODE_<type><code>_rest ; type is 1-2 lowercase letters (a, w, sw, sc, e, ...)
_NAME_RE = re.compile(r"^([A-Z][A-Z0-9]+)_([a-z]{1,2})(\d+)")


def _parse_name(name: str) -> tuple[str | None, str | None, int | None]:
    m = _NAME_RE.match(name)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), int(m.group(3))


@dataclass
class AlertCatalog:
    lib: str
    alerts: dict[str, dict] = field(default_factory=dict)
    log_records: int = 0
    joined: int = 0


def _read_log_map(elf: ElfImage) -> dict[tuple[str, int], list[str]]:
    """Return {(node, code): [ETH signal names]} from globalAlertLogSignalData."""
    sym = elf.sym("globalAlertLogSignalData")
    if not sym:
        return {}
    out: dict[tuple[str, int], list[str]] = {}
    for i in range(sym["size"] // LOG_STRIDE):
        b = sym["value"] + i * LOG_STRIDE
        node = elf.cstr(elf.ptr_target(b + 0))
        code = elf.u32(b + 8)
        sigs = elf.cstr(elf.ptr_target(b + 16))
        if node:
            out[(node, code)] = [s for s in sigs.split(",") if s] if sigs else []
    return out


def extract_alerts(path: str | Path) -> AlertCatalog:
    """Extract the alert catalog from *path* (a libQtCarAlerts .so)."""
    elf = ElfImage(path)
    basic = elf.sym("globalBasicAlertDataTable")
    if not basic:
        raise ValueError(f"{path}: no globalBasicAlertDataTable (not an Alerts lib?)")
    log_map = _read_log_map(elf)

    cat = AlertCatalog(lib=Path(path).name)
    cat.log_records = len(log_map)
    for i in range(basic["size"] // BASIC_STRIDE):
        b = basic["value"] + i * BASIC_STRIDE
        name = elf.cstr(elf.ptr_target(b + 0))
        if not name:
            continue
        node, ctype, code = _parse_name(name)
        rec: dict = {
            "node": node,
            "code": code,
            "code_type": ctype,
            "index": elf.u32(b + 0x10),
            "description": elf.cstr(elf.ptr_target(b + 0x18)),
            "cause": elf.cstr(elf.ptr_target(b + 0x38)),
            "clear": elf.cstr(elf.ptr_target(b + 0x40)),
            "effect": elf.cstr(elf.ptr_target(b + 0x48)),
            "audience": elf.cstr(elf.ptr_target(b + 0x30)),
            "flags": elf.u32(b + 0x20) | (elf.u32(b + 0x24) << 32),
            "hash": f"0x{(elf.u32(b + 8) | (elf.u32(b + 12) << 32)):016x}",
        }
        note = elf.cstr(elf.ptr_target(b + 0x28))
        if note:
            rec["note"] = note
        sigs = log_map.get((node, code)) if node is not None else None
        if sigs:
            rec["log_signals"] = sigs
            cat.joined += 1
        cat.alerts[name] = rec
    return cat


def to_dict(cat: AlertCatalog, product: str = "Model3", rev: str = "") -> dict:
    with_desc = sum(1 for a in cat.alerts.values() if a["description"])
    return {
        "product": product,
        "rev": rev,
        "lib": cat.lib,
        "count": len(cat.alerts),
        "with_description": with_desc,
        "with_log_signals": cat.joined,
        "alerts": cat.alerts,
    }


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Dump the MCU alert catalog")
    ap.add_argument("lib", type=Path, nargs="?",
                    help="libQtCarAlerts.so (default: from config.ROOT)")
    ap.add_argument("--root", help="firmware extraction root (default: TM3_ROOT)")
    ap.add_argument("--rev", help="firmware rev label (default: from root name)")
    ap.add_argument("--product", default="Model3")
    ap.add_argument("-o", "--out", type=Path, help="output JSON path")
    ap.add_argument("--names-out", type=Path,
                    help="also write the sorted plaintext alert names (feeds "
                         "dump_alerts.py's --catalog)")
    a = ap.parse_args()

    # Resolve lib/rev from config.ROOT the same way candata_to_dbc does.
    import config as _cfg
    from candata_to_dbc import _rev_from_lib
    root = Path(a.root).expanduser() if a.root else _cfg.ROOT
    lib = a.lib
    if lib is None:
        if root is None:
            sys.exit("no lib given and config.ROOT (TM3_ROOT) is unset")
        lib = root / "usr/tesla/UI/lib/libQtCarAlerts.so"
    rev = a.rev or _cfg.FW_VERSION or _rev_from_lib(Path(lib), root)

    cat = extract_alerts(lib)
    doc = to_dict(cat, product=a.product, rev=rev)
    print(f"{cat.lib}: rev={rev} alerts={doc['count']} "
          f"with_description={doc['with_description']} "
          f"with_log_signals={doc['with_log_signals']}", file=sys.stderr)
    out = a.out or Path(f"{rev}-{a.product}-so-alerts.json")
    out.write_text(json.dumps(doc, indent=2, sort_keys=True))
    print(f"wrote {out}")
    if a.names_out:
        a.names_out.write_text("\n".join(sorted(cat.alerts)) + "\n")
        print(f"wrote {a.names_out} ({len(cat.alerts)} names)")
