#!/usr/bin/env python3
"""BMS node — HV battery liveness (feeds DIR_a092_bmsMIA).

DLC>=8 arrival clears bmsMIA regardless of payload; content is a drive-ready pack.
"""

from __future__ import annotations

import time
from functools import partial

from sim_core import BASELINE_FW, Node, SimFrame
from tesla_frames import pack_le, vcfront_hv_charge_enable

# DBC scaling that moved 2020.8.1 -> 2022.45.15 (layouts unchanged). 2020 LSBs on a 2022 DI read
# minBusVoltage 300 V as 600 V (clamped 470 V) > the 373 V pack, so the DI's min-bus-voltage
# limiter cut discharge power to ~0 in drive -> DI_a125 noBatteryPower. The 0x2D2/0x252 LSBs are
# firmware-confirmed on the 2022 DIR; currentUnfiltered is DBC-only.
_SCALE_2020 = {"bus_v": 0.01, "dis_i": 0.128, "dis_p": 0.01, "i_unf_off": -500.0}
_SCALE_2022 = {"bus_v": 0.02, "dis_i": 0.15, "dis_p": 0.013, "i_unf_off": -822.0}


# BMS_packVoltage reports a MEASURED bus voltage, never a fixed one (a fixed value splits the
# DIR's bus-voltage cross-check, DIR_a038_busVsensor, 5%): the DIR's DIR_vBat (0x126 @0|10,
# 1 V/LSB 2022+, 0.5 V in 2020) if it is on the bus, else a real PCS's PCS_dcdcHvBusVolt
# (0x2B4 @10|12, 0.146484375 V; the sim's PCS doesn't send 0x2B4), else 0 V.
_HV_SOURCE_STALE_S = 1.0


def _bms_hvBusStatus(s=_SCALE_2020, v=0.0) -> bytearray:  # 0x132, 10ms
    return pack_le(
        [
            (0, 16, v / 0.01),  # BMS_packVoltage
            (16, 16, 0),  # BMS_packCurrent     0 A (SNA=-32768)
            (32, 16, (0 - s["i_unf_off"]) / 0.05),  # BMS_currentUnfiltered 0 A
        ]
    )


def _bms_hvBusStatus_2026(s=_SCALE_2022, v=0.0) -> bytearray:  # 0x132, DLC **6**
    # 2026.8.3 shortens 0x132 to DLC 6 and the DIR's DLC check is an EXACT match (both
    # short AND long frames are rejected), so the
    # 8-byte 2022 build is dropped outright on a 2026 DU -- taking the HV bus voltage with it.
    # Bytes 0-5 are unchanged: BMS_packVoltage @0|16 was RENAMED to BMS_dcLinkVoltage at the
    # same offset, width and 0.01 V/LSB scale. Only BMS_chgTimeToFull @48|12 (bytes 6-7) was
    # dropped -- we never populated it, so the payload is identical and only the length changes.
    return pack_le(
        [
            (0, 16, v / 0.01),  # BMS_dcLinkVoltage (was BMS_packVoltage)
            (16, 16, 0),  # BMS_packCurrent     0 A (SNA=-32768)
            (32, 16, (0 - s["i_unf_off"]) / 0.05),  # BMS_currentUnfiltered 0 A
        ],
        6,
    )


def _bms_powerAvailable_2026(s=_SCALE_2022) -> bytearray:  # 0x252, 100ms -> drive-ready
    # 2026.8.3 re-lays 0x252: BMS_powerLimitsState moved @48|1 -> @42|1 (and
    # BMS_notEnoughPowerForHeatPump @42 -> @52, BMS_totalHvPowerBudget @43|9 is new, replacing
    # BMS_hvacPowerBudget). Leaving powerLimitsState at 48 would land it inside
    # totalHvPowerBudget and read as POWER_NOT_CALCULATED -> the DIR's min() power limiter sees
    # no drive power. maxRegenPower/maxDischargePower keep their offsets AND their 0.01/0.013
    # kW/LSB scales, so the a125 noBatteryPower inputs are untouched.
    return pack_le(
        [
            (0, 16, 60.0 / 0.01),  # BMS_maxRegenPower      ~60
            (16, 16, 150.0 / s["dis_p"]),  # BMS_maxDischargePower  ~150
            (42, 1, 1),  # BMS_powerLimitsState = POWER_CALCULATED_FOR_DRIVE (was @48)
        ]
    )


def _bms_powerAvailable(s=_SCALE_2020) -> bytearray:  # 0x252, 100ms  -> drive-ready
    return pack_le(
        [
            (0, 16, 60.0 / 0.01),  # BMS_maxRegenPower      ~60
            (16, 16, 150.0 / s["dis_p"]),  # BMS_maxDischargePower  ~150
            (48, 1, 1),  # BMS_powerLimitsState = POWER_CALCULATED_FOR_DRIVE
        ]
    )


def _bms_driveLimits(s=_SCALE_2020, v_min=300.0) -> bytearray:  # 0x2D2, 100ms
    return pack_le(
        [
            (0, 16, v_min / s["bus_v"]),  # BMS_minBusVoltage (DIR clamps to >= 210 V)
            (16, 16, 400.0 / s["bus_v"]),  # BMS_maxBusVoltage      400 V
            (48, 14, 500.0 / s["dis_i"]),  # BMS_maxDischargeCurrent ~500 A
        ]
    )


def _bms_packConfig_0x392() -> bytearray:  # 0x392, DLC8 — 2022.45.15 only
    # 0x392 = BMS_packConfig in 2022+ (was EPAS3P_alertMatrix in 2020, DBC-confirmed). The DIR
    # reads the config only when byte0 (BMS_packConfigMultiplexer) == 1.
    return pack_le([(0, 8, 1)], 8)


def _bms_limits_0x452() -> bytearray:  # 0x452, DLC3, no E2E — 2022.45.15 only
    # Two packed torque limits (b0-9, b10-20) the DIR min()'s into the powertrain torque/power set.
    # Captured payload C2 40 1F = b0-9:194, b10-20:2000 (both non-SNA).
    return pack_le([(0, 10, 194), (10, 11, 2000)], 3)


def _bms_thermalStatus() -> bytearray:  # 0x312, 1000ms
    return pack_le(
        [
            (17, 9, (25 - (-25)) / 0.25),  # BMS_inletActiveCoolTargetT 25 C
            (44, 9, (25 - (-25)) / 0.25),  # BMS_minPackTemperature     25 C
            (53, 9, (25 - (-25)) / 0.25),  # BMS_maxPackTemperature     25 C
        ]
    )


# BMS_status 0x212 signal sets per operating mode (start,width,value), from the PCS
# operating-mode tables + compact.json signal layout.
_BMS_STATUS_MODES = {
    "drive": [
        (1, 1, 0),  # BMS_notEnoughPowerForDrive = 0
        (8, 3, 4),  # BMS_contactorState = BMS_CTRSET_CLOSED
        (16, 3, 3),  # BMS_hvState        = HV_UP_FOR_DRIVE
        (32, 4, 1),  # BMS_state          = BMS_DRIVE
        (56, 4, 1),  # BMS_smStateRequest = BMS_DRIVE
    ],
    "dcdc": [
        (8, 3, 4),  # contactorState = CLOSED
        (16, 3, 6),  # hvState        = HV_UP
        (32, 4, 2),  # state          = BMS_SUPPORT
        (56, 4, 2),  # smStateRequest = BMS_SUPPORT
    ],
    "charge": [
        (8, 3, 4),  # contactorState = CLOSED
        (11, 3, 3),  # uiChargeStatus = BMS_CHARGING
        (16, 3, 4),  # hvState        = HV_UP_FOR_CHARGE
        (29, 1, 1),  # chargeRequest  = 1
        (32, 4, 3),  # state          = BMS_CHARGE
        (56, 4, 3),  # smStateRequest = BMS_CHARGE
    ],
}


# BMS_status 0x212 for 2026.8.3. Same operating modes, re-laid: BMS_hvState moved @16|3 -> @60|3
# (byte7 bits 4-6, sharing byte 7 with smStateRequest @56|4) and BMS_chargeRequest @29 -> @30.
# contactorState/state/smStateRequest kept their offsets. Left at the 2022 offsets, hvState would
# land in BMS_batteryInputPower @14|16 (new) and the DIR would read HV as DOWN.
_BMS_STATUS_MODES_2026 = {
    "drive": [
        (1, 1, 0),  # BMS_notEnoughPowerForDrive = 0
        (8, 3, 4),  # BMS_contactorState = BMS_CTRSET_CLOSED
        (32, 4, 1),  # BMS_state          = BMS_DRIVE
        (56, 4, 1),  # BMS_smStateRequest = BMS_DRIVE
        (60, 3, 3),  # BMS_hvState        = HV_UP_FOR_DRIVE   (was @16)
    ],
    "dcdc": [
        (8, 3, 4),  # contactorState = CLOSED
        (32, 4, 2),  # state          = BMS_SUPPORT
        (56, 4, 2),  # smStateRequest = BMS_SUPPORT
        (60, 3, 6),  # hvState        = HV_UP                 (was @16)
    ],
    "charge": [
        (8, 3, 4),  # contactorState = CLOSED
        (11, 3, 3),  # uiChargeStatus = BMS_CHARGING
        (30, 1, 1),  # chargeRequest  = 1                     (was @29)
        (32, 4, 3),  # state          = BMS_CHARGE
        (56, 4, 3),  # smStateRequest = BMS_CHARGE
        (60, 3, 4),  # hvState        = HV_UP_FOR_CHARGE      (was @16)
    ],
}


class Bms(Node):
    name = "BMS"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.mode = "drive"  # drive | dcdc | charge — driver externality (BMS_status 0x212)
        self.min_bus_voltage = 300.0
        self._hv: dict[str, tuple[float, float]] = {}  # source -> (volts, monotonic time)

    def _pack_v(self) -> float:
        now = time.monotonic()
        for src in ("DIR", "PCS"):  # the inverter's own measurement first
            v, t = self._hv.get(src, (None, 0.0))
            if v is not None and now - t <= _HV_SOURCE_STALE_S:
                return v
        return 0.0

    def frames(self) -> list[SimFrame]:
        # Proven bench set. Only 0x212 + 0x312 exist in the 2020 DIR; 0x132/0x252/0x2D2 are
        # 2022-new but harmless on a 2020 DU (no handler -> filtered).
        return self._frames(_SCALE_2020)

    def _frames(self, s) -> list[SimFrame]:
        return [
            SimFrame("BMS_hvBusStatus", 0x132, 0.010, lambda: _bms_hvBusStatus(s, self._pack_v())),
            SimFrame("BMS_status", 0x212, 0.100, self._bms_status),
            SimFrame("BMS_powerAvailable", 0x252, 0.100, partial(_bms_powerAvailable, s)),
            SimFrame("BMS_driveLimits", 0x2D2, 0.100,
                     lambda: _bms_driveLimits(s, self.min_bus_voltage)),
            SimFrame("BMS_thermalStatus", 0x312, 1.000, _bms_thermalStatus),
        ]

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15: the baseline set at 2022 DBC scaling (``_SCALE_2022``), plus two
        bmsMIA (a092) members absent in the 2020 DIR: BMS_limits 0x452 and BMS_packConfig
        0x392 (reassigned from EPAS3P_alertMatrix; epas3p drops it in its 2022 variant)."""
        return [
            *self._frames(_SCALE_2022),
            SimFrame("BMS_limits", 0x452, 0.100, _bms_limits_0x452),
            SimFrame("BMS_packConfig", 0x392, 1.000, _bms_packConfig_0x392),
        ]

    def _frames_2026(self) -> list[SimFrame]:
        """2026.8.3: the 2022 set with the three re-laid frames swapped in. 0x2D2 BMS_driveLimits
        is identical across 2022->2026 in BOTH layout and scaling, and 0x312/0x392/0x452 are
        unchanged, so those carry over untouched. Scaling stays ``_SCALE_2022`` -- no BMS LSB
        moved between 2022.45.15 and 2026.8.3 (checked against both DBCs), so the a125
        noBatteryPower calibration that _SCALE_2022 exists for still holds."""
        s = _SCALE_2022
        repl = {
            0x132: SimFrame("BMS_hvBusStatus", 0x132, 0.010,
                            lambda: _bms_hvBusStatus_2026(s, self._pack_v())),
            0x212: SimFrame("BMS_status", 0x212, 0.100, self._bms_status_2026),
            0x252: SimFrame(
                "BMS_powerAvailable", 0x252, 0.100, partial(_bms_powerAvailable_2026, s)
            ),
        }
        return [repl.get(f.can_id, f) for f in self._frames_2022()]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }

    def set_mode(self, mode: str) -> str:
        """Pack operating mode reflected in BMS_status (drive|dcdc|charge)."""
        key = str(mode).strip().lower()
        if key not in _BMS_STATUS_MODES:
            raise ValueError(f"BMS mode must be one of {list(_BMS_STATUS_MODES)}")
        self.mode = key
        return key

    def configure(self, **s) -> None:
        """Scenario keys: mode, min_bus_voltage."""
        mode = s.pop("mode", None)
        if mode is not None:
            self.set_mode(mode)
        if (v := s.pop("min_bus_voltage", None)) is not None:
            self.min_bus_voltage = float(v)
        super().configure(**s)

    def rx_handlers(self):
        return {0x3A1: self._on_vcfront_status,  # VCFRONT_vehicleStatus
                0x126: self._on_dir_hv_status,   # DIR_hvStatus
                0x2B4: self._on_pcs_rail_status}  # PCS_dcdcRailStatus

    def _on_dir_hv_status(self, data, send) -> None:
        if len(data) >= 2:
            raw = data[0] | (data[1] & 3) << 8
            lsb = 1.0 if self.fw is None or self.fw >= "2022.45.15" else 0.5
            self._hv["DIR"] = (raw * lsb, time.monotonic())

    def _on_pcs_rail_status(self, data, send) -> None:
        if len(data) >= 3:
            raw = int.from_bytes(data[:3], "little") >> 10 & 0xFFF
            self._hv["PCS"] = (raw * 0.146484375, time.monotonic())

    def _on_vcfront_status(self, data, send) -> None:
        # VCFRONT_bmsHvChargeEnable (2020: bit 0; 2022+: 0x3A1 page 1): charge, else drive.
        charge = vcfront_hv_charge_enable(data, muxed=self.fw is None or self.fw >= "2022.45.15")
        if charge is not None:
            self.mode = "charge" if charge else "drive"

    def _bms_status(self) -> bytearray:  # 0x212, 100ms
        return pack_le(_BMS_STATUS_MODES[self.mode])

    def _bms_status_2026(self) -> bytearray:  # 0x212, 100ms (2026.8.3 layout)
        return pack_le(_BMS_STATUS_MODES_2026[self.mode])


NODE = Bms
