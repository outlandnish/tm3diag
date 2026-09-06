"""Encode UDS request payloads / decode response payloads from ODJ field specs.

Generalizes the hand-written parse in ``scripts/di/resolver_cal.py`` into a
table-driven codec off the ODJ ``SubSpec``/``FieldSpec`` metadata, so the ODIN
runner's odx.* nodes (start/stop/results routines, read/write DID) can name
parameters instead of slicing bytes.

Wire convention (confirmed against the DIR ``RESOLVER_LEARNING``/``OFFSET_LEARNING``
results specs, which match resolver_cal byte-for-byte):
  * Byte-aligned multi-byte fields (``bit_position == 0`` and ``bit_length`` a
    multiple of 8) are **big-endian** -- the C28x ECUs byte-swap each 16-bit word
    before returning it, so on the wire the high byte comes first. ``int`` is
    signed, ``uint`` unsigned, ``ascii``/``bytes`` returned as text/raw.
  * Sub-byte fields are **LSB-relative bit fields** within the byte at
    ``byte_position`` (e.g. ``RUNNING`` = bit 4 of byte 186).

``parsed=True`` applies ``enum_map`` (raw value -> enum name); ``parsed=False``
returns the raw number -- the OdxGetParsedValue vs OdxGetRawValue distinction.

NOTE: ``odj.FieldSpec`` currently carries only the *enum* map, not *linear*
(slope/offset) scaling, so a parsed scaled field (e.g. RMSERROR x 1/512) decodes
as its raw integer. Enum-status fields -- what graphs branch on -- are exact;
adding linear scaling is a follow-up in odj.py + here.
"""
from __future__ import annotations

from .odj import FieldSpec, SubSpec


class OdjCodecError(ValueError):
    """A field layout the codec can't (yet) encode/decode."""


def _byte_aligned(fs: FieldSpec) -> bool:
    return fs.bit_position == 0 and fs.bit_length % 8 == 0


def decode_field(fs: FieldSpec, data: bytes, *, parsed: bool = True):
    """Decode one field out of a response payload. Returns None if the field lies
    past the end of a short payload."""
    if _byte_aligned(fs):
        n = fs.bit_length // 8
        if fs.byte_position >= len(data) and n:
            return None
        raw = bytes(data[fs.byte_position:fs.byte_position + n])
        if len(raw) < n:
            raw = raw.ljust(n, b"\x00")
        if fs.data_type == "ascii":
            return raw.decode("ascii", "replace").rstrip("\x00").strip()
        if fs.data_type == "bytes":
            return raw
        value = int.from_bytes(raw, "big", signed=(fs.data_type == "int"))
    elif fs.bit_length < 8 and fs.bit_position + fs.bit_length <= 8:
        # sub-byte flag/enum within a single byte (all observed ODJ bit-fields)
        if fs.byte_position >= len(data):
            return None
        value = (data[fs.byte_position] >> fs.bit_position) & ((1 << fs.bit_length) - 1)
        if fs.data_type == "int" and value >> (fs.bit_length - 1):
            value -= 1 << fs.bit_length
    else:
        raise OdjCodecError(
            f"unsupported field layout: bit_position={fs.bit_position} "
            f"bit_length={fs.bit_length} (multi-byte non-aligned not seen in ODJ)")
    if parsed and fs.enum_map:
        # A pure TRUE/FALSE enum decodes to a Python bool so graphs can compare a
        # status against literal True/False (e.g. RUNNING vs in_progress=[True]).
        if {k.upper() for k in fs.enum_map} == {"TRUE", "FALSE"}:
            return bool(value)
        inverse = {v: k for k, v in fs.enum_map.items()}
        return inverse.get(value, value)
    return value


def decode_response(sub: SubSpec | None, data: bytes, *, parsed: bool = True) -> dict:
    """Decode every output field of a SubSpec into a {name: value} dict."""
    if sub is None:
        return {}
    return {name: decode_field(fs, data, parsed=parsed) for name, fs in sub.output.items()}


def _coerce_scalar(fs: FieldSpec, value):
    """Map an enum name back to its number; leave everything else as-is."""
    if fs.enum_map and isinstance(value, str):
        return fs.enum_map.get(value, value)
    return value


def encode_fields(fields: dict[str, FieldSpec], values: dict,
                  input_size: int | None = None) -> bytes:
    """Build a request payload from a {name: value} dict per a set of FieldSpecs:
    byte-aligned fields are **big-endian** (the wire convention), sub-byte fields are
    LSB-relative bit fields. Fields absent from ``values`` are zero-fill; the buffer is
    at least ``input_size`` bytes.

    Shared packer for everything that sends named UDS inputs -- routine start/stop
    (SubSpec), WriteDataByIdentifier (SubSpec), and InputOutputControl (IoControlEntry)
    -- so they all pack identically (and correctly) to the wire.
    """
    if not fields:
        return b""
    buf = bytearray(input_size or 0)

    def _ensure(end: int) -> None:
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))

    for name, fs in fields.items():
        if name not in values:
            continue
        value = _coerce_scalar(fs, values[name])
        if _byte_aligned(fs):
            n = fs.bit_length // 8
            if fs.data_type == "ascii":
                raw = str(value).encode("ascii", "replace")[:n].ljust(n, b"\x00")
            elif fs.data_type == "bytes":
                raw = bytes(value)[:n].ljust(n, b"\x00")
            else:
                raw = int(value).to_bytes(n, "big", signed=(fs.data_type == "int"))
            _ensure(fs.byte_position + n)
            buf[fs.byte_position:fs.byte_position + n] = raw
        elif fs.bit_length < 8 and fs.bit_position + fs.bit_length <= 8:
            _ensure(fs.byte_position + 1)
            mask = ((1 << fs.bit_length) - 1) << fs.bit_position
            buf[fs.byte_position] = (
                (buf[fs.byte_position] & ~mask & 0xFF)
                | ((int(value) << fs.bit_position) & mask))
        else:
            raise OdjCodecError(
                f"unsupported input field layout for {name!r}: "
                f"bit_position={fs.bit_position} bit_length={fs.bit_length}")
    return bytes(buf)


def encode_request(sub: SubSpec | None, values: dict) -> bytes:
    """Build a request payload from a {name: value} dict per the SubSpec's input
    fields (see ``encode_fields``). Fields absent from ``values`` are zero-fill."""
    if sub is None:
        return b""
    return encode_fields(sub.input, values, sub.input_size)
