"""ECU_SCRIPT_MAP — lookup from `ecu_type` (lowercase) to (FlashScript, module_byte).

Keys are lowercase ecu_type values from `signed_metadata_map.tsv`. Module
bytes come from each node's table entry (see docs/FIRMWARE_UPDATE.md).
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
    SCRIPT_VCRIGHT,
)

# (FlashScript, module_byte)
_Entry = tuple[FlashScript, int]

ECU_SCRIPT_MAP: dict[str, _Entry] = {
    # gtw3 — stub
    "gtw3": (SCRIPT_GTW3, 0x00),

    # Standard script
    # All these ECUs have module byte 0x00 in the node table (distinct from the node_id used by
    # udsContextSwitch).
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
    # same SCRIPT_STANDARD as the regular CP app, but with DISTINCT module bytes.
    # The module byte (WDBI 0x0102) is NOT cosmetic here: the CP bootloader's
    # RequestDownload window validator gates the allowed address range on the
    # currently-selected module —
    #     module 0x00 -> CP app
    #     module 0x06 -> cpPlcPib
    #     module 0x08 -> cpPlcFw
    # so a RequestDownload for cpPlcFw/cpPlcPib under module 0x00 is rejected
    # NRC 0x31 (requestOutOfRange). cpPlcFw is loaded into the QCA7420 PLC modem
    # at boot; cpPlcPib is the modem PIB (Personality Identifier Block — modem
    # config). Fails safe: a wrong
    # module byte NRCs, it can't mis-target another region.
    "cpplcfw":  (SCRIPT_STANDARD, 0x08),
    "cpplcpib": (SCRIPT_STANDARD, 0x06),

    # vcfront / ibstcal
    "vcfront": (SCRIPT_VCFRONT, 0x00),
    "ibstcal": (SCRIPT_IBSTCAL, 0x00),

    # vcright
    "vcright": (SCRIPT_VCRIGHT, 0x00),

    # vcleft
    "vcleft": (SCRIPT_VCLEFT, 0x00),

    # pcs/pcscpu2/di/dis/pm/pms/pmr/pmrs/dir/dirs
    #
    # Module bytes for primary/secondary CPU selection (DID 0x0102):
    #
    # di/dis have their own dedicated CAN nodes (0x606/0x605) — the 0x04 wire
    # byte is confirmed for those nodes.
    # pmr/pmrs are the rear equivalent of pm/pms (rear power management).
    # dir/dirs are the rear equivalent of di/dis (rear drive inverter).
    "pcs":     (SCRIPT_PCS, 0x00),  # primary / CPU1 — verified via PM log
    "pm":      (SCRIPT_PCS, 0x00),  # primary / CPU1 — verified via PM log
    "pms":     (SCRIPT_PCS, 0x00),  # primary / CPU1
    "pmr":     (SCRIPT_PCS, 0x00),  # primary / CPU1 — rear motor
    "pmrs":    (SCRIPT_PCS, 0x00),  # primary / CPU1 — rear motor (signed)
    # secondary / CPU2 — shared PCS node; sim value, unverified
    "pcscpu2": (SCRIPT_PCS, 0x0C),
    # secondary / CPU2 — verified via DI log (separate node)
    "di":      (SCRIPT_PCS, 0x0C),
    # secondary / CPU2 — separate node, assumed same as di
    "dis":     (SCRIPT_PCS, 0x0C),
    # secondary / CPU2 — rear drive inverter, assumed same as di/dis
    "dir":     (SCRIPT_PCS, 0x0C),
    "dirs":    (SCRIPT_PCS, 0x0C),

    # park
    "park": (SCRIPT_PARK, 0x00),

    # aps
    "aps": (SCRIPT_APS, 0x00),

    # RAM app scripts
    #
    # Module bytes for *ramapp entries come from the node table. Same caveat
    # as di/dis/pcscpu2 — this is the override value, not a wire byte we've
    # empirically confirmed against real hardware. The first time we see a
    # successful flash log of any of these we should re-check.
    "vcleftramapp":  (SCRIPT_RAMAPP, 0x06),
    "vcrightramapp": (SCRIPT_RAMAPP, 0x0F),
    "vcfrontramapp": (SCRIPT_RAMAPP, 0x0F),
    # was vcsecrumapp (typo); seed metadata uses vcsecramapp
    "vcsecramapp":   (SCRIPT_RAMAPP, 0x0F),
    "sccmksub":      (SCRIPT_RAMAPP, 0x06),

    # OPC RAMAPPs delivered to the PMS module's primary side. Seed metadata
    # references these as separate ecu_types alongside the parent pm/pms
    # flash; without entries here, get_script() would KeyError on a normal
    # 3-file PMS update (pms.bhx + dis.bhx + pmsramapp.bhx).
    #
    # Module byte = 0x00 is a CONSERVATIVE GUESS:
    #   - matches the prog-1 moduleToProgram(0)
    #   - matches the parent PM/PMS wire byte (verified via EV Controls PM log)
    # If the bootloader rejects 0x00, try the existing ramapp values 0x06
    # / 0x0F next. Untested on hardware.
    "pmramapp":  (SCRIPT_RAMAPP, 0x00),
    "pmsramapp": (SCRIPT_RAMAPP, 0x00),

    # ibst
    "ibst": (SCRIPT_IBST, 0x00),

    # espcal / rcmcal
    "espcal": (SCRIPT_ESPCAL, 0x07),
    "rcmcal": (SCRIPT_ESPCAL, 0x07),

    # esp
    "esp": (SCRIPT_ESP, 0x00),

    # rcm
    "rcm": (SCRIPT_RCM, 0x00),

    # tpms
    "tpms": (SCRIPT_TPMS, 0x00),

    # cmp
    "cmp": (SCRIPT_CMP, 0x00),

    # ptc
    "ptc": (SCRIPT_PTC, 0x00),

    # vcright/vcfront/vcsec ramapp, bleepcenter
    "bleepcenter": (SCRIPT_RAMAPP_ALT, 0x0F),

    # vcleftramapp alt
    # (same key as RAMAPP above; the prog-0 path with vendor preflight)
    # Differentiated by ecu_type suffix in TSV when needed; default to VCLEFTRAMAPP.

    # opc / opcs
    "opc":  (SCRIPT_OPC, 0x0C),
    "opcs": (SCRIPT_OPC, 0x0C),

    # ths / swc / lumbar* / bleep*
    "ths":      (SCRIPT_THS, 0x0C),
    "swc":      (SCRIPT_THS, 0x0C),
    "lumbarl":  (SCRIPT_THS, 0x0B),
    "lumbar":   (SCRIPT_THS, 0x0B),
    "lumbarr":  (SCRIPT_THS, 0x0B),
    "bleep":    (SCRIPT_THS, 0x0F),
    "bleepleft":  (SCRIPT_THS, 0x0F),
    "bleepright": (SCRIPT_THS, 0x0F),

    # Bootloader-updater pairs are added programmatically below (see
    # BL_PARENT_ECUS / _add_bootloader_entries) so the *bu/*bl set stays in
    # one place and _groups.py can share it.
}


# ---------------------------------------------------------------------------
# Bootloader-updater (`*bu`) + bootloader-image (`*bl`) pairs
# ---------------------------------------------------------------------------
#
# For every parent ECU that ships a bootloader update, the metadata map carries
# a `<parent>bu` (updater agent) and `<parent>bl` (bootloader image) ecu_type.
# They flash via the parent ECU's CAN IDs (nothing extra at the transport
# layer) and the module byte is 0x00 for all of them. The `bu` runs
# SCRIPT_BL_UPDATER, the `bl` runs SCRIPT_BL; bu→bl→app order is mandatory.
# (The other per-node byte is the parent's node_id, not the module byte.)
#
# `vcfront` is the one exception: its updater needs the VCRIGHT OTA preamble,
# so `vcfrontbu` uses SCRIPT_BL_UPDATER_VCFRONT instead of SCRIPT_BL_UPDATER.
#
# This is the authoritative list of parent ECU node names with bootloader
# artifacts, sourced from `signed_metadata_map.tsv` across the firmware sets.
#
# IMPORTANT: each entry is a *parent app* name; the bu/bl children are derived
# as `<parent>bu`/`<parent>bl`. `epbl` (EPB-left) and `epbr` (EPB-right) are
# themselves real app ECUs that happen to end in "bl"/"br" — they are parents
# here, so their children are `epblbu`/`epblbl` and `epbrbu`/`epbrbl`. The
# parents `epbl`/`epbr` are NOT in the derived child set (_BL_PARENT_NODE), so
# they stay classified as apps. Driving bu/bl detection off this explicit set
# — rather than an `endswith('bl')` string test — is what keeps `epbl` from
# being misread as a bootloader image.
BL_PARENT_ECUS: tuple[str, ...] = (
    "bleepcradle",
    "cp",
    "dpb",
    "epas3p",
    "epas3s",
    "epbl",      # EPB-left app; children epblbu/epblbl
    "epbr",      # EPB-right app; children epbrbu/epbrbl
    "esp",
    "hvbms",
    "hvp",
    "ibst",
    "icr",
    "idb",
    "ocs1p",
    "park",
    "pcs",
    "pcscpu2",
    "plg",
    "pm",
    "pmf",
    "pmr",
    "rcu",
    "trcm",
    "vcbatt",
    "vcfront",
    "vcleft",
    "vcright",
    "vcseat2l",
    "vcseat2r",
    "vcsec",
    "wpc",
)

# A few parents only ship a bootloader image (`*bl`) with no matching updater
# (`*bu`) in the observed artifact set, or vice versa. Listing both keys is
# harmless — get_script is only called for ecu_types that actually appear in a
# plan — so we generate the full pair for every parent and rely on the planner
# to request only the files that exist.


# PCS/PM-family parents are dual-CPU (CPU1 primary + CPU2 secondary). For these,
# the bootloader IMAGE (`*bl`) payload targets the CPU2/secondary flash region
# (SHDR addr 0x82000 — sector 1, just above the never-erased sector-0 bootloader),
# NOT the CPU1 app base (0x88000). So the `*bl` flash must select the secondary
# module byte (0x0C) — selecting CPU1 (0x00) and then RequestDownload @ 0x82000
# is an address/module mismatch the bootloader rejects with NRC 0x22
# (conditionsNotCorrect). The `*bu` (updater agent) still goes to the CPU1 app
# slot (0x88000) with module 0x00. Single-CPU parents keep 0x00 for both.
# (pcscpu2 is itself the secondary side, so its bl is also a secondary-region write.)
_PCS_FAMILY_BL_PARENTS: frozenset[str] = frozenset(
    {"pcs", "pcscpu2", "pm", "pmf", "pmr"}
)


def _add_bootloader_entries(table: dict[str, _Entry]) -> None:
    for parent in BL_PARENT_ECUS:
        updater = (
            SCRIPT_BL_UPDATER_VCFRONT if parent == "vcfront" else SCRIPT_BL_UPDATER
        )
        bl_module = 0x0C if parent in _PCS_FAMILY_BL_PARENTS else 0x00
        table[f"{parent}bu"] = (updater, 0x00)
        table[f"{parent}bl"] = (SCRIPT_BL, bl_module)


_add_bootloader_entries(ECU_SCRIPT_MAP)


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
