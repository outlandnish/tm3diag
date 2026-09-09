#!/usr/bin/env python3
"""DI node — the vehicle-level drive-inverter aggregate (originNode=di).

DI is the *logical* drive unit the rest of the car sees: DI_systemStatus (0x118), DI_speed,
DI_alertMatrix1-4, etc. -- distinct in the DBC from the per-axle physical inverters DIR/DIF
(scripts/dir/dir.py). On a RWD car the DI role is fulfilled by the rear inverter hardware
(the DIR/PMR), so on a bench with a real rear inverter these frames come from it -> mark DI
(with DIR/PMR) ``real`` in the bench config so the sim doesn't transmit them. With no inverter
connected (a fully virtual car) the sim broadcasts them.

Frames are the originNode=di cyclic set from Model3_ETH.compact.json (2020.8.1): id / cycle /
dlc verbatim. Payloads are skeleton (all-zero) for now -- enough to model the layout and to
give a virtual car its DI liveness; fill in real signal content when a scenario needs a
virtual inverter to report meaningful status. Bus defaults to vehicle (provisional).
"""

from __future__ import annotations

from sim_core import Node, SimFrame, zeros

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

    def frames(self) -> list[SimFrame]:
        return [SimFrame(n, i, p, zeros(d)) for n, i, p, d in _FRAMES]


# ---------------------------------------------------------------------------
# DI_systemStatus (0x118) decode -- the DI's OWN status frame. These maps are the DI's
# domain knowledge, shared by CONSUMERS that watch what the inverter reports (tm3web's
# driver HUD, an orchestrator 0x118 watch). Enum labels + the bit overlay for signals Tesla
# STRIPPED from the 2022+ compact.json but the firmware still transmits at their 2020
# positions (recovered by overlaying the 2020 layout).
# ---------------------------------------------------------------------------
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

# name -> (start_bit, width). LITTLE-endian, start=LSB. DI_accelPedalPos scale 0.4 %, 255=SNA.
_DI_0X118_RECOVERED = {
    "DI_systemState": (16, 3),
    "DI_immobilizerState": (27, 3),
    "DI_accelPedalPos": (32, 8),
}


NODE = Di
