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

Header format (reverse-engineered; see docs/private/.../tesla_0x0900_header.md):
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
import argparse, struct, sys
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
              f"  w[{' '.join('%04x'%x for x in wds)}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--load", type=lambda s: int(s, 0), required=True,
                    help="load address from the filename (e.g. 0xc000)")
    ap.add_argument("--mode", choices=["analyze", "build"], default="analyze")
    ap.add_argument("--rows", type=int, default=16, help="record rows to dump in analyze")
    a = ap.parse_args()
    if a.mode == "analyze":
        analyze(a.image, a.load, a.rows)
    else:
        print("build mode not implemented yet — record format still being reversed",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
