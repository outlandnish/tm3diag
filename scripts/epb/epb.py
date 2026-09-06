#!/usr/bin/env python3
"""EPB node — electronic parking brake (0x2A8/0x2E8, feeds brakeMIA a110). Bus A / CANA.

CLOSED-LOOP: the node OWNS an ``EpbResponder`` and transitions on ``DI_epbRequest`` (0x118
@44 w2): PARK -> PARKED, UNPARK -> RELEASED. It broadcasts EPBL/EPBR_systemStatus reflecting
that state; the rolling counter + Tesla checksum are placed by the SimFrame wrapper
(ctr@52, cksum@56). This is the canonical example of a node that changes state on what the
DI sends.
"""
from __future__ import annotations

from sim_core import Node, SimFrame
from tesla_frames import EpbResponder


class Epb(Node):
    name = "EPB"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.epb = EpbResponder()

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("EPBL_status", 0x2A8, 0.100, self.epb.payload, 52, 56),
            SimFrame("EPBR_status", 0x2E8, 0.100, self.epb.payload, 52, 56),
        ]

    def rx_handlers(self):
        return {0x118: self._on_di_status}  # DI_systemStatus

    def _on_di_status(self, data, send) -> None:
        # DI_epbRequest on 0x118 (@44 w2): 1=PARK -> PARKED, 2=UNPARK -> RELEASED.
        if len(data) >= 8:
            self.epb.on_epb_request((int.from_bytes(bytes(data[:8]), "little") >> 44) & 3)


NODE = Epb
