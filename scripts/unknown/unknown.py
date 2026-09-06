#!/usr/bin/env python3
"""UNKNOWN node — holding pen for frames not yet attributed to a source ECU.

As RE pins each frame's originator, move it to that ECU's node (edit sim_registry +
the node's frames()). Keeps the map honest about known vs. guessed.

  0x13D, 0x2B2 (vehicle bus): necessary-but-UNDOCUMENTED PCS-context signals inherited from
    the old PCS bench (pcs_send.py). They are in NONE of the Model 3 DBC/compact.json message
    sets, so their originator + signal layout are unknown; content here is the canned default
    the PCS bench used. Attribute to their real node once the PCS firmware is RE'd (0x13D was
    guessed "OBC_control" -- there is no OBC in a Model 3 -- and 0x2B2 "charge_power").
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
            # 0x11D re-homed to the ESP node (esp.py) — firmware-pinned ESP-sourced,
            # espMIA a091 member + VDC slip source (a195/196/197/210). 2026-08-18.
            SimFrame("UNK_0x13D", 0x13D, 0.010, _unk_0x13D),
            SimFrame("UNK_0x2B2", 0x2B2, 0.100, _unk_0x2B2),
        ]


NODE = Unknown
