"""FlashScript instances — one per distinct hashpicker_sim VM script.

Each FlashScript mirrors a decoded VM bytecode sequence; see
`docs/FIRMWARE_UPDATE.md`.
"""

from ._context import FlashScript
from ._steps import (
    step_board_info,
    step_check_flash_count_0,
    step_check_flash_count_1,
    step_check_flash_count_2,
    step_check_rev,
    step_clear_dtc,
    step_ecu_reset,
    step_erase,
    step_halt_if_running_boot_updater,
    step_hard_reset_with_retries,
    step_module_to_program,
    step_programming_session,
    step_security_access,
    step_sleep_100ms,
    step_sleep_300ms,
    step_sleep_500ms,
    step_sleep_1000ms,
    step_sleep_5000ms,
    step_transfer_loop,
    step_vcright_ota_prep,
    step_vendor_preflight,
    step_verify_comp_fw,
    step_verify_crc,
    step_wait_for_bootloader,
)

# gtw3: stub only, no flash sequence
SCRIPT_GTW3 = FlashScript(steps=[])

# Standard: hvbms, cp, epas3p/s, epbl/r, hvp, ocs1p, sccmk, vcsec, tas
SCRIPT_STANDARD = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_300ms,
    ],
)

# vcfront / ibstcal (prog 1: standard flash only)
SCRIPT_VCFRONT = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_500ms,
    ],
)

# vcright (prog 0: standard flash)
SCRIPT_VCRIGHT = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_500ms,
    ],
)

# vcleft (pre-flash vendor routine)
SCRIPT_VCLEFT = FlashScript(
    steps=[
        step_vendor_preflight,
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_500ms,
    ],
)

# pcs/pcscpu2/di/dis/pm/pms (prog 0: extended erase timeout)
# module_byte is set per-entry from ECU_SCRIPT_MAP.
# erase_timeout=10s: a successful EV Controls PM flash showed erase taking
# ~4.65s with no responsePending in between (silence on the bus until the
# 71 01 FF 00 00 lands). 5s was barely enough; 10s matches the binary's
# netSetTimeout(5)→P2*=10s for the PCS family.
SCRIPT_PCS = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_300ms,
    ],
    erase_timeout=10.0,
)

# park (prog 0: extended erase timeout, 5 s post-reset sleep)
SCRIPT_PARK = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
        step_sleep_5000ms,
    ],
    erase_timeout=1.0,
)

# park / aps (prog 0)
SCRIPT_APS = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=1.0,
)

# RAM app scripts: vcleftramapp, vcrightramapp, vcfrontramapp,
#              vcsecramapp, sccmksub, pmramapp, pmsramapp
#              (prog 0: no boardPartSerialGet)
SCRIPT_RAMAPP = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
)

# ibst (prog 0: flash count check + DTC clear + security level 3)
SCRIPT_IBST = FlashScript(
    steps=[
        step_check_flash_count_2,
        step_clear_dtc,
        step_ecu_reset,
        step_wait_for_bootloader,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# espcal / rcmcal (calibration flash, security level 3)
SCRIPT_ESPCAL = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# esp (flash count check + security level 3)
SCRIPT_ESP = FlashScript(
    steps=[
        step_check_flash_count_1,
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_ecu_reset,
    ],
    security_level=3,
)

# ibstcal bootloader path (hard reset with retries)
SCRIPT_IBSTCAL = FlashScript(
    steps=[
        step_check_flash_count_0,
        step_hard_reset_with_retries,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_hard_reset_with_retries,
        step_sleep_100ms,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# rcm (Pektron: flash count + hard reset + explicit erase)
SCRIPT_RCM = FlashScript(
    steps=[
        step_check_flash_count_0,
        step_hard_reset_with_retries,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_sleep_100ms,
        step_hard_reset_with_retries,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# tpms (security level 4 — baolong_hash)
SCRIPT_TPMS = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    security_level=4,
    erase_timeout=3.0,
)

# cmp (security level 7 — pektron-style)
SCRIPT_CMP = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    security_level=7,
)

# ptc (non-standard erase, 10 s timeout)
SCRIPT_PTC = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=10.0,
)

# vcright/vcfront/vcsec ramapp, bleepcenter (prog 0)
SCRIPT_RAMAPP_ALT = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
    ],
)

# vcleftramapp (prog 0: pre-flash vendor routine)
SCRIPT_VCLEFTRAMAPP = FlashScript(
    steps=[
        step_vendor_preflight,
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
    ],
    erase_timeout=5.0,
)

# opc / opcs (prog 1: standard flash, 3 s timeout)
SCRIPT_OPC = FlashScript(
    steps=[
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=3.0,
)

# ths / swc / lumbar* / bleep* (prog 2: standard flash, 3 s timeout)
SCRIPT_THS = FlashScript(
    steps=[
        step_sleep_500ms,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=3.0,
)

# bootloader-updater (`*bu` files: parkbu, hvbmsbu, hvpbu)
# Standard prog-0 flash with fw_type=1 — flashes the update agent into the regular
# app slot. Confirmed against the device firmware: update-2020.img bu script at
# 0x40035EF2 (the script the `*bu` node entries point to). Decoded bytecode:
#   haltIfRunningBootUpdater(0)   ← 0x29: abort if ECU already reports fw_type 2
#   reset(0) + enterBootloader(0)
#   diagnosticSession(2)  netSetTimeout(3)
#   varifyCompAndFirmwareType(1)  securityAccess(0)
#   CALL sub0 [moduleToProgram(0) + initializeEraseModule(0) + transferData(0) + RET]
#   checkModuleProgrammed  checkCorrectComponentAndRev
#   reset(0)  halt
SCRIPT_BL_UPDATER = FlashScript(
    steps=[
        step_halt_if_running_boot_updater,  # ← 0x29 leading guard (device-confirmed)
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,  # expected_fw_type=1 (default)
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=3.0,
)

SCRIPT_BL = FlashScript(
    steps=[
        step_sleep_1000ms,
        step_wait_for_bootloader,  # confirm the bu agent is up before DSC
        step_programming_session,
        step_verify_comp_fw,  # expected_fw_type=2 set below
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=3.0,
    expected_fw_type=0x02,
)

SCRIPT_BL_UPDATER_VCFRONT = FlashScript(
    steps=[
        step_vcright_ota_prep,  # ← sub4 (VCRIGHT detour)
        step_halt_if_running_boot_updater,  # ← 0x29 leading guard (bu variant)
        step_ecu_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,  # expected_fw_type=1 (default)
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_ecu_reset,
    ],
    erase_timeout=3.0,
)
