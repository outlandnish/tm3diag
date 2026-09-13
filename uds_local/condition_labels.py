"""Human-readable value labels for the signed metadata's condition keys.

A condition key is a GTW_carConfig signal: the metadata's `vdcType` is the
signal `GTW_vdcType` on 0x7FF. Two sources can name its values, and they do not
cover the same ground:

* ``compact.json`` -- what Tesla ships to the diagnostic tool, so only the
  subset it needs. On 2026.8.3 it has tables for `chassisType` and
  `drivetrainType` but NOT `vdcType`, `espValveType`, `rcmLocation` or
  `brakeHwType`, which is why those came out as bare 0/1.
* the generated DBC (``candata_to_dbc``) -- built from that firmware's own
  ``libQtCarCANData``, so it carries the whole GTW_carConfig catalog:
  ``VAL_ 2047 GTW_vdcType 0 "BOSCH_VDC" 1 "TESLA_VDC" ;``

So the DBC is preferred and compact.json fills its gaps. Neither is required:
a key nothing can name falls back to its raw value, which is still a correct
answer -- an unlabelled number beats a wrong label, and beats refusing to
offer the choice at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from decode_bin import load_json as _load_json

# `VAL_TABLE_ <name> ...;` and `VAL_ <msg id> <signal> ...;` -- both carry the
# same `<int> "<label>"` pairs, and for GTW_carConfig both name the signal.
_VAL_LINE = re.compile(r"^\s*(?:VAL_TABLE_\s+|VAL_\s+\d+\s+)(\S+)\s+(.*?);\s*$")
_VAL_PAIR = re.compile(r'(-?\d+)\s+"([^"]*)"')
_GTW = "GTW_"


def load_dbc_condition_labels(dbc_path: Path | str | None) -> dict[str, dict[str, str]]:
    """Return {condition_key: {int_str: label}} from a DBC's GTW_carConfig tables.

    Read as TEXT, not through cantools: the generated DBC has ~26k signals and
    this needs one message's value tables, so a full parse would cost seconds to
    answer a question a scan answers in milliseconds -- and it keeps the label
    lookup free of a cantools dependency.

    Only signals named exactly ``GTW_<key>`` count. The catalog also has
    ``DIR_a144_vdcType`` and ``PM_a043_GTW_vdcType`` (alert payloads echoing the
    config back), which are not the config signal and must not answer for it.

    Returns {} when the path is None, absent, or unreadable.
    """
    if dbc_path is None:
        return {}
    path = Path(dbc_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not (line.startswith("VAL_") or line.lstrip().startswith("VAL_")):
            continue                      # cheap reject: ~26k signal lines
        m = _VAL_LINE.match(line)
        if not m or not m.group(1).startswith(_GTW):
            continue
        key = m.group(1)[len(_GTW):]
        pairs = dict(_VAL_PAIR.findall(m.group(2)))
        if pairs:
            result.setdefault(key, {}).update(pairs)
    return result


def load_condition_labels(compact_path: Path | str | None,
                          dbc_path: Path | str | None = None,
                          ) -> dict[str, dict[str, str]]:
    """Return {condition_key: {int_str: label}} from compact.json, then the DBC.

    The DBC wins where both name a value: it is built from the same firmware and
    covers the whole GTW_carConfig catalog, while compact.json carries only the
    diagnostic subset. Either source may be missing; a key neither one names
    simply has no labels, and the caller shows the raw value.
    """
    labels = _load_compact_labels(compact_path)
    for key, table in load_dbc_condition_labels(dbc_path).items():
        labels.setdefault(key, {}).update(table)
    return labels


def labels_for(label_map: dict, key: str) -> dict[str, str]:
    """The value labels for one condition key, matching case-insensitively.

    The metadata spells the same key both ways -- `brakeHWType` on most rows and
    `brakeHwType` on the ESP's -- while the signal is `GTW_brakeHWType`, so an
    exact match alone leaves one of them showing bare numbers. Folding is safe
    HERE and only here: this decides what a value is CALLED, never which row
    matches, so the worst a wrong fold could do is mislabel, and two keys
    differing only in case are the same key.
    """
    table = (label_map or {}).get(key)
    if table:
        return table
    folded = key.casefold()
    for name, values in (label_map or {}).items():
        if name.casefold() == folded:
            return values
    return {}


def _load_compact_labels(compact_path: Path | str | None) -> dict[str, dict[str, str]]:
    """{value_table_name: {int_str: label}} from a compact.json file.

    Returns {} when compact_path is None, the file is absent, or JSON is invalid.
    Uses decode_bin.load_json so an encrypted .bin twin is auto-decrypted.
    """
    if compact_path is None:
        return {}
    path = Path(compact_path)
    try:
        data = _load_json(path)
    except Exception:
        return {}
    result: dict[str, dict[str, str]] = {}
    try:
        for msg in data.get("messages", {}).values():
            for sig in msg.get("signals", {}).values():
                table_name = sig.get("value_table_name")
                value_desc = sig.get("value_description")
                if table_name and value_desc:
                    result[table_name] = {str(v): k for k, v in value_desc.items()}
    except (AttributeError, TypeError):
        return {}
    return result
