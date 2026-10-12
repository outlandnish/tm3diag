"""Module-byte regression tests for flash region routing.

Guards the CP PLC subcomponent routing: the CP bootloader's RequestDownload
window validator gates the allowed address range on the selected module byte
(WDBI 0x0102), so cpPlcFw / cpPlcPib must select their own regions instead of
the CP app window. A wrong byte fails safe (NRC 0x31, requestOutOfRange); it
cannot mis-target another region.

Also guards the PCS/PM-family bootloader image (`*bl`): it selects the updater's
bootloader-program mode 0x05, not the CPU2-routed app selector 0x0C.
See flash_scripts/_ecu_map.py.
"""

from flash_scripts._ecu_map import get_script
from flash_scripts._scripts import SCRIPT_BL, SCRIPT_STANDARD

PCS_FAMILY_BL = ("pcsbl", "pcscpu2bl", "pmbl", "pmfbl", "pmrbl")


def test_cp_app_stays_module_0():
    script, module = get_script("cp")
    assert script is SCRIPT_STANDARD
    assert module == 0x00, "cp app flashes the [0x8000,0xe0000] window (module 0x00)"


def test_cp_plc_fw_selects_modem_fw_region():
    script, module = get_script("cpplcfw")
    assert script is SCRIPT_STANDARD
    assert module == 0x08, (
        "cpPlcFw must select module 0x08; "
        f"got 0x{module:02X} — module 0x00 is the CP app window and NRCs 0x31"
    )


def test_cp_plc_pib_selects_modem_pib_region():
    script, module = get_script("cpplcpib")
    assert script is SCRIPT_STANDARD
    assert module == 0x06, (
        "cpPlcPib must select module 0x06; "
        f"got 0x{module:02X} — module 0x00 is the CP app window and NRCs 0x31"
    )


def test_cp_plc_subcomponents_distinct_from_app():
    """The three CP regions must not collide on one module byte."""
    bytes_ = {et: get_script(et)[1] for et in ("cp", "cpplcfw", "cpplcpib")}
    assert len(set(bytes_.values())) == 3, f"CP regions must be distinct: {bytes_}"


def test_pcs_family_bl_uses_bootloader_mode_5():
    for et in PCS_FAMILY_BL:
        script, module = get_script(et)
        assert script is SCRIPT_BL, f"{et} should map to SCRIPT_BL"
        assert module == 0x05, (
            f"{et} must select bootloader mode 0x05, "
            f"got 0x{module:02X} — 0x0C is the CPU2-routed app path"
        )


def test_bu_agents_stay_module_0():
    for et in ("pmrbu", "pmbu", "pmfbu", "pcsbu", "hvbmsbu", "parkbu"):
        assert get_script(et)[1] == 0x00, f"{et} (updater) must use module 0x00"


def test_single_cpu_bl_stays_module_0():
    for et in ("hvbmsbl", "parkbl", "ibstbl", "espbl", "cpbl"):
        assert get_script(et)[1] == 0x00, f"{et} (single-CPU bl) must use module 0x00"
