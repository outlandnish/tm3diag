"""Step functions — atoms composed into FlashScript instances.

Each step is `(sess, ctx) -> None`. Steps mutate `ctx` to pass state forward
(e.g. `step_verify_comp_fw` stashes `protocol_ver`) and call methods on `sess`
to drive the underlying UDS transport.
"""

from __future__ import annotations

import logging
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
from ._display import _bar

if TYPE_CHECKING:
    from uds_local.client import UdsSession

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reset / bootloader handover
# ---------------------------------------------------------------------------

def step_ecu_reset(sess: UdsSession, ctx: FlashContext) -> None:
    """ECUReset `11 81` (subfunction 0x01 with SPR set) — fire-and-forget."""
    ctx.display.set_detail("ECU Reset (11 81)")
    sess.ecu_reset_no_wait(0x01)


def step_wait_for_bootloader(sess: UdsSession, ctx: FlashContext) -> None:
    """Wait for the bootloader to take over after a reset."""
    ctx.display.set_detail("Bootloader handover...")
    sess.wait_for_bootloader()
    ctx.display.set_detail("Bootloader ready")


def step_probe_bootloader_state(sess: UdsSession, ctx: FlashContext) -> None:
    """[DIAG] Read 0xF180 and 0xF181 to determine bootloader vs application state."""
    ctx.display.set_detail("[DIAG] Probing bootloader state...")
    label = "unknown"
    try:
        f180 = sess.read_did(0xF180)
        if len(f180) >= 9:
            firmware_type = f180[8]
            label = (
                "BOOTLOADER (fw_type=0)" if firmware_type == 0
                else f"APP (fw_type=0x{firmware_type:02X})"
            )
    except Exception as e:
        label = f"0xF180 error: {type(e).__name__}"
    try:
        sess.read_did(0xF181)
        ctx.display.set_detail(f"[DIAG] {label}  0xF181: present (app)")
    except Exception:
        ctx.display.set_detail(f"[DIAG] {label}  0xF181: NRC (bootloader)")


def step_hard_reset(sess: UdsSession, ctx: FlashContext) -> None:
    """ECUReset hard reset, wait for response."""
    ctx.display.set_detail("Hard reset (11 01)...")
    sess.ecu_reset(0x01)


def step_hard_reset_with_retries(sess: UdsSession, ctx: FlashContext) -> None:
    """ECUReset hard reset with 3 retries + 10 s delay each."""
    for attempt in range(3):
        ctx.display.set_detail(f"Hard reset (attempt {attempt + 1}/3)...")
        try:
            sess.ecu_reset(0x01)
            return
        except Exception:
            if attempt < 2:
                ctx.display.set_detail(
                    f"Hard reset attempt {attempt + 1} failed — retrying in 10s"
                )
                time.sleep(10)
    ctx.display.set_detail("Hard reset (final attempt)...")
    sess.ecu_reset(0x01)


# ---------------------------------------------------------------------------
# Identification / session / auth
# ---------------------------------------------------------------------------

def step_board_info(sess: UdsSession, ctx: FlashContext) -> None:
    """Read board part/serial DIDs — logged only, failure does not abort."""
    ctx.display.set_detail("Reading board info...")
    parts = []
    for did in _BOARD_INFO_DIDS:
        try:
            data = sess.read_did(did)
            val = data.decode("ascii", errors="replace").rstrip("\x00").strip()
            if val:
                parts.append(f"0x{did:04X}: {val!r}")
        except Exception:
            pass
    # Some ECUs return NRC 0x13 for multi-frame DIDs — drain stale frames.
    sess.drain_rx()
    ctx.display.set_detail(
        "Board info: " + ("  ".join(parts) if parts else "no data")
    )


def step_start_tester_present(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("TesterPresent keepalive: start")
    sess.start_tester_present()


def step_stop_tester_present(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("TesterPresent keepalive: stop")
    sess.stop_tester_present()


def step_programming_session(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)


def step_verify_comp_fw(sess: UdsSession, ctx: FlashContext) -> None:
    """RDBI 0x0101 — parse and stash protocol_ver on the context."""
    ctx.display.set_detail("Verifying component / FW type (DID 0x0101)...")
    comp_fw = sess.read_did(0x0101)
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
    if fw_type != ctx.expected_fw_type:
        from uds_local.client import MalformedResponseError
        raise MalformedResponseError(
            0x22,
            f"DID 0x0101 FIRMWARE_TYPE 0x{fw_type:02X} != expected"
            f" 0x{ctx.expected_fw_type:02X}",
        )
    ctx.protocol_ver = protocol_ver
    ctx.display.set_detail(
        f"Component: key=0x{component_key:02X}"
        f"  fw_type=0x{fw_type:02X}"
        f"  protocol_ver=0x{protocol_ver:02X}"
    )


def step_halt_if_running_boot_updater(sess: UdsSession, ctx: FlashContext) -> None:
    """`haltIfRunningBootUpdater` guard — the bu script's leading opcode.

    Decoded from update-2020.img: the bu script (0x40035EF2) opens with VM
    opcode 0x29 = haltIfRunningBootUpdater (device handler 0x400067DE). It reads
    DID 0x0101 and, if fw_type byte == 0x02 (BOOTLOADER), sets a per-node halt
    bit so the flow stops — i.e. "don't re-flash the bu agent over an ECU that's
    already running a bootloader image." On the host side this is a pre-flight
    guard: abort the bu flash if the ECU already reports fw_type 2 (a malformed
    or short response is treated as "not a bootloader image" and lets the flash
    proceed, matching the device handler's `rc == 0 && len == 3` gate).
    """
    ctx.display.set_detail("Guard: haltIfRunningBootUpdater (DID 0x0101)...")
    try:
        comp_fw = sess.read_did(0x0101)
    except Exception:
        return
    if len(comp_fw) == 3 and comp_fw[1] == 0x02:
        from uds_local.client import MalformedResponseError
        raise MalformedResponseError(
            0x22,
            "haltIfRunningBootUpdater: ECU already reports fw_type 0x02 "
            "(bootloader image) — halting bu flash. Re-flash the application "
            "first if this ECU is stuck running a bootloader/updater image.",
        )


def step_security_access(sess: UdsSession, ctx: FlashContext) -> None:
    """SecurityAccess with seed level chosen from protocol_ver + idx."""
    idx = ctx.security_level
    base_level = _SECURITY_SEED_LEVEL.get(idx, idx * 2 + 1)
    if idx == 0 and ctx.protocol_ver is not None and ctx.protocol_ver < 3:
        seed_level = 0x01
    else:
        seed_level = base_level
    ctx.display.set_detail(
        f"SecurityAccess  idx={idx}  seed=0x{seed_level:02X}"
        + (f"  proto={ctx.protocol_ver}" if ctx.protocol_ver is not None else "")
    )
    sess.security_access(level_idx=idx, seed_level=seed_level)


# ---------------------------------------------------------------------------
# Erase / transfer / verify
# ---------------------------------------------------------------------------

def step_module_to_program(sess: UdsSession, ctx: FlashContext) -> None:
    """Set extended timeout then select CPU/flash region (WDBI 0x0102)."""
    from uds_local.client import UdsError
    ctx.display.set_detail(f"Module selection (WDBI 0x0102 = 0x{ctx.module_byte:02X})")
    sess.set_timeout(ctx.erase_timeout)
    try:
        sess.module_to_program(ctx.module_byte)
    except UdsError as e:
        if ctx.fallback_module_byte is not None and e.nrc in (0x10, 0x31):
            ctx.display.set_detail(
                f"Module selection: NRC 0x{e.nrc:02X} — retrying with"
                f" fallback 0x{ctx.fallback_module_byte:02X}"
            )
            try:
                sess.module_to_program(ctx.fallback_module_byte)
                ctx.module_byte = ctx.fallback_module_byte
            except UdsError as e2:
                ctx.display.set_detail(
                    f"Module selection: fallback also failed NRC 0x{e2.nrc:02X} — continuing"
                )
        else:
            ctx.display.set_detail(
                f"Module selection: NRC 0x{e.nrc:02X} ({e.nrc_name}) — not supported, continuing"
            )


def step_erase(sess: UdsSession, ctx: FlashContext) -> None:
    """RC 0xFF00 initializeEraseModule."""
    ctx.display.set_detail(f"Erasing flash (RC 0xFF00  timeout={ctx.erase_timeout:.0f}s)...")
    sess.start_tester_present()
    try:
        sess.routine_control(_RC_ERASE)
    finally:
        sess.set_timeout(3.0)
        sess.stop_tester_present()
    ctx.display.set_detail("Erase complete")


def _request_download_with_module_fallback(
    sess: UdsSession, ctx: FlashContext, address: int, size: int
) -> int:
    """RequestDownload, retrying with the fallback module byte on NRC 0x31/0x22.

    A bootloader can *accept* moduleToProgram with the wrong secondary-select
    byte but then reject RequestDownload at the target address — with either NRC
    0x31 (requestOutOfRange) or NRC 0x22 (conditionsNotCorrect, observed on a
    12804 PMR bootloader when the module byte and the 0x82000 secondary-region
    address disagree) — so step_module_to_program's own NRC fallback never fires.
    When that happens, re-select the module with the fallback byte, re-erase
    (erase is module-scoped, so the new selection needs a fresh erase), and retry
    the download. Only attempted once, and only for the first segment.
    """
    from uds_local.client import UdsError
    try:
        return sess.request_download(address, size)
    except UdsError as e:
        if (
            e.nrc not in (0x31, 0x22)
            or ctx.fallback_module_byte is None
            or ctx.module_byte == ctx.fallback_module_byte
        ):
            raise
        ctx.display.set_detail(
            f"RequestDownload: NRC 0x{e.nrc:02X} ({e.nrc_name}) — "
            f"re-selecting module 0x{ctx.module_byte:02X} -> "
            f"fallback 0x{ctx.fallback_module_byte:02X} and retrying"
        )
        sess.module_to_program(ctx.fallback_module_byte)
        ctx.module_byte = ctx.fallback_module_byte
        # Erase is module-scoped — the freshly selected module needs its own erase.
        sess.start_tester_present()
        try:
            sess.set_timeout(ctx.erase_timeout)
            sess.routine_control(_RC_ERASE)
        finally:
            sess.set_timeout(3.0)
            sess.stop_tester_present()
        return sess.request_download(address, size)


def step_transfer_loop(sess: UdsSession, ctx: FlashContext) -> None:
    """RequestDownload + TransferData + RequestTransferExit for each SHDR."""
    display = ctx.display
    segments = ctx.bhx_file.segments
    n_segs = len(segments)
    total_bytes = sum(s.length for s in segments)
    bytes_done = 0

    for seg_idx, seg in enumerate(segments):
        seg_label = f"SHDR {seg_idx + 1}/{n_segs}"
        display.set_detail(f"Requesting download: {seg_label}  addr=0x{seg.start_address:08X}")
        # First segment may hit NRC 0x31 from a wrong (older) module byte that the
        # newer firmware silently accepted at moduleToProgram — retry with fallback.
        if seg_idx == 0:
            max_block_len = _request_download_with_module_fallback(
                sess, ctx, seg.start_address, seg.length
            )
        else:
            max_block_len = sess.request_download(seg.start_address, seg.length)

        seg_start = bytes_done

        def on_progress(
            sent: int, _: int,
            _start: int = seg_start,
            _label: str = seg_label,
            _addr: int = seg.start_address,
        ) -> None:
            display.set_detail(
                f"Transfer {_bar(_start + sent, total_bytes)}"
                f"  {_label}  addr=0x{_addr:08X}"
            )

        on_progress(0, seg.length)
        sess.transfer_data(seg.data, max_block_len, progress_cb=on_progress)
        bytes_done += seg.length

        display.set_detail(f"RequestTransferExit: {seg_label}")
        sess.request_transfer_exit()

    display.set_detail(f"Transfer complete  {_bar(total_bytes, total_bytes)}")


def step_transfer_loop_inter_shdr(sess: UdsSession, ctx: FlashContext) -> None:
    """Transfer loop with inter-SHDR re-auth + re-erase (GHDR v2 ramAppPayload)."""
    display = ctx.display
    segments = ctx.bhx_file.segments
    n_segs = len(segments)
    total_bytes = sum(s.length for s in segments)
    bytes_done = 0

    for seg_idx, seg in enumerate(segments):
        seg_label = f"SHDR {seg_idx + 1}/{n_segs}"
        display.set_detail(f"Requesting download: {seg_label}  addr=0x{seg.start_address:08X}")
        max_block_len = sess.request_download(seg.start_address, seg.length)

        seg_start = bytes_done

        def on_progress(
            sent: int, _: int,
            _start: int = seg_start,
            _label: str = seg_label,
            _addr: int = seg.start_address,
        ) -> None:
            display.set_detail(
                f"Transfer {_bar(_start + sent, total_bytes)}"
                f"  {_label}  addr=0x{_addr:08X}"
            )

        on_progress(0, seg.length)
        sess.transfer_data(seg.data, max_block_len, progress_cb=on_progress)
        bytes_done += seg.length

        display.set_detail(f"RequestTransferExit: {seg_label}")
        sess.request_transfer_exit()

        is_last = seg_idx == len(segments) - 1
        if not is_last:
            display.set_detail("[inter-SHDR] CRC check...")
            sess.routine_control(_RC_VERIFY_CRC)
            display.set_detail("[inter-SHDR] Revision check...")
            sess.routine_control(_RC_CHECK_REV)
            display.set_detail("[inter-SHDR] Re-auth...")
            sess.diagnostic_session(0x02)
            sess.security_access(ctx.security_level)
            sess.diagnostic_session(0x02)
            display.set_detail("[inter-SHDR] Re-erase...")
            sess.routine_control(_RC_ERASE, b"\x01")

    display.set_detail(f"Transfer complete  {_bar(total_bytes, total_bytes)}")


def step_verify_crc(sess: UdsSession, ctx: FlashContext) -> None:
    """RC 0x0201 checkModuleProgrammedCorrectly.

    NOTE (validation in progress): the device replies 71 01 02 01 <status>, where
    status 0x00 = CRC MATCH and 0x04 = CRC MISMATCH (both are UDS *positive*
    responses, so routine_control() does NOT catch a mismatch). We READ and LOG the
    status here (non-fatal) so we can finally see whether verifyCRC actually passes
    on a real flash and what expected value the device uses. Whether the host must
    SUPPLY the expected CRC (in the RC arg) or the device derives it from the image
    is still UNCONFIRMED — this logging is how we find out. Do not treat a pass as
    given until the status byte is observed = 0x00.
    """
    ctx.display.set_detail("CRC check (RC 0x0201)...")
    resp = sess.routine_control(_RC_VERIFY_CRC)
    status = resp[0] if resp else None
    if status == 0x00:
        ctx.display.set_detail("CRC check: status=0x00 (MATCH)")
    else:
        # non-fatal: surface it loudly but don't abort the (still-being-validated) pipeline
        msg = (f"CRC check: status={f'0x{status:02X}' if status is not None else 'EMPTY'} "
               f"(0x04=MISMATCH; expected 0x00). raw={resp.hex() if resp else '<none>'}")
        ctx.display.set_detail(msg)
        _log.warning("step_verify_crc: %s", msg)


def step_check_rev(sess: UdsSession, ctx: FlashContext) -> None:
    """RC 0x0202 checkCorrectComponentAndRev.

    Device replies 71 01 02 02 <status>: 0x00 = OK; 1..4 = which header rev/part-id
    field mismatched (1=part/module-id, 2=byte-pair, 3=major-rev, 4=minor-rev), all
    positive responses. We READ + LOG the status (non-fatal) for the same reason as
    step_verify_crc — the response was never validated before.
    """
    ctx.display.set_detail("Revision check (RC 0x0202)...")
    resp = sess.routine_control(_RC_CHECK_REV)
    status = resp[0] if resp else None
    if status == 0x00:
        ctx.display.set_detail("Revision check: status=0x00 (OK)")
    else:
        msg = (f"Revision check: status={f'0x{status:02X}' if status is not None else 'EMPTY'} "
               f"(1-4=field mismatch; expected 0x00). raw={resp.hex() if resp else '<none>'}")
        ctx.display.set_detail(msg)
        _log.warning("step_check_rev: %s", msg)


# ---------------------------------------------------------------------------
# Sleeps
# ---------------------------------------------------------------------------

def step_sleep_100ms(_sess: UdsSession, _ctx: FlashContext) -> None:
    time.sleep(0.1)


def step_sleep_300ms(_sess: UdsSession, _ctx: FlashContext) -> None:
    time.sleep(0.3)


def step_sleep_500ms(_sess: UdsSession, _ctx: FlashContext) -> None:
    time.sleep(0.5)


def step_sleep_1000ms(_sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("Waiting 1s for bootloader-update agent...")
    time.sleep(1.0)


def step_sleep_5000ms(_sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("Waiting 5s post-reset...")
    time.sleep(5.0)


# ---------------------------------------------------------------------------
# Flash-count gating + DTC clear
# ---------------------------------------------------------------------------

def step_check_flash_count_0(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail(f"Flash count check (limit={FLASH_COUNT_LIMITS[0]})...")
    sess.check_flash_count(FLASH_COUNT_LIMITS[0])


def step_check_flash_count_1(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail(f"Flash count check (limit={FLASH_COUNT_LIMITS[1]})...")
    sess.check_flash_count(FLASH_COUNT_LIMITS[1])


def step_check_flash_count_2(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail(f"Flash count check (limit={FLASH_COUNT_LIMITS[2]})...")
    sess.check_flash_count(FLASH_COUNT_LIMITS[2])


def step_clear_dtc(sess: UdsSession, ctx: FlashContext) -> None:
    ctx.display.set_detail("ClearDiagnosticInformation...")
    sess.clear_dtc()


# ---------------------------------------------------------------------------
# Vendor preflights / OTA-mode prep
# ---------------------------------------------------------------------------

def step_vendor_preflight(sess: UdsSession, ctx: FlashContext) -> None:
    """RC 0x0601 — vcleft/vcleftramapp vendor pre-flash routine."""
    ctx.display.set_detail("Vendor pre-flash routine (RC 0x0601)...")
    sess.vendor_preflight_routine()
    ctx.display.set_detail("Vendor pre-flash routine: OK")


def step_vcright_ota_prep(sess: UdsSession, ctx: FlashContext) -> None:
    """sub4 — VCRIGHT-side OTA prep before flashing VCFRONT bu."""
    if ctx.channel is None:
        raise RuntimeError(
            "step_vcright_ota_prep needs a CAN channel on FlashContext. "
            "Pass it via FlashScript.run(channel=..., interface=...)."
        )

    import config as _cfg
    from uds_local.client import UdsSession
    from uds_local.node_config import load_node_config

    ctx.display.set_detail("VCRIGHT OTA prep: opening transient session...")
    vcright_cfg = load_node_config(
        "vcright", _cfg.NODES_JSON, _cfg.ETH_COMPACT, _cfg.ODJ_DIR
    )

    interface = ctx.interface or "socketcan"
    with UdsSession(vcright_cfg, ctx.channel, interface=interface) as vsess:
        ctx.display.set_detail("VCRIGHT OTA prep: DiagnosticSessionControl(EXTENDED)...")
        vsess.diagnostic_session(0x03)
        ctx.display.set_detail("VCRIGHT OTA prep: SecurityAccess...")
        vsess.security_access(level_idx=0, seed_level=0x05)
        ctx.display.set_detail("VCRIGHT OTA prep: VCWaitForOTAMode (RC 0x0540)...")
        vsess.wait_for_ota_mode()
        ctx.display.set_detail("VCRIGHT OTA prep: IOCBI 0x0218 lockout...")
        vsess.io_control_short_term_adjustment(0x0218, 1)
    ctx.display.set_detail("VCRIGHT OTA prep: complete")
