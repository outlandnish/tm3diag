#!/usr/bin/env python3
"""GTW node — gateway: car config + update status. Bus A / CANA.

gtwMIA (DIR a087) is an AGGREGATE over {0x7FF, 0x528, 0x3ED}. 0x528/0x3ED are arrival-only
but the DIR EXACT-checks their DLC (0x528=4, 0x3ED=1) — sending 8 trips DIR_a094_canDataBusA.

0x7FF GTW_carConfig is MULTIPLEXED and encoded from NAMED signals through the loaded CAN
database, so that database — not this file — is the authority for a revision's bit layout.
That distinction matters here more than anywhere else in the sim: of the 15 car-config
signals the DIR reads, NINE moved between 2020, 2022 and 2026. GTW_tpmsType went m2@11|1 ->
m7@49|2, GTW_cabinPTCHeaterType m2@31|1 -> m1@10|1, GTW_brakeHWType m2@59|2 -> m1@43|4 ->
m1@43|5. Encoding by name picks a rev's layout up for free; it also means the loaded DB must
MATCH the bench's firmware, or every one of those nine lands in the wrong bits.

Set a parameter by its ENUM LABEL wherever you can, because the same number means different
things across revisions: GTW_performancePackage 4 is BASE_PLUS_AWD in 2020 but BASE_2022 from
2022 on, and GTW_chassisType 3 is undefined in 2020 but Y_CHASSIS after. Some labels were
themselves renamed with the value kept (2020 GTW_packEnergy 50_KWH/74_KWH/62_KWH vs SR/LR/MR
later; 2026 prefixes every GTW_brakeHWType with its caliper vendor), so a label valid on one
rev can be rejected on another — the error lists what the loaded DB accepts.

Signals a revision simply doesn't have — 2020 has no GTW_compressorType, diBurnInType,
packPerformanceDeviation or cabinPTCHeaterType — are skipped rather than fatal; setting one
explicitly warns, since the operator asked for something the loaded DB cannot express.

Defaults are raw values (stable across 2020/2022/2026), all zero except GTW_chassisType
(3_CHASSIS) and GTW_brakeLineSwitchType (VC_ONLY -- a bench default so the sim's ECU brake nodes
keep UI/manual control instead of following the DI's wired switch; see scripts/vcleft/vcleft.py).
They are NOT a considered bench profile: GTW_numberHVILNodes 0 and GTW_brakeHWType 0 in
particular are worth setting deliberately.

Needs the CAN DB: constructed with ``NodeContext(db=...)``.
"""

from __future__ import annotations

import struct
import time
import warnings

from sim_core import BASELINE_FW, Node, SimFrame, zeros
from tesla_frames import GTW_CARCONFIG_ID, MuxedConfigTx

# Scenario key -> (DBC signal, default raw value). The 15 signals the DIR reads out of
# GTW_carConfig. Defaults hold at the same NUMBER on every revision we ship layouts for.
CARCONFIG = {
    "country": ("GTW_country", 0),  # raw 16-bit, no enum table
    "brake_hw_type": ("GTW_brakeHWType", 0),
    "drivetrain_type": ("GTW_drivetrainType", 0),  # RWD
    "tpms_type": ("GTW_tpmsType", 0),
    "vdc_type": ("GTW_vdcType", 0),
    "cabin_ptc_heater_type": ("GTW_cabinPTCHeaterType", 0),
    "spoiler_type": ("GTW_spoilerType", 0),
    "autopilot": ("GTW_autopilot", 0),
    "number_hvil_nodes": ("GTW_numberHVILNodes", 0),
    "performance_package": ("GTW_performancePackage", 0),
    "chassis_type": ("GTW_chassisType", 2),  # 3_CHASSIS
    "pack_energy": ("GTW_packEnergy", 0),
    "pack_performance_deviation": ("GTW_packPerformanceDeviation", 0),
    "di_burn_in_type": ("GTW_diBurnInType", 0),
    "compressor_type": ("GTW_compressorType", 0),
    "brake_line_switch_type": ("GTW_brakeLineSwitchType", 1),  # VC_ONLY bench default (see below)
}


def _gtw_time() -> bytearray:  # 0x528 GTW_time, DLC 4: big-endian uint32 unix seconds
    # The 2022 DIR brake-temp estimator cools its brake temps over (now - time saved at power-off)
    # from this clock; a zero clock gives now <= saved -> DI_a228 brakeTempEstUnavailable.
    return bytearray(struct.pack(">I", int(time.time()) & 0xFFFFFFFF))


class Gtw(Node):
    name = "GTW"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.carcfg = MuxedConfigTx(self.ctx.db, GTW_CARCONFIG_ID)
        # Raw value staged per scenario key, and the keys this revision's DB has no signal
        # for -- so a caller can tell "left at 0" from "not expressible on this rev".
        self.config: dict[str, int] = {}
        self.unsupported: list[str] = []
        for key, (_signal, default) in CARCONFIG.items():
            self._apply(key, default, explicit=False)

    def frames(self) -> list[SimFrame]:
        return self._frames(zeros(4))

    def _frames_2022(self) -> list[SimFrame]:
        """2022.45.15: GTW_time carries a real clock (``_gtw_time``)."""
        return self._frames(_gtw_time)

    def _frames(self, time_builder) -> list[SimFrame]:
        return [
            SimFrame("GTW_carConfig", 0x7FF, 0.100, self.carcfg.next_frame),
            SimFrame("GTW_time", 0x528, 0.100, time_builder),  # DIR exact-checks DLC=4
            SimFrame("GTW_updateStatus", 0x3ED, 0.100, zeros(1)),  # DIR exact-checks DLC=1
        ]

    def _frames_2026(self) -> list[SimFrame]:
        """2026.8.3: the 2022 set plus GTW_carState 0x318, new to the DIR's rx set.

        DLC8 and GATED -- counter@52 + checksum@56, seed id_lo+id_hi =
        0x1B, so the default magic applies. MIA-supervised, hence sent rather than skipped:
        arrival clears the MIA, and optional-node MIA only bites in DRIVE.

        zeros(8) is sufficient -- the MIA clear is unconditional once validation passes, and the
        one decoded field is a single bit (frame bit 48) that nothing in the torque path reads.
        Note bit 48 sits in byte6 BELOW the counter nibble at 52, so the counter does not
        disturb it.
        """
        return [
            *self._frames_2022(),
            SimFrame("GTW_carState", 0x318, 0.100, zeros(8), 52, 56),
        ]

    def fw_variants(self):
        return {
            BASELINE_FW: self.frames,
            "2022.45.15": self._frames_2022,
            "2026.8.3": self._frames_2026,
        }

    def _apply(self, key: str, value, *, explicit: bool) -> int | None:
        """Stage ``value`` (raw number or enum label) for ``key`` on the config frame.

        Returns the raw value staged, or None if this revision's database has no such
        signal. A bad label raises: MuxedConfigTx names what the loaded DB accepts."""
        signal = CARCONFIG[key][0]
        try:
            raw = self.carcfg.set(signal, value)
        except KeyError:  # signal absent from this revision's database
            if key not in self.unsupported:
                self.unsupported.append(key)
            if explicit:
                warnings.warn(
                    f"GTW: {signal} ({key}) is not in the loaded CAN database, so it cannot "
                    f"be sent on this revision; ignoring {value!r}",
                    stacklevel=3,
                )
            return None
        self.config[key] = raw
        return raw

    def set_config(self, key: str, value) -> int | None:
        """Driver externality: set one GTW_carConfig parameter by its scenario name.

        ``value`` is an enum label (preferred — it survives a revision renumbering) or a
        raw number."""
        if key not in CARCONFIG:
            raise ValueError(
                f"GTW: unknown car-config parameter {key!r}; expected one of {sorted(CARCONFIG)}"
            )
        return self._apply(key, value, explicit=True)

    def set_carconfig(self, signal: str, value) -> int:
        """Driver externality: set a GTW_carConfig signal by its raw DBC name (dashboard)."""
        return self.carcfg.set(signal, value)

    def configure(self, **s) -> None:  # scenario keys: the CARCONFIG keys
        for key in [k for k in s if k in CARCONFIG]:
            self.set_config(key, s.pop(key))
        super().configure(**s)


NODE = Gtw
