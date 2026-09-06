#!/usr/bin/env python3
"""VCFRONT node — front vehicle controller: vehicle status + LV power state.

vcfrontMIA (DIR a155) is an AGGREGATE over SEVEN frames: {0x221, 0x241, 0x321, 0x3A1,
0x102, 0x3C2} here + 0x103 (VCRIGHT_doorStatus, sourced by the VCRIGHT node). The
aggregate clears only when EVERY member arrives with a valid checksum + counter (0x3A1 /
0x221) or DLC8 (the plain ones). 0x2E1 is NOT a member — but it AND 0x102 carry status codes the
DIR chassis hold/roll FSM gates on (must be 2, not 0); see _vcfront_0x102 / _vcfront_status_0x2e1.

The node OWNS its LV/power state (``LvPowerState``, 0x221) and its 0x3A1 vehicle status.
The driver sets ``power`` (off|accessory|conditioning|drive) via ``set_lv``; the charge
scenario (12V-for-charge / bmsHvChargeEnable) will drive the 0x3A1 signal set here too —
VCFRONT is expected to carry the most state (drive vs charge).
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros
from tesla_frames import VEHICLE_POWER_STATE, LvPowerState, pack_le


def _vcfront_sensors() -> bytearray:  # 0x321, 1000ms  (temp @10 w11, SNA 0x7FF)
    return pack_le([(10, 11, round((25.0 - (-40.0)) / 0.125))], 8)  # 25 C, non-SNA


def _vcfront_0x102() -> bytearray:  # 0x102 status nibbles -> DI chassis hold/roll FSM gate
    # The DIR stores bits0-3 and bits4-7 as two status codes and flags a value of 0 as
    # INVALID. The DIR hold/roll input gate requires each of
    # the six VCFRONT status codes (incl these two) == 2, else it forces the hold/roll FSM
    # not-ready -> DI_locStatus rollPreventionState + vehicleHoldState stay FAULT. zeros(8) failed
    # this. (DBC labels 0x102 VCLEFT_doorStatus, but the DIR reads it as two status nibbles.)
    return pack_le([(0, 4, 2), (4, 4, 2)], 8)  # byte0 = 0x22


def _vcfront_status_0x2e1() -> bytearray:  # 0x2E1 mux0 status -> hold/roll FSM gate code
    # The DIR reads bits3-6 as a status code (gated on bits0-2==0 = mux 0); needs == 2.
    # Same hold/roll FSM gate as 0x102. NOTE: three of the six gate codes have a value-source
    # in orphaned RX code we couldn't statically pin -- if hold/roll is still FAULT after this
    # + drive-operational, bisect them with --set.
    return pack_le([(3, 4, 2)], 8)  # byte0 = 0x10 (bits0-2=0 mux, bits3-6=2)


class Vcfront(Node):
    name = "VCFRONT"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.lv = LvPowerState(VEHICLE_POWER_STATE["drive"])
        # Charge-enable state (0x3A1). Idle default OFF -> drive build unchanged.
        self.hv_charge_enable = False
        # Reactive inputs observed on the bus (see on_rx): user charge request + EVSE present.
        self._ui_charge_req = False
        self._cp_evse = False

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("VCFRONT_vehicleStatus", 0x3A1, 0.050, self._vehicle_status, 52, 56),  # 2022 cycle=50ms/20Hz
            SimFrame("VCFRONT_status", 0x2E1, 0.017, _vcfront_status_0x2e1),
            SimFrame("VCFRONT_coolant", 0x241, 0.100, zeros(7)),
            SimFrame("VCFRONT_sensors", 0x321, 0.100, _vcfront_sensors, 52, 56),  # 2022 DIR gates 0x321: cksum@byte7 + ctr@byte6[4:7] (magic 0x24)
            SimFrame("VCFRONT_0x102", 0x102, 0.100, _vcfront_0x102),
            SimFrame("VCFRONT_0x3C2", 0x3C2, 0.050, zeros(8)),  # 2022 cycle=50ms/20Hz (a155 member)
            SimFrame("VCFRONT_LVPowerState", 0x221, 0.050, self.lv.frame),
        ]

    def _vehicle_status(self) -> bytearray:  # 0x3A1, 100ms, counter@52 checksum@56 magic 0xA4
        sigs = [
            (10, 3, 3),  # VCFRONT_diPowerOnState = DI_POWERED_ON_FOR_DRIVE
            (31, 1, 1),  # VCFRONT_driverDoorStatus = DOOR_CLOSED
            (16, 11, 14.0 / 0.0125),  # VCFRONT_pcs12vVoltageTarget ~14 V
        ]
        if self.hv_charge_enable:
            # Tell the PCS the pack side authorizes HV charging + 12V rail ready.
            sigs += [
                (0, 1, 1),   # VCFRONT_bmsHvChargeEnable = 1
                (14, 2, 1),  # VCFRONT_12vStatusForDrive = READY_FOR_DRIVE_12V(1)
            ]
        return pack_le(sigs)

    def set_lv(self, state: str) -> int:
        """Driver externality: VCFRONT_vehiclePowerState (off|accessory|conditioning|drive)."""
        key = str(state).strip().lower()
        if key not in VEHICLE_POWER_STATE:
            raise ValueError(f"lv state must be one of {list(VEHICLE_POWER_STATE)}")
        self.lv.vps = VEHICLE_POWER_STATE[key]
        return self.lv.vps

    def set_charge_enable(self, on: bool) -> bool:
        """Driver externality: VCFRONT authorizes HV charging (0x3A1 bmsHvChargeEnable +
        12vStatusForDrive). The orchestrator sets this when starting a charge session."""
        self.hv_charge_enable = bool(on)
        return self.hv_charge_enable

    def configure(self, **s) -> None:  # scenario keys: lv_power_state, hv_charge_enable
        lv = s.pop("lv_power_state", None)
        if lv is not None:
            self.set_lv(lv)
        ce = s.pop("hv_charge_enable", None)
        if ce is not None:
            self.set_charge_enable(ce)
        super().configure(**s)

    def rx_handlers(self):
        # Start a charge session reactively: authorize HV charging once the user has
        # requested charge (UI_chargeRequest 0x333) AND an EVSE is present (CP_evseStatus
        # 0x21D). BMS + HVP react to the resulting bmsHvChargeEnable in turn.
        return {0x333: self._on_ui_charge, 0x21D: self._on_cp_evse}

    def _on_ui_charge(self, data, send) -> None:  # UI_chargeEnableRequest @2 w1
        self._ui_charge_req = bool((int.from_bytes(bytes(data), "little") >> 2) & 1)
        self.hv_charge_enable = self._ui_charge_req and self._cp_evse

    def _on_cp_evse(self, data, send) -> None:  # CP_evseAccept @0 w1
        self._cp_evse = bool(int.from_bytes(bytes(data), "little") & 1)
        self.hv_charge_enable = self._ui_charge_req and self._cp_evse


NODE = Vcfront
