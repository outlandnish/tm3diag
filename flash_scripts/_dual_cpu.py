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

from ._constants import _RC_CHECK_REV, _RC_ERASE, _RC_VERIFY_CRC

if TYPE_CHECKING:
    from uds_local.client import UdsSession


# ecu_type → role in PCS-family dual-CPU pairings
_PCS_PRIMARY_TYPES = frozenset({"pcs", "pm", "pms"})
_PCS_SECONDARY_TYPES = frozenset({"pcscpu2", "di", "dis"})

# Module bytes for prog 1's two moduleToProgram calls.
#
# These are the **bytecode literals** from prog 1 at 0x006510a0
# (`05 04` then `05 00`).

_PROG1_MODULE_SECONDARY = 0x04
_PROG1_MODULE_PRIMARY = 0x00


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
    sess: "UdsSession",
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
      moduleToProgram(4)   netSetTimeout(30)  — CPU2/secondary
      initializeEraseModule(0)
      netSetTimeout(1)
      transferData(secondary_bhx)
      checkModuleProgrammedCorrectly
      moduleToProgram(0)   netSetTimeout(30)  — CPU1/primary
      initializeEraseModule(0)
      transferData(primary_bhx)
      netSetTimeout(4)
      checkModuleProgrammedCorrectly
      checkCorrectComponentAndRev
      reset(soft)
    """
    print(f"  Dual-CPU sequence (prog 1, single auth session):")
    print(f"    primary   ({primary_entry.dest_name}, ecu_type={primary_entry.component})"
          f"  → moduleToProgram(0x{_PROG1_MODULE_PRIMARY:02X})")
    print(f"    secondary ({secondary_entry.dest_name}, ecu_type={secondary_entry.component})"
          f"  → moduleToProgram(0x{_PROG1_MODULE_SECONDARY:02X}) [flashed first]")

    print("  Step: ECUReset 11 81 (SPR, no wait)")
    sess.ecu_reset_no_wait(0x01)
    print("  Step: Wait for bootloader handover")
    sess.wait_for_bootloader()

    print("  Step: DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)

    print("  Step: ReadDataByIdentifier COMP_AND_FW_TYPE (0x0101)")
    comp_fw = sess.read_did(0x0101)
    print(
        f"    raw response: {len(comp_fw)} bytes —"
        f" {' '.join(f'{b:02X}' for b in comp_fw)}"
    )
    if len(comp_fw) < 3:
        from uds_local.client import MalformedResponseError
        raise MalformedResponseError(
            0x22,
            f"DID 0x0101 response too short ({len(comp_fw)} bytes, expected 3 "
            "for [component_key, fw_type, protocol_ver])",
        )
    # ODJ-documented layout (application response):
    #   byte[0] = COMPONENT_KEY, byte[1] = FIRMWARE_TYPE, byte[2] = PROTOCOL_VER
    component_key, fw_type, protocol_ver = comp_fw[0], comp_fw[1], comp_fw[2]
    print(
        f"    parsed (per app ODJ): component_key=0x{component_key:02X}"
        f"  fw_type=0x{fw_type:02X}"
        f"  protocol_ver=0x{protocol_ver:02X}"
    )
    if fw_type != 0x01:
        from uds_local.client import MalformedResponseError
        raise MalformedResponseError(
            0x22,
            f"DID 0x0101 FIRMWARE_TYPE byte 0x{fw_type:02X} != expected 0x01"
        )

    seed_level = 0x01 if protocol_ver < 3 else 0x05
    print(
        f"  Step: SecurityAccess (idx=0 protocol_ver={protocol_ver}"
        f" seed_level=0x{seed_level:02X})"
    )
    sess.security_access(level_idx=0, seed_level=seed_level)

    # ---- CPU2 / secondary first ----
    _flash_one_cpu_in_session(
        sess,
        module_byte=_PROG1_MODULE_SECONDARY,
        bhx_file=secondary_bhx,
        label=f"secondary ({secondary_entry.component})",
        verify_rev_at_end=False,
    )

    # ---- CPU1 / primary second ----
    _flash_one_cpu_in_session(
        sess,
        module_byte=_PROG1_MODULE_PRIMARY,
        bhx_file=primary_bhx,
        label=f"primary ({primary_entry.component})",
        verify_rev_at_end=True,
    )

    print("  Step: ECUReset 11 81 (SPR, no wait) — return to application")
    sess.ecu_reset_no_wait(0x01)
    sess.sleep(0.3)


def _flash_one_cpu_in_session(
    sess: "UdsSession",
    module_byte: int,
    bhx_file: object,
    label: str,
    verify_rev_at_end: bool,
) -> None:
    """One CPU's slice of prog 1 — assumes session + auth already established."""
    print(f"  --- {label} ---")
    print(f"  Step: netSetTimeout(30) + moduleToProgram(0x{module_byte:02X})")
    sess.set_timeout(30.0)
    sess.module_to_program(module_byte)

    print("  Step: RC 0xFF00 initializeEraseModule (P2=30s)")
    sess.start_tester_present()
    try:
        sess.routine_control(_RC_ERASE, b"\x01")
    finally:
        sess.stop_tester_present()

    print("  Step: netSetTimeout(1)")
    sess.set_timeout(1.0)

    for seg_idx, seg in enumerate(bhx_file.segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        sess.transfer_data(seg.data, max_block_len)
        sess.request_transfer_exit()

    if verify_rev_at_end:
        print("  Step: netSetTimeout(4)")
        sess.set_timeout(4.0)
    print("  Step: RC 0x0201 checkModuleProgrammedCorrectly")
    sess.routine_control(_RC_VERIFY_CRC)
    if verify_rev_at_end:
        print("  Step: RC 0x0202 checkCorrectComponentAndRev")
        sess.routine_control(_RC_CHECK_REV)
