"""Dual-CPU prog 1 runner for the PCS family (script 0x00651070 prog 1).

Script `0x00651070` prog 1 flashes both CPUs of a PCS-family ECU in a single
authenticated session. CPU2 (the secondary, ecu_type=pcscpu2/di/dis) is
flashed first with bootloader-internal module byte 0x04, then CPU1
(ecu_type=pcs/pm/pms) with module byte 0x00. The 0x04/0x00 codes are
distinct from the prog-0 node module bytes (0x0C / 0x00) — see
FIRMWARE_UPDATE.md "Prog 1 module bytes vs node module bytes".

When find_firmware returns both a primary and a secondary entry for the same
lookup, dfu.py auto-switches to this sequence instead of running prog 0
twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._context import FlashContext
from ._ecu_map import get_script
from ._scripts import SCRIPT_PCS
from ._steps import (
    step_check_rev,
    step_ecu_reset,
    step_erase,
    step_module_to_program,
    step_programming_session,
    step_security_access,
    step_sleep_300ms,
    step_transfer_loop,
    step_verify_comp_fw,
    step_verify_crc,
    step_wait_for_bootloader,
)

if TYPE_CHECKING:
    from uds_local.client import UdsSession


# ecu_type → role in PCS-family dual-CPU pairings
_PCS_PRIMARY_TYPES = frozenset({"pcs", "pm", "pms", "pmr", "pmrs"})
_PCS_SECONDARY_TYPES = frozenset({"pcscpu2", "di", "dis", "dir", "dirs"})

# Fallback module bytes for secondary-CPU / secondary-region flashes — tried if
# the primary byte gets NRC 0x10/0x31/0x22. The two known secondary-select bytes
# are 0x0C (the EcuNodeEntry+0x20 / node-table value) and 0x04; which one a given bootloader accepts is firmware-dependent, so
# we lead with one and fall back to the other.
_SECONDARY_MODULE_FALLBACK: dict[str, int] = {
    "pcscpu2": 0x04,
    "di": 0x04,
    "dis": 0x04,
    "dir": 0x04,
    "dirs": 0x04,
    # PCS/PM-family bootloader images (secondary-region writes)
    "pcsbl": 0x04,
    "pcscpu2bl": 0x04,
    "pmbl": 0x04,
    "pmfbl": 0x04,
    "pmrbl": 0x04,
}


def secondary_fallback_module_byte(ecu_type: str) -> int | None:
    """Return the fallback module byte for a secondary CPU ecu_type, or None."""
    return _SECONDARY_MODULE_FALLBACK.get(ecu_type.lower())


def find_dual_cpu_pair(selected: list) -> tuple[object, object] | None:
    """Detect a PCS-family dual-CPU pairing in `selected`.

    Returns `(primary_entry, secondary_entry)` if both a primary and secondary
    PCS-family ecu_type are present; otherwise `None`. Each entry must expose a
    `component` attribute (matches metadata.FirmwareEntry).
    """
    primary: object | None = None
    secondary: object | None = None
    for entry in selected:
        ecu_type = entry.component.lower()
        if ecu_type in _PCS_PRIMARY_TYPES and primary is None:
            primary = entry
        elif ecu_type in _PCS_SECONDARY_TYPES and secondary is None:
            secondary = entry
    if primary is not None and secondary is not None:
        return primary, secondary
    return None


def run_pcs_dual_cpu(
    sess: UdsSession,
    primary_bhx: object,
    primary_entry: object,
    secondary_bhx: object,
    secondary_entry: object,
) -> None:
    """Execute script 0x00651070 prog 1 — both CPUs in one authenticated session.

    Sequence (matches the decoded VM bytecode at 0x00651070+0x30):
      reset(soft) + enterBootloader(0)
      diagnosticSession(2)
      varifyCompAndFirmwareType(1)
      securityAccess(0)                       — protocol_ver-aware
      moduleToProgram(secondary_module_byte)   netSetTimeout(30)  — CPU2/secondary
      initializeEraseModule(0)
      netSetTimeout(1)
      transferData(secondary_bhx)
      checkModuleProgrammedCorrectly
      moduleToProgram(primary_module_byte)   netSetTimeout(30)  — CPU1/primary
      initializeEraseModule(0)
      transferData(primary_bhx)
      netSetTimeout(4)
      checkModuleProgrammedCorrectly
      checkCorrectComponentAndRev
      reset(soft)
    """
    primary_module_byte = get_script(primary_entry.component.lower())[1]
    secondary_module_byte = get_script(secondary_entry.component.lower())[1]

    print("  Dual-CPU sequence (prog 1, single auth session):")
    print(
        f"    primary   ({primary_entry.dest_name}, ecu_type={primary_entry.component})"
        f"  → moduleToProgram(0x{primary_module_byte:02X})"
    )
    print(
        f"    secondary ({secondary_entry.dest_name}, ecu_type={secondary_entry.component})"
        f"  → moduleToProgram(0x{secondary_module_byte:02X}) [flashed first]"
    )

    ctx = FlashContext(
        bhx_file=secondary_bhx,
        entry=secondary_entry,
        module_byte=secondary_module_byte,
        fallback_module_byte=_SECONDARY_MODULE_FALLBACK.get(secondary_entry.component.lower()),
        erase_timeout=SCRIPT_PCS.erase_timeout,
        security_level=SCRIPT_PCS.security_level,
        expected_fw_type=SCRIPT_PCS.expected_fw_type,
    )

    # Outer setup — once for the whole dual-CPU session
    step_ecu_reset(sess, ctx)
    step_wait_for_bootloader(sess, ctx)
    step_programming_session(sess, ctx)
    step_verify_comp_fw(sess, ctx)  # sets ctx.protocol_ver for step_security_access
    step_security_access(sess, ctx)

    # ---- CPU2 / secondary first ----
    print(f"  --- secondary ({secondary_entry.component}) ---")
    step_module_to_program(sess, ctx)
    step_erase(sess, ctx)
    step_transfer_loop(sess, ctx)
    step_verify_crc(sess, ctx)

    # ---- CPU1 / primary second ----
    print(f"  --- primary ({primary_entry.component}) ---")
    ctx.bhx_file = primary_bhx
    ctx.entry = primary_entry
    ctx.module_byte = primary_module_byte
    ctx.fallback_module_byte = None
    step_module_to_program(sess, ctx)
    step_erase(sess, ctx)
    step_transfer_loop(sess, ctx)
    step_verify_crc(sess, ctx)
    step_check_rev(sess, ctx)

    step_ecu_reset(sess, ctx)
    step_sleep_300ms(sess, ctx)
