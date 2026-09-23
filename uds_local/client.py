"""UdsSession: wraps py-uds Client with TesterPresent background thread."""

from __future__ import annotations

import collections
import contextlib
import logging
import threading
import time
import warnings
from typing import Any

import can
from uds.addressing import AddressingType
from uds.can.addressing import CanAddressingFormat, NormalCanAddressingInformation
from uds.can.packet import CanPacket, CanPacketType
from uds.can.transport_interface import PyCanTransportInterface
from uds.message import UdsMessage

from .broadcast_config import broadcast_for
from .node_config import NodeConfig
from .security_provider import compute_key

# Silence two routine py-uds warnings (notifier timeout adjust; non-UDS frames).
warnings.filterwarnings("ignore", message="Notifier's timeout value was changed",
                        module=r"uds\..*")
warnings.filterwarnings("ignore", category=RuntimeWarning,
                        message="A CAN packet that does not start UDS message",
                        module=r"uds\..*")

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

# Keep-alive gating: a 3E 80 mid ISO-TP transfer can make the ECU abort it.
_N_CR_S = 1.0          # a multi-frame reply silent this long is dead
_REPLY_GUARD_S = 0.05  # P2server: after a request, give the reply this long to start
_TP_POLL_S = 0.02      # how soon a changed keep-alive interval takes effect
_TP_GAP_WARN_S = 0.3   # log a keep-alive gap this much longer than the interval
_TP_HEARTBEAT_S = 2.0  # log the keep-alive's counters this often
_ECU_TURNAROUND_S = 0.02  # min gap between the ECU's last frame and our next request
_FC_RETRIES = 2           # resends of a multi-frame request whose flow control never came
_SID_CDI = 0x14  # ClearDiagnosticInformation
_SID_IOCBI = 0x2F  # InputOutputControlByIdentifier
_SID_RDTC = 0x19  # ReadDTCInformation

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


# ISO 14229-1 NRC names
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

    Covers a missing response, a truncated response, or a well-formed response
    whose payload field carries the wrong value. `nrc` is None (no wire NRC).
    """

    def __init__(self, sid: int, detail: str):
        Exception.__init__(
            self, f"Malformed response for SID 0x{sid:02X}: {detail}"
        )
        self.sid = sid
        self.nrc = None
        self.nrc_name = None
        self.detail = detail


class BusUnavailableError(Exception):
    """Raised when the CAN interface can't be opened or drops mid-session.

    Carries the channel for an actionable CLI hint.
    """

    def __init__(self, channel: str, cause: Exception):
        self.channel = channel
        self.cause = cause
        super().__init__(f"CAN bus {channel!r} is unavailable: {cause}")


class _BroadcastWatcher(can.Listener):
    """Counts inbound frames matching one broadcast (heartbeat) CAN ID.

    `wait_for_bootloader` breaks Phase 1 when the counter advances (the ECU
    resumed broadcasting after reset). For nodes where `broadcast_for(...)`
    returns None, no watcher is installed and the wait is fixed-budget.
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


class _BusErrorListener(can.Listener):
    """Records the first fatal error from the Notifier RX thread instead of
    letting it crash with a stderr traceback.

    Exposed via ``bus_error`` so callers can tell a dead bus from a silent ECU.
    """

    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    def on_message_received(self, msg: can.Message) -> None:
        pass

    def on_error(self, exc: Exception) -> None:
        if self.error is None:
            self.error = exc
            _log.warning("CAN bus error in notifier thread: %s", exc)

    def stop(self) -> None:
        pass


class _ResponseFrameLog(can.Listener):
    """Records the raw frames the ECU sends on its response CAN id.

    `_send_raw` reports a failed receive as "no response received", but the
    transport raises TimeoutError both when the ECU said NOTHING and when a
    multi-frame reply STARTED and did not finish (a dropped consecutive frame,
    an interleaved packet). Those need opposite fixes, and only the wire tells
    them apart -- so keep the last few response-id frames and quote them in the
    error.
    """

    def __init__(self, can_id: int, depth: int = 6) -> None:
        super().__init__()
        self._can_id = can_id
        self._frames: collections.deque = collections.deque(maxlen=depth)
        self._lock = threading.Lock()
        self._reply_start = 0.0   # when the last SF/FF arrived
        self._segment_due = 0     # bytes still to come in a multi-frame reply
        self._last_frame = 0.0

    def on_message_received(self, msg: can.Message) -> None:
        if msg.arbitration_id != self._can_id or msg.is_error_frame:
            return
        now, data = time.monotonic(), bytes(msg.data)
        pci = data[0] >> 4 if data else None
        with self._lock:
            self._frames.append((now, data))
            self._last_frame = now
            if pci == 0:                                  # single frame
                self._reply_start, self._segment_due = now, 0
            elif pci == 1 and len(data) >= 2:             # first frame: 12-bit length
                self._reply_start = now
                self._segment_due = (((data[0] & 0x0F) << 8) | data[1]) - (len(data) - 2)
            elif pci == 2:                                # consecutive frame
                self._segment_due -= len(data) - 1

    def last_frame_at(self) -> float:
        with self._lock:
            return self._last_frame

    def reply_started_since(self, t: float) -> bool:
        with self._lock:
            return self._reply_start >= t

    def reply_in_progress(self, now: float) -> bool:
        """A multi-frame reply has started and not finished (nor stalled past N_Cr)."""
        with self._lock:
            return self._segment_due > 0 and now - self._last_frame < _N_CR_S

    def mark(self) -> float:
        """Timestamp to report frames since (called just before a request)."""
        return time.monotonic()

    def summary(self, since: float) -> str:
        """What the ECU put on the wire since `since`, for an error message."""
        with self._lock:
            seen = [(t, d) for t, d in self._frames if t >= since]
        if not seen:
            return f"no frames from 0x{self._can_id:03X}"
        first = seen[0][1]
        kind = ("first frame of a multi-frame reply" if first and first[0] >> 4 == 1
                else "single frame" if first and first[0] >> 4 == 0 else "frame")
        return (f"{len(seen)} frame(s) from 0x{self._can_id:03X} ({kind}), "
                f"first {first.hex(' ')}, last {seen[-1][1].hex(' ')}")


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
        try:
            self._bus = can.Bus(interface=interface, channel=channel)
        except Exception as exc:
            raise BusUnavailableError(channel, exc) from exc
        # Share one Notifier with the transport; python-can allows only one
        # active Notifier per bus.
        self._frame_notifier = can.Notifier(self._bus, [])
        # Records/suppresses a mid-session bus drop.
        self._bus_error_listener = _BusErrorListener()
        self._install_listener(self._bus_error_listener)
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
        self._tp_interval = 0.5
        # Serializes our request frames with the keep-alive's (see _tp_clear).
        self._tx_lock = threading.Lock()
        self._awaiting_since: float | None = None
        # Keep-alive health (see tp_stats).
        self._tp_sent = 0
        self._tp_fail = 0
        self._tp_max_gap = 0.0
        self._tp_last_ok: float | None = None
        self._tp_phase = "stopped"  # what the keep-alive thread is doing (heartbeat log)
        # Wire-level record of the ECU's replies, so a failed receive can say
        # whether the ECU answered at all (see _ResponseFrameLog).
        self._rx_log = _ResponseFrameLog(node.response_can_id)
        self._install_listener(self._rx_log)
        self._rx_since = 0.0

        bcast = broadcast_for(node.name)

        # Per-node broadcast watcher for wait_for_bootloader Phase 1 early exit.
        self._broadcast_watcher: _BroadcastWatcher | None = None
        if bcast is not None:
            self._broadcast_watcher = _BroadcastWatcher(bcast.can_id)
            self._install_listener(self._broadcast_watcher)

    def _install_listener(self, listener: can.Listener) -> None:
        self._frame_notifier.add_listener(listener)

    def start_tester_present(self) -> None:
        """Send TesterPresent (3E 80, suppress positive response) every
        `set_tester_present_interval` seconds (default 0.5), never mid-transfer.

        Idempotent: a no-op if the keep-alive thread is already running, so callers
        can start it defensively even after `wait_for_bootloader` already did.
        """
        if self._tp_thread is not None and self._tp_thread.is_alive():
            return
        self._tp_stop.clear()
        self._tp_thread = threading.Thread(target=self._tp_loop, daemon=True)
        self._tp_thread.start()

    def stop_tester_present(self) -> None:
        self._tp_stop.set()
        if self._tp_thread:
            self._tp_thread.join()
            self._tp_thread = None

    def _tp_clear(self, now: float) -> bool:
        """Whether a 3E 80 sent now stays out of an ISO-TP transfer. Call holding
        _tx_lock (our own multi-frame requests hold it while they go out).

        Deferred only while the ECU's multi-frame reply is on the wire, or for
        _REPLY_GUARD_S after a request until the reply starts (its first frame can
        otherwise arrive just before our 3E 80, ahead of our flow control). A slow
        ECU or a 0x78 response-pending wait does not hold it up.
        """
        if self._rx_log.reply_in_progress(now):
            return False
        since = self._awaiting_since
        return (since is None or now - since >= _REPLY_GUARD_S
                or self._rx_log.reply_started_since(since))

    def set_tester_present_interval(self, seconds: float) -> float:
        """Set the keep-alive period (takes effect within _TP_POLL_S); returns the old one."""
        if seconds <= 0:
            raise ValueError(f"TesterPresent interval must be > 0, got {seconds}")
        previous, self._tp_interval = self._tp_interval, float(seconds)
        return previous

    def send_tester_present(self) -> None:
        """One `3E 80` now (still kept out of a transfer on the wire)."""
        self._send_tp_when_clear(self._tp_packet())

    def _tp_packet(self) -> CanPacket:
        return CanPacket(
            packet_type=CanPacketType.SINGLE_FRAME,
            addressing_format=CanAddressingFormat.NORMAL_ADDRESSING,
            addressing_type=AddressingType.PHYSICAL,
            can_id=self._node.request_can_id,
            payload=[_SID_TP, 0x80],
            # Padded like the segmenter's requests: the DIR drops any frame on its
            # request id whose DLC isn't 8, so an optimized `03 3E 80` never arrives.
            dlc=8,
        )

    def _send_tp_when_clear(self, packet: CanPacket,
                            stop: threading.Event | None = None) -> None:
        mine = threading.current_thread() is self._tp_thread  # phase is the keep-alive's
        while stop is None or not stop.is_set():
            if mine:
                self._tp_phase = "waiting for tx lock"
            with self._tx_lock:
                if self._tp_clear(time.monotonic()):
                    if mine:
                        self._tp_phase = "sending"
                    self._transport.send_packet(packet)
                    return
            if mine:
                self._tp_phase = "deferred (reply on the wire)"
            time.sleep(0.002)

    def tp_stats(self) -> dict:
        """Keep-alive health for diagnosing an ECU's FAIL_NO_TESTER_PRESENT: how many 3E 80
        we sent, how many sends failed, and the largest gap between two sends (seconds)."""
        last_ok = self._tp_last_ok
        alive = self._tp_thread is not None and self._tp_thread.is_alive()
        return {"session": f"{id(self):x}", "thread_alive": alive, "phase": self._tp_phase,
                "interval_s": self._tp_interval,
                "sent": self._tp_sent, "failed": self._tp_fail,
                "max_gap_s": round(self._tp_max_gap, 3),
                "since_last_s": round(time.monotonic() - last_ok, 3) if last_ok else None}

    def _tp_loop(self) -> None:
        tp_packet = self._tp_packet()
        last_sent: float | None = None
        last_ok: float | None = None  # last 3E 80 actually handed to the bus
        next_beat = time.monotonic()
        _log.info("TesterPresent 0x%03X: keep-alive thread started (session %x)",
                  self._node.request_can_id, id(self))
        while not self._tp_stop.is_set():
            if time.monotonic() >= next_beat:  # heartbeat: is this thread still turning over?
                _log.info("TesterPresent 0x%03X: session %x sent %d failed %d interval %.2f s",
                          self._node.request_can_id, id(self), self._tp_sent, self._tp_fail,
                          self._tp_interval)
                next_beat = time.monotonic() + _TP_HEARTBEAT_S
            self._tp_phase = "idle"
            # First one immediately: a learn routine started right after the session
            # opens fails at once if no TesterPresent has reached the ECU yet.
            wait = 0.0 if last_sent is None else last_sent + self._tp_interval - time.monotonic()
            if wait > 0:
                self._tp_stop.wait(min(wait, _TP_POLL_S))  # re-reads the interval
                continue
            last_sent = time.monotonic()
            try:
                self._send_tp_when_clear(tp_packet, self._tp_stop)
                now = time.monotonic()
                self._tp_sent += 1
                # An ECU watchdog (the DIR learn's) ends the job on a TesterPresent gap;
                # say when we made one, and how long.
                if last_ok is not None:
                    gap = now - last_ok
                    self._tp_max_gap = max(self._tp_max_gap, gap)
                    if gap > self._tp_interval + _TP_GAP_WARN_S:
                        _log.warning("TesterPresent 0x%03X: %.2f s since the last one "
                                     "(interval %.2f s)", self._node.request_can_id,
                                     gap, self._tp_interval)
                last_ok = self._tp_last_ok = now
            except Exception as exc:  # noqa: BLE001  (a keep-alive tick must never crash)
                self._tp_fail += 1
                _log.warning("TesterPresent 0x%03X send failed: %r",
                             self._node.request_can_id, exc)
                # Bus dropped out from under the keep-alive thread. Record it
                # (so the main thread can report it) and exit quietly instead
                # of letting the daemon thread crash with a stderr traceback.
                if self._is_bus_down_error(exc):
                    self._bus_error_listener.on_error(exc)
                    _log.warning("TesterPresent 0x%03X: keep-alive stopped (bus down)",
                                 self._node.request_can_id)
                    return
                # Transient send failure -- e.g. ENOBUFS (errno 105): the socketcan
                # TX queue is momentarily full on a busy shared bus. Skip this tick and
                # retry next interval instead of tearing the daemon thread down.
                continue
        self._tp_phase = "stopped"
        _log.info("TesterPresent 0x%03X: keep-alive thread stopped (session %x, sent %d)",
                  self._node.request_can_id, id(self), self._tp_sent)

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
        """Update transport timeouts (seconds → ms): N_Bs (inter-frame) and
        N_Cr (consecutive frame). Call before long operations, restore after.
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
                if resp and len(resp) >= 3 and resp[2] == 0x05:
                    # NRC 0x05: skip the retry delay
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
        bus.send() is patched to retry on ENOBUFS (errno 105).
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
        # A running keep-alive would feed the resident bootloader and fight the reset
        # (hold it in the bl / delay the app boot). Stop it; wait_for_bootloader restarts
        # the keep-alive when we deliberately want to hold the bl.
        self.stop_tester_present()
        resp = self._send_raw([_SID_ER, reset_type])
        self._check_positive(resp, _SID_ER)

    def ecu_reset_no_wait(self, reset_type: int = 0x01) -> None:
        """Send ECUReset with `suppressPositiveResponse` set — fire-and-forget.

        Frame is `11 (reset_type | 0x80)`
        """
        self.stop_tester_present()  # see ecu_reset: don't let the keep-alive fight the reset
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
        """Two-phase bootloader handover wait.

        Phase 1 — keep-alive (`keepalive_phase_s` s): send `3E 80` (fire-and-
        forget TesterPresent) at `keepalive_interval_s` cadence. Exits early
        when the per-node broadcast counter advances (if a `_BroadcastWatcher`
        is installed); otherwise burns the full budget.

        Phase 2 — confirmation: send `3E 00` and wait for `7E 00`, P2 timeout
        `confirm_p2_ms` (40 ms), up to `confirm_max_attempts` (14) retries.

        Raises `TimeoutError` on failure with phase 1 frame count and phase 2 NRCs.
        """
        phase1_start = time.monotonic()
        end_phase1 = phase1_start + keepalive_phase_s
        keepalive_count = 0
        bus_errors = 0
        watcher = self._broadcast_watcher
        baseline = watcher.count if watcher is not None else None
        early_exit_ms: float | None = None
        while time.monotonic() < end_phase1:
            try:
                self._send_tp_no_wait()
                keepalive_count += 1
            except Exception:
                # Expected: no ACK while the ECU reboots. Count and continue.
                bus_errors += 1
            # Early exit on broadcast counter advance
            if watcher is not None and watcher.count != baseline:
                early_exit_ms = (time.monotonic() - phase1_start) * 1000
                break
            time.sleep(keepalive_interval_s)
        if bus_errors:
            _log.debug(
                "Phase 1: %d keep-alive TX errors (expected on a single-node bus"
                " while the ECU reboots) — deferring to Phase 2 for confirmation",
                bus_errors,
            )
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
        time.sleep(0.010)

        # Phase 2: fast 3E 00 probes.
        nrc_count = 0
        send_errors = 0
        first_nrc: int | None = None
        for _attempt in range(1, confirm_max_attempts + 1):
            try:
                resp = self._send_raw([_SID_TP, 0x00], timeout_ms=confirm_p2_ms)
            except Exception:
                # No ACK yet while the ECU boots; count and keep probing.
                send_errors += 1
                time.sleep(confirm_p2_ms / 1000.0)
                continue
            if resp and resp[0] == 0x7E:
                # Signed resident bootloaders (e.g. pmrbl 2026.8.3) auto-hand-off to the
                # app on a timer the older permissive bl never had: ~50 ms from a bare
                # reset, bumped to ~2 s on every UDS request. The flood only covers
                # wait_for_bootloader itself, so start the 2 Hz keep-alive now to hold the
                # window open — otherwise a bootloader-context read (0xF180 identity, a
                # slow erase, etc.) issued >2 s later silently lands in the app instead.
                self.start_tester_present()
                return
            if resp and resp[0] == 0x7F:
                nrc_count += 1
                if first_nrc is None and len(resp) >= 3:
                    first_nrc = resp[2]
        diag = (
            f"phase 1 sent {keepalive_count} keep-alive frames"
            + (f" ({bus_errors} TX errors)" if bus_errors else "")
            + f"; phase 2 sent {confirm_max_attempts} TesterPresent probes"
            f" (P2={confirm_p2_ms} ms)"
            + (f", {send_errors} TX errors" if send_errors else "")
            + ", no positive response"
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

        Call after read steps that may leave stale NRC or partial ISO-TP
        frames (e.g. ECUs that return NRC 0x13 for multi-frame DIDs).
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

    def read_dtcs(self, status_mask: int = 0xFF) -> dict[int, int]:
        """ReadDTCInformation reportDTCByStatusMask (0x19 02) — return
        {dtc_code: status} for DTCs matching ``status_mask`` (0xFF = any).

        Positive response: ``59 02 <availabilityMask> [<dtc hi mid lo> <status>]*``.
        """
        resp = self._send_raw([_SID_RDTC, 0x02, status_mask & 0xFF])
        self._check_positive(resp, _SID_RDTC)
        out: dict[int, int] = {}
        body = resp[3:]  # skip 59 02 <availabilityMask>
        for i in range(0, len(body) - 3, 4):
            dtc = (body[i] << 16) | (body[i + 1] << 8) | body[i + 2]
            out[dtc] = body[i + 3]
        return out

    def _send_raw(self, payload: list[int], timeout_ms: float = 2000) -> list[int]:
        try:
            return self._exchange(payload, timeout_ms)
        finally:
            self._awaiting_since = None

    def _exchange(self, payload: list[int], timeout_ms: float) -> list[int]:
        _log.debug("TX  %s", bytes(payload).hex(" "))
        self._rx_since = self._rx_log.mark()
        msg = UdsMessage(
            payload=bytearray(payload),
            addressing_type=AddressingType.PHYSICAL,
        )
        # SecurityAccess is a stateful seed/key handshake: a resent 27 05 hands back a fresh
        # seed that voids the key the caller is about to compute from the first one, and a
        # resent 27 06 can land the DIR one step out. Never resend it -- let it fail and the
        # caller redo the whole handshake.
        retries = 0 if payload[0] == _SID_SA else _FC_RETRIES
        try:
            for attempt in range(retries + 1):
                # An ECU that serves one ISO-TP direction at a time (the DIR) drops a request
                # that arrives while it is still closing out its own multi-frame reply; give
                # it a moment after its last frame.
                idle = time.monotonic() - self._rx_log.last_frame_at()
                if idle < _ECU_TURNAROUND_S:
                    time.sleep(_ECU_TURNAROUND_S - idle)
                try:
                    with self._tx_lock:
                        self._transport.send_message(msg)
                        # The reply guard (see _tp_clear) runs from the request's last frame.
                        self._awaiting_since = time.monotonic()
                    break
                except TimeoutError:
                    # No flow control for our first frame: the ECU missed it. Resend.
                    if attempt == retries:
                        raise
                    _log.warning("no flow control for %s; resending",
                                 bytes(payload[:2]).hex(" "))
        except Exception as exc:
            err_str = str(exc)
            if "105" in err_str or "buffer" in err_str.lower():
                raise RuntimeError(
                    "TX queue full (ENOBUFS) — run: "
                    f"sudo ip link set {getattr(self._bus, 'channel', '?')} "
                    "txqueuelen 1000"
                ) from exc
            if self._is_bus_down_error(exc):
                raise BusUnavailableError(
                    getattr(self._bus, "channel", "?"), exc) from exc
            raise
        expected_sid = payload[0]
        positive_sid = expected_sid + 0x40
        # A positive response echoes the DID (22/2E) or subtype + routine id (31); a late
        # reply to an earlier request of the same service must not pass as this one's.
        echo = {_SID_RDBI: payload[1:3], _SID_WDBI: payload[1:3],
                _SID_RC: payload[1:4]}.get(expected_sid, [])
        deadline_ms = timeout_ms
        while True:
            try:
                record = self._transport.receive_message(
                    start_timeout=deadline_ms, end_timeout=deadline_ms
                )
            except Exception:
                # Empty receive = no response, unless the notifier saw a
                # bus-down error — surface that instead of a false timeout.
                bus_err = self.bus_error
                if bus_err is not None:
                    raise BusUnavailableError(
                        getattr(self._bus, "channel", "?"), bus_err) from bus_err
                return []
            resp = list(record.payload)
            if not resp:
                return resp
            # 0x78 = requestCorrectlyReceivedResponsePending
            if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
                deadline_ms = timeout_ms
                continue
            # Skip stale frames: a valid response is positive (SID + 0x40) or
            # negative (7F <our-SID> <NRC>).
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
            elif resp[1:1 + len(echo)] != echo:
                _log.warning("[stale] discarding %s while waiting for the reply to %s",
                             bytes(resp[:4]).hex(" "), bytes(payload[:4]).hex(" "))
                continue
            return resp

    @property
    def bus_error(self) -> Exception | None:
        """The first fatal error the notifier RX thread saw, or None.

        Lets callers distinguish a dead bus from a silent ECU.
        """
        return self._bus_error_listener.error

    @staticmethod
    def _is_bus_down_error(exc: Exception) -> bool:
        """True if ``exc`` looks like the CAN interface going down.

        Matches errno 100 (ENETDOWN) / 19 (ENODEV) and their text, so both
        ``CanOperationError`` and bare ``OSError`` from python-can are caught.
        """
        if isinstance(exc, OSError) and exc.errno in (100, 19):
            return True
        if getattr(exc, "error_code", None) in (100, 19):
            return True
        text = str(exc).lower()
        return "network is down" in text or "no such device" in text

    def _check_positive(self, resp: list[int], expected_sid: int) -> None:
        if not resp:
            # Quote the wire: "no frames" is a silent ECU, frames-but-no-message
            # is a reception that started and broke (see _ResponseFrameLog).
            log = getattr(self, "_rx_log", None)
            wire = log.summary(getattr(self, "_rx_since", 0.0)) if log else "no wire record"
            raise MalformedResponseError(expected_sid, f"no response received ({wire})")
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
