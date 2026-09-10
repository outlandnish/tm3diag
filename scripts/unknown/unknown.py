#!/usr/bin/env python3
"""UNKNOWN node — holding pen for frames not yet attributed to a source ECU.

  0x13D, 0x2B2 (vehicle bus): undocumented PCS-context signals inherited from the old PCS
    bench (pcs_send.py). Not in any Model 3 DBC/compact.json message set; content here is the
    canned default the PCS bench used.
"""
from __future__ import annotations

from sim_core import Node, SimFrame


def _unk_0x13D() -> bytearray:  # undocumented PCS-context; canned default (byte1 ~ AC limit)
    return bytearray([0x05, 0x1E, 0xAA, 0x1A, 0xFF, 0x02])


def _unk_0x2B2() -> bytearray:  # undocumented PCS-context; canned default (zeros)
    return bytearray(5)


class Unknown(Node):
    name = "UNKNOWN"

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("UNK_0x13D", 0x13D, 0.010, _unk_0x13D),
            SimFrame("UNK_0x2B2", 0x2B2, 0.100, _unk_0x2B2),
        ]


NODE = Unknown
