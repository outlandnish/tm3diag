"""Tests for the odin_runner BenchBackend odx/uds adapters + Engine handlers.

A FakeSession stands in for uds_local.UdsSession and a synthetic NodeConfig supplies
the ODJ specs, so these run with no bench and no firmware data. The RESOLVER_LEARNING
results spec mirrors the real DIR ODJ (see test_odj_codec).
"""
import struct
from pathlib import Path

import can
import odin_runner
import pytest
from odin_runner import BenchBackend, Engine, _CanRxCache, _OdxAdapter, _UdsAdapter

import config
from config import can_channel as _cfg_can_channel
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


# _UdsAdapter — raw payloads onto UdsSession
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


# _OdxAdapter — named params via the ODJ codec
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
        # A security-gated routine authenticates first: SecurityAccess(seed_level) before
        # the routine, else the ECU NRCs 0x33. Mirrors PMR CAN_COMM_SELF_TEST (0x3FD, level 5).
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
        assert ("security_access", 0, 5) in s.calls
        kinds = [c[0] for c in s.calls]
        assert kinds.index("security_access") < kinds.index("routine_control")

    def test_unsecured_routine_skips_auth(self):
        s = FakeSession()
        _OdxAdapter(s, _cfg()).start_and_wait(
            "RESOLVER_LEARNING", "RUNNING", [True], 1, time_scale=0.0)
        assert not any(c[0] in ("diagnostic_session", "security_access") for c in s.calls)

    def test_start_routine_decodes_the_start_response_not_results(self):
        # START_ROUTINE_RESULTS lives in the StartRoutine (0x31 01) response, not
        # RequestRoutineResults (0x31 03) -- the two records carry different fields
        # (see the real DIR ODJ: ROTOR_LEARNING start.output has START_ROUTINE_RESULTS,
        # results.output has ROUTINE_STATUS). start_routine must decode the START
        # response and must NOT issue a separate 0x03 read.
        rotor = RoutineEntry(
            name="ROTOR_LEARNING", hex_id=0x0406, stop=None,
            start=SubSpec(security_level=0, input_size=1, output_size=1,
                          input={"LEARN_SELECT": _fs(8, 0, enum={"ROTOR_OFFSET": 1})},
                          output={"START_ROUTINE_RESULTS": _fs(
                              8, 0, enum={"STARTED": 0, "FAILED_INCORRECT_CONDITIONS": 1})}),
            results=SubSpec(security_level=0, input={}, input_size=0, output_size=1,
                            output={"ROUTINE_STATUS": _fs(8, 0, enum={"RUNNING": 0})}))
        cfg = NodeConfig(name="DIR", request_can_id=1, response_can_id=2,
                         security_algorithm="tesla_hash", security_buffer_size=16,
                         security_kw={}, routines={"ROTOR_LEARNING": rotor})

        class _Sess(FakeSession):
            def routine_control(self, rid, arg=b"", subtype=0x01):
                self.calls.append(("routine_control", rid, bytes(arg), subtype))
                if rid == 0x0406 and subtype == 0x01:
                    return bytes([1])  # START_ROUTINE_RESULTS = FAILED_INCORRECT_CONDITIONS
                return b""

        s = _Sess()
        out = _OdxAdapter(s, cfg).start_routine(
            "ROTOR_LEARNING", {"LEARN_SELECT": "ROTOR_OFFSET"})
        assert out["START_ROUTINE_RESULTS"] == "FAILED_INCORRECT_CONDITIONS"
        # exactly the start went out (subtype 01); no separate results read (03)
        subtypes = [c[3] for c in s.calls if c[0] == "routine_control"]
        assert subtypes == [0x01]


# Engine handlers end-to-end (mini-graphs through a BenchBackend + fake sessions)
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


class _FrameDb:
    """Minimal CanDatabase shape: `messages` (for the signal index) + decode_frame."""

    messages = {
        0x118: {"name": "DI_systemStatus",
                "signals": {"DI_gear": {}, "DI_tractionControlMode": {}}},
        0x108: {"name": "DIR_torque", "signals": {"DIR_axleSpeed": {}}},
    }

    def decode_frame(self, mid, data):
        if mid == 0x118:
            return [{"signal": "DI_gear", "value": data[0]},
                    {"signal": "DI_tractionControlMode", "value": data[1]}]
        if mid == 0x108:
            return [{"signal": "DIR_axleSpeed",
                     "value": int.from_bytes(data[:2], "little")}]
        return []


# can_read maps the ETH bus token through TM3_VEHICLE_CHANNEL, so the frames have
# to be filed under that same channel or every lookup misses. Hardcoding "can0"
# meant these passed only on a host wired that way -- the bench runs the vehicle
# bus on can1, where all six read None. The literal below only stands in for
# "nothing configured"; it is used on both sides, so the test stays
# self-consistent either way.
_VEH = _cfg_can_channel("ETH") or "vcan0"


def _frame_bench(frames_by_channel):
    """BenchBackend reading a host's retained frames ({channel: {id: (data, ts)}})."""
    return BenchBackend(_VEH, frame_source=frames_by_channel.get, db=_FrameDb())


class TestFrameSourceReads:
    """A host already on the bus (tm3web) hands over retained frames; ODIN decodes
    ANY signal it has seen, on demand, without opening a second socket."""

    def test_reads_a_signal_from_a_retained_frame(self):
        bb = _frame_bench({_VEH: {0x118: (bytes([4, 5, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb.can_read("DI_gear", bus="ETH") == 4
        assert bb.can_read("DI_tractionControlMode", bus="ETH") == 5

    def test_reads_a_signal_the_hud_never_decodes(self):
        # DIR_axleSpeed (0x108) is outside tm3web's _DASH_IDS -- the case that
        # ruled out reusing dash_sig as the source.
        bb = _frame_bench({_VEH: {0x108: (bytes([0x30, 0x02, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb.can_read("DIR_axleSpeed", bus="ETH") == 560

    def test_unseen_id_reads_as_absent(self):
        bb = _frame_bench({_VEH: {0x118: (bytes(8), 1.0)}})
        assert bb.can_read("DIR_axleSpeed", bus="ETH") is None

    def test_unknown_signal_name_reads_as_absent(self):
        bb = _frame_bench({_VEH: {0x118: (bytes(8), 1.0)}})
        assert bb.can_read("NOT_A_SIGNAL", bus="ETH") is None

    def test_channel_with_no_frames_reads_as_absent(self):
        assert _frame_bench({}).can_read("DI_gear", bus="ETH") is None

    def test_never_opens_its_own_socket(self):
        bb = _frame_bench({_VEH: {0x118: (bytes([4, 5, 0, 0, 0, 0, 0, 0]), 1.0)}})

        def boom(channel):
            raise AssertionError("opened a second socket despite a frame source")

        bb._can_cache = boom
        assert bb.can_read("DI_gear", bus="ETH") == 4
        assert bb.can_read("DIR_axleSpeed", bus="ETH") is None   # absent, still no socket

    def test_bus_token_selects_the_right_channel(self):
        import config as _cfg
        veh, party = _cfg.can_channel("ETH"), _cfg.can_channel("PARTY")
        if not party or party == veh:
            pytest.skip("no distinct party channel configured")
        bb = _frame_bench({
            veh: {0x118: (bytes([4, 0, 0, 0, 0, 0, 0, 0]), 1.0)},
            party: {0x118: (bytes([3, 0, 0, 0, 0, 0, 0, 0]), 1.0)},
        })
        assert bb.can_read("DI_gear", bus="ETH") == 4
        assert bb.can_read("DI_gear", bus="PARTY") == 3

    def test_injected_db_is_used_verbatim(self):
        # The default ETH_COMPACT (2022) has no DI_tractionControlMode at all, so a
        # host's DB must win -- otherwise the dyno gate silently reads nothing.
        bb = _frame_bench({_VEH: {0x118: (bytes([4, 5, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb._db() is not None
        assert bb.can_read("DI_tractionControlMode", bus="ETH") == 5

    def test_cid_derive_runs_off_the_retained_frames(self):
        bb = _frame_bench({_VEH: {0x118: (bytes([4, 5, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Dyno"

    def test_cid_derive_reports_a_non_dyno_bus(self):
        bb = _frame_bench({_VEH: {0x118: (bytes([1, 0, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Normal"

    # VAPI_* aliases come from the firmware's VAPI table, so they need the MCU libs.
    @pytest.mark.skipif(config.vapi_libs() is None, reason="needs the MCU firmware libs")
    @pytest.mark.parametrize("gear,shift", [(4, "D"), (1, "P")])
    def test_cid_derive_resolves_vapi_aliases(self, gear, shift):
        bb = _frame_bench({_VEH: {0x118: (bytes([gear, 0, 0, 0, 0, 0, 0, 0]), 1.0)}})
        assert bb.cid_get("VAPI_shiftState") == shift


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
    """FakeSession + a recording wait_for_bootloader."""
    def wait_for_bootloader(self, **_kw):
        self.calls.append(("wait_for_bootloader",))


class TestEnsureApplicationState:
    def test_bootloader_resets_then_waits_and_tracks(self):
        sess = _BlSession()
        bb, _ = _bench(PMR=sess)
        bb.ensure_application_state("PMR", "BOOTLOADER")
        assert ("ecu_reset_no_wait", 0x01) in sess.calls
        assert ("wait_for_bootloader",) in sess.calls
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


class TestVehicleConditions:
    """Which firmware row applies is decided by the car's configuration, and
    every condition key is a GTW_carConfig signal named GTW_<key>."""

    @staticmethod
    def _bb(signals):
        bb = BenchBackend("chan")
        bb.can_read = lambda name, bus=None: signals.get(name)
        return bb

    def test_conditions_come_off_gtw_carconfig(self):
        bb = self._bb({"GTW_chassisType": 2, "GTW_drivetrainType": 0,
                       "GTW_vdcType": 1})
        assert bb.vehicle_conditions() == {"chassisType": "2",
                                           "drivetrainType": "0", "vdcType": "1"}

    def test_a_key_the_bus_never_carried_is_absent_not_guessed(self):
        # find_firmware reads an absent key as "no constraint"; inventing a
        # default would silently pick somebody else's firmware.
        assert self._bb({"GTW_chassisType": 2}).vehicle_conditions() == {
            "chassisType": "2"}

    def test_a_declared_value_wins_over_the_bus(self):
        # On a drive-unit bench vehicle_sim is the thing transmitting 0x7FF, so
        # reading it back is reading our own assertion; the operator's is the
        # deliberate one.
        bb = self._bb({"GTW_vdcType": 0})
        assert bb.vehicle_conditions({"vdcType": "1"})["vdcType"] == "1"

    def test_blank_declarations_are_ignored(self):
        bb = self._bb({"GTW_vdcType": 0})
        assert bb.vehicle_conditions({"vdcType": "", "other": None}) == {
            "vdcType": "0"}

    def test_declared_values_are_stringified(self):
        assert self._bb({}).vehicle_conditions({"vdcType": 1}) == {"vdcType": "1"}


class TestFlashArming:
    def test_flashing_is_disarmed_by_default(self):
        res = BenchBackend("chan").flash_module(update=("pmr",))
        assert res["exit_status"] == 1
        assert "not armed" in res["stderr"]

    def test_an_armed_backend_with_no_artifacts_says_so(self, tmp_path):
        bb = BenchBackend("chan", allow_flash=True, artifacts_dir=tmp_path / "nope")
        res = bb.flash_module(update=("pmr",), hwidacq=("pmr",))
        assert res["exit_status"] == 1
        assert "artifacts" in res["stderr"]

    def test_no_components_is_refused_not_reported_as_a_pass(self):
        res = BenchBackend("chan", allow_flash=True).flash_module(update=())
        assert res["exit_status"] == 1


class _FakeNotifier:
    """Stands in for the can.Notifier the RX cache already owns."""

    def __init__(self):
        self.listeners = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)


class TestHighRateLogging:
    """cid.StartHRL writes a real CAN log. On a car the gateway records the trace
    and ships it to Tesla; here it lands on disk and the upload step says where."""

    @staticmethod
    def _bb(tmp_path, notifier):
        bb = BenchBackend("vcan0", hrl_dir=tmp_path)
        # Pretend the RX cache already opened this channel, so hrl_start attaches
        # to the existing notifier instead of opening a second socket.
        bb._can["vcan0"] = (object(), notifier, object())
        return bb

    def test_capture_writes_a_log_and_rides_the_existing_notifier(self, tmp_path):
        notifier = _FakeNotifier()
        bb = self._bb(tmp_path, notifier)

        path = bb.hrl_start(timeout=300)

        assert path.parent == tmp_path
        assert path.name.startswith("hrl-vcan0-") and path.suffix == ".asc"
        assert len(notifier.listeners) == 1        # the logger, on the SAME notifier
        logger = notifier.listeners[0]
        logger.on_message_received(
            can.Message(arbitration_id=0x118, data=b"\x04\x00", is_extended_id=False))

        assert bb.hrl_stop() == path
        assert notifier.listeners == []            # detached again
        assert "118" in path.read_text()           # the frame really got written

    def test_a_second_start_does_not_open_a_second_capture(self, tmp_path):
        notifier = _FakeNotifier()
        bb = self._bb(tmp_path, notifier)
        first = bb.hrl_start()
        assert bb.hrl_start() == first
        assert len(notifier.listeners) == 1
        bb.hrl_stop()

    def test_upload_reports_the_path_and_does_not_claim_an_upload(self, tmp_path):
        notifier = _FakeNotifier()
        bb = self._bb(tmp_path, notifier)
        path = bb.hrl_start()

        res = bb.hrl_upload(hrl_type="gtw")

        assert res == {"path": str(path), "uploaded": False, "hrl_type": "gtw"}

    def test_stop_without_a_capture_is_not_an_error(self, tmp_path):
        assert self._bb(tmp_path, _FakeNotifier()).hrl_stop() is None
