"""CAN signal decoder for Model3_ETH.compact.json or a DBC file."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import config as _cfg
from decode_bin import load_json as _load_json

_ETH_COMPACT = _cfg.ETH_COMPACT


def _warn_extended_mux(msg_name: str, sig_name: str, mux_ids: list) -> None:
    """Warn when a muxed signal is valid for more than one selector value.

    Both the compact-JSON and DBC decode paths model multiplexing with a single
    scalar mux_id per signal (decode_frame matches mux_id == muxer_value). Tesla
    Model 3 firmware only ever uses scalar mux_ids, but Model S/X (or a future
    build) may use extended/nested multiplexing where one signal spans several
    selector values. That case would be silently flattened to the first id, so
    surface it loudly instead of decoding incorrectly.
    """
    warnings.warn(
        f"extended multiplexing not supported: signal {msg_name}.{sig_name} is "
        f"valid for mux ids {list(mux_ids)}; decoder will only honor the first "
        f"({mux_ids[0]}). Decoded values for the other slots may be wrong.",
        stacklevel=2,
    )


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


def _phys_to_raw(phys: float | int, sig: dict[str, Any]) -> int:
    """Inverse of _apply_scale: physical value -> raw integer field."""
    scale = sig.get("scale", 1) or 1
    offset = sig.get("offset", 0)
    width = sig.get("width", 1)
    signedness = sig.get("signedness", "UNSIGNED")

    raw = round((phys - offset) / scale)
    if signedness == "SIGNED" and raw < 0:
        raw += 1 << width
    return raw & ((1 << width) - 1)


def _pack_bits(raw: int, sig: dict[str, Any], length: int) -> int:
    """Place ``raw`` into a little-endian integer view of a ``length``-byte frame.

    Mirrors _extract_bits_little / _extract_bits_big so encode round-trips decode.
    """
    start = sig["start_position"]
    width = sig["width"]
    endian = sig.get("endianness", "LITTLE")
    raw &= (1 << width) - 1

    if endian == "LITTLE":
        return raw << start

    # Big-endian (Motorola): walk bits MSB-first from (byte, bit) like the
    # decoder, setting each bit in the little-endian frame integer.
    out = 0
    b = start // 8
    bit = start % 8
    remaining = width
    while remaining > 0:
        bits_this_byte = min(bit + 1, remaining)
        shift = bit + 1 - bits_this_byte
        chunk = (raw >> (remaining - bits_this_byte)) & ((1 << bits_this_byte) - 1)
        out |= chunk << (b * 8 + shift)
        remaining -= bits_this_byte
        b += 1
        bit = 7
    return out


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

    def __init__(self, path: Path | None = _ETH_COMPACT) -> None:
        self.messages: dict[int, dict[str, Any]] = {}  # msg_id -> msg
        self._by_node: dict[str, list[int]] = {}
        self._cantools_db = None

        # No compact JSON configured (no firmware root / TM3_ROOT) -> an empty DB.
        # Callers still work: tm3web shows raw undecoded frames, and a DBC can be
        # supplied instead via CanDatabase.from_dbc(). Decoding just names nothing.
        if path is None:
            return

        # Use the same loader as uds_local/node_config.py so an encrypted .bin
        # twin (Model3_ETH.compact.json.bin) is auto-decrypted instead of being
        # fed raw to json.load (which fails with a UnicodeDecodeError).
        raw = _load_json(Path(path))

        for name, msg in raw["messages"].items():
            mid = msg["message_id"]
            msg["name"] = name
            self.messages[mid] = msg
            node = msg.get("originNode", "unknown")
            self._by_node.setdefault(node, []).append(mid)

            for sname, sig in msg.get("signals", {}).items():
                # mux_ids lists a muxer's valid selectors (expected); a non-muxer
                # carrying >1 id means extended multiplexing we don't model.
                ids = sig.get("mux_ids")
                if not sig.get("is_muxer") and isinstance(ids, list) and len(ids) > 1:
                    _warn_extended_mux(name, sname, ids)

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
                if not sig.is_multiplexer and sig.multiplexer_ids and len(sig.multiplexer_ids) > 1:
                    _warn_extended_mux(ct_msg.name, sig.name, sig.multiplexer_ids)
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

    def message_by_name(self, name: str) -> dict[str, Any] | None:
        for msg in self.messages.values():
            if msg.get("name") == name:
                return msg
        return None

    def encode_frame(
        self,
        msg: int | str,
        signal_values: dict[str, float | int],
        *,
        strict: bool = True,
    ) -> bytes:
        """Encode named signal values into a raw CAN payload (inverse of decode).

        ``msg`` is a message id or name. ``signal_values`` maps signal name ->
        physical value; unspecified signals are left at 0. Returns the frame
        bytes (length from the DB's ``length_bytes``, default 8).

        With ``strict`` (default), an unknown signal name raises KeyError so a
        typo doesn't silently no-op. A muxer signal value selects the slot.
        """
        m = self.messages.get(msg) if isinstance(msg, int) else self.message_by_name(msg)
        if m is None:
            raise KeyError(f"message {msg!r} not in database")

        length = m.get("length_bytes", 8)
        value = 0  # little-endian integer view of the whole frame
        signals = m.get("signals", {})

        if strict:
            unknown = set(signal_values) - set(signals)
            if unknown:
                raise KeyError(f"unknown signal(s) for {m.get('name', msg)}: {sorted(unknown)}")

        for sname, sig in signals.items():
            if sname not in signal_values:
                continue
            raw = _phys_to_raw(signal_values[sname], sig)
            value |= _pack_bits(raw, sig, length)

        return value.to_bytes(length, "little")

    def nodes(self) -> list[str]:
        return sorted(self._by_node.keys())

    def messages_for_node(self, node: str) -> list[dict[str, Any]]:
        return [self.messages[mid] for mid in self._by_node.get(node, [])]

    def _get_alertlog(self):
        """Lazily build the Tesla alertLog decoder (best-effort, cached)."""
        dec = getattr(self, "_alertlog", None)
        if dec is not None or getattr(self, "_alertlog_tried", False):
            return dec
        self._alertlog_tried = True
        self._alertlog = None
        try:
            import alert_log
            self._alertlog = alert_log.get_decoder()
        except Exception:
            pass  # no firmware libs / not applicable -> plain decoding only
        return self._alertlog

    def _decode_alertlog(self, msg_id: int, data: bytes) -> list[dict[str, Any]]:
        """Synthetic rows for a <NODE>_alertLog frame (empty if not one)."""
        dec = self._get_alertlog()
        if dec is None or not dec.is_alertlog(msg_id):
            return []
        r = dec.decode(msg_id, bytes(data))
        if r is None:
            return []
        rows = [{"signal": "alertCode", "value": r.alert_code,
                 "label": r.alert or "", "units": ""}]
        if r.rationality and r.offending_id is not None:
            rows.append({"signal": "offendingMessage", "value": r.offending_id,
                         "label": r.offending_name or f"0x{r.offending_id:03X}",
                         "units": ""})
            rows.append({"signal": "errorType", "value": r.error_type,
                         "label": r.error_name or "", "units": ""})
            if r.bad_value1 is not None:
                rows.append({"signal": "badValue1", "value": r.bad_value1,
                             "label": "", "units": ""})
                rows.append({"signal": "badValue2", "value": r.bad_value2,
                             "label": "", "units": ""})
        return rows

    def decode_frame(
        self, msg_id: int, data: bytes
    ) -> list[dict[str, Any]] | None:
        """Decode a raw CAN frame into a list of signal result dicts."""
        overlay = self._decode_alertlog(msg_id, data)
        msg = self.messages.get(msg_id)
        if msg is None:
            return overlay or None

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

        results = list(overlay)
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
