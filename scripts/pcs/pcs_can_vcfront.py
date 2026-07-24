#!/usr/bin/env python3
"""Encoders/decoders for the three VCFRONT->PCS frames that drive a024_vcfrontMia.

Derived from RE of PCS variant-411 CPU1 firmware (2024.8.9):
  - PCS_rxVCFRONT_sensors_0x321        @ 0x9395e
  - PCS_rxVCFRONT_vehicleStatus_0x3A1  @ 0x932f8
  - PCS_rxVCFRONT_LVPowerState_0x545   @ 0x93276
  - a024 MIA supervisors: PCS_miaTimeout_0x51E_0x217_0x321 @ 0x907fe (0x321),
    PCS_miaTimeout_0x3A1_0x441 @ 0x91793 (0x3A1), PCS_miaTimeout_0x545 @ 0x95047,
    scheduler FUN_0009432f @ 0x9432f.

== KEY FINDING (corrects the earlier handoff assumption) ==
NONE of the three VCFRONT handlers verify a checksum OR a rolling counter. Each
handler, at the assembly level, does only:
  1. buffer the frame, DLC must be >= 8 (else it's logged and ignored);
  2. an internal "enable + freshness-slot phase" gate (firmware-maintained state,
     NOT carried in the payload);
  3. extract exactly ONE scaled field, compare against an SNA sentinel, range it;
  4. age that message's MIA down-counter toward 0 and CLEAR its a024 sub-flag in
     the shared word _DAT_14733.

=> a024_vcfrontMia clears when 0x321 + 0x3A1 + 0x545 simply ARRIVE at rate with
   DLC 8. There is no checksum/counter to get "wrong". The controller's
   vehicleStatusCounter/vehicleStatusChecksum (0x3A1) and calc_checksum (0x545)
   are HARMLESS but IRRELEVANT to the alert -- the firmware never reads them.

These encoders therefore exist mainly to (a) populate the one meaningful signal
each frame carries (so the PCS sees sane values, not just to clear the alert) and
(b) document the real 2024 bit layout. A zero payload with DLC 8 at rate already
clears a024.

== Bit layout convention ==
C28x is word-addressed; the handler reads 16-bit words w0..w3 where
    w0 = b0 | b1<<8   w1 = b2 | b3<<8   w2 = b4 | b5<<8   w3 = b6 | b7<<8
Signals below are expressed as standard DBC little-endian (Intel) start-bit +
width over the 64-bit frame (bit = byte*8 + bit_in_byte).

== Timeout budget (why 100 ms / 50 ms TX is plenty) ==
Each MIA counter ages +10 per supervisor tick of silence, trips at >=100 (=> ~10
supervisor periods of grace), and is knocked DOWN ~11 per received frame.
Supervisor prescalers off the scheduler base tick (FUN_0009432f): 0x321 every
1000 ticks, 0x3A1 every 50, 0x545 every 33. With the ~1 ms PCS base tick that is
roughly: 0x321 ~10 s grace, 0x3A1 ~500 ms, 0x545 ~330 ms -- so 100 ms (0x321/0x3A1)
and 50 ms (0x545) transmit periods are comfortable.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# generic little-endian bit field helpers over an 8-byte frame
# ---------------------------------------------------------------------------
def _get_le_field(frame: bytes, start_bit: int, width: int) -> int:
    """Read a little-endian (Intel) field: start_bit is the LSB position."""
    val = int.from_bytes(bytes(frame), "little")
    return (val >> start_bit) & ((1 << width) - 1)


def _set_le_field(frame: bytearray, start_bit: int, width: int, raw: int) -> None:
    val = int.from_bytes(bytes(frame), "little")
    mask = ((1 << width) - 1) << start_bit
    val = (val & ~mask) | ((raw & ((1 << width) - 1)) << start_bit)
    frame[:] = val.to_bytes(8, "little")


# ---------------------------------------------------------------------------
# 0x321 VCFRONT_sensors  (handler 0x9395e)
#   field = ((w1 & 0x1F) << 6) | (w0 >> 10)  -> LE start bit 10, width 11
#   SNA = 0x7FF ; scale 0.125 ; offset -40.0  (a temperature, deg C)
# ---------------------------------------------------------------------------
F321_START, F321_WIDTH = 10, 11
F321_SNA = 0x7FF
F321_SCALE, F321_OFFSET = 0.125, -40.0


def encode_0x321(temp_c: float | None = None) -> bytes:
    """Build a 0x321 frame. temp_c=None -> SNA. Any DLC-8 frame clears a024."""
    frame = bytearray(8)
    if temp_c is None:
        raw = F321_SNA
    else:
        raw = round((temp_c - F321_OFFSET) / F321_SCALE)
        raw = max(0, min(F321_SNA - 1, raw))
    _set_le_field(frame, F321_START, F321_WIDTH, raw)
    return bytes(frame)


def decode_0x321(frame: bytes):
    raw = _get_le_field(frame, F321_START, F321_WIDTH)
    if raw == F321_SNA:
        return None
    return raw * F321_SCALE + F321_OFFSET


# ---------------------------------------------------------------------------
# 0x3A1 VCFRONT_vehicleStatus  (handler 0x932f8)
#   lockout: byte0 bit0 must be 0 for the field to be consumed (else skipped;
#            the a024 clear still happens regardless).
#   field = ((w3 & 0xF) << 6) | (w2 >> 10)  -> LE start bit 42, width 10
#   SNA = 0x3FF ; scale 0.1 ; offset 0.0
# ---------------------------------------------------------------------------
F3A1_START, F3A1_WIDTH = 42, 10
F3A1_SNA = 0x3FF
F3A1_SCALE, F3A1_OFFSET = 0.1, 0.0
F3A1_LOCKOUT_BIT = 0  # byte0 bit0; must be 0 to update the signal


def encode_0x3A1(value: float | None = None, lockout: bool = False) -> bytes:
    frame = bytearray(8)
    if lockout:
        _set_le_field(frame, F3A1_LOCKOUT_BIT, 1, 1)
    raw = F3A1_SNA if value is None else max(
        0, min(F3A1_SNA - 1, round((value - F3A1_OFFSET) / F3A1_SCALE)))
    _set_le_field(frame, F3A1_START, F3A1_WIDTH, raw)
    return bytes(frame)


def decode_0x3A1(frame: bytes):
    if _get_le_field(frame, F3A1_LOCKOUT_BIT, 1):
        return ("locked", None)
    raw = _get_le_field(frame, F3A1_START, F3A1_WIDTH)
    if raw == F3A1_SNA:
        return ("sna", None)
    return ("ok", raw * F3A1_SCALE + F3A1_OFFSET)


# ---------------------------------------------------------------------------
# 0x545 VCFRONT_LVPowerState  (handler 0x93276)
#   lockout: byte0 bits[1:0] must be 0 for the field to be consumed.
#   field = ((w3 & 0xF) << 8) | (w2 >> 8)  -> LE start bit 40, width 12
#   SNA = 0xFFF ; scale 0.005443676 ; offset 0.0
# ---------------------------------------------------------------------------
F545_START, F545_WIDTH = 40, 12
F545_SNA = 0xFFF
F545_SCALE, F545_OFFSET = 0.005443676, 0.0
F545_LOCKOUT_MASK = 0x3  # byte0 bits[1:0]; must be 0 to update the signal


def encode_0x545(value: float | None = None, lockout: int = 0) -> bytes:
    frame = bytearray(8)
    if lockout:
        _set_le_field(frame, 0, 2, lockout & F545_LOCKOUT_MASK)
    raw = F545_SNA if value is None else max(
        0, min(F545_SNA - 1, round((value - F545_OFFSET) / F545_SCALE)))
    _set_le_field(frame, F545_START, F545_WIDTH, raw)
    return bytes(frame)


def decode_0x545(frame: bytes):
    if _get_le_field(frame, 0, 2) & F545_LOCKOUT_MASK:
        return ("locked", None)
    raw = _get_le_field(frame, F545_START, F545_WIDTH)
    if raw == F545_SNA:
        return ("sna", None)
    return ("ok", raw * F545_SCALE + F545_OFFSET)


if __name__ == "__main__":
    # Round-trip self-test mirroring the firmware extraction (bit math only;
    # the firmware applies no checksum/counter check on these three).
    # 0x321: pick 25 C -> raw = (25 - -40)/0.125 = 520
    f = encode_0x321(25.0)
    assert _get_le_field(f, F321_START, F321_WIDTH) == 520, f.hex(" ")
    assert abs(decode_0x321(f) - 25.0) < 1e-6
    assert decode_0x321(encode_0x321(None)) is None  # SNA
    print(f"0x321  25.0C  -> {f.hex(' ')}  decode={decode_0x321(f):.3f}")

    # 0x3A1: pick 13.5 -> raw 135
    f = encode_0x3A1(13.5)
    assert _get_le_field(f, F3A1_START, F3A1_WIDTH) == 135, f.hex(" ")
    assert decode_0x3A1(f) == ("ok", 13.5)
    assert decode_0x3A1(encode_0x3A1(None)) == ("sna", None)
    assert decode_0x3A1(encode_0x3A1(13.5, lockout=True))[0] == "locked"
    print(f"0x3A1  13.5   -> {f.hex(' ')}  decode={decode_0x3A1(f)}")

    # 0x545: pick 12.0 V -> raw = round(12/0.005443676) = 2204
    f = encode_0x545(12.0)
    raw545 = _get_le_field(f, F545_START, F545_WIDTH)
    assert raw545 == round(12.0 / F545_SCALE), (raw545, f.hex(" "))
    val = decode_0x545(f)
    assert val[0] == "ok" and abs(val[1] - 12.0) < F545_SCALE
    assert decode_0x545(encode_0x545(None)) == ("sna", None)
    assert decode_0x545(encode_0x545(12.0, lockout=1))[0] == "locked"
    print(f"0x545  12.0V  -> {f.hex(' ')}  decode={val}")

    # The alert-clearing invariant: ANY DLC-8 frame (even all-zero) is accepted
    # for a024 purposes. Demonstrate the fields decode without error on zeros.
    z = bytes(8)
    print(f"zero   0x321={decode_0x321(z)} 0x3A1={decode_0x3A1(z)} 0x545={decode_0x545(z)}")
    print("OK: VCFRONT frame round-trips match the firmware field extraction.")
