"""Flash script definitions for Tesla Model 3 ECUs.

Each ECU family uses a distinct UDS flash sequence, expressed as composable step
functions assembled into FlashScript instances (see docs/FIRMWARE_UPDATE.md).
`ECU_SCRIPT_MAP` maps lowercase ecu_type names (from signed_metadata_map.tsv) to
the FlashScript they use.

Submodules:
  _constants  shared RC IDs, board-info DIDs, FLASH_COUNT_LIMITS, seed-level table
  _context    FlashContext / FlashScript dataclasses, StepFn type alias
  _steps      step_* atoms used by FlashScript.steps lists
  _scripts    SCRIPT_* FlashScript definitions
  _ecu_map    ECU_SCRIPT_MAP and get_script
  _dual_cpu   PCS-family prog 1 runner + detector
  _groups     bootloader, subcomponent and RAM-app detection helpers
"""

from ._constants import FLASH_COUNT_LIMITS as FLASH_COUNT_LIMITS
from ._context import FlashContext as FlashContext
from ._context import FlashScript as FlashScript
from ._context import StepFn as StepFn
from ._dual_cpu import find_dual_cpu_pair as find_dual_cpu_pair
from ._dual_cpu import run_pcs_dual_cpu as run_pcs_dual_cpu
from ._dual_cpu import secondary_fallback_module_byte as secondary_fallback_module_byte
from ._ecu_map import ECU_SCRIPT_MAP as ECU_SCRIPT_MAP
from ._ecu_map import get_script as get_script
from ._groups import (  # noqa: F401
    find_bootloader_entries,
    find_ramapp_entries,
    find_subcomponent_entries,
    is_bootloader_ecu_type,
    is_ramapp_ecu_type,
    is_subcomponent_ecu_type,
    parent_node_for_bootloader,
    parent_node_for_subcomponent,
)
from ._headless import QuietDisplay as QuietDisplay
from ._headless import flash_components as flash_components
from ._headless import select_entries as select_entries
from ._headless import uds_node_for as uds_node_for
from ._scripts import (  # noqa: F401
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
from ._steps import (  # noqa: F401
    step_board_info,
    step_check_flash_count_0,
    step_check_flash_count_1,
    step_check_flash_count_2,
    step_check_rev,
    step_clear_dtc,
    step_ecu_reset,
    step_erase,
    step_halt_if_running_boot_updater,
    step_hard_reset,
    step_hard_reset_with_retries,
    step_module_to_program,
    step_probe_bootloader_state,
    step_programming_session,
    step_security_access,
    step_sleep_100ms,
    step_sleep_300ms,
    step_sleep_500ms,
    step_sleep_1000ms,
    step_sleep_5000ms,
    step_start_tester_present,
    step_stop_tester_present,
    step_transfer_loop,
    step_transfer_loop_inter_shdr,
    step_vcright_ota_prep,
    step_vendor_preflight,
    step_verify_comp_fw,
    step_verify_crc,
    step_wait_for_bootloader,
)
