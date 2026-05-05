"""UdsSession: wraps py-uds Client with TesterPresent background thread."""

from __future__ import annotations

import threading
import time
from typing import Any

import can
from uds.addressing import AddressingType
from uds.can.addressing import NormalCanAddressingInformation, CanAddressingFormat
from uds.can.packet import CanPacket, CanPacketType
from uds.can.transport_interface import PyCanTransportInterface
from uds.message import UdsMessage

from .node_config import NodeConfig
from .security import compute_key

_SESSION_DEFAULT = 0x01
_SESSION_PROGRAMMING = 0x02
_SESSION_EXTENDED = 0x03

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
_IOCP_SHORT_TERM_ADJUSTMENT = 0x03
# RC 0x0601: vendor pre-flash routine (vcleft / vcleftramapp)
_RC_VENDOR_PREFLIGHT = 0x0601
# RC 0x0540: VCWaitForOTAMode — start, then poll until response[0] == 2
_RC_OTA_MODE = 0x0540


# ISO 14229-1 NRC names — the only NRCs hashpicker_sim's UDS stack recognizes
# (table at 0x0043e200; 22 entries; no Tesla-custom NRCs). Any NRC outside
# this set is shown as "unknown" but still printed numerically.
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
        addressing = NormalCanAddressingInformation(
            rx_physical_params={"can_id": node.response_can_id},
            tx_physical_params={"can_id": node.request_can_id},
            rx_functional_params={"can_id": 0x7E8},
            tx_functional_params={"can_id": 0x7DF},
        )
        self._transport: PyCanTransportInterface = PyCanTransportInterface(
            network_manager=self._bus,
            addressing_information=addressing,
        )
        self._node = node
        self._tp_stop = threading.Event()
        self._tp_thread: threading.Thread | None = None

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
        """Full seed → key exchange.

        `level_idx` picks the algorithm (via the node config) and is forwarded
        to `compute_key`. `seed_level` is the actual UDS sub-function byte;
        when `None`, falls back to `level_idx * 2 + 1` (legacy behavior, only
        correct for `level_idx == 0` with `protocol_ver < 3`).

        Seed levels per `DAT_00650e08[idx*16]` in `uds_security_access`:
          idx 0 → 0x05, but **overridden to 0x01 if protocol_ver < 3** (most ECUs)
          idx 1 → 0x01
          idx 2 → 0x05
          idx 3 → 0x11 (tesla_hash — ibst, esp, espcal, rcmcal, rcm)
          idx 4 → 0x11 (baolong_hash — tpms)
          idx 5 → 0x03
          idx 6 → 0x01
          idx 7 → 0x05 (pektron-style FUN_0040be8e — cmp)
        Callers should compute `seed_level` from `protocol_ver` (read at DID
        0x0101 byte[2]) and pass it explicitly — see flash_scripts.py.
        """
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

    def routine_control(self, routine_id: int, arg: bytes = b"") -> bytes:
        payload = [
            _SID_RC, _RC_START,
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
        print(f"    Flash count: {count} / {limit}")

    def vendor_preflight_routine(self) -> None:
        """RC 0x0601 — vcleft/vcleftramapp pre-flash vendor routine.

        Starts the routine, then polls requestResults (subfunction 3) up to
        50 times (100 ms apart) until the response byte equals 1.
        """
        import time as _time
        _RC_REQ_RESULTS = 0x03
        payload_start = [
            _SID_RC, _RC_START,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        resp = self._send_raw(payload_start)
        self._check_positive(resp, _SID_RC)

        payload_poll = [
            _SID_RC, _RC_REQ_RESULTS,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        for _ in range(50):
            _time.sleep(0.1)
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

        Replicates `uds_vc_wait_for_ota_mode` (`0x00409dd8`): bumps P2 to 1 s,
        starts the routine, sleeps 100 ms, polls requestResults; loops up to
        `attempts` times. Returns on success; raises `UdsError` on timeout or
        unexpected response.

        Used in sub4 (`0x006513a0`) when prepping VCRIGHT for a VCFRONT bu
        flash. **Requires the vehicle to actually be in OTA state** — not
        achievable on a bench setup against a single ECU.
        """
        self.set_timeout(1.0)
        rid_hi = (_RC_OTA_MODE >> 8) & 0xFF
        rid_lo = _RC_OTA_MODE & 0xFF
        for attempt in range(attempts):
            print(f"    wait_for_ota_mode attempt {attempt + 1}/{attempts}")
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
            resp = self._send_raw([_SID_RC, _RC_REQUEST_RESULTS, rid_hi, rid_lo])
            self._check_positive(resp, _SID_RC)
            # response: 71 03 05 40 <result_count?> <result>
            if len(resp) >= 5 and resp[4] == 0x02:
                print(f"    OTA mode active (response[0]=0x02)")
                return
        raise MalformedResponseError(
            _SID_RC,
            f"OTA mode never reported active (resp[4] != 0x02) after {attempts} attempts",
        )

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

    def transfer_data(self, payload: bytes, max_block_len: int) -> None:
        """Transfer payload in chunks.

        max_block_len includes the 1-byte sequence counter.
        """
        chunk_size = max_block_len - 2  # subtract SID + seq bytes
        seq = 0x01
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + chunk_size]
            resp = self._send_raw([_SID_TD, seq] + list(chunk))
            self._check_positive(resp, _SID_TD)
            offset += len(chunk)
            seq = 0x00 if seq == 0xFF else seq + 1  # wrap 0xFF → 0x00

    def request_transfer_exit(self) -> None:
        resp = self._send_raw([_SID_RTE])
        self._check_positive(resp, _SID_RTE)

    def ecu_reset(self, reset_type: int = 0x01) -> None:
        resp = self._send_raw([_SID_ER, reset_type])
        self._check_positive(resp, _SID_ER)

    def ecu_reset_no_wait(self, reset_type: int = 0x01) -> None:
        """Send ECUReset with `suppressPositiveResponse` set — fire-and-forget.

        Frame is `11 (reset_type | 0x80)`. Matches the VM's `reset(0)` opcode
        (`uds_reset` at `0x0040934c` → `FUN_004301d2(..., '\\0')`), which OR's the
        SPR bit into the subfunction and does not wait for `51 xx`. Sending plain
        `11 01` here is technically valid but provokes a positive response the
        ECU may not finish before it reboots, occasionally racing the bootloader
        handover.
        """
        msg = UdsMessage(
            payload=bytearray([_SID_ER, reset_type | 0x80]),
            addressing_type=AddressingType.PHYSICAL,
        )
        self._transport.send_message(msg)

    def _send_tp_no_wait(self) -> None:
        """Fire-and-forget TesterPresent (`3E 80`) — keep-alive, no response expected.

        Matches `FUN_00430bb0(handle, '\\0')` used by the VM's
        `enterBootloader(0)` keep-alive loop.
        """
        msg = UdsMessage(
            payload=bytearray([_SID_TP, 0x80]),
            addressing_type=AddressingType.PHYSICAL,
        )
        self._transport.send_message(msg)

    def wait_for_bootloader(
        self,
        timeout_s: float = 10.0,
        keepalive_phase_s: float = 1.5,
        keepalive_interval_s: float = 0.04,
        confirm_interval_s: float = 0.1,
    ) -> None:
        """Two-phase bootloader handover wait, mimicking VM `enterBootloader(0)`.

        **Phase 1 — keep-alive** (`keepalive_phase_s` seconds): send `3E 80`
        (TesterPresent fire-and-forget, no response expected) at
        `keepalive_interval_s` cadence. Matches the VM's
        `FUN_00402120` loop, which spams TP-no-wait while watching for the
        boot-broadcast ID to change. We don't have boot-broadcast decoding
        yet, so we just spend a fixed budget here. ECUs that take their
        time coming up (PCS C28x DSP ~4–6 s) get the headroom they need.

        **Phase 2 — TP-with-response confirmation**: send `3E 00` (ISO
        14229's only valid TP sub-function) at `confirm_interval_s` cadence,
        retry on no-response/NRC, succeed on the first `7E 00`. Matches
        `FUN_00401f8c`. NRC 0x12 (subFunctionNotSupported) means the
        bootloader is up but rejecting our frame — which is the bug that
        landed us here in the first place when we had `3E 01`.

        Total budget = `timeout_s` (default 10 s); phase 1 takes
        `keepalive_phase_s` of that, phase 2 gets the rest.
        """
        if keepalive_phase_s >= timeout_s:
            raise ValueError("keepalive_phase_s must be < timeout_s")

        # Phase 1: fire-and-forget keep-alive
        end_phase1 = time.monotonic() + keepalive_phase_s
        keepalive_count = 0
        while time.monotonic() < end_phase1:
            try:
                self._send_tp_no_wait()
                keepalive_count += 1
            except Exception:
                # Bus may be transiently down right after reset; keep trying.
                pass
            time.sleep(keepalive_interval_s)
        print(
            f"    Phase 1: {keepalive_count} keep-alive 3E 80 frames sent"
            f" over {keepalive_phase_s:.1f}s"
        )

        # Phase 2: TP-with-response confirmation
        deadline = time.monotonic() + (timeout_s - keepalive_phase_s)
        attempts = 0
        nrc_count = 0
        first_nrc: int | None = None
        confirm_start = time.monotonic()
        confirm_timeout_ms = max(int(confirm_interval_s * 1000), 50)
        while time.monotonic() < deadline:
            attempts += 1
            resp = self._send_raw([_SID_TP, 0x00], timeout_ms=confirm_timeout_ms)
            if resp and resp[0] == 0x7E:
                elapsed = time.monotonic() - confirm_start
                print(
                    f"    Phase 2: bootloader replied to 3E 00 after"
                    f" {attempts} attempt(s) ({elapsed:.2f}s)"
                )
                return
            if resp and resp[0] == 0x7F:
                nrc_count += 1
                if first_nrc is None and len(resp) >= 3:
                    first_nrc = resp[2]
            time.sleep(confirm_interval_s)
        diag = (
            f"phase 1 sent {keepalive_count} keep-alive frames; phase 2 sent"
            f" {attempts} TesterPresent probes, no positive response"
        )
        if nrc_count:
            diag += (
                f" ({nrc_count} negative responses"
                + (f", first NRC 0x{first_nrc:02X}" if first_nrc is not None else "")
                + ")"
            )
        raise TimeoutError(
            f"Bootloader handover did not complete in {timeout_s}s — {diag}"
        )

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

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
        self._transport.send_message(msg)
        deadline_ms = timeout_ms
        while True:
            try:
                record = self._transport.receive_message(
                    start_timeout=deadline_ms, end_timeout=deadline_ms
                )
            except Exception:
                return []
            resp = list(record.payload)
            # 0x78 = requestCorrectlyReceivedResponsePending — ECU still working
            if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
                deadline_ms = timeout_ms
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

    def __enter__(self) -> "UdsSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_tester_present()
        try:
            if self._transport.notifier is not None:
                self._transport.notifier.stop()
        except Exception:
            pass
        try:
            self._bus.shutdown()
        except Exception:
            pass
