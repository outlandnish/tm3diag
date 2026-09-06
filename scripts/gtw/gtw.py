#!/usr/bin/env python3
"""GTW node — gateway: car config + update status. Bus A / CANA.

gtwMIA (DIR a087) is an AGGREGATE over {0x7FF, 0x528, 0x3ED}. 0x7FF GTW_carConfig is
MULTIPLEXED, built from NAMED signals via the DB encoder (the node OWNS a ``MuxedConfigTx``)
so every field is settable by the driver via ``set_carconfig``. 0x528/0x3ED are arrival-only
but the DIR EXACT-checks their DLC (0x528=4, 0x3ED=1) — sending 8 trips DIR_a094_canDataBusA.

Needs the CAN DB: constructed with ``NodeContext(db=...)``.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros
from tesla_frames import GTW_CARCONFIG_ID, GTW_DEFAULTS, MuxedConfigTx


class Gtw(Node):
    name = "GTW"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.carcfg = MuxedConfigTx(self.ctx.db, GTW_CARCONFIG_ID, defaults=GTW_DEFAULTS)

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("GTW_carConfig", 0x7FF, 0.100, self.carcfg.next_frame),
            SimFrame("GTW_0x528", 0x528, 0.100, zeros(4)),  # DIR exact-checks DLC=4
            SimFrame("GTW_0x3ED", 0x3ED, 0.100, zeros(1)),  # DIR exact-checks DLC=1
        ]

    def set_carconfig(self, signal: str, value) -> int:
        """Driver externality: set a GTW_carConfig signal by name."""
        return self.carcfg.set(signal, value)


NODE = Gtw
