#!/usr/bin/env python3
"""Parser for Tesla gateway "cluster" logs (CL/DATA/*.CLH + *.CLB).

These appear on service / ConfigLoader SD cards under CL/DATA/. Each log is a
pair:

  - <n>.CLH  a fixed-size index ("Poppyseed" magic + firmware git SHA, then
             32-byte segment records)
  - <n>.CLB  the body: one variable-length segment per index record, each an
             8-byte segment header followed by a LEB128 varint stream

The varint stream is a delta-encoded vehicle *signal* log keyed by an internal
enumerated signal id (a dense, contiguous id space well above the 11-bit CAN
range — so these are decoded signals, not raw CAN frames). Mapping signal ids
to names requires the firmware's signal table, which is not on the card; this
module parses the container and exposes the raw varint records so that layer
can be added once a firmware dump is available.

Usage:
    python clog.py info    <n>.CLH
    python clog.py varints <n>.CLH [--segment N] [--limit N]
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

CLH_MAGIC = b"Poppyseed\x00"
RECORD_MAGIC = b"\xba\xdd\xca\xfe"
CLH_HEADER_SIZE = 0x40
CLH_RECORD_SIZE = 0x20
SEGMENT_HEADER_SIZE = 8


@dataclass
class SegmentIndex:
    seq: int                 # 0-based segment number
    byte_length: int         # segment length in the CLB (== end_offset - start_offset)
    start_time: int          # unix epoch seconds
    end_time: int            # unix epoch seconds
    start_offset: int        # byte offset of this segment in the CLB
    end_offset: int          # byte offset one past this segment in the CLB


@dataclass
class ClusterLogIndex:
    git_sha: str
    segments: list[SegmentIndex] = field(default_factory=list)


def parse_clh(path: Path | str) -> ClusterLogIndex:
    """Parse a .CLH index file."""
    data = Path(path).read_bytes()
    if data[2:2 + len(CLH_MAGIC)] != CLH_MAGIC:
        raise ValueError(f"not a CLH file (bad magic at 0x02): {data[2:12]!r}")
    git_sha = data[0x0C:0x20].hex()

    segments: list[SegmentIndex] = []
    off = CLH_HEADER_SIZE
    while off + CLH_RECORD_SIZE <= len(data):
        rec = data[off:off + CLH_RECORD_SIZE]
        if rec[0:4] != RECORD_MAGIC:
            break  # zero padding / end of records
        byte_length = int.from_bytes(rec[8:11], "big")
        start_time = struct.unpack_from(">I", rec, 11)[0]
        end_time = struct.unpack_from(">I", rec, 15)[0]
        seq = struct.unpack_from(">I", rec, 19)[0]
        start_offset = struct.unpack_from(">I", rec, 23)[0]
        end_offset = struct.unpack_from(">I", rec, 27)[0]
        segments.append(SegmentIndex(
            seq=seq,
            byte_length=byte_length,
            start_time=start_time,
            end_time=end_time,
            start_offset=start_offset,
            end_offset=end_offset,
        ))
        off += CLH_RECORD_SIZE
    return ClusterLogIndex(git_sha=git_sha, segments=segments)


def _read_uvarint(buf: bytes, i: int, end: int) -> tuple[int, int]:
    """Decode an unsigned LEB128 varint at buf[i:end]. Returns (value, next_i)."""
    shift = 0
    result = 0
    while i < end:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    raise ValueError("truncated varint")


def iter_segment_varints(clb: bytes, seg: SegmentIndex) -> list[int]:
    """Decode the LEB128 varint stream of one CLB segment (after its header)."""
    i = seg.start_offset + SEGMENT_HEADER_SIZE
    end = seg.end_offset
    out: list[int] = []
    while i < end:
        value, i = _read_uvarint(clb, i, end)
        out.append(value)
    return out


def _clb_path_for(clh_path: Path) -> Path:
    return clh_path.with_suffix(".CLB")


def _fmt_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _cmd_info(clh_path: Path) -> None:
    idx = parse_clh(clh_path)
    print(f"File     : {clh_path}")
    print(f"Firmware : {idx.git_sha}")
    print(f"Segments : {len(idx.segments)}")
    for s in idx.segments:
        span = s.end_offset - s.start_offset
        flag = "" if span == s.byte_length else f"  (len field={s.byte_length}!)"
        print(
            f"  seq {s.seq}: {_fmt_time(s.start_time)} -> {_fmt_time(s.end_time)}  "
            f"offset 0x{s.start_offset:x}-0x{s.end_offset:x} ({span} bytes){flag}"
        )


def _cmd_varints(clh_path: Path, segment: int | None, limit: int) -> None:
    idx = parse_clh(clh_path)
    clb = _clb_path_for(clh_path).read_bytes()
    for s in idx.segments:
        if segment is not None and s.seq != segment:
            continue
        vals = iter_segment_varints(clb, s)
        shown = vals if limit == 0 else vals[:limit]
        print(f"seg {s.seq}: {len(vals)} varints"
              + (f" (showing {len(shown)})" if len(shown) != len(vals) else ""))
        print(f"  {shown}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="show the segment index")
    p_info.add_argument("clh", type=Path)

    p_var = sub.add_parser("varints", help="decode the varint stream of segment(s)")
    p_var.add_argument("clh", type=Path)
    p_var.add_argument("--segment", type=int, default=None, help="only this seq")
    p_var.add_argument("--limit", type=int, default=40, help="0 = no limit")

    args = parser.parse_args()
    if args.cmd == "info":
        _cmd_info(args.clh)
    elif args.cmd == "varints":
        _cmd_varints(args.clh, args.segment, args.limit)


if __name__ == "__main__":
    main()
