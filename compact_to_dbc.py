"""Convert Model3_ETH.compact.json to a DBC file.

Usage:
    python compact_to_dbc.py [input.json] [output.dbc]

Defaults to config.ETH_COMPACT -> Model3_ETH.dbc in the project root.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import config as _cfg
from decode_bin import load_json

_DEFAULT_OUT = Path(__file__).parent / "Model3_ETH.dbc"


def _dbc_bit_pos(start_bit: int, width: int, endianness: str) -> tuple[int, str]:
    """Return (dbc_start_bit, byte_order) for the DBC SG_ line.

    DBC little-endian (Intel):  start_bit is the LSB bit position, same numbering.
    DBC big-endian (Motorola):  start_bit is the MSB bit position using Motorola
                                 sequential numbering (byte*8 + bit_within_byte,
                                 where bit 7 is MSB of byte 0).
    The JSON uses the same convention, so we pass through unchanged.
    """
    byte_order = "1" if endianness == "LITTLE" else "0"
    return start_bit, byte_order


def _value_type(sig: dict) -> str:
    """Return DBC value type string: + for unsigned, - for signed."""
    return "-" if sig.get("signedness") == "SIGNED" else "+"


def _mux_indicator(sig: dict) -> str:
    """Return the DBC multiplexing indicator for a signal."""
    if sig.get("is_muxer"):
        return " M"
    mux_id = sig.get("mux_id")
    if mux_id is not None:
        return f" m{mux_id}"
    return ""


def _sanitize(name: str) -> str:
    """Replace characters that DBC tools don't allow in identifiers."""
    return name.replace(" ", "_").replace("-", "_")


def convert(src: Path, dst: Path) -> None:
    """Convert a compact.json file at *src* to a DBC at *dst*."""
    convert_db(load_json(src), dst)


def convert_db(db: dict, dst: Path) -> None:
    """Convert an in-memory compact-schema *db* dict to a DBC at *dst*."""
    messages = db["messages"]

    lines: list[str] = []

    lines.append(f'VERSION "{db.get("dbc_version", "")}"')
    lines.append("")
    lines.append("NS_ :")
    lines.append("")
    lines.append("BS_:")
    lines.append("")

    # ---- Named value tables ----
    # The compact schema names each enum via `value_table_name`; the same name
    # always maps to the same value set, so we can emit a shared VAL_TABLE_ for
    # each and still keep per-signal VAL_ entries below for tool compatibility.
    value_tables: dict[str, dict] = {}
    for msg in messages.values():
        for sig in msg.get("signals", {}).values():
            name = sig.get("value_table_name")
            vd = sig.get("value_description")
            if name and vd:
                value_tables.setdefault(_sanitize(name), vd)

    for tbl_name, vd in sorted(value_tables.items()):
        pairs = " ".join(f'{v} "{k}"' for k, v in sorted(vd.items(), key=lambda x: x[1]))
        lines.append(f"VAL_TABLE_ {tbl_name} {pairs} ;")
    if value_tables:
        lines.append("")

    # Collect all node names
    nodes: set[str] = set()
    for msg in messages.values():
        for n in msg.get("senders", []):
            nodes.add(n)
        origin = msg.get("originNode")
        if origin:
            nodes.add(origin)
        for sig in msg.get("signals", {}).values():
            for r in sig.get("receivers", []):
                nodes.add(r)
    nodes.discard("")

    lines.append("BU_: " + " ".join(sorted(nodes)))
    lines.append("")

    # ---- Messages ----
    # Collect value_descriptions to emit after all messages
    val_defs: list[tuple[int, str, dict]] = []  # (msg_id, sig_name, value_description)

    sig_comments: list[tuple[int, str, str]] = []  # (msg_id, sig_name, comment)

    for msg_name, msg in sorted(messages.items(), key=lambda kv: kv[1]["message_id"]):
        msg_id = msg["message_id"]
        length = msg.get("length_bytes", 8)
        sender = _sanitize(msg.get("originNode") or (msg.get("senders") or ["Vector__XXX"])[0])

        lines.append(f"BO_ {msg_id} {_sanitize(msg_name)}: {length} {sender}")

        for sig_name, sig in sorted(msg.get("signals", {}).items()):
            start_bit, byte_order = _dbc_bit_pos(
                sig["start_position"], sig["width"], sig.get("endianness", "LITTLE")
            )
            width = sig["width"]
            scale = sig.get("scale", 1)
            offset = sig.get("offset", 0)
            mn = sig.get("min", 0)
            mx = sig.get("max", 0)
            units = sig.get("units", "") or ""
            receivers = [_sanitize(r) for r in sig.get("receivers", []) if r]
            recv_str = ",".join(receivers) if receivers else "Vector__XXX"

            mux = _mux_indicator(sig)
            vtype = _value_type(sig)

            lines.append(
                f" SG_ {_sanitize(sig_name)}{mux} : "
                f"{start_bit}|{width}@{byte_order}{vtype} "
                f"({scale},{offset}) "
                f"[{mn}|{mx}] "
                f'"{units}" '
                f"{recv_str}"
            )

            vd = sig.get("value_description")
            if vd:
                val_defs.append((msg_id, sig_name, vd))

            comment = sig.get("comment")
            if comment:
                sig_comments.append((msg_id, sig_name, comment))

        lines.append("")

    # ---- Signal comments (CM_ SG_) ----
    if sig_comments:
        for msg_id, sig_name, comment in sig_comments:
            esc = comment.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'CM_ SG_ {msg_id} {_sanitize(sig_name)} "{esc}";')
        lines.append("")

    # ---- Value definitions ----
    if val_defs:
        for msg_id, sig_name, vd in val_defs:
            pairs = " ".join(f'{v} "{k}"' for k, v in sorted(vd.items(), key=lambda x: x[1]))
            lines.append(f"VAL_ {msg_id} {_sanitize(sig_name)} {pairs} ;")
        lines.append("")

    # ---- Message cycle-time attributes ----
    # Emit GenMsgCycleTime as a BA_ entry for tools that support it
    cyclic_msgs = [
        (msg["message_id"], msg["cycle_time"])
        for msg in messages.values()
        if msg.get("cycle_time", 0) > 0
    ]
    if cyclic_msgs:
        lines.append('BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;')
        lines.append('BA_DEF_DEF_ "GenMsgCycleTime" 0;')
        lines.append("")
        for mid, ct in sorted(cyclic_msgs):
            lines.append(f'BA_ "GenMsgCycleTime" BO_ {mid} {ct};')
        lines.append("")

    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {dst} ({len(messages)} messages)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert compact JSON to DBC")
    parser.add_argument("input", nargs="?", type=Path, help="Input compact JSON (default: from config)")
    parser.add_argument("output", nargs="?", type=Path, help="Output DBC file (default: <PRODUCT>_<BUS>.dbc)")
    parser.add_argument(
        "--all", action="store_true",
        help="Convert every bus DB for the product (config.COMPACT_DBS) to "
             "<PRODUCT>_<BUS>.dbc in the project root. Ignores positional args.",
    )
    args = parser.parse_args()

    if args.all:
        if not _cfg.COMPACT_DBS:
            parser.error("no compact DBs found — is TM3_ROOT (and TM3_PRODUCT) set?")
        for bus, path in sorted(_cfg.COMPACT_DBS.items()):
            out = Path(__file__).parent / f"{_cfg.PRODUCT}_{bus}.dbc"
            convert(path, out)
    else:
        src = args.input or _cfg.ETH_COMPACT
        dst = args.output or _DEFAULT_OUT
        convert(src, dst)
