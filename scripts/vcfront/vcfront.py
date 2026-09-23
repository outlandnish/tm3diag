#!/usr/bin/env python3
"""VCFRONT node — front vehicle controller: vehicle status + LV power state.

vcfrontMIA (DIR a155) is an aggregate over SEVEN frames: {0x221, 0x241, 0x321, 0x3A1,
0x102} here + 0x103 (VCRIGHT_doorStatus, VCRIGHT node) + 0x3C2 (VCLEFT_switchStatus, VCLEFT
node -- the shared brake-switch line lives there now). The aggregate
clears only when every member arrives with a valid checksum + counter (0x3A1 / 0x221) or DLC8
(the plain ones). 0x2E1 is NOT a member, but it and 0x102 carry status codes the DIR chassis
hold/roll FSM gates on (must be 2, not 0); see _vcfront_0x102 / _vcfront_status_0x2e1.

The node owns its LV/power state (``LvPowerState``, 0x221) and its 0x3A1 vehicle status. The
driver sets ``power`` (off|accessory|conditioning|drive) via ``set_lv``.
"""
from __future__ import annotations

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import VEHICLE_POWER_STATE, LvPowerState, pack_le


def _vcfront_sensors() -> bytearray:  # 0x321, 1000ms  (temp @10 w11, SNA 0x7FF)
    return pack_le([(10, 11, round((25.0 - (-40.0)) / 0.125))], 8)  # 25 C, non-SNA


def _vcfront_0x102() -> bytearray:  # 0x102 status nibbles -> DI chassis hold/roll FSM gate
    # The DIR reads bits0-3 and bits4-7 as two status codes (0 = INVALID). The hold/roll gate
    # requires all six VCFRONT status codes == 2, else DI_locStatus rollPreventionState +
    # vehicleHoldState stay FAULT. (DBC labels 0x102 VCLEFT_doorStatus.)
    return pack_le([(0, 4, 2), (4, 4, 2)], 8)  # byte0 = 0x22


def _vcfront_status_0x2e1() -> bytearray:  # 0x2E1 mux0 status -> hold/roll FSM gate code
    # The DIR reads bits3-6 as a status code (gated on bits0-2==0 = mux 0); needs == 2.
    # Same hold/roll FSM gate as 0x102.
    return pack_le([(3, 4, 2)], 8)  # byte0 = 0x10 (bits0-2=0 mux, bits3-6=2)


class Vcfront(Node):
    name = "VCFRONT"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.lv = LvPowerState(VEHICLE_POWER_STATE["drive"])
        # Charge-enable state (0x3A1). Idle default OFF -> drive build unchanged.
        self.hv_charge_enable = False
        # 12vStatusForDrive (0x3A1) — the DI/DIR drive-start LV gate. Independent of charging:
        # the bench LV supply is healthy, so default READY. Set False to exercise the deny path.
        self.lv_ready_for_drive = True
        # Reactive inputs observed on the bus (see on_rx): user charge request + EVSE present.
        self._ui_charge_req = False
        self._cp_evse = False
        self._status_page = 1  # 0x3A1 mux page last sent (2022+); first frame is page 0

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("VCFRONT_vehicleStatus", 0x3A1, 0.050, self._vehicle_status, 52, 56),  # 2022 cycle=50ms/20Hz
            SimFrame("VCFRONT_status", 0x2E1, 0.017, _vcfront_status_0x2e1),
            SimFrame("VCFRONT_coolant", 0x241, 0.100, zeros(7)),
            SimFrame("VCFRONT_sensors", 0x321, 0.100, _vcfront_sensors, 52, 56),  # 2022 DIR gates 0x321: cksum@byte7 + ctr@byte6[4:7] (magic 0x24)
            SimFrame("VCFRONT_0x102", 0x102, 0.100, _vcfront_0x102),
            SimFrame("VCFRONT_LVPowerState", 0x221, 0.050, self.lv.frame),
        ]

    def _vcleft_switch_status(self) -> bytearray:  # 0x3C2 = VCLEFT_switchStatus, mux0
        # 0x3C2 is VCLEFT's, not VCFRONT's -- this node just sources it (a155 MIA member).
        #
        # The brake pedal switch is a PHYSICAL line the DI reads on its own GPIO. When
        # GTW_brakeLineSwitchType == DI_VC_SHARED(0) that same line is shared with the VC, and
        # the DI cross-checks its GPIO against the VC's report here. Disagreement makes the DI
        # publish DI_brakePedalState = INVALID -- a live plausibility state, so NO DTC is set.
        # (The signal doesn't exist pre-2022, which is why old firmware never did this.)
        #
        # So this must track the PHYSICAL switch at the DI, not the ESP/IBST CAN brake posture:
        # if the pedal (or a bench jumper) is grounded, set brake_switch_pressed.
        pressed = int(self.brake_switch_pressed)
        return pack_le([(4, 1, pressed), (60, 1, pressed)], 8)  # index@0=0 -> mux0

    def _vehicle_status(self) -> bytearray:  # 0x3A1, 100ms, counter@52 checksum@56 magic 0xA4
        sigs = [
            (10, 3, 3),  # VCFRONT_diPowerOnState = DI_POWERED_ON_FOR_DRIVE
            (31, 1, 1),  # VCFRONT_driverDoorStatus = DOOR_CLOSED
            (16, 11, 14.0 / 0.0125),  # VCFRONT_pcs12vVoltageTarget ~14 V
        ]
        if self.lv_ready_for_drive:
            sigs.append((14, 2, 1))  # VCFRONT_12vStatusForDrive = READY_FOR_DRIVE_12V(1)
        if self.hv_charge_enable:
            sigs.append((0, 1, 1))  # VCFRONT_bmsHvChargeEnable = 1
        return pack_le(sigs)

    def _vehicle_status_muxed(self) -> bytearray:
        """0x3A1 from 2022: VCFRONT_vehicleStatusMuxIndex @0. Page 0 carries the drive status,
        page 1 VCFRONT_bmsHvChargeEnable @1; alternate like the real VCFRONT."""
        self._status_page ^= 1
        if self._status_page:
            return pack_le([(0, 1, 1), (1, 1, int(self.hv_charge_enable))])
        sigs = [
            (10, 3, 3),  # VCFRONT_diPowerOnState = DI_POWERED_ON_FOR_DRIVE
            (31, 1, 1),  # VCFRONT_driverDoorStatus = DOOR_CLOSED
            # 2020's pcs12vVoltageTarget bits, kept so the drive frame is unchanged; on 2022 they
            # read as VCFRONT_dcr12VMilliOhms @16|8 = 96 (bits 24-26 unused).
            (16, 11, 14.0 / 0.0125),
        ]
        if self.lv_ready_for_drive:
            sigs.append((14, 2, 1))  # VCFRONT_12vStatusForDrive = READY_FOR_DRIVE_12V(1)
        return pack_le(sigs)

    def _frames_2022(self) -> list[SimFrame]:
        return [
            SimFrame("VCFRONT_vehicleStatus", 0x3A1, 0.050, self._vehicle_status_muxed, 52, 56)
            if f.can_id == 0x3A1 else f
            for f in self.frames()
        ]

    def _frames_2026(self) -> list[SimFrame]:
        # 2026.8.3 RESEEDS the 0x3A1 checksum magic: 0xA4 (2020) -> 0x2A (2022+) -> 0xC0 (2026).
        # Read out of the DIR's 0x3A1 check in each revision (2022 gives 0x2A, matching
        # tesla_frames.magic()'s existing special case; 2026 gives 0xC0). Every other gated
        # frame's seed is still id_lo+id_hi in 2026 --
        # checked all 17, only 0x3A1 and 0x25B deviate.
        #
        # This one is easy to miss and expensive: a wrong seed fails the validator, the handler
        # returns the same 0 as a DLC mismatch, and 0x3A1 is a vcfrontMIA (a155) member -- so the
        # symptom is an MIA on a frame that looks perfectly healthy on the wire.
        #
        # 0x221 and 0x321 also became gated / stayed gated in 2026, but both keep magic
        # id_lo+id_hi (0x23 / 0x24) and already carry counter+checksum, so they are unchanged.
        return [
            SimFrame(
                "VCFRONT_vehicleStatus", 0x3A1, 0.050, self._vehicle_status_muxed, 52, 56,
                cksum_magic=0xC0,
            )
            if f.can_id == 0x3A1 else f
            for f in self.frames()
        ]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }

    def set_lv(self, state: str) -> int:
        """Driver externality: VCFRONT_vehiclePowerState (off|accessory|conditioning|drive)."""
        key = str(state).strip().lower()
        if key not in VEHICLE_POWER_STATE:
            raise ValueError(f"lv state must be one of {list(VEHICLE_POWER_STATE)}")
        self.lv.vps = VEHICLE_POWER_STATE[key]
        return self.lv.vps

    def set_charge_enable(self, on: bool) -> bool:
        """Driver externality: VCFRONT authorizes HV charging (0x3A1 bmsHvChargeEnable)."""
        self.hv_charge_enable = bool(on)
        return self.hv_charge_enable

    def set_lv_ready_for_drive(self, on: bool) -> bool:
        """Driver externality: VCFRONT_12vStatusForDrive (0x3A1) — the DI/DIR LV drive gate."""
        self.lv_ready_for_drive = bool(on)
        return self.lv_ready_for_drive

    def configure(self, **s) -> None:  # keys: lv_power_state, hv_charge_enable, lv_ready_for_drive
        lv = s.pop("lv_power_state", None)
        if lv is not None:
            self.set_lv(lv)
        ce = s.pop("hv_charge_enable", None)
        if ce is not None:
            self.set_charge_enable(ce)
        rd = s.pop("lv_ready_for_drive", None)
        if rd is not None:
            self.set_lv_ready_for_drive(rd)
        super().configure(**s)

    def rx_handlers(self):
        # Authorize HV charging once the user requests charge (UI_chargeRequest 0x333) AND an
        # EVSE is present (CP_evseStatus 0x21D).
        return {0x333: self._on_ui_charge, 0x21D: self._on_cp_evse}

    def _on_ui_charge(self, data, send) -> None:  # UI_chargeEnableRequest @2 w1
        self._ui_charge_req = bool((int.from_bytes(bytes(data), "little") >> 2) & 1)
        self.hv_charge_enable = self._ui_charge_req and self._cp_evse

    def _on_cp_evse(self, data, send) -> None:  # CP_evseAccept @0 w1
        self._cp_evse = bool(int.from_bytes(bytes(data), "little") & 1)
        self.hv_charge_enable = self._ui_charge_req and self._cp_evse


NODE = Vcfront
