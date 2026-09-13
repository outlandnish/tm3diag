#!/usr/bin/env python3
"""DI node — the vehicle-level drive-inverter aggregate (originNode=di).

DI is the logical drive unit the rest of the car sees (DI_systemStatus 0x118, DI_speed,
DI_alertMatrix1-4, etc.), distinct in the DBC from the per-axle physical inverters DIR/DIF
(scripts/dir/dir.py). On a RWD car the DI role is fulfilled by the rear inverter (DIR/PMR),
so with a real rear inverter on the bench these frames come from it — mark DI (with DIR/PMR)
``real`` in the bench config; with no inverter connected the sim broadcasts them.

Frames are the originNode=di cyclic set from Model3_ETH.compact.json (2020.8.1): id / cycle /
dlc verbatim. Payloads are skeleton (all-zero). Bus defaults to vehicle.
"""

from __future__ import annotations

from sim_core import Node, SimFrame, zeros
from tesla_frames import DI_BRAKE_SWITCH_BIT, DI_BRAKE_SWITCH_ID, pack_le

# name, arbitration id, period (s), dlc  -- originNode=di, send_type=Cyclic
_FRAMES = [
    ("DI_systemStatus", 0x118, 0.010, 8),
    ("DI_speed", 0x257, 0.020, 8),
    ("DI_vehicleEstimates", 0x267, 1.000, 8),
    ("DI_systemPower", 0x268, 0.100, 5),
    ("DI_locStatus", 0x286, 0.100, 7),
    ("DI_chassisControlStatus", 0x2B6, 0.100, 2),
    ("DI_maxRatedPower", 0x336, 1.000, 3),
    ("DI_alertMatrix1", 0x367, 1.000, 8),
    ("DI_alertMatrix2", 0x368, 1.000, 8),
    ("DI_alertMatrix3", 0x36B, 1.000, 8),
    ("DI_alertMatrix4", 0x36E, 1.000, 8),
    ("DI_odometerStatus", 0x3B6, 1.000, 4),
    ("DI_estimatedBrakeTemp", 0x3FE, 1.000, 5),
    ("DI_chassisControl2", 0x745, 0.100, 5),
]


class Di(Node):
    name = "DI"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        # The DI's own hardwired brake switch. Broadcast on 0x1D6 bit33; peers
        # mirror it into the brake vote (see tesla_frames.di_brake_switch_pressed). On a bench
        # with a real DIR/PMR the real unit sources 0x1D6 -- this models the virtual-car case.
        self.brake_switch_pressed = False

    def frames(self) -> list[SimFrame]:
        fr = [SimFrame(n, i, p, zeros(d)) for n, i, p, d in _FRAMES]
        fr.append(SimFrame("DI_brakeSwitch", DI_BRAKE_SWITCH_ID, 0.100, self._brake_switch_line))
        return fr

    def _brake_switch_line(self) -> bytearray:  # 0x1D6, bit33 = wired brake switch (1=pressed)
        return pack_le([(DI_BRAKE_SWITCH_BIT, 1, int(self.brake_switch_pressed))], 8)

    def set_brake_switch(self, on: bool) -> bool:
        """Driver externality: the DI's hardwired brake switch (broadcast on 0x1D6 bit33)."""
        self.brake_switch_pressed = bool(on)
        return self.brake_switch_pressed

    def configure(self, **s) -> None:  # brake_switch_pressed
        bs = s.pop("brake_switch_pressed", None)
        if bs is not None:
            self.set_brake_switch(bs)
        super().configure(**s)


# DI_systemStatus (0x118) decode. Enum labels + bit overlay for signals Tesla stripped from
# the 2022+ compact.json but the firmware still transmits at their 2020 positions.
DI_STATUS_ID = 0x118
DI_GEAR_LABELS = {0: "INVALID", 1: "P", 2: "R", 3: "N", 4: "D", 7: "SNA"}
DI_IMMO_LABELS = {
    0: "INIT_SNA",
    1: "REQUEST",
    2: "AUTHENTICATING",
    3: "DISARMED",
    4: "IDLE",
    5: "RESET",
    6: "FAULT",
}
DI_SYS_LABELS = {
    0: "UNAVAILABLE",
    1: "IDLE",
    2: "STANDBY",
    3: "FAULT",
    4: "ABORT",
    5: "ENABLE",
}
DI_HVIL_LABELS = {0: "DISABLED", 1: "STG1", 2: "CLOSED", 3: "SNA"}

NODE = Di
