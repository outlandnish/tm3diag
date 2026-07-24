#!/usr/bin/env python3
"""Encoder/validator for the PCS 0x441 VC_pcsDCDCInterface protected control frame.

Derived from RE of PCS variant-411 CPU1 firmware (2024.8.9), functions:
  - PCS_rxVC_pcsInterface_0x441  @ 0x8f7da   (RX handler / validator)
  - PCS_canChecksum_0x441        @ 0x94f3e   (additive checksum fold)

The PCS rejects a 0x441 frame (and eventually raises a107_vcPcsDCDCInterfaceMia)
unless BOTH hold:
  1. byte7 (checksum) reproduces the firmware fold, and
  2. byte6 bits[5:2] (4-bit rolling counter) increments by 1 (mod 16) each frame.

== Frame layout (DLC 8, bytes 0..7 on the wire) ==
The C28x firmware stores the 8 CAN bytes as 4 little-endian 16-bit words:
    w0 = b0 | b1<<8     w1 = b2 | b3<<8
    w2 = b4 | b5<<8     w3 = b6 | b7<<8
The handler reads w3 = *(frame+3):
    checksum     = w3 >> 8        -> byte 7
    rollingCount = (w3 >> 2) & 0xF  (in the LOW byte of w3) -> byte 6 bits[5:2]
So:
    byte 7 = checksum
    byte 6 = [.. .. cnt cnt cnt cnt .. ..]  (counter in bits 5..2)
    bytes 0..5 = payload (vehicle->PCS control signals; layout TBD)

== Checksum derivation (exact, from PCS_canChecksum_0x441) ==
The fold is:  acc = (0x69 - C) ; for w in [w3,w2,w1,w0]: acc += (w>>8) + w ; return acc & 0xFF
where C is the received checksum byte. Because w3 carries C in its high byte, the
C terms cancel and the self-consistent checksum reduces to a plain byte-sum:

    checksum = (0x69 + b0+b1+b2+b3+b4+b5 + byte6) & 0xFF

i.e. magic 0x69 + the sum of the six payload bytes + the counter byte (byte6),
all the non-checksum bytes, mod 256. (byte7 itself is excluded.)

NOTE: 0x69 is the per-ID magic for 0x441 (the firmware's `MOVB AL,#0x69`). Other
Tesla IDs use different magics; do not reuse 0x69 elsewhere.
"""

MAGIC_0x441 = 0x69


def checksum_0x441(payload6: bytes, counter: int) -> int:
    """Compute the byte-7 checksum for a 0x441 frame.

    payload6 : the 6 payload bytes (bytes 0..5).
    counter  : 4-bit rolling counter (0..15), goes in byte6 bits[5:2].
    """
    if len(payload6) != 6:
        raise ValueError("payload6 must be exactly 6 bytes (CAN bytes 0..5)")
    byte6 = (counter & 0xF) << 2
    return (MAGIC_0x441 + sum(payload6) + byte6) & 0xFF


def encode_0x441(payload6: bytes, counter: int) -> bytes:
    """Build a full 8-byte 0x441 frame that the PCS will accept."""
    byte6 = (counter & 0xF) << 2
    byte7 = checksum_0x441(payload6, counter)
    return bytes(payload6) + bytes([byte6, byte7])


def validate_0x441(frame: bytes) -> tuple[bool, bool]:
    """Re-implement the firmware acceptance test. Returns (checksum_ok, counter_value).

    Note: counter-sequence validity (==prev+1 mod 16) is stateful and not checked here;
    this only validates the checksum and extracts the counter.
    """
    if len(frame) != 8:
        raise ValueError("0x441 frame is DLC 8")
    counter = (frame[6] >> 2) & 0xF
    want = checksum_0x441(frame[:6], counter)
    return (frame[7] == want, counter)


class Rolling0x441:
    """Stateful 0x441 transmitter: increments the 4-bit rolling counter each frame."""

    def __init__(self, start: int = 0):
        self._cnt = start & 0xF

    def next_frame(self, payload6: bytes) -> bytes:
        frame = encode_0x441(payload6, self._cnt)
        self._cnt = (self._cnt + 1) & 0xF
        return frame


if __name__ == "__main__":
    # Self-test: every frame we encode must validate, across all 16 counter values.
    tx = Rolling0x441(start=0)
    payload = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06])
    for i in range(16):
        f = tx.next_frame(payload)
        ok, cnt = validate_0x441(f)
        assert ok, f"checksum mismatch on frame {i}: {f.hex()}"
        assert cnt == i, f"counter mismatch: got {cnt}, expected {i}"
        print(f"cnt={cnt:2d}  frame={f.hex(' ')}  cksum=0x{f[7]:02x}")
    print("OK: all 16 rolling frames validate against the firmware fold.")
