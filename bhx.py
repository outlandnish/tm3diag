#!/usr/bin/env python3
"""
BHX firmware image format parser / builder.

File layout (all multi-byte integers are big-endian):

  GlobalHeader          (12 or 16 bytes, depending on version)
  SegmentHeader         (20 bytes)  \
  raw segment data      (length bytes) | repeated for each segment
  SegmentHeader         (20 bytes)  |
  raw segment data      (length bytes) /

GlobalHeader (v1, 12 bytes):
  [0:4]   b'GHDR'
  [4:8]   uint32  version = 1
  [8:12]  uint32  total_payload_bytes  (sum of all segment lengths)

GlobalHeader (v2, 16 bytes):
  [0:4]   b'GHDR'
  [4:8]   uint32  version = 2
  [8:12]  uint32  total_payload_bytes
  [12:16] uint32  ram_app_payload      (non-zero → inter-SHDR verify+auth+erase cycle)

SegmentHeader (20 bytes):
  [0:4]   b'SHDR'
  [4:8]   uint32  version = 1
  [8:12]  uint32  start_address        (flash destination address)
  [12:16] uint32  length               (segment payload byte count)
  [16:20] uint32  crc32                (CRC-32 of the segment data,
                                        standard poly)

Notes
-----
- Checksum: the SHDR crc32 field is a standard CRC-32 (zlib/ISO 3309) of the
  raw segment data bytes.  It is passed to the ECU in the UDS RequestDownload
  service call.  hashpicker_sim's TransferData loop separately validates the
  ECU's 16-bit response checksum (either word-interleaved or byte-sum) against
  the transferred block, independent of this field.
- GHDR v2 ram_app_payload: when non-zero and segment_count > 0, hashpicker_sim
  runs a full verify+auth+erase cycle between SHDRs (see FIRMWARE_UPDATE.md Step 5b).
- GHDR v2 has only been seen in the parser code path; no v2 files exist in
  the known artifact set.
- Segments are stored contiguously with no padding between SegmentHeader and
  data, and no padding between consecutive segments.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start_address: int
    data: bytes
    checksum: int = 0          # as stored in file; 0 = not yet computed

    @property
    def length(self) -> int:
        return len(self.data)

    def compute_crc32(self) -> int:
        """Standard CRC-32 (zlib/ISO 3309) of the segment data."""
        return zlib.crc32(self.data) & 0xFFFF_FFFF

    # The two algorithms below are used by hashpicker_sim to validate the
    # ECU's UDS TransferData response checksum (per-block, not per-segment).
    # They are NOT the SHDR checksum algorithm.

    def _uds_checksum_word_interleaved(self, block: bytes) -> int:
        total = 0
        for i, b in enumerate(block):
            total += b * 0x100 if (i & 1) == 0 else b
        return total & 0xFFFF_FFFF

    def _uds_checksum_byte_sum(self, block: bytes) -> int:
        return sum(block) & 0xFFFF_FFFF


@dataclass
class BhxFile:
    segments: list[Segment] = field(default_factory=list)
    ghdr_version: int = 1      # 1 or 2
    ram_app_payload: int = 0   # only used when ghdr_version == 2

    @property
    def total_payload_bytes(self) -> int:
        return sum(s.length for s in self.segments)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class BhxParseError(Exception):
    pass


def _read_exact(data: bytes, offset: int, n: int) -> tuple[bytes, int]:
    end = offset + n
    if end > len(data):
        raise BhxParseError(
            f"Unexpected end of file at offset {offset:#x}: "
            f"need {n} bytes, only {len(data) - offset} available"
        )
    return data[offset:end], end


def parse(data: bytes) -> BhxFile:
    """Parse raw BHX bytes and return a BhxFile."""
    offset = 0

    # --- Global header ---
    tag, offset = _read_exact(data, offset, 4)
    if tag != b'GHDR':
        raise BhxParseError(f"Expected b'GHDR' at offset 0, got {tag!r}")

    raw, offset = _read_exact(data, offset, 4)
    ghdr_version = struct.unpack('>I', raw)[0]

    if ghdr_version == 1:
        raw, offset = _read_exact(data, offset, 4)
        total_payload = struct.unpack('>I', raw)[0]
        ram_app_payload = 0
    elif ghdr_version == 2:
        raw, offset = _read_exact(data, offset, 8)
        total_payload, ram_app_payload = struct.unpack('>II', raw)
    else:
        raise BhxParseError(f"Unknown GHDR version: {ghdr_version}")

    bhx = BhxFile(ghdr_version=ghdr_version, ram_app_payload=ram_app_payload)

    # --- Segment headers + data ---
    while offset < len(data):
        tag, offset = _read_exact(data, offset, 4)
        if tag != b'SHDR':
            raise BhxParseError(
                f"Expected b'SHDR' at offset {offset - 4:#x}, got {tag!r}"
            )

        raw, offset = _read_exact(data, offset, 4)
        shdr_version = struct.unpack('>I', raw)[0]
        if shdr_version != 1:
            raise BhxParseError(f"Unknown SHDR version: {shdr_version}")

        raw, offset = _read_exact(data, offset, 12)
        start_address, length, checksum = struct.unpack('>III', raw)

        seg_data, offset = _read_exact(data, offset, length)
        bhx.segments.append(Segment(
            start_address=start_address,
            data=bytes(seg_data),
            checksum=checksum,
        ))

    actual_total = bhx.total_payload_bytes
    if actual_total != total_payload:
        raise BhxParseError(
            f"GHDR total_payload_bytes={total_payload} "
            f"but sum of segment lengths={actual_total}"
        )

    return bhx


def parse_file(path: str | Path) -> BhxFile:
    return parse(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build(bhx: BhxFile) -> bytes:
    """Serialise a BhxFile back to raw BHX bytes."""
    out = bytearray()

    total = bhx.total_payload_bytes

    if bhx.ghdr_version == 1:
        out += struct.pack('>4sII', b'GHDR', 1, total)
    elif bhx.ghdr_version == 2:
        out += struct.pack('>4sIII', b'GHDR', 2, total, bhx.ram_app_payload)
    else:
        raise ValueError(f"Unknown GHDR version: {bhx.ghdr_version}")

    for seg in bhx.segments:
        csum = seg.checksum if seg.checksum else seg.compute_crc32()
        out += struct.pack('>4sIIII', b'SHDR', 1,
                           seg.start_address, seg.length, csum)
        out += seg.data

    return bytes(out)


def build_file(bhx: BhxFile, path: str | Path) -> None:
    Path(path).write_bytes(build(bhx))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def from_binary_segments(
    segments: list[tuple[int, bytes]],
    ghdr_version: int = 1,
) -> BhxFile:
    """
    Convenience constructor.

    segments: list of (start_address, data) tuples
    """
    bhx = BhxFile(ghdr_version=ghdr_version)
    for addr, data in segments:
        bhx.segments.append(Segment(start_address=addr, data=data))
    return bhx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_info(path: Path) -> None:
    bhx = parse_file(path)
    print(f"File  : {path}")
    print(
        f"GHDR  : version={bhx.ghdr_version}  "
        f"total_payload={bhx.total_payload_bytes:#010x} "
        f"({bhx.total_payload_bytes})"
    )
    if bhx.ghdr_version == 2:
        print(f"        total_size={bhx.ram_app_payload:#010x}")
    for i, seg in enumerate(bhx.segments, 1):
        crc = seg.compute_crc32()
        match = "OK" if seg.checksum == crc else "MISMATCH"
        print(
            f"SHDR #{i}: addr={seg.start_address:#010x}  "
            f"length={seg.length:#010x} ({seg.length})  "
            f"crc32={seg.checksum:#010x} [{match}]"
        )


def _cmd_extract(path: Path, out_dir: Path) -> None:
    bhx = parse_file(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, seg in enumerate(bhx.segments, 1):
        out = out_dir / f"segment_{i:02d}_{seg.start_address:#010x}.bin"
        out.write_bytes(seg.data)
        print(f"Wrote {out}  ({seg.length} bytes)")


def _cmd_create(addr_bin_pairs: list[str], out_path: Path) -> None:
    """
    addr_bin_pairs: alternating hex_address raw_bin_file pairs
    e.g. ["0x4000", "seg0.bin", "0x8000", "seg1.bin"]
    """
    if len(addr_bin_pairs) % 2 != 0:
        raise SystemExit("create requires pairs of <hex_addr> <bin_file>")
    segments = []
    for i in range(0, len(addr_bin_pairs), 2):
        addr = int(addr_bin_pairs[i], 16)
        data = Path(addr_bin_pairs[i + 1]).read_bytes()
        segments.append((addr, data))
    bhx = from_binary_segments(segments)
    build_file(bhx, out_path)
    print(
        (
            f"Wrote {out_path}  "
            f"({out_path.stat().st_size} bytes, {len(segments)} segment(s))"
        )
    )


if __name__ == "__main__":
    import sys

    usage = """\
Usage:
  bhx.py info   <file.bhx>
  bhx.py extract <file.bhx> [output_dir]
  bhx.py create  <out.bhx> <hex_addr> <bin_file> [<hex_addr> <bin_file> ...]
"""

    args = sys.argv[1:]
    if not args:
        print(usage)
        sys.exit(0)

    cmd = args[0]
    if cmd == "info" and len(args) >= 2:
        _cmd_info(Path(args[1]))
    elif cmd == "extract" and len(args) >= 2:
        out = Path(args[2]) if len(args) >= 3 else Path(
            args[1]).with_suffix("")
        _cmd_extract(Path(args[1]), out)
    elif cmd == "create" and len(args) >= 4:
        _cmd_create(args[2:], Path(args[1]))
    else:
        print(usage)
        sys.exit(1)
