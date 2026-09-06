"""Tests for the odin_runner BenchBackend odx/uds adapters + Engine handlers.

Self-contained: a FakeSession stands in for uds_local.UdsSession (records calls,
returns canned bytes) and a synthetic NodeConfig supplies the ODJ routine/DID
specs, so these run with no bench and no firmware data. The RESOLVER_LEARNING
results spec mirrors the real DIR ODJ (see test_odj_codec).
"""
import struct
from pathlib import Path

import can
import odin_runner
from odin_runner import BenchBackend, Engine, _CanRxCache, _OdxAdapter, _UdsAdapter

from uds_local.client import UdsSession
from uds_local.node_config import NodeConfig
from uds_local.odj import FieldSpec, OdjEntry, RoutineEntry, SubSpec


def _fs(bit_length, byte_position, bit_position=0, data_type="uint", enum=None):
    return FieldSpec(bit_length=bit_length, byte_position=byte_position,
                     bit_position=bit_position, data_type=data_type,
                     enum_map=enum or {})


_RESOLVER = RoutineEntry(
    name="RESOLVER_LEARNING", hex_id=0x0407, start=None, stop=None,
    results=SubSpec(security_level=0, input={}, input_size=0, output_size=187,
                    output={
                        "RMSERROR": _fs(16, 184, data_type="int"),
                        "LEARN_RESULT": _fs(3, 186, 0, "uint",
                                            {"LEARN_SUCCESS": 0, "SPEED_RANGE": 4}),
                        "RUNNING": _fs(1, 186, 4, "uint", {"FALSE": 0, "TRUE": 1}),
                    }))
_BOARD_SN = OdjEntry(
    name="BOARD_SERIAL_NUMBER", hex_id=0xF013,
    read=SubSpec(security_level=0, input={}, output_size=112, input_size=0,
                 output={"BOARD_SERIAL_NUMBER": _fs(112, 0, data_type="ascii")}),
    write=None)


def _cfg(name="DI"):
    return NodeConfig(name=name, request_can_id=0x1, response_can_id=0x2,
                      security_algorithm="tesla_hash", security_buffer_size=16,
                      security_kw={}, dids={"BOARD_SERIAL_NUMBER": _BOARD_SN},
                      routines={"RESOLVER_LEARNING": _RESOLVER})


def _resolver_bytes(flags):
    return b"\x00" * 184 + struct.pack(">h", 0) + bytes([flags])


class FakeSession:
    """Records UDS calls; returns canned bytes for RESOLVER_LEARNING results."""

    def __init__(self):
        self.calls = []

    def routine_control(self, rid, arg=b"", subtype=0x01):
        self.calls.append(("routine_control", rid, bytes(arg), subtype))
        if rid == 0x0407 and subtype == 0x03:
            return _resolver_bytes(0x00)  # LEARN_SUCCESS, RUNNING=False
        return b""

    def read_did(self, did):
        self.calls.append(("read_did", did))
        if did == 0xF013:
            return b"1234567890ABCD"
        return b""

    def write_did(self, did, data):
        self.calls.append(("write_did", did, bytes(data)))

    def diagnostic_session(self, mode):
        self.calls.append(("diagnostic_session", mode))

    def security_access(self, level_idx=0, seed_level=None):
        self.calls.append(("security_access", level_idx, seed_level))

    def ecu_reset(self, reset_type=0x01):
        self.calls.append(("ecu_reset", reset_type))

    def ecu_reset_no_wait(self, reset_type=0x01):
        self.calls.append(("ecu_reset_no_wait", reset_type))

    def clear_dtc(self, group=0xFFFFFF):
        self.calls.append(("clear_dtc", group))

    def read_dtcs(self, status_mask=0xFF):
        self.calls.append(("read_dtcs", status_mask))
        return {0x111111: 0x08}

    def start_tester_present(self):
        self.calls.append(("start_tester_present",))

    def stop_tester_present(self):
        self.calls.append(("stop_tester_present",))

    def __exit__(self, *_):
        pass


# ---------------------------------------------------------------------------
# _UdsAdapter — raw payloads onto UdsSession
# ---------------------------------------------------------------------------

class TestUdsAdapter:
    def test_routine_control_hex_and_subtype(self):
        s = FakeSession()
        _UdsAdapter(s).routine_control("0xf00a", "00640052", "START_ROUTINE")
        assert s.calls[0] == ("routine_control", 0xF00A, bytes.fromhex("00640052"), 0x01)

    def test_read_write_data(self):
        s = FakeSession()
        a = _UdsAdapter(s)
        a.read_data("0xf013")
        a.write_data("0xf01c", "04")
        assert ("read_did", 0xF013) in s.calls
        assert ("write_did", 0xF01C, b"\x04") in s.calls

    def test_security_access_level(self):
        s = FakeSession()
        _UdsAdapter(s).security_access("LEVEL_5")
        assert s.calls[0] == ("security_access", 0, 5)

    def test_diagnostic_session_extended(self):
        s = FakeSession()
        _UdsAdapter(s).diagnostic_session("EXTENDED_DIAGNOSTIC_SESSION")
        assert s.calls[0] == ("diagnostic_session", 0x03)

    def test_ecu_reset_response_required_toggles_no_wait(self):
        s = FakeSession()
        a = _UdsAdapter(s)
        a.ecu_reset("HARD_RESET", True)
        a.ecu_reset("HARD_RESET", False)
        assert ("ecu_reset", 0x01) in s.calls
        assert ("ecu_reset_no_wait", 0x01) in s.calls

    def test_clear_dtcs_named_mask_falls_back_to_clear_all(self):
        s = FakeSession()
        _UdsAdapter(s).clear_dtcs("TestFailed")
        assert s.calls[0] == ("clear_dtc", 0xFFFFFF)


# ---------------------------------------------------------------------------
# _OdxAdapter — named params via the ODJ codec
# ---------------------------------------------------------------------------

class TestOdxAdapter:
    def test_start_and_wait_parses_results(self):
        s = FakeSession()
        out = _OdxAdapter(s, _cfg()).start_and_wait(
            "RESOLVER_LEARNING", "RUNNING", [True], 1, time_scale=0.0)
        assert out["LEARN_RESULT"] == "LEARN_SUCCESS"
        assert out["RUNNING"] is False
        # started (subtype 01) then polled results (subtype 03)
        subtypes = [c[3] for c in s.calls if c[0] == "routine_control"]
        assert subtypes[0] == 0x01 and 0x03 in subtypes

    def test_read_data_decodes_ascii_did(self):
        s = FakeSession()
        out = _OdxAdapter(s, _cfg()).read_data("BOARD_SERIAL_NUMBER")
        assert out["BOARD_SERIAL_NUMBER"] == "1234567890ABCD"

    def test_get_value_parsed_vs_raw(self):
        a = _OdxAdapter(FakeSession(), _cfg())
        assert a.get_value("RESOLVER_LEARNING", "LEARN_RESULT", 4, parsed=True) == "SPEED_RANGE"
        assert a.get_value("RESOLVER_LEARNING", "LEARN_RESULT", 4, parsed=False) == 4

    def test_security_gated_routine_authenticates_first(self):
        # A routine whose ODJ subspec declares a security level must trigger a
        # programming-session SecurityAccess(seed_level) BEFORE the routine goes out
        # (Tesla's odx layer does this from the ODJ; without it the ECU NRCs 0x33).
        # Mirrors PMR CAN_COMM_SELF_TEST (0x3FD, level 5).
        s = FakeSession()
        gated = RoutineEntry(
            name="CAN_COMM_SELF_TEST", hex_id=0x03FD, stop=None,
            start=SubSpec(security_level=5, input={}, output={},
                          input_size=0, output_size=0),
            results=SubSpec(security_level=5, input={}, output={"OK": _fs(1, 0)},
                            input_size=0, output_size=1))
        cfg = NodeConfig(name="PMR", request_can_id=1, response_can_id=2,
                         security_algorithm="tesla_hash", security_buffer_size=16,
                         security_kw={}, routines={"CAN_COMM_SELF_TEST": gated})
        _OdxAdapter(s, cfg).start_and_wait(
            "CAN_COMM_SELF_TEST", None, [], 1, time_scale=0.0)
        assert ("diagnostic_session", 0x03) in s.calls   # extended diagnostic session
        assert ("security_access", 0, 5) in s.calls      # seed_level from the ODJ
        kinds = [c[0] for c in s.calls]
        assert kinds.index("security_access") < kinds.index("routine_control")

    def test_unsecured_routine_skips_auth(self):
        # A level-0 routine (RESOLVER_LEARNING here) must NOT enter a session or auth.
        s = FakeSession()
        _OdxAdapter(s, _cfg()).start_and_wait(
            "RESOLVER_LEARNING", "RUNNING", [True], 1, time_scale=0.0)
        assert not any(c[0] in ("diagnostic_session", "security_access") for c in s.calls)


# ---------------------------------------------------------------------------
# Engine handlers end-to-end (mini-graphs through a BenchBackend + fake sessions)
# ---------------------------------------------------------------------------

def _bench(**nodes):
    """A BenchBackend with pre-seeded (cfg, FakeSession) per node — no real bus."""
    bb = BenchBackend("chan")
    sessions = {}
    for node, sess in nodes.items():
        bb._nodes[node.upper()] = (_cfg(node), sess)
        sessions[node] = sess
    return bb, sessions


def lit(v):
    return {"value": v}


def conn(t):
    return {"connection": t}


class TestEngineHandlers:
    def test_uds_ecu_reset(self):
        sess = FakeSession()
        bb, _ = _bench(RCM=sess)
        eng = Engine(bb, Path("."), time_scale=1.0)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("reset.run")},
            "reset": {"type": "uds.UdsEcuReset", "node_name": lit("RCM"),
                      "reset_type": lit("HARD_RESET"), "response_required": lit(True),
                      "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert eng.run_graph(graph, {}).exit_code == 0
        assert ("ecu_reset", 0x01) in sess.calls

    def test_odx_start_and_wait_results_into_metric(self):
        sess = FakeSession()
        bb, _ = _bench(DI=sess)
        eng = Engine(bb, Path("."), time_scale=1.0)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("saw.run")},
            "saw": {"type": "odx.OdxStartAndWaitResults", "node_name": lit("DI"),
                    "routine_name": lit("RESOLVER_LEARNING"),
                    "status_parameter": lit("RUNNING"),
                    "in_progress_statuses": lit([True]), "timeout": lit(1),
                    "done": conn("cap.capture")},
            "cap": {"type": "reporting.CaptureMetric", "metric_name": lit("res"),
                    "value": conn("saw.results"), "result_code": lit(0),
                    "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = eng.run_graph(graph, {})
        assert res.metrics[0]["value"]["LEARN_RESULT"] == "LEARN_SUCCESS"

    def test_esp_is_stubbed(self):
        # ESP has no session; uds('ESP') must not touch _nodes / raise.
        bb = BenchBackend("chan")
        assert isinstance(bb.uds("ESP"), odin_runner._StubUds)
        bb.uds("ESP").ecu_reset("HARD_RESET", True)  # no-op, no crash


class _FakeCanDb:
    def decode_frame(self, arb_id, data):
        if arb_id == 0x100:
            return [{"signal": "SIG_A", "value": 42}, {"signal": "SIG_B", "value": 7}]
        return []


class TestLiveCanCache:
    def test_rx_cache_decodes_into_signals(self):
        cache = _CanRxCache(_FakeCanDb())
        cache.on_message_received(
            can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False))
        assert cache.get("SIG_A") == 42
        assert cache.get("SIG_B") == 7
        assert cache.get("UNSEEN") is None

    def test_rx_cache_ignores_error_and_unknown_frames(self):
        cache = _CanRxCache(_FakeCanDb())
        cache.on_message_received(
            can.Message(arbitration_id=0x999, data=b"\x00", is_extended_id=False))
        assert cache.get("SIG_A") is None


class TestProtoReadFile:
    def test_reads_from_firmware_dump(self, tmp_path):
        (tmp_path / "opt").mkdir()
        (tmp_path / "opt" / "VERSION").write_text("VERSION 1.2\n")
        bb = BenchBackend("chan", firmware_root=tmp_path)
        eng = Engine(bb, Path("."), time_scale=1.0)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("rf.run")},
            "rf": {"type": "proto.ReadFile", "filepath": lit("/opt/VERSION"),
                   "mode": lit("r"), "done": conn("cap.capture")},
            "cap": {"type": "reporting.CaptureMetric", "metric_name": lit("v"),
                    "value": conn("rf.contents"), "result_code": lit(0),
                    "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert eng.run_graph(graph, {}).metrics[0]["value"] == "VERSION 1.2\n"


class TestReadDtcs:
    def test_udssession_parses_dtc_response(self):
        # Real UdsSession.read_dtcs parse, bypassing __init__ (no bus needed).
        sess = UdsSession.__new__(UdsSession)
        sess._send_raw = lambda payload, **k: [
            0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x08, 0xAB, 0xCD, 0xEF, 0x2F]
        assert sess.read_dtcs(0xFF) == {0x123456: 0x08, 0xABCDEF: 0x2F}

    def test_udssession_empty_when_healthy(self):
        sess = UdsSession.__new__(UdsSession)
        sess._send_raw = lambda payload, **k: [0x59, 0x02, 0xFF]  # no DTCs
        assert sess.read_dtcs() == {}

    def test_engine_read_dtcs_handler(self):
        sess = FakeSession()
        bb, _ = _bench(RCM=sess)
        eng = Engine(bb, Path("."), time_scale=1.0)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("rd.run")},
            "rd": {"type": "uds.UdsReadDtcs", "node_name": lit("RCM"),
                   "dtc_mask": lit("0x1"), "done": conn("cap.capture")},
            "cap": {"type": "reporting.CaptureMetric", "metric_name": lit("dtcs"),
                    "value": conn("rd.dtcs"), "result_code": lit(0),
                    "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = eng.run_graph(graph, {})
        assert res.metrics[0]["value"] == {0x111111: 0x08}
        assert ("read_dtcs", 0x1) in sess.calls   # mask '0x1' parsed to int 1


class TestStoreOutputs:
    def test_persists_outputs_keyed_by_board(self, tmp_path):
        from uds_local.datastore import DataStore
        ds = DataStore(tmp_path / "odin_data.json")
        bb, _ = _bench(DIR=FakeSession())  # read_did(0xF013) -> b"1234567890ABCD"
        bb._datastore = ds
        bb.store_outputs({"data_out": {"UNIT_ODOMETER": {"DRIVE_UNIT_ODOMETER": 5},
                                       "ANGLE_OFFSET": b"\xff\xff"}})
        stored = DataStore(tmp_path / "odin_data.json").get("1234567890ABCD", "outputs")
        assert stored["data_out"]["UNIT_ODOMETER"] == {"DRIVE_UNIT_ODOMETER": 5}
        assert stored["data_out"]["ANGLE_OFFSET"] == "ffff"   # bytes -> hex

    def test_noop_when_no_outputs_or_no_board(self, tmp_path):
        from uds_local.datastore import DataStore
        ds = DataStore(tmp_path / "odin_data.json")
        bb, _ = _bench(DIR=FakeSession())
        bb._datastore = ds
        bb.store_outputs({})                 # nothing to store
        assert ds.boards() == []
        bb2 = BenchBackend("chan")           # no nodes opened -> no board id
        bb2._datastore = ds
        bb2.store_outputs({"x": 1})
        assert ds.boards() == []

    def test_mock_backend_store_outputs_is_noop(self):
        odin_runner.MockBackend("success").store_outputs({"x": 1})  # no error, no-op


class _BlSession(FakeSession):
    """FakeSession + a recording wait_for_bootloader (which UdsSession has, but the
    plain FakeSession doesn't)."""
    def wait_for_bootloader(self, **_kw):
        self.calls.append(("wait_for_bootloader",))


class TestEnsureApplicationState:
    def test_bootloader_resets_then_waits_and_tracks(self):
        sess = _BlSession()
        bb, _ = _bench(PMR=sess)
        bb.ensure_application_state("PMR", "BOOTLOADER")
        assert ("ecu_reset_no_wait", 0x01) in sess.calls
        assert ("wait_for_bootloader",) in sess.calls   # same handover as the flasher
        assert "PMR" in bb._bootloader

    def test_idempotent_bootloader(self):
        sess = _BlSession()
        bb, _ = _bench(PMR=sess)
        bb.ensure_application_state("PMR", "BOOTLOADER")
        sess.calls.clear()
        bb.ensure_application_state("PMR", "BOOTLOADER")   # already in BL -> no reset
        assert sess.calls == []

    def test_application_resets_back_without_wait(self):
        sess = _BlSession()
        bb, _ = _bench(PMR=sess)
        bb.ensure_application_state("PMR", "BOOTLOADER")
        sess.calls.clear()
        bb.ensure_application_state("PMR", "APPLICATION")
        assert ("ecu_reset_no_wait", 0x01) in sess.calls
        assert not any(c[0] == "wait_for_bootloader" for c in sess.calls)
        assert "PMR" not in bb._bootloader

    def test_none_and_already_app_are_noops(self):
        sess = _BlSession()
        bb, _ = _bench(PMR=sess)
        bb.ensure_application_state("PMR", None)             # no state -> no-op
        bb.ensure_application_state("PMR", "APPLICATION")    # already app (untracked)
        assert not any(c[0] in ("ecu_reset_no_wait", "wait_for_bootloader")
                       for c in sess.calls)
