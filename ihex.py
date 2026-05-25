"""Intel HEX parser — presents the same interface as bhx.BhxFile."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from intelhex import IntelHex

from bhx import Segment


@dataclass
class IHexFile:
    segments: list = field(default_factory=list)


def parse_file(src: Path | IO) -> IHexFile:
    ih = IntelHex()
    ih.loadhex(src if not isinstance(src, Path) else str(src))
    f = IHexFile()
    for start, end in ih.segments():
        data = bytes(ih.tobinarray(start=start, end=end - 1))
        f.segments.append(Segment(start_address=start, data=data))
    return f
