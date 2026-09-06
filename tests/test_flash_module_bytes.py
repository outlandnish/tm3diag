"""Module-byte regression tests for flash region routing.

Guards the CP PLC subcomponent routing: the CP bootloader's RequestDownload
window validator gates the allowed address range on the selected module byte
(WDBI 0x0102), so cpPlcFw / cpPlcPib must select their own regions instead of
the CP app window. A wrong byte fails safe (NRC 0x31, requestOutOfRange); it
cannot mis-target another region. See flash_scripts/_ecu_map.py.
"""

from flash_scripts._ecu_map import get_script
from flash_scripts._scripts import SCRIPT_STANDARD


def test_cp_app_stays_module_0():
    script, module = get_script("cp")
    assert script is SCRIPT_STANDARD
    assert module == 0x00, "cp app flashes the [0x8000,0xe0000] window (module 0x00)"


def test_cp_plc_fw_selects_modem_fw_region():
    script, module = get_script("cpplcfw")
    assert script is SCRIPT_STANDARD
    assert module == 0x08, (
        "cpPlcFw (@0x100000) must select module 0x08 -> [0x100000,0x200000]; "
        f"got 0x{module:02X} — module 0x00 is the CP app window and NRCs 0x31"
    )


def test_cp_plc_pib_selects_modem_pib_region():
    script, module = get_script("cpplcpib")
    assert script is SCRIPT_STANDARD
    assert module == 0x06, (
        "cpPlcPib (@0xe0000) must select module 0x06 -> [0xe0000,0x100000]; "
        f"got 0x{module:02X} — module 0x00 is the CP app window and NRCs 0x31"
    )


def test_cp_plc_subcomponents_distinct_from_app():
    """The three CP regions must not collide on one module byte."""
    bytes_ = {et: get_script(et)[1] for et in ("cp", "cpplcfw", "cpplcpib")}
    assert len(set(bytes_.values())) == 3, f"CP regions must be distinct: {bytes_}"
