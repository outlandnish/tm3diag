"""CAN signal decoder for Model3_ETH.compact.json or a DBC file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config as _cfg

_ETH_COMPACT = _cfg.ETH_COMPACT


def _extract_bits_little(data: bytes, start_bit: int, width: int) -> int:
    """Extract bits from a CAN frame using little-endian (Intel) bit numbering."""
    value = int.from_bytes(data, "little")
    return (value >> start_bit) & ((1 << width) - 1)


def _extract_bits_big(data: bytes, start_bit: int, width: int) -> int:
    """Extract bits from a CAN frame using big-endian (Motorola) bit numbering.

    The start_bit is the MSB position in Motorola byte-swapped notation.
    """
    # Convert Motorola start bit to a linear bit offset within the frame bytes
    # Motorola start_bit: byte_index * 8 + (7 - bit_within_byte) but stored as
    # the bit position of the MSBit within a big-endian view.
    # Standard approach: walk bits from MSB downward.
    byte_order = start_bit // 8
    bit_in_byte = start_bit % 8

    result = 0
    remaining = width
    b = byte_order
    bit = bit_in_byte

    while remaining > 0:
        bits_this_byte = min(bit + 1, remaining)
        mask = (1 << bits_this_byte) - 1
        shift = bit + 1 - bits_this_byte
        chunk = (data[b] >> shift) & mask
        result = (result << bits_this_byte) | chunk
        remaining -= bits_this_byte
        b += 1
        bit = 7

    return result


def _apply_scale(raw: int, sig: dict[str, Any]) -> float:
    scale = sig.get("scale", 1)
    offset = sig.get("offset", 0)
    signedness = sig.get("signedness", "UNSIGNED")
    width = sig.get("width", 1)

    if signedness == "SIGNED" and raw >= (1 << (width - 1)):
        raw -= 1 << width

    return raw * scale + offset


def _is_hex_signal(sig: dict[str, Any], name: str) -> bool:
    lower = name.lower()
    return sig.get("width", 0) % 8 == 0 and ("hash" in lower or "crc" in lower)


def _int_to_hex(value: int, width_bits: int) -> str | None:
    if value == 0:
        return None
    return value.to_bytes(width_bits // 8, "little").hex()


def decode_signal(data: bytes, sig: dict[str, Any], name: str = "") -> tuple[float | int, str | None]:
    """Return (physical_value, enum_label_or_None) for a signal."""
    start = sig["start_position"]
    width = sig["width"]
    endian = sig.get("endianness", "LITTLE")

    if endian == "LITTLE":
        raw = _extract_bits_little(data, start, width)
    else:
        raw = _extract_bits_big(data, start, width)

    phys = _apply_scale(raw, sig)

    if _is_hex_signal(sig, name):
        return phys, _int_to_hex(raw, width)

    label: str | None = None
    vd = sig.get("value_description")
    if vd:
        # value_description maps label -> raw_int
        for lbl, val in vd.items():
            if val == raw:
                label = lbl
                break

    return phys, label


class CanDatabase:
    """Parsed representation of a compact JSON or DBC CAN database."""

    def __init__(self, path: Path = _ETH_COMPACT) -> None:
        with open(path) as f:
            raw = json.load(f)

        self.messages: dict[int, dict[str, Any]] = {}  # msg_id -> msg
        self._by_node: dict[str, list[int]] = {}
        self._cantools_db = None

        for name, msg in raw["messages"].items():
            mid = msg["message_id"]
            msg["name"] = name
            self.messages[mid] = msg
            node = msg.get("originNode", "unknown")
            self._by_node.setdefault(node, []).append(mid)

    @classmethod
    def from_dbc(cls, path: Path) -> CanDatabase:
        import cantools
        ct_db = cantools.database.load_file(str(path))

        obj = cls.__new__(cls)
        obj.messages = {}
        obj._by_node = {}
        obj._cantools_db = ct_db

        for ct_msg in ct_db.messages:
            sender = ct_msg.senders[0] if ct_msg.senders else "unknown"
            signals: dict[str, Any] = {}
            for sig in ct_msg.signals:
                vd = None
                if sig.choices:
                    # cantools choices: {int_value: "label"} — invert to match
                    # compact JSON convention {label: int_value}
                    vd = {str(label): int(val) for val, label in sig.choices.items()}
                signals[sig.name] = {
                    "start_position": sig.start,
                    "width": sig.length,
                    "endianness": "LITTLE" if sig.byte_order == "little_endian" else "BIG",
                    "signedness": "SIGNED" if sig.is_signed else "UNSIGNED",
                    "scale": sig.scale if sig.scale is not None else 1,
                    "offset": sig.offset if sig.offset is not None else 0,
                    "min": sig.minimum if sig.minimum is not None else 0,
                    "max": sig.maximum if sig.maximum is not None else 0,
                    "units": sig.unit or "",
                    "is_muxer": sig.is_multiplexer,
                    "mux_id": sig.multiplexer_ids[0] if sig.multiplexer_ids else None,
                    "value_description": vd,
                    "receivers": list(sig.receivers),
                }
            msg_entry: dict[str, Any] = {
                "message_id": ct_msg.frame_id,
                "name": ct_msg.name,
                "length_bytes": ct_msg.length,
                "originNode": sender,
                "cycle_time": ct_msg.cycle_time or 0,
                "signals": signals,
            }
            obj.messages[ct_msg.frame_id] = msg_entry
            obj._by_node.setdefault(sender, []).append(ct_msg.frame_id)

        return obj

    def nodes(self) -> list[str]:
        return sorted(self._by_node.keys())

    def messages_for_node(self, node: str) -> list[dict[str, Any]]:
        return [self.messages[mid] for mid in self._by_node.get(node, [])]

    def decode_frame(
        self, msg_id: int, data: bytes
    ) -> list[dict[str, Any]] | None:
        """Decode a raw CAN frame into a list of signal result dicts."""
        msg = self.messages.get(msg_id)
        if msg is None:
            return None

        # Determine muxer value if present
        muxer_value: int | None = None
        for sig in msg["signals"].values():
            if sig.get("is_muxer"):
                raw = _extract_bits_little(
                    data, sig["start_position"], sig["width"]
                ) if sig.get("endianness", "LITTLE") == "LITTLE" else _extract_bits_big(
                    data, sig["start_position"], sig["width"]
                )
                muxer_value = raw
                break

        results = []
        for sname, sig in msg["signals"].items():
            if sig.get("is_muxer"):
                continue  # don't surface the mux selector itself
            mux_id = sig.get("mux_id")
            if mux_id is not None and mux_id != muxer_value:
                continue  # wrong mux slot

            if len(data) * 8 < sig["start_position"] + sig["width"]:
                continue  # frame too short

            phys, label = decode_signal(data, sig, sname)
            results.append(
                {
                    "signal": sname,
                    "value": phys,
                    "label": label,
                    "units": sig.get("units", ""),
                }
            )

        return results
