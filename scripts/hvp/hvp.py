#!/usr/bin/env python3
"""HVP node — High Voltage Processor: commands the PCS + owns the HV contactors.

HVP is the ECU the PCS obeys. It sources two frames (both on the eth/vehicle bus,
origin=hvp in Model3_ETH.compact.json):

  0x22A HVP_pcsControl     10ms dlc4 -- pcsControlRequest + charge/dcdc HW enables +
                                        dcLinkVoltageRequest (what the PCS should do)
  0x20A HVP_contactorState 10ms dlc6 -- pack contactor negative/positive/set states +
                                        closingAllowed / dcLinkAllowedToEnergize / hvil

Signal start/width are taken verbatim from compact.json (2020.8.1 / Model3_ETH). Neither
frame carries a rolling counter or checksum in that firmware, so these are plain frames.

The node OWNS its control intent as internal state (``control`` / ``charge_hw`` /
``dcdc_hw`` / ``contactor_stage`` / ``hv_voltage``) and broadcasts it every cycle. The
DEFAULT is idle/safe (SHUTDOWN, contactors OPEN) so a drive bench that doesn't touch HVP
asserts nothing. The driver/orchestrator drives a charge session by moving the node
through modes via ``set_mode`` (off/dcdc/charge/both/precharge) -- eventually this will be
reactive (HVP responding to BMS/VCFRONT/CP broadcasts) rather than directly commanded.
"""
from __future__ import annotations

from sim_core import Node, SimFrame
from tesla_frames import pack_le

# HVP_pcsControlRequest enum
_PCS_CONTROL = {"SHUTDOWN": 0, "SUPPORT": 1, "PRECHARGE": 2, "DISCHARGE": 3}

# contactor stage -> (neg, pos, setState, closingAllowed, energize)
# neg: OPEN=1 PULLED_IN=4 ECONOMIZED=6 | pos: OPEN=1 PRECHARGE=2 ECONOMIZED=6
# setState: OPEN=1 CLOSING=2 CLOSED=5
_CONTACTOR = {
    "open":      (1, 1, 1, 0, 0),
    "precharge": (4, 2, 2, 1, 1),
    "closed":    (6, 6, 5, 1, 1),
}
_HVIL_STATUS_OK = 1

# operating mode -> (control, charge_hw, dcdc_hw, contactor_stage)
_MODE = {
    "off":       ("SHUTDOWN",  False, False, "open"),
    "dcdc":      ("SUPPORT",   False, True,  "closed"),
    "charge":    ("SUPPORT",   True,  False, "closed"),
    "both":      ("SUPPORT",   True,  True,  "closed"),
    "precharge": ("PRECHARGE", True,  True,  "precharge"),
}


class Hvp(Node):
    name = "HVP"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        # Idle/safe default: PCS shut down, contactors open (drive bench unperturbed).
        self.control = "SHUTDOWN"
        self.charge_hw = False
        self.dcdc_hw = False
        self.contactor_stage = "open"
        self.hv_voltage = 67.2  # V — DC-link target (bench 18S pack); orchestrator overrides

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("HVP_pcsControl", 0x22A, 0.010, self._pcs_control),
            SimFrame("HVP_contactorState", 0x20A, 0.010, self._contactor_state),
        ]

    def set_mode(self, mode: str) -> str:
        """Driver externality: move HVP through an operating mode
        (off|dcdc|charge|both|precharge). Sets control + HW enables + contactor stage."""
        key = str(mode).strip().lower()
        if key not in _MODE:
            raise ValueError(f"HVP mode must be one of {list(_MODE)}")
        self.control, self.charge_hw, self.dcdc_hw, self.contactor_stage = _MODE[key]
        return key

    def set_hv_voltage(self, volts: float) -> float:
        """Driver externality: DC-link voltage request (HVP_dcLinkVoltageRequest)."""
        self.hv_voltage = float(volts)
        return self.hv_voltage

    def configure(self, **s) -> None:  # scenario keys: mode, hv_voltage
        mode = s.pop("mode", None)
        if mode is not None:
            self.set_mode(mode)
        volts = s.pop("hv_voltage", None)
        if volts is not None:
            self.set_hv_voltage(volts)
        super().configure(**s)

    def rx_handlers(self):
        return {0x3A1: self._on_vcfront_status}  # VCFRONT_vehicleStatus

    def _on_vcfront_status(self, data, send) -> None:
        # Command the PCS to charge once VCFRONT authorizes HV charging (bmsHvChargeEnable
        # @0): SUPPORT + charge HW + contactors closed. Otherwise idle (SHUTDOWN, open).
        # This is VCFRONT "starting a charge session with the PCS".
        charge = bool(int.from_bytes(bytes(data), "little") & 1)
        self.set_mode("charge" if charge else "off")

    def _pcs_control(self) -> bytearray:  # 0x22A, dlc4
        return pack_le(
            [
                (0, 16, self.hv_voltage / 0.1),          # HVP_dcLinkVoltageRequest
                (16, 2, _PCS_CONTROL.get(self.control, 0)),  # HVP_pcsControlRequest
                (18, 1, int(self.charge_hw)),            # HVP_pcsChargeHwEnabled
                (19, 1, int(self.dcdc_hw)),              # HVP_pcsDcdcHwEnabled
                # HVP_dcLinkVoltageFiltered@20w11 left 0 (no live measurement on the bench)
            ],
            4,
        )

    def _contactor_state(self) -> bytearray:  # 0x20A, dlc6
        neg, pos, setst, closing, energize = _CONTACTOR[self.contactor_stage]
        return pack_le(
            [
                (0, 3, neg),               # HVP_packContNegativeState
                (3, 3, pos),               # HVP_packContPositiveState
                (8, 4, setst),             # HVP_packContactorSetState
                (35, 1, closing),          # HVP_packCtrsClosingAllowed
                (36, 1, energize),         # HVP_dcLinkAllowedToEnergize
                (40, 4, _HVIL_STATUS_OK),  # HVP_hvilStatus = STATUS_OK
            ],
            6,
        )


NODE = Hvp
