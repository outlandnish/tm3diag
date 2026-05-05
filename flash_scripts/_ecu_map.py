"""ECU_SCRIPT_MAP — lookup from `ecu_type` (lowercase) to (FlashScript, module_byte).

Keys are lowercase ecu_type values from `signed_metadata_map.tsv`. Module
bytes are sourced from the binary's node table at offset `+0x20` per
docs/FIRMWARE_UPDATE.md.
"""

from ._context import FlashScript
from ._scripts import (
    SCRIPT_APS,
    SCRIPT_BL,
    SCRIPT_BL_UPDATER,
    SCRIPT_BL_UPDATER_VCFRONT,
    SCRIPT_CMP,
    SCRIPT_ESP,
    SCRIPT_ESPCAL,
    SCRIPT_GTW3,
    SCRIPT_IBST,
    SCRIPT_IBSTCAL,
    SCRIPT_OPC,
    SCRIPT_PARK,
    SCRIPT_PCS,
    SCRIPT_PTC,
    SCRIPT_RAMAPP,
    SCRIPT_RAMAPP_ALT,
    SCRIPT_RCM,
    SCRIPT_STANDARD,
    SCRIPT_THS,
    SCRIPT_TPMS,
    SCRIPT_VCFRONT,
    SCRIPT_VCLEFT,
    SCRIPT_VCLEFTRAMAPP,
    SCRIPT_VCRIGHT,
)

# (FlashScript, module_byte)
_Entry = tuple[FlashScript, int]

ECU_SCRIPT_MAP: dict[str, _Entry] = {
    # gtw3 — stub
    "gtw3": (SCRIPT_GTW3, 0x00),

    # Standard script (0x00650fb0)
    # All these ECUs have module byte 0x00 at +0x20 in the binary's node table.
    # (The byte at +0x1C is a `node_id` used by `udsContextSwitch`, not the
    # module byte — earlier versions of this comment had them confused.)
    "hvbms":  (SCRIPT_STANDARD, 0x00),
    "cp":     (SCRIPT_STANDARD, 0x00),
    "epas3p": (SCRIPT_STANDARD, 0x00),
    "epas3s": (SCRIPT_STANDARD, 0x00),
    "epbl":   (SCRIPT_STANDARD, 0x00),
    "epbr":   (SCRIPT_STANDARD, 0x00),
    "hvp":    (SCRIPT_STANDARD, 0x00),
    "ocs1p":  (SCRIPT_STANDARD, 0x00),
    "sccmk":  (SCRIPT_STANDARD, 0x00),
    "vcsec":  (SCRIPT_STANDARD, 0x00),
    "tas":    (SCRIPT_STANDARD, 0x00),

    # CP PLC modem subcomponents — flashed via the CP MCU's bootloader using the
    # same SCRIPT_STANDARD as the regular CP app. Module byte is 0x00 (the CP
    # MCU's bootloader routes the .hex file contents to the PLC modem over its
    # internal interconnect based on each record's address range — no module
    # byte differentiates them at the wire level).
    # `cpPlcFw` is the modem firmware, `cpPlcPib` is the modem PIB
    # (Personality Identifier Block — modem config).
    "cpplcfw":  (SCRIPT_STANDARD, 0x00),
    "cpplcpib": (SCRIPT_STANDARD, 0x00),

    # vcfront / ibstcal (0x00651000)
    "vcfront": (SCRIPT_VCFRONT, 0x00),
    "ibstcal": (SCRIPT_IBSTCAL, 0x00),

    # vcright (0x00651030)
    "vcright": (SCRIPT_VCRIGHT, 0x00),

    # vcleft (0x00651050)
    "vcleft": (SCRIPT_VCLEFT, 0x00),

    # pcs/pcscpu2/di/dis/pm/pms (0x00651070)
    # Module byte values are taken from prog 1's bytecode literals at
    # 0x006510a0 (`05 04` then `05 00`), NOT from EcuNodeEntry+0x20. The sim
    # has +0x20=0x0C for di/dis/pcscpu2, but two successful third-party flash
    # captures (DI: `2E 01 02 04`, PM: `2E 01 02 00`) show the real
    # bootloader expects 0x04 / 0x00 — the prog 1 literals — not the entry
    # override values. The sim's context+0x29 override mechanism produces
    # the wrong wire byte for the secondary side; we bypass it here.
    "pcs":     (SCRIPT_PCS, 0x00),  # primary / CPU1 — verified via PM log
    "pm":      (SCRIPT_PCS, 0x00),  # primary / CPU1 — verified via PM log
    "pms":     (SCRIPT_PCS, 0x00),  # primary / CPU1
    "pcscpu2": (SCRIPT_PCS, 0x04),  # secondary / CPU2 — was 0x0C, see note above
    "di":      (SCRIPT_PCS, 0x04),  # secondary / CPU2 — verified via DI log
    "dis":     (SCRIPT_PCS, 0x04),  # secondary / CPU2 — was 0x0C, see note above

    # park (0x006510d0)
    "park": (SCRIPT_PARK, 0x00),

    # aps (0x006510f0)
    "aps": (SCRIPT_APS, 0x00),

    # RAM app scripts (0x00651110)
    #
    # Module bytes for *ramapp entries are drawn from EcuNodeEntry+0x20 in
    # the binary. Same caveat as the di/dis/pcscpu2 case applies — this is
    # the sim's context+0x29 override value, not a wire byte we've
    # empirically confirmed against real hardware. The first time we see a
    # successful flash log of any of these we should re-check.
    "vcleftramapp":  (SCRIPT_RAMAPP, 0x06),
    "vcrightramapp": (SCRIPT_RAMAPP, 0x0F),
    "vcfrontramapp": (SCRIPT_RAMAPP, 0x0F),
    "vcsecramapp":   (SCRIPT_RAMAPP, 0x0F),  # was vcsecrumapp (typo); seed metadata uses vcsecramapp
    "sccmksub":      (SCRIPT_RAMAPP, 0x06),

    # OPC RAMAPPs delivered to the PMS module's primary side. Seed metadata
    # references these as separate ecu_types alongside the parent pm/pms
    # flash; without entries here, get_script() would KeyError on a normal
    # 3-file PMS update (pms.bhx + dis.bhx + pmsramapp.bhx).
    #
    # Module byte = 0x00 is a CONSERVATIVE GUESS:
    #   - matches the prog 1 bytecode literal at script_ramapp
    #     (sub1 = `05 00` = moduleToProgram(0))
    #   - matches the parent PM/PMS wire byte (verified via a third-party PM log)
    # If the bootloader rejects 0x00, try the existing ramapp values 0x06
    # / 0x0F next. Untested on hardware.
    "pmramapp":  (SCRIPT_RAMAPP, 0x00),
    "pmsramapp": (SCRIPT_RAMAPP, 0x00),

    # ibst (0x00651140)
    "ibst": (SCRIPT_IBST, 0x00),

    # espcal / rcmcal (0x00651170)
    "espcal": (SCRIPT_ESPCAL, 0x07),
    "rcmcal": (SCRIPT_ESPCAL, 0x07),

    # esp (0x00651190)
    "esp": (SCRIPT_ESP, 0x00),

    # rcm (0x006511d0)
    "rcm": (SCRIPT_RCM, 0x00),

    # tpms (0x006511f0)
    "tpms": (SCRIPT_TPMS, 0x00),

    # cmp (0x00651230)
    "cmp": (SCRIPT_CMP, 0x00),

    # ptc (0x00651270)
    "ptc": (SCRIPT_PTC, 0x00),

    # vcright/vcfront/vcsec ramapp, bleepcenter (0x00651290)
    "bleepcenter": (SCRIPT_RAMAPP_ALT, 0x0F),

    # vcleftramapp alt (0x006512b0)
    # (same key as RAMAPP above; 0x006512b0 is the prog-0 path with vendor preflight)
    # Differentiated by ecu_type suffix in TSV when needed; default to VCLEFTRAMAPP.

    # opc / opcs (0x006512d0)
    "opc":  (SCRIPT_OPC, 0x0C),
    "opcs": (SCRIPT_OPC, 0x0C),

    # ths / swc / lumbar* / bleep* (0x006512e0)
    "ths":      (SCRIPT_THS, 0x0C),
    "swc":      (SCRIPT_THS, 0x0C),
    "lumbarl":  (SCRIPT_THS, 0x0B),
    "lumbar":   (SCRIPT_THS, 0x0B),
    "lumbarr":  (SCRIPT_THS, 0x0B),
    "bleep":    (SCRIPT_THS, 0x0F),
    "bleepleft":  (SCRIPT_THS, 0x0F),
    "bleepright": (SCRIPT_THS, 0x0F),

    # Bootloader-updater pairs (`*bu` first, then `*bl`) — see _BL_PARENT_NODE
    # in flash_scripts._groups. They use the parent ECU's CAN IDs; nothing
    # extra to set up at the transport layer. Module byte at +0x20 in the
    # binary is 0x00 for all of these. Scripts: bu uses 0x00651300, bl uses
    # 0x00651340. (The byte at +0x1C is the parent ECU's node_id, not the
    # module byte.)
    "parkbu":    (SCRIPT_BL_UPDATER,         0x00),
    "parkbl":    (SCRIPT_BL,                 0x00),
    "hvbmsbu":   (SCRIPT_BL_UPDATER,         0x00),
    "hvbmsbl":   (SCRIPT_BL,                 0x00),
    "hvpbu":     (SCRIPT_BL_UPDATER,         0x00),
    "hvpbl":     (SCRIPT_BL,                 0x00),
    "vcfrontbu": (SCRIPT_BL_UPDATER_VCFRONT, 0x00),
    "vcfrontbl": (SCRIPT_BL,                 0x00),
}


def get_script(ecu_type: str) -> _Entry:
    """Look up (FlashScript, module_byte) for an ecu_type name.

    Raises KeyError with a helpful message if the type is unknown.
    """
    key = ecu_type.lower()
    if key not in ECU_SCRIPT_MAP:
        raise KeyError(
            f"No flash script defined for ecu_type {ecu_type!r}. "
            f"Known types: {sorted(ECU_SCRIPT_MAP)}"
        )
    return ECU_SCRIPT_MAP[key]
