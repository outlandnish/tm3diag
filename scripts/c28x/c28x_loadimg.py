#!/usr/bin/env python3
"""Parse + reassemble Tesla C28x PM-family firmware images (0x0900 header).

These .bin payloads (PMR / pmrbl / pmrbu / PM-RAMAPP) are NOT flat code. Layout:

    [0x0900 header]            magic+ver, type, sig, base/flags, ENTRY[6], END[7], hash
    [load/init records]        repeating fixed-size records (being reversed here)
    [payload chunk(s)]         the actual code/data the loader places in memory

A linear disassembly of the raw .bin desyncs because it reads the header + records
as if they were code. To analyze in Ghidra we must apply the records to reconstruct
the runtime memory image, then disassemble THAT at the right base.

This tool runs in two modes:
  analyze  — dump the header + a structured view of the post-header records so the
             record format can be pinned down by eye / by cross-image diff.
  build    — (once the format is confirmed) emit the reassembled flat runtime image.

Header format (reverse-engineered):
  u32[0] magic 0x0900 (low) | version<<16 (0x1c=P28)
  u32[1] image type/seq (pmrbl=1, pmrbu=2)
  u32[2..3] signature/id
  u32[4] base/flags (0x8000 PM flash, 0x4601 RAMAPP)
  u32[5] constant 0x0600
  u32[6] ENTRY address (often below load addr -> sector A, not shipped)
  u32[7] END address
  u32[8] flags/0
  u32[9..] signature/hash block
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def u16(d, o): return d[o] | (d[o + 1] << 8)
def u32(d, o): return struct.unpack_from("<I", d, o)[0]


def parse_header(d: bytes) -> dict:
    if u16(d, 0) != 0x0900:
        raise ValueError(f"not a 0x0900 image (magic=0x{u16(d,0):04x})")
    f = [u32(d, i * 4) for i in range(9)]
    return {
        "magic": f[0] & 0xFFFF,
        "version": (f[0] >> 16) & 0xFF,
        "img_type": f[1] & 0xFFFF,
        "sig0": f[2], "sig1": f[3],
        "base_flags": f[4],
        "const5": f[5],
        "entry": f[6],
        "end": f[7],
        "flags8": f[8],
    }


def analyze(path: Path, load_addr: int, nrecs: int) -> None:
    d = path.read_bytes()
    h = parse_header(d)
    print(f"== {path.name} ==  file={len(d)}B ({len(d)//2} words)  load=0x{load_addr:x}")
    for k, v in h.items():
        print(f"  hdr.{k:10} = 0x{v:08x}" if isinstance(v, int) else f"  hdr.{k}={v}")
    print(f"  derived: entry below load by 0x{load_addr - h['entry']:x} words"
          f"  ;  end-entry = 0x{h['end'] - h['entry']:x}")
    # Dump the region after the header as both word- and dword- views so the record
    # stride/format can be eyeballed. Header+hash is ~0x24-0x40 bytes; show from 0x20.
    start = 0x20
    print(f"  --- post-header dwords from 0x{start:x} ({nrecs} x 16B rows) ---")
    for r in range(nrecs):
        o = start + r * 16
        if o + 16 > len(d):
            break
        dws = [u32(d, o + i * 4) for i in range(4)]
        wds = [u16(d, o + i * 2) for i in range(8)]
        print(f"    0x{o:04x}: dw[{dws[0]:08x} {dws[1]:08x} {dws[2]:08x} {dws[3]:08x}]"
              f"  w[{' '.join(f'{x:04x}' for x in wds)}]")


def byteswap(path: Path, out: Path) -> None:
    """Byte-swap every 16-bit word of a Tesla C28x flash image so it matches the
    C28x instruction stream Ghidra expects.

    KEY FINDING (2026-06-23): Tesla TMS320F28377D flash images are stored with each
    16-bit word's two bytes REVERSED relative to how the C28x core fetches them. This
    is a property of the F28377D flash format, NOT of a particular ECU — it applies to
    EVERY F28377D-based device's image: PM/DI (inverter), PCS, and any other module on
    that part. Covers all eras and both BHX-extracted payloads and the raw 2026 .bins.
    Reading them as native little-endian words desyncs disassembly into valid-but-
    incoherent garbage (this is why earlier linear sweeps failed). Swapping the bytes
    of every word yields coherent code: real function prologues (MOVL *SP++ ×3 = b2bd
    aabd a2bd, then ADDB SP,#N), LCR/LC call graphs, Q-math, and LRETR at ~7/KB.

    Validated against OpenInverter (real F28377D code built with TI CGT 25.11.1):
    its instruction-stream prologue `b2bd aabd a2bd` occurs 0x in the Tesla images
    read as LE16 but 67/329/161x (PMR2019/DIR2019/PMR2026) read byte-swapped.

    UPDATE 2026-06-25 (validated): the earlier note that pmrbl/pmrbu artifacts are
    "different / undetermined / not the flat app body" was WRONG. The bu .bhx
    (PM_PCBA_28_UP-…) is a SINGLE flat segment @0x88000 (49152 bytes / 0x6000 words),
    and swap16(bhx_payload) == the coherent Ghidra image byte-for-byte (49152/49152).
    The bl (PM_PCBA_28-…) is the same: a flat @0x82000 image. So byte-swapping DOES
    yield coherent, decompilable flat C28x code for these bootloader artifacts — the
    full UDS/flash handlers decompile cleanly. The chain is simply:
        bhx segment payload  <--byteswap (this fn, its own inverse)-->  loadable/Ghidra
    To rebuild a flashable artifact after patching the loadable image: byteswap it back
    and use the result as the .bhx segment payload. (Their function prologues vary; not
    every fn starts with the b2bd/aabd/a2bd triple — that pattern is one of several.)
    """
    d = path.read_bytes()
    sw = bytearray(len(d))
    for o in range(0, len(d) - 1, 2):
        sw[o] = d[o + 1]
        sw[o + 1] = d[o]
    if len(d) & 1:
        sw[-1] = d[-1]
    out.write_bytes(sw)
    print(f"byte-swapped {len(d)} bytes ({len(d)//2} words): {path.name} -> {out}")
    print("Import in Ghidra as TMS320C28x:LE:32:default; set image base to the load "
          "address; seed disassembly at a MOVL*SP++ prologue (search 'bd b2 bd aa bd a2').")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--load", type=lambda s: int(s, 0), default=0,
                    help="load address from the filename (e.g. 0xc000)")
    ap.add_argument("--mode", choices=["analyze", "build", "swap"], default="analyze")
    ap.add_argument("--rows", type=int, default=16, help="record rows to dump in analyze")
    ap.add_argument("--out", type=Path,
                    help="output path for swap mode (default: <image>.swapped.bin)")
    a = ap.parse_args()
    if a.mode == "analyze":
        analyze(a.image, a.load, a.rows)
    elif a.mode == "swap":
        out = a.out or a.image.with_suffix(a.image.suffix + ".swapped.bin")
        byteswap(a.image, out)
    else:
        print("build mode not implemented yet — record format still being reversed",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
