"""Intel HEX parser — presents the same interface as bhx.BhxFile.

Run directly for a small CLI:

    python ihex.py info    <file.hex|file.hgz>
    python ihex.py decode  <file.hgz> [out.hex]   # gunzip + normalise to Intel HEX
    python ihex.py extract <file.hex|file.hgz> [out_dir]
"""

import gzip
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from intelhex import IntelHex

from bhx import Segment


@dataclass
class IHexFile:
    segments: list = field(default_factory=list)


def _load_tolerant(text: str) -> IntelHex:
    """Load Intel HEX text, tolerating multiple start-address records.

    Tesla's gateway image (gwapp.img / GW.HGZ) is a dual-bank image carrying
    two type-05 Start Linear Address records (one entry point per bank).
    intelhex rejects the duplicate with DuplicateStartAddressRecordError, so
    we strip type-03/05 records — they hold only the CPU entry point, which
    the Segment model does not use — and keep all data records.
    """
    ih = IntelHex()
    try:
        ih.loadhex(io.StringIO(text))
        return ih
    except Exception:
        kept = [
            line for line in text.splitlines()
            if not (line.startswith(":") and line[7:9] in ("03", "05"))
        ]
        ih = IntelHex()
        ih.loadhex(io.StringIO("\n".join(kept)))
        return ih


def _to_file(ih: IntelHex) -> IHexFile:
    f = IHexFile()
    for start, end in ih.segments():
        data = bytes(ih.tobinarray(start=start, end=end - 1))
        f.segments.append(Segment(start_address=start, data=data))
    return f


def parse_bytes(data: bytes) -> IHexFile:
    return _to_file(_load_tolerant(data.decode("ascii")))


def parse_file(src: Path | IO) -> IHexFile:
    if isinstance(src, Path):
        text = src.read_text()
    elif isinstance(src, str):
        text = Path(src).read_text()
    else:
        text = src.read()
        if isinstance(text, bytes):
            text = text.decode("ascii")
    return _to_file(_load_tolerant(text))


def _read_hex_text(path: Path) -> str:
    """Read Intel HEX text from a .hex file or a gzip'd .hgz file."""
    raw = path.read_bytes()
    if path.suffix.lower() == ".hgz" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("ascii")


def load_intelhex(path: Path | str) -> IntelHex:
    """Load a .hex/.hgz file into an IntelHex, tolerating dual start records."""
    return _load_tolerant(_read_hex_text(Path(path)))


def to_hex_text(ih: IntelHex) -> str:
    """Serialise an IntelHex object back to canonical Intel HEX text.

    The result has a single, valid end/start-record structure — the duplicate
    Start Linear Address records that make Tesla's dual-bank gwapp.img
    unparseable by stock tools are dropped on the round trip.
    """
    buf = io.StringIO()
    ih.write_hex_file(buf)
    return buf.getvalue()


def decode_to_hex(src: Path | str, dest: Path | str | None = None) -> Path:
    """Decode a .hgz (or .hex) into canonical Intel HEX text on disk.

    Returns the output path. Defaults dest to the source with a .hex suffix.
    """
    src = Path(src)
    if dest is None:
        dest = src.with_suffix(".hex")
    dest = Path(dest)
    dest.write_text(to_hex_text(load_intelhex(src)))
    return dest


def _cmd_info(path: Path) -> None:
    f = parse_file(path) if path.suffix.lower() not in (".hgz",) else _to_file(
        load_intelhex(path)
    )
    print(f"File  : {path}")
    print(f"Segments: {len(f.segments)}  "
          f"total={sum(s.length for s in f.segments)} bytes")
    for i, seg in enumerate(f.segments, 1):
        end = seg.start_address + seg.length - 1
        print(
            f"  #{i}: {seg.start_address:#010x}-{end:#010x}  "
            f"length={seg.length:#x} ({seg.length})  "
            f"crc32={seg.compute_crc32():08x}"
        )


def _cmd_extract(path: Path, out_dir: Path) -> None:
    f = _to_file(load_intelhex(path))
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, seg in enumerate(f.segments, 1):
        out = out_dir / f"segment_{i:02d}_{seg.start_address:#010x}.bin"
        out.write_bytes(seg.data)
        print(f"Wrote {out}  ({seg.length} bytes)")


if __name__ == "__main__":
    import sys

    usage = """\
Usage:
  ihex.py info    <file.hex|file.hgz>
  ihex.py decode  <file.hgz> [out.hex]
  ihex.py extract <file.hex|file.hgz> [out_dir]
"""

    args = sys.argv[1:]
    if not args:
        print(usage)
        sys.exit(0)

    cmd = args[0]
    if cmd == "info" and len(args) >= 2:
        _cmd_info(Path(args[1]))
    elif cmd == "decode" and len(args) >= 2:
        out = Path(args[2]) if len(args) >= 3 else None
        dest = decode_to_hex(Path(args[1]), out)
        print(f"Wrote {dest}  ({dest.stat().st_size} bytes)")
    elif cmd == "extract" and len(args) >= 2:
        out_dir = Path(args[2]) if len(args) >= 3 else Path(args[1]).with_suffix("")
        _cmd_extract(Path(args[1]), out_dir)
    else:
        print(usage)
        sys.exit(1)
