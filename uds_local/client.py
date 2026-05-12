"""UdsSession: wraps py-uds Client with TesterPresent background thread."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import can
from uds.addressing import AddressingType
from uds.can.addressing import CanAddressingFormat, NormalCanAddressingInformation
from uds.can.packet import CanPacket, CanPacketType
from uds.can.transport_interface import PyCanTransportInterface
from uds.message import UdsMessage

from .broadcast_config import broadcast_for
from .node_config import NodeConfig
from .security import compute_key

_log = logging.getLogger(__name__)

_SESSION_DEFAULT = 0x01
_SESSION_PROGRAMMING = 0x02
_SESSION_EXTENDED = 0x03
_SESSION_SAFETY = 0x04

_SID_DSC = 0x10  # DiagnosticSessionControl
_SID_SA = 0x27  # SecurityAccess
_SID_RDBI = 0x22  # ReadDataByIdentifier
_SID_WDBI = 0x2E  # WriteDataByIdentifier
_SID_RC = 0x31  # RoutineControl
_SID_RD = 0x34  # RequestDownload
_SID_TD = 0x36  # TransferData
_SID_RTE = 0x37  # RequestTransferExit
_SID_ER = 0x11  # ECUReset
_SID_TP = 0x3E  # TesterPresent
_SID_CDI = 0x14  # ClearDiagnosticInformation
_SID_IOCBI = 0x2F  # InputOutputControlByIdentifier

_RC_START = 0x01
_RC_REQUEST_RESULTS = 0x03

# DID 0x0102: moduleToProgram — selects CPU/flash region in bootloader
_DID_MODULE_TO_PROGRAM = 0x0102
# DID 0xF100: flash count — enforced per-ECU limit
_DID_FLASH_COUNT = 0xF100
# DID 0x0218: VCFRONT door-lock IOCBI used by sub4 lockout sequence
_DID_VCFRONT_LOCKOUT = 0x0218
# IOCBI controlParameter values (ISO 14229)
_IOCP_RETURN_TO_ECU          = 0x00
_IOCP_RESET_TO_DEFAULT       = 0x01
_IOCP_FREEZE_CURRENT_STATE   = 0x02
_IOCP_SHORT_TERM_ADJUSTMENT  = 0x03
# RC 0x0601: vendor pre-flash routine (vcleft / vcleftramapp)
_RC_VENDOR_PREFLIGHT = 0x0601
# RC 0x0540: VCWaitForOTAMode — start, then poll until response[0] == 2
_RC_OTA_MODE = 0x0540


# ISO 14229-1 NRC names — the only NRCs hashpicker_sim's UDS stack recognizes
_NRC_NAMES: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x23: "ISOSAEReserved",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


def nrc_name(nrc: int) -> str:
    """Return the ISO 14229 name for an NRC byte, or 'unknown' if unrecognized."""
    return _NRC_NAMES.get(nrc, "unknown")


class UdsError(Exception):
    """Raised on negative UDS responses (`7F <SID> <NRC>` from the ECU)."""

    def __init__(self, sid: int, nrc: int):
        self.sid = sid
        self.nrc = nrc
        self.nrc_name = nrc_name(nrc)
        super().__init__(
            f"Negative response for SID 0x{sid:02X}: NRC 0x{nrc:02X} ({self.nrc_name})"
        )


class MalformedResponseError(UdsError):
    """Raised when the response is locally rejectable — not a wire NRC.

    Three distinct conditions roll up here:
      * no response received within the timeout (transport layer empty result)
      * response received but truncated below the expected length
      * response received and well-formed at the SID level, but a payload field
        doesn't carry the expected value (e.g. RC status byte, OTA-mode poll)

    Inherits from UdsError so callers that `except UdsError` keep catching it.
    `nrc` is None on this subclass to make clear there is no wire NRC byte.
    """

    def __init__(self, sid: int, detail: str):
        # Skip UdsError.__init__ so we don't fabricate an NRC.
        Exception.__init__(
            self, f"Malformed response for SID 0x{sid:02X}: {detail}"
        )
        self.sid = sid
        self.nrc = None
        self.nrc_name = None
        self.detail = detail


class _BroadcastWatcher(can.Listener):
    """Counts inbound frames matching a single broadcast (heartbeat) CAN ID.

    Mirrors update.img's `enter_bootloader_v0` (@ `0x40000732`), which
    snapshots a per-node counter at `0x400331a8`/`0x4003325c` and exits its
    keep-alive loop the instant the counter advances — i.e. the moment the
    target ECU resumes broadcasting after the reset, signalling that it has
    booted into the bootloader.

    For nodes whose firmware doesn't track a broadcast (HVBMS, ESP, all
    high-numbered nodes), `broadcast_config.broadcast_for(...)` returns
    `None` and we don't install one of these — `wait_for_bootloader` falls
    back to a fixed-budget wait, which is exactly what the firmware does
    for those same nodes.
    """

    def __init__(self, can_id: int) -> None:
        super().__init__()
        self._can_id = can_id
        self._count = 0

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame or msg.is_remote_frame:
            return
        if msg.arbitration_id == self._can_id:
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def can_id(self) -> int:
        return self._can_id

    def stop(self) -> None:
        pass


class FlashCountError(Exception):
    """Raised when the ECU's flash count is at or over its per-ECU limit."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(
            f"Flash count {count} at or over limit {limit}"
        )


class UdsSession:
    def __init__(
        self,
        node: NodeConfig,
        channel: str,
        interface: str = "socketcan",
    ):
        self._bus = can.Bus(interface=interface, channel=channel)
        # Pre-create one shared Notifier and hand it to the transport so there
        # is never more than one active Notifier on the bus. The transport's
        # notifier starts as None and gets set lazily on first send/receive —
        # if we let that happen a second Notifier would be created and python-can
        # would raise "A bus can not be added to multiple active Notifier
        # instances".
        self._frame_notifier = can.Notifier(self._bus, [])
        addressing = NormalCanAddressingInformation(
            rx_physical_params={"can_id": node.response_can_id},
            tx_physical_params={"can_id": node.request_can_id},
            rx_functional_params={"can_id": 0x7E8},
            tx_functional_params={"can_id": 0x7DF},
        )
        self._transport: PyCanTransportInterface = PyCanTransportInterface(
            network_manager=self._bus,
            addressing_information=addressing,
            notifier=self._frame_notifier,
        )
        self._node = node
        self._tp_stop = threading.Event()
        self._tp_thread: threading.Thread | None = None

        bcast = broadcast_for(node.name)

        # Install per-node broadcast watcher so wait_for_bootloader can
        # short-circuit Phase 1 the moment the ECU resumes broadcasting
        self._broadcast_watcher: _BroadcastWatcher | None = None
        if bcast is not None:
            self._broadcast_watcher = _BroadcastWatcher(bcast.can_id)
            self._install_listener(self._broadcast_watcher)

    def _install_listener(self, listener: can.Listener) -> None:
        self._frame_notifier.add_listener(listener)

    # ------------------------------------------------------------------
    # TesterPresent keep-alive
    # ------------------------------------------------------------------

    def start_tester_present(self) -> None:
        """Send TesterPresent (3E 80, suppress positive response) at 2 Hz."""
        self._tp_stop.clear()
        self._tp_thread = threading.Thread(target=self._tp_loop, daemon=True)
        self._tp_thread.start()

    def stop_tester_present(self) -> None:
        self._tp_stop.set()
        if self._tp_thread:
            self._tp_thread.join()
            self._tp_thread = None

    def _tp_loop(self) -> None:
        tp_packet = CanPacket(
            packet_type=CanPacketType.SINGLE_FRAME,
            addressing_format=CanAddressingFormat.NORMAL_ADDRESSING,
            addressing_type=AddressingType.PHYSICAL,
            can_id=self._node.request_can_id,
            payload=[_SID_TP, 0x80],
        )
        while not self._tp_stop.wait(0.5):
            self._transport.send_packet(tp_packet)

    # ------------------------------------------------------------------
    # UDS services
    # ------------------------------------------------------------------

    def diagnostic_session(self, mode: int = _SESSION_DEFAULT) -> None:
        resp = self._send_raw([_SID_DSC, mode])
        self._check_positive(resp, _SID_DSC)

    def security_access(
        self,
        level_idx: int = 0,
        seed_level: int | None = None,
    ) -> None:
        # Full seed → key exchange.
        algo = self._node.security_algorithm
        buf_size = self._node.security_buffer_size
        kw = self._node.security_kw

        if seed_level is None:
            seed_level = level_idx * 2 + 1
        seed_subfn = seed_level
        key_subfn = seed_subfn + 1

        resp = self._send_raw([_SID_SA, seed_subfn])
        # NRC 0x35 (requestSequenceError) = already unlocked
        if resp and resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x35:
            return
        self._check_positive(resp, _SID_SA)
        seed = bytes(resp[2:2 + buf_size])

        key = compute_key(algo, seed, kw)

        resp = self._send_raw([_SID_SA, key_subfn] + list(key))
        self._check_positive(resp, _SID_SA)

    def read_did(self, did_id: int) -> bytes:
        resp = self._send_raw(
            [_SID_RDBI, (did_id >> 8) & 0xFF, did_id & 0xFF]
        )
        self._check_positive(resp, _SID_RDBI)
        # Positive response: 62 <DID high> <DID low> <data...>
        return bytes(resp[3:])

    def write_did(self, did_id: int, data: bytes) -> None:
        payload = (
            [_SID_WDBI, (did_id >> 8) & 0xFF, did_id & 0xFF] + list(data)
        )
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_WDBI)

    def routine_control(self, routine_id: int, arg: bytes = b"", subtype: int = 0x01) -> bytes:
        payload = [
            _SID_RC, subtype,
            (routine_id >> 8) & 0xFF, routine_id & 0xFF,
        ] + list(arg)
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_RC)
        # Positive response: 71 01 <routine high> <routine low> <result...>
        return bytes(resp[4:])

    def module_to_program(self, module_byte: int) -> None:
        """WDBI DID 0x0102 — select CPU/flash region before erase+transfer."""
        self.write_did(_DID_MODULE_TO_PROGRAM, bytes([module_byte]))

    def set_timeout(self, p2_seconds: float) -> None:
        """Update the transport timeouts (seconds → milliseconds).

        Sets both N_Bs (inter-frame) and N_Cr (consecutive frame) timeouts.
        Call before long operations (erase, large transfers) and restore after.
        """
        p2_ms = p2_seconds * 1000
        self._transport.n_bs_timeout = p2_ms
        self._transport.n_cr_timeout = p2_ms

    def check_flash_count(self, limit: int) -> None:
        """Read DID 0xF100 and raise FlashCountError if count >= limit."""
        data = self.read_did(_DID_FLASH_COUNT)
        count = int.from_bytes(data[:4], "big")
        if count >= limit:
            raise FlashCountError(count, limit)

    def vendor_preflight_routine(self) -> None:
        """RC 0x0601 — vcleft/vcleftramapp pre-flash vendor routine.

        Starts the routine, then polls requestResults (subfunction 3) up to
        50 times (100 ms apart) until the response byte equals 1.
        """
        payload_start = [
            _SID_RC, _RC_START,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        resp = self._send_raw(payload_start)
        self._check_positive(resp, _SID_RC)

        payload_poll = [
            _SID_RC, _RC_REQUEST_RESULTS,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        for _ in range(50):
            time.sleep(0.1)
            resp = self._send_raw(payload_poll)
            self._check_positive(resp, _SID_RC)
            if len(resp) >= 5 and resp[4] == 0x01:
                return
        raise MalformedResponseError(
            _SID_RC,
            "vendor pre-flash routine never reported completion (resp[4] != 0x01) after 50 polls",
        )

    def wait_for_ota_mode(self, attempts: int = 5) -> None:
        """VCWaitForOTAMode — RC 0x0540 start, then poll until response byte == 2.
        """
        self.set_timeout(1.0)
        rid_hi = (_RC_OTA_MODE >> 8) & 0xFF
        rid_lo = _RC_OTA_MODE & 0xFF
        for _ in range(attempts):
            resp = self._send_raw([_SID_RC, _RC_START, rid_hi, rid_lo])
            if not resp or (resp and resp[0] == 0x7F):
                # Start failed; brief delay and retry the entire start+poll cycle
                if resp and len(resp) >= 3 and resp[2] == 0x05:
                    # NRC 0x05 — pass through, don't sleep
                    pass
                else:
                    time.sleep(1.0)
                continue
            time.sleep(0.1)
            resp = self._send_raw(
                [_SID_RC, _RC_REQUEST_RESULTS, rid_hi, rid_lo])
            self._check_positive(resp, _SID_RC)
            # response: 71 03 05 40 <result_count?> <result>
            if len(resp) >= 5 and resp[4] == 0x02:
                return
        raise MalformedResponseError(
            _SID_RC,
            f"OTA mode never reported active (resp[4] != 0x02) after {attempts} attempts",
        )

    def io_control(self, did: int, control_param: int, data: bytes = b"") -> bytes:
        """Generic IOCBI (0x2F) — caller supplies the controlParameter byte and data."""
        payload = [_SID_IOCBI, (did >> 8) & 0xFF, did & 0xFF, control_param] + list(data)
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_IOCBI)
        return bytes(resp[4:]) if len(resp) > 4 else b""

    def io_control_short_term_adjustment(self, did: int, control_byte: int) -> None:
        """IOCBI (0x2F) with controlParameter=3 (shortTermAdjustment) and 1-byte data.

        Used by `vcFrontLockoutIOControl` opcode against DID 0x0218.
        """
        payload = [
            _SID_IOCBI,
            (did >> 8) & 0xFF, did & 0xFF,
            _IOCP_SHORT_TERM_ADJUSTMENT,
            control_byte & 0xFF,
        ]
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_IOCBI)

    def request_download(self, address: int, size: int) -> int:
        """Returns maxBlockLen (number of bytes including sequence counter)."""
        # Data format: 0x00 (no compression/encryption)
        # Address and length format: 0x44 (4-byte address, 4-byte size)
        addr_bytes = address.to_bytes(4, "big")
        size_bytes = size.to_bytes(4, "big")
        payload = [_SID_RD, 0x00, 0x44] + list(addr_bytes) + list(size_bytes)
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_RD)
        # Positive response: 74 <length_format> <maxBlockLen...>
        # length_format nibble high = number of bytes for maxBlockLen
        length_format = resp[1]
        max_block_len_size = (length_format >> 4) & 0xF
        max_block_len = int.from_bytes(resp[2:2 + max_block_len_size], "big")
        return min(max_block_len, 512)

    def transfer_data(
        self,
        payload: bytes,
        max_block_len: int,
        progress_cb=None,
    ) -> None:
        """Transfer payload in chunks.

        max_block_len includes the 1-byte sequence counter.
        progress_cb(bytes_sent, total_bytes) is called after each chunk.

        bus.send() is patched for the duration to retry on ENOBUFS (errno 105).
        select()-based timeouts don't help here — select checks the socket send
        buffer, not the kernel CAN TX ring (txqueuelen). On a 500kbps bus each
        frame is ~0.26ms, so a 1ms sleep drains ~4 slots from a depth-10 queue.
        """
        chunk_size = max_block_len - 2  # subtract SID + seq bytes
        seq = 0x01
        offset = 0
        total = len(payload)

        orig_send = self._bus.send

        def _send_with_retry(msg, **_):
            for _ in range(50):
                try:
                    return orig_send(msg)
                except can.CanOperationError as exc:
                    if getattr(exc, "error_code", None) != 105:
                        raise
                    time.sleep(0.001)
            raise can.CanOperationError(
                "TX queue persistently full after retries", 105)

        self._bus.send = _send_with_retry
        try:
            while offset < total:
                chunk = payload[offset:offset + chunk_size]
                resp = self._send_raw([_SID_TD, seq] + list(chunk))
                self._check_positive(resp, _SID_TD)
                offset += len(chunk)
                seq = 0x00 if seq == 0xFF else seq + 1  # wrap 0xFF → 0x00
                if progress_cb is not None:
                    progress_cb(offset, total)
        finally:
            self._bus.send = orig_send

    def request_transfer_exit(self) -> None:
        resp = self._send_raw([_SID_RTE])
        self._check_positive(resp, _SID_RTE)

    def ecu_reset(self, reset_type: int = 0x01) -> None:
        resp = self._send_raw([_SID_ER, reset_type])
        self._check_positive(resp, _SID_ER)

    def ecu_reset_no_wait(self, reset_type: int = 0x01) -> None:
        """Send ECUReset with `suppressPositiveResponse` set — fire-and-forget.

        Frame is `11 (reset_type | 0x80)`
        """
        msg = UdsMessage(
            payload=bytearray([_SID_ER, reset_type | 0x80]),
            addressing_type=AddressingType.PHYSICAL,
        )
        self._transport.send_message(msg)

    def _send_tp_no_wait(self) -> None:
        """Fire-and-forget TesterPresent (`3E 80`) — keep-alive, no response expected.
        """
        msg = UdsMessage(
            payload=bytearray([_SID_TP, 0x80]),
            addressing_type=AddressingType.PHYSICAL,
        )
        self._transport.send_message(msg)

    def wait_for_bootloader(
        self,
        keepalive_phase_s: float = 3.34,
        keepalive_interval_s: float = 0.01,
        confirm_p2_ms: int = 40,
        confirm_max_attempts: int = 14,
    ) -> None:
        """Two-phase bootloader handover wait, mirroring update.img's
        `enter_bootloader_v0` @ 0x40000732.

        **Phase 1 — keep-alive** (`keepalive_phase_s` s): send `3E 80`
        (TesterPresent fire-and-forget) at `keepalive_interval_s` cadence
        (10 ms by default — 334 × 10 ms = 3.34 s, matching the binary's loop).
        Exits early as soon as the per-node boot-broadcast counter advances. If a
        `_BroadcastWatcher` was installed for this node (see
        `broadcast_config.NODE_BROADCAST_CONFIG`), we replicate that early-exit
        by snapshotting its count and breaking the loop on advance. For nodes
        without a broadcast tracker (HVBMS, ESP, TPMS, etc.), Phase 1 burns
        the full budget — same as the firmware does for those.

        **Phase 2 — TP-with-response confirmation**: send `3E 00` and wait
        for `7E 00`, with **P2 timeout = `confirm_p2_ms` (40 ms by default, and back-to-back
        retries up to `confirm_max_attempts` (14 by default).

        On success, returns silently. On total failure, raises `TimeoutError`
        with a diagnostic that includes phase 1 frame count and phase 2 NRCs.
        """
        # Phase 1: fire-and-forget keep-alive
        phase1_start = time.monotonic()
        end_phase1 = phase1_start + keepalive_phase_s
        keepalive_count = 0
        bus_errors = 0
        # Snapshot the broadcast counter (if a watcher is installed) so we
        # can short-circuit Phase 1 the moment a new heartbeat arrives.
        watcher = self._broadcast_watcher
        baseline = watcher.count if watcher is not None else None
        early_exit_ms: float | None = None
        while time.monotonic() < end_phase1:
            try:
                self._send_tp_no_wait()
                keepalive_count += 1
            except Exception as exc:
                bus_errors += 1
                if bus_errors >= 5:
                    # The binary aborts on the first send error; we tolerate
                    # a few transients (bus may glitch right after reset)
                    # but bail if it's persistent — that signals the bus is
                    # down, not the ECU rebooting.
                    raise RuntimeError(
                        f"Phase 1: {bus_errors} consecutive bus send errors — "
                        "bus is probably down; abandoning bootloader handover"
                    ) from exc
            # Early exit on broadcast counter advance (mirrors the binary)
            if watcher is not None and watcher.count != baseline:
                early_exit_ms = (time.monotonic() - phase1_start) * 1000
                break
            time.sleep(keepalive_interval_s)
        if early_exit_ms is not None:
            _log.debug(
                "Phase 1: broadcast 0x%03X advanced after %.0f ms"
                " (%d keep-alive 3E 80 frames sent before exit)",
                watcher.can_id, early_exit_ms, keepalive_count,
            )
        elif baseline is not None:
            _log.debug(
                "Phase 1: %d keep-alive 3E 80 frames sent over %.2fs"
                " — broadcast 0x%03X never advanced (target may not have rebooted)",
                keepalive_count, keepalive_phase_s, watcher.can_id,
            )
        else:
            _log.debug(
                "Phase 1: %d keep-alive 3E 80 frames sent over %.2fs"
                " — no broadcast tracker for this node, fixed wait",
                keepalive_count, keepalive_phase_s,
            )
        # Inter-phase sleep
        time.sleep(0.010)

        # Phase 2: 14 fast 3E 00 probes (P2 = 40 ms) back-to-back.
        nrc_count = 0
        first_nrc: int | None = None
        for _attempt in range(1, confirm_max_attempts + 1):
            resp = self._send_raw([_SID_TP, 0x00], timeout_ms=confirm_p2_ms)
            if resp and resp[0] == 0x7E:
                return
            if resp and resp[0] == 0x7F:
                nrc_count += 1
                if first_nrc is None and len(resp) >= 3:
                    first_nrc = resp[2]
            # No inter-attempt sleep — back-to-back like the binary
        diag = (
            f"phase 1 sent {keepalive_count} keep-alive frames; phase 2 sent"
            f" {confirm_max_attempts} TesterPresent probes (P2={confirm_p2_ms} ms),"
            " no positive response"
        )
        if nrc_count:
            diag += (
                f" ({nrc_count} negative responses"
                + (f", first NRC 0x{first_nrc:02X}" if first_nrc is not None else "")
                + ")"
            )
        raise TimeoutError(f"Bootloader handover did not complete — {diag}")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def drain_rx(self, timeout_ms: float = 20) -> None:
        """Discard any frames queued in the transport receive buffer.

        Call after non-fatal read steps that may leave stale NRC or partial
        ISO-TP frames in the buffer (e.g. step_board_info on ECUs that return
        NRC 0x13 for multi-frame DIDs instead of sending Consecutive Frames).
        """
        while True:
            try:
                self._transport.receive_message(
                    start_timeout=timeout_ms, end_timeout=timeout_ms
                )
            except Exception:
                break

    def clear_dtc(self, group: int = 0xFFFFFF) -> None:
        """ClearDiagnosticInformation (0x14) — group 0xFFFFFF clears all."""
        b = group.to_bytes(3, "big")
        resp = self._send_raw([_SID_CDI, b[0], b[1], b[2]])
        self._check_positive(resp, _SID_CDI)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_raw(self, payload: list[int], timeout_ms: float = 2000) -> list[int]:
        msg = UdsMessage(
            payload=bytearray(payload),
            addressing_type=AddressingType.PHYSICAL,
        )
        try:
            self._transport.send_message(msg)
        except Exception as exc:
            err_str = str(exc)
            if "105" in err_str or "buffer" in err_str.lower():
                raise RuntimeError(
                    "TX queue full (ENOBUFS) — run: "
                    f"sudo ip link set {getattr(self._bus, 'channel', '?')} "
                    "txqueuelen 1000"
                ) from exc
            raise
        expected_sid = payload[0]
        positive_sid = expected_sid + 0x40
        deadline_ms = timeout_ms
        while True:
            try:
                record = self._transport.receive_message(
                    start_timeout=deadline_ms, end_timeout=deadline_ms
                )
            except Exception:
                return []
            resp = list(record.payload)
            if not resp:
                return resp
            # 0x78 = requestCorrectlyReceivedResponsePending — ECU still working
            if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
                deadline_ms = timeout_ms
                continue
            # Skip stale frames left over from a previous request (e.g. an aborted
            # multi-frame read). A well-formed response is either a positive response
            # (SID + 0x40) or a negative response (7F <our-SID> <NRC>).
            if resp[0] == 0x7F:
                if len(resp) < 2 or resp[1] != expected_sid:
                    _log.debug(
                        "[stale] discarding NRC frame for SID 0x%02X"
                        " while waiting for 0x%02X",
                        resp[1] if len(resp) >= 2 else 0xFF,
                        expected_sid,
                    )
                    continue
            elif resp[0] != positive_sid:
                _log.debug(
                    "[stale] discarding positive response 0x%02X"
                    " while waiting for 0x%02X",
                    resp[0],
                    positive_sid,
                )
                continue
            return resp

    @staticmethod
    def _check_positive(resp: list[int], expected_sid: int) -> None:
        if not resp:
            raise MalformedResponseError(expected_sid, "no response received")
        if resp[0] == 0x7F:
            if len(resp) < 3:
                raise MalformedResponseError(
                    expected_sid,
                    f"truncated negative response (got {len(resp)} bytes, "
                    "expected at least 3 for `7F <SID> <NRC>`)",
                )
            raise UdsError(expected_sid, resp[2])

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> UdsSession:
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_tester_present()
        with contextlib.suppress(Exception):
            self._frame_notifier.stop()
        with contextlib.suppress(Exception):
            self._bus.shutdown()
