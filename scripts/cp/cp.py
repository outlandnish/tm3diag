#!/usr/bin/env python3
"""CP node — charge port: liveness + EVSE-connection state.

Sources three frames (0x25D only on the 2022.45.15 target):
  0x210 CP_status     100ms dlc8 -- vehicle-bus charge-port status (CANData catalog /
     Model3_ETH.compact.json 2020.8.1): CP_chargeCableState @16 w2, CP_doorControlState
     @13 w3 = doorSenseClosed(6). Nothing on the inverter bench consumes it.
  0x25D CP_status (DIR-facing copy) 100ms dlc8 -- what the 2022 DIR subscribes to for
     charge-port state.
  0x21D CP_evseStatus 100ms dlc8 -- EVSE-connection report (origin=CP,
     Model3_ETH.compact.json); signal start/width verbatim from compact.json.

0x25D field decode (2022.45.15 gen-26 DIR; 0x210 is not in the DIR's 42-id RX table):

    state = word0 >> 14                       # frame bits 14-15, little-endian
    if (word0 & 0xC000) == 0: state = 0       # 0 = SNA -> INVALID

cable-state map: 1 -> 0 (cable NOT connected), 2 -> 1, anything else (incl 0/SNA) -> 2 =
connected. A missing 0x25D therefore latches "cable connected", and the a168
proximityDriveDenial path sets DI_a162_chargeCableConnected. 0x25D is also the
DI_a105_cpMIA liveness member, so it must ARRIVE at 100 ms regardless of content.

Bit 14, not 16: 2019/2020 compact.json puts CP_chargeCableState at bit 16, the 2026 DBC
at 14|2@1+, and the 2022 firmware reads bit 14. Live capture: 0x25D [8] 24 48 44 C9 40 A1
02 90 -> byte1 0x48 & 0xC0 = 0x40 -> field = 1 = NOT_CONNECTED. Only bits 14-15 are read,
so the rest of the 0x25D payload is left zero; 0x210 keeps the 2020 layout.

The node owns whether an EVSE is plugged in and at what current limit (evse_connected /
evse_limit_a); default is UNPLUGGED. The orchestrator simulates plugging in via
set_evse(connected, limit_a), which reports the cable CONNECTED to the DIR.
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame
from tesla_frames import pack_le

# CP_evseStatus enums (PCS operating-mode 2 + compact.json layout)
_PROX_DISCONNECTED = 0
_PROX_LATCHED = 3           # CP_proximity: cable latched
_PILOT_LINE_CHARGE = 2      # CP_pilot: AC line present
_AC_CHARGE_ENABLED = 3      # CP_acChargeState

# CP_chargeCableState (CANData value table): 0 = UNKNOWN_SNA reads as "connected" to the DIR.
_CABLE_NOT_CONNECTED = 1
_CABLE_CONNECTED = 2

_CABLE_BIT_0X210 = 16  # 2020.8.1 compact.json layout
_CABLE_BIT_0X25D = 14  # 2022 DIR parse + 2026 DBC + live capture (see module docstring)


class Cp(Node):
    name = "CP"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.evse_connected = False
        self.evse_limit_a = 15.0  # A — advertised EVSE current when connected

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("CP_status", 0x210, 0.100, self._cp_status),
            SimFrame("CP_evseStatus", 0x21D, 0.100, self._cp_evse_status),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        # 2022 DIR reads charge-port state on 0x25D; both copies ship (see module docstring).
        return [
            *self.frames(),
            SimFrame("CP_status_0x25D", 0x25D, 0.100, self._cp_status_25d),
        ]

    def fw_variants(self):
        return {BASELINE_FW: self.frames, "2022.45.15": self._frames_2022}

    def set_evse(self, connected: bool, limit_a: float | None = None) -> bool:
        """Simulate an EVSE plugged in (or unplugged) at an optional current limit (A).
        Reported on 0x21D CP_evseStatus and as CP_chargeCableState on both CP_status copies."""
        self.evse_connected = bool(connected)
        if limit_a is not None:
            self.evse_limit_a = float(limit_a)
        return self.evse_connected

    def configure(self, **s) -> None:  # scenario keys: evse_connected, evse_limit_a
        connected = s.pop("evse_connected", None)
        limit = s.pop("evse_limit_a", None)
        if connected is not None or limit is not None:
            self.set_evse(self.evse_connected if connected is None else connected, limit)
        super().configure(**s)

    def _cable_state(self) -> int:
        return _CABLE_CONNECTED if self.evse_connected else _CABLE_NOT_CONNECTED

    def _cp_status(self) -> bytearray:  # 0x210, 100ms
        return pack_le(
            [
                (0, 2, 0),   # CP_type = CP_TYPE_US_TESLA
                (13, 3, 6),  # CP_doorControlState = CP_doorSenseClosed
                (_CABLE_BIT_0X210, 2, self._cable_state()),  # CP_chargeCableState
            ]
        )

    def _cp_status_25d(self) -> bytearray:  # 0x25D, 100ms
        # Bits 14-15 are the only field the DIR extracts; rest stays zero.
        return pack_le([(_CABLE_BIT_0X25D, 2, self._cable_state())])

    def _cp_evse_status(self) -> bytearray:  # 0x21D, 100ms
        if not self.evse_connected:
            return pack_le([], 8)
        a = self.evse_limit_a
        return pack_le(
            [
                (0, 1, 1),                    # CP_evseAccept
                (2, 2, _PROX_LATCHED),        # CP_proximity = LATCHED
                (4, 3, _PILOT_LINE_CHARGE),   # CP_pilot = LINE_CHARGE
                (8, 8, a / 0.5),              # CP_pilotCurrent   (scale 0.5 A)
                (24, 7, a),                   # CP_cableCurrentLimit (scale 1 A)
                (53, 3, _AC_CHARGE_ENABLED),  # CP_acChargeState = ENABLED
            ],
            8,
        )


NODE = Cp
