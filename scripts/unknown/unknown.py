#!/usr/bin/env python3
"""UNKNOWN node — holding pen for frames not yet attributed to a source ECU.

  0x13D (vehicle bus): undocumented PCS-context signal inherited from the old PCS bench
    (pcs_send.py). Not in any Model 3 DBC/compact.json message set; content here is the
    canned default the PCS bench used.

  0x2B2 moved to the BMS node (BMS_energyStatus in the 2022+ DBCs).
"""
from __future__ import annotations

from sim_core import Node, SimFrame


def _unk_0x13D() -> bytearray:  # undocumented PCS-context; canned default (byte1 ~ AC limit)
    return bytearray([0x05, 0x1E, 0xAA, 0x1A, 0xFF, 0x02])


class Unknown(Node):
    name = "UNKNOWN"

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("UNK_0x13D", 0x13D, 0.010, _unk_0x13D),
        ]


NODE = Unknown
