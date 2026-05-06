"""Step functions — atoms composed into FlashScript instances.

Each step is `(sess, ctx) -> None`. Steps mutate `ctx` to pass state forward
(e.g. `step_verify_comp_fw` stashes `protocol_ver`) and call methods on `sess`
to drive the underlying UDS transport.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ._constants import (
    _BOARD_INFO_DIDS,
    _RC_CHECK_REV,
    _RC_ERASE,
    _RC_VERIFY_CRC,
    _SECURITY_SEED_LEVEL,
    FLASH_COUNT_LIMITS,
)
from ._context import FlashContext

if TYPE_CHECKING:
    from uds_local.client import UdsSession


# ---------------------------------------------------------------------------
# Reset / bootloader handover
# ---------------------------------------------------------------------------

def step_soft_reset(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset `11 81` (subfunction 0x01 with SPR set) — fire-and-forget.

    Matches VM `reset(0)`. When used as the opening reset, `step_wait_for_bootloader`
    must follow immediately so Phase 1 keep-alives start right after the reset frame,
    matching `enter_bootloader_v0` in update.img. For trailing resets, callers append
    an explicit `step_sleep_*` step if needed.
    """
    print("  Step: ECUReset 11 81 (SPR, no wait)")
    sess.ecu_reset_no_wait(0x01)


def step_wait_for_bootloader(sess: "UdsSession", ctx: FlashContext) -> None:
    """Wait for the bootloader to take over after a reset (VM `enterBootloader(0)`).

    Required after the opening reset of every flash script. Without this, the
    application services DSC / RDBI / SecurityAccess (they all succeed there)
    but `WDBI 0x0102` (`moduleToProgram`) is bootloader-only and gets rejected.
    """
    print("  Step: Wait for bootloader handover")
    sess.wait_for_bootloader()


def step_probe_bootloader_state(sess: "UdsSession", ctx: FlashContext) -> None:
    """[DIAG] Read 0xF180 and 0xF181 to determine if we're in the bootloader or the app.

    Both ends of the boot/app pair typically respond to common DIDs, DSC, SA,
    and TP — those don't distinguish. But:
      * `0xF180` (BOOTLOADER_VERSION) is exposed by both, but its byte 8
        (FIRMWARE_TYPE) is the device's self-reported firmware-type:
          0 = bootloader, 1 = regular firmware (per Tesla's tagging)
      * `0xF181` (APPLICATION_VERSION) usually only exists in the app — the
        bootloader returns NRC 0x31 for it.

    Run before DSC(2) so we see the post-reset state without altering it.
    """
    print("  Step: [DIAG] Probing bootloader vs application state")
    try:
        f180 = sess.read_did(0xF180)
        hex_str = " ".join(f"{b:02X}" for b in f180)
        print(f"    DID 0xF180 ({len(f180)} bytes): {hex_str}")
        if len(f180) >= 9:
            modules = f180[0]
            component_id = (f180[1] << 8) | f180[2]
            pcba_id = f180[3]
            assembly_id = f180[4]
            usage_id = (f180[5] << 8) | f180[6]
            firmware_type = f180[8]
            label = (
                "BOOTLOADER (firmware_type=0)" if firmware_type == 0
                else f"APP (firmware_type=0x{firmware_type:02X})"
            )
            print(
                f"      MODULES=0x{modules:02X}"
                f"  COMPONENT_ID=0x{component_id:04X}"
                f"  PCBA_ID=0x{pcba_id:02X}"
                f"  ASSEMBLY_ID=0x{assembly_id:02X}"
                f"  USAGE_ID=0x{usage_id:04X}"
                f"  FIRMWARE_TYPE=0x{firmware_type:02X}"
            )
            print(f"      → likely {label}")
    except Exception as e:
        print(f"    DID 0xF180: ERROR — {type(e).__name__}: {e}")

    try:
        f181 = sess.read_did(0xF181)
        hex_str = " ".join(f"{b:02X}" for b in f181)
        print(f"    DID 0xF181 ({len(f181)} bytes): {hex_str}")
    except Exception as e:
        print(
            f"    DID 0xF181: ERROR ({type(e).__name__}: {e})"
            " — likely BOOTLOADER (no APPLICATION_VERSION exposed)"
        )


def step_hard_reset(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset hard reset, wait for response (opcode 8 operand 0)."""
    print("  Step: ECUReset (hard reset, wait for response)")
    sess.ecu_reset(0x01)


def step_hard_reset_with_retries(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset hard reset with 3 retries + 10 s delay each (opcode 8 operand 2)."""
    print("  Step: ECUReset (hard reset, 3 retries + 10 s delay)")
    for attempt in range(3):
        try:
            sess.ecu_reset(0x01)
            return
        except Exception:
            if attempt < 2:
                print(
                    f"    Reset attempt {attempt + 1} failed, retrying in 10 s")
                time.sleep(10)
    sess.ecu_reset(0x01)


# ---------------------------------------------------------------------------
# Identification / session / auth
# ---------------------------------------------------------------------------

def step_board_info(sess: "UdsSession", ctx: FlashContext) -> None:
    """Read board part/serial DIDs — logged only, failure does not abort."""
    print("  Step: Board part/serial DIDs (0xF012-0xF015)")
    for did in _BOARD_INFO_DIDS:
        try:
            data = sess.read_did(did)
            print(
                f"    0x{did:04X}: "
                f"{data.decode('ascii', errors='replace').rstrip(chr(0))!r}"
            )
        except Exception:
            pass


def step_start_tester_present(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: TesterPresent keepalive start")
    sess.start_tester_present()


def step_stop_tester_present(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: TesterPresent keepalive stop")
    sess.stop_tester_present()


def step_programming_session(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)  # ECU reboots into bootloader after programming session


def step_verify_comp_fw(sess: "UdsSession", ctx: FlashContext) -> None:
    """RDBI 0x0101 — log raw bytes, parse, stash protocol_ver on the context.

    ODJ-documented (application response) layout:
      byte[0] = COMPONENT_KEY
      byte[1] = FIRMWARE_TYPE        ← expected to equal ctx.expected_fw_type
      byte[2] = BOOTLOADER_PROTOCOL_VERSION

    Bootloader-mode response is observed to differ on at least PCS — the
    response there looks like [COMPONENT_ID_LO, COMPONENT_ID_HI, PROTOCOL_VER]
    with no FIRMWARE_TYPE field at all. We log the raw bytes and downgrade
    the fw_type mismatch to a warning so the flash can proceed; the wire
    byte order may not match the application ODJ in bootloader mode.
    """
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
    component_key = comp_fw[0]
    fw_type = comp_fw[1]
    protocol_ver = comp_fw[2]
    print(
        f"    parsed (per app ODJ): component_key=0x{component_key:02X}"
        f"  fw_type=0x{fw_type:02X}"
        f"  protocol_ver=0x{protocol_ver:02X}"
    )
    if fw_type != ctx.expected_fw_type:
        from uds_local.client import MalformedResponseError
        raise MalformedResponseError(
            0x22,
            f"DID 0x0101 FIRMWARE_TYPE byte 0x{fw_type:02X} != expected"
            f" 0x{ctx.expected_fw_type:02X}. Bootloader handover"
            " did not complete).",
        )
    ctx.protocol_ver = protocol_ver


def step_security_access(sess: "UdsSession", ctx: FlashContext) -> None:
    """SecurityAccess with the seed level chosen from protocol_ver + idx."""
    idx = ctx.security_level
    base_level = _SECURITY_SEED_LEVEL.get(idx, idx * 2 + 1)
    if idx == 0 and ctx.protocol_ver is not None and ctx.protocol_ver < 3:
        seed_level = 0x01  # legacy-protocol override
    else:
        seed_level = base_level
    print(
        f"  Step: SecurityAccess (idx={idx} protocol_ver={ctx.protocol_ver}"
        f" seed_level=0x{seed_level:02X})"
    )
    sess.security_access(level_idx=idx, seed_level=seed_level)


# ---------------------------------------------------------------------------
# Erase / transfer / verify
# ---------------------------------------------------------------------------

def step_module_to_program(sess: "UdsSession", ctx: FlashContext) -> None:
    """Set extended timeout then select CPU/flash region (WDBI 0x0102).

    netSetTimeout must precede moduleToProgram per the flash protocol.
    DID 0x0102 is bootloader-only and not present in every ECU's bootloader;
    a NRC here is logged as a warning and the flash continues so the erase
    step can surface the real failure if one exists.
    """
    from uds_local.client import UdsError
    print(f"  Step: moduleToProgram (module=0x{ctx.module_byte:02X})")
    sess.set_timeout(ctx.erase_timeout)
    try:
        sess.module_to_program(ctx.module_byte)
    except UdsError as e:
        print(
            f"    WARNING: moduleToProgram NRC 0x{e.nrc:02X} ({e.nrc_name})"
            " — DID 0x0102 not supported by this ECU, continuing"
        )


def step_erase(sess: "UdsSession", ctx: FlashContext) -> None:
    """RC 0xFF00 initializeEraseModule.

    Wire frame is exactly `31 01 FF 00` — 4 bytes, no data after the routine
    ID. Verified against `uds_initialize_erase_module` (`0x00409a7f`) which
    calls the RC sender with `data_len=0, data_buf=0`, and the sender's
    header struct (`local_10 = 4` header length, no data appended) at
    `FUN_00430af9`. (Cross-checked against WDBI's sender `FUN_004309a5`
    which uses `local_10 = 3` header + N bytes of data — same convention.)

    Earlier versions of this tool sent a trailing `0x01` byte; that came
    from a doc misread, not the binary. The protocol_ver=5 PCS bootloader
    rejects the 5-byte form with NRC 0x31; older bootloaders may have been
    lenient.
    """
    print(f"  Step: RC 0xFF00 initializeEraseModule (P2={ctx.erase_timeout}s)")
    sess.start_tester_present()
    try:
        sess.routine_control(_RC_ERASE)
    finally:
        sess.set_timeout(3.0)
        sess.stop_tester_present()


def step_transfer_loop(sess: "UdsSession", ctx: FlashContext) -> None:
    """RequestDownload + TransferData + RequestTransferExit for each SHDR."""
    for seg_idx, seg in enumerate(ctx.bhx_file.segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        print(f"    RequestDownload → maxBlockLen={max_block_len}")
        chunk_size = max_block_len - 2
        print(
            f"    TransferData ({seg.length} bytes, {chunk_size}-byte chunks)"
        )
        sess.transfer_data(seg.data, max_block_len)
        print("    RequestTransferExit")
        sess.request_transfer_exit()


def step_transfer_loop_inter_shdr(sess: "UdsSession", ctx: FlashContext) -> None:
    """Transfer loop with inter-SHDR re-auth + re-erase (GHDR v2 ramAppPayload)."""
    segments = ctx.bhx_file.segments
    for seg_idx, seg in enumerate(segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        print(f"    RequestDownload → maxBlockLen={max_block_len}")
        sess.transfer_data(seg.data, max_block_len)
        print("    RequestTransferExit")
        sess.request_transfer_exit()

        is_last = seg_idx == len(segments) - 1
        if not is_last:
            print("    [inter-SHDR] RC 0x0201 checkModuleProgrammedCorrectly")
            sess.routine_control(_RC_VERIFY_CRC)
            print("    [inter-SHDR] RC 0x0202 checkCorrectComponentAndRev")
            sess.routine_control(_RC_CHECK_REV)
            print("    [inter-SHDR] DiagnosticSessionControl(PROGRAMMING)")
            sess.diagnostic_session(0x02)
            print("    [inter-SHDR] SecurityAccess")
            sess.security_access(ctx.security_level)
            print("    [inter-SHDR] moduleToProgram")
            sess.diagnostic_session(0x02)
            print("    [inter-SHDR] RC 0xFF00 initializeEraseModule")
            sess.routine_control(_RC_ERASE, b"\x01")


def step_verify_crc(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: RC 0x0201 checkModuleProgrammedCorrectly")
    sess.routine_control(_RC_VERIFY_CRC)


def step_check_rev(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: RC 0x0202 checkCorrectComponentAndRev")
    sess.routine_control(_RC_CHECK_REV)


# ---------------------------------------------------------------------------
# Sleeps
# ---------------------------------------------------------------------------

def step_sleep_100ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.1)


def step_sleep_300ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.3)


def step_sleep_500ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.5)


def step_sleep_1000ms(sess: "UdsSession", ctx: FlashContext) -> None:
    """Wait 1s — used at the start of SCRIPT_BL to let the bu update agent boot."""
    print("  Step: sleep 1000ms (waiting for bootloader-update agent)")
    time.sleep(1.0)


def step_sleep_5000ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(5.0)


# ---------------------------------------------------------------------------
# Flash-count gating + DTC clear
# ---------------------------------------------------------------------------

def step_check_flash_count_0(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[0])


def step_check_flash_count_1(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[1])


def step_check_flash_count_2(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[2])


def step_clear_dtc(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: ClearDiagnosticInformation")
    sess.clear_dtc()


# ---------------------------------------------------------------------------
# Vendor preflights / OTA-mode prep
# ---------------------------------------------------------------------------

def step_vendor_preflight(sess: "UdsSession", ctx: FlashContext) -> None:
    """RC 0x0601 — vcleft/vcleftramapp vendor pre-flash routine."""
    print("  Step: routineControl 0x0601 (vendor pre-flash)")
    sess.vendor_preflight_routine()


def step_vcright_ota_prep(sess: "UdsSession", ctx: FlashContext) -> None:
    """sub4 (`0x006513a0`) — VCRIGHT-side OTA prep before flashing VCFRONT bu.

    Opens a transient UDS session to VCRIGHT (CAN IDs 0x608/0x609) on the
    same physical CAN channel as the parent VCFRONT session, runs:

        diagnosticSession(3)            extended session
        setSecurityAccessLevel(3)       internal — sets the override gate
        securityAccess(0)               seed level 0x05 (the override-protected path)
        VCWaitForOTAMode(0)             RC 0x540 polling, response[0] must == 2
        vcFrontLockoutIOControl(1)      IOCBI 0x218 controlParam=3, byte 1
        restoreUdsContext(0)            (handled by closing the session)

    then closes the VCRIGHT session.

    **Operational prerequisite:** VCWaitForOTAMode requires the vehicle to be
    actively in OTA state — initiated by the vehicle's overall state machine,
    not by this tool. On a bench setup this will time out.
    """
    if ctx.channel is None:
        raise RuntimeError(
            "step_vcright_ota_prep needs a CAN channel on FlashContext. "
            "Pass it via FlashScript.run(channel=..., interface=...)."
        )

    # Local imports to avoid pulling these into the module-level dependency
    # graph (keeps unit tests light).
    import config as _cfg
    from uds_local.client import UdsSession
    from uds_local.node_config import load_node_config

    print("  Step: VCRIGHT-side OTA prep (sub4) — opening transient session")
    vcright_cfg = load_node_config(
        "vcright", _cfg.NODES_JSON, _cfg.ETH_COMPACT, _cfg.ODJ_DIR
    )

    interface = ctx.interface or "socketcan"
    with UdsSession(vcright_cfg, ctx.channel, interface=interface) as vsess:
        print("    [VCRIGHT] DiagnosticSessionControl(EXTENDED 0x03)")
        vsess.diagnostic_session(0x03)
        # setSecurityAccessLevel(3) is internal: it sets context+0x02 = 3 so
        # the protocol_ver < 3 override does NOT fire when securityAccess(0)
        # runs. We replicate that by passing seed_level=0x05 explicitly — the
        # table value at idx 0 — bypassing the protocol_ver branch altogether.
        print("    [VCRIGHT] SecurityAccess (idx=0, forced seed_level=0x05)")
        vsess.security_access(level_idx=0, seed_level=0x05)
        print("    [VCRIGHT] VCWaitForOTAMode (RC 0x0540 poll for response[0]==2)")
        vsess.wait_for_ota_mode()
        print("    [VCRIGHT] IOCBI 0x0218 shortTermAdjustment, control byte 0x01")
        vsess.io_control_short_term_adjustment(0x0218, 1)
        print("    [VCRIGHT] closing transient session (restoreUdsContext)")
