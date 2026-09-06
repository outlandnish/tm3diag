"""Tests for scripts/odin_service.py -- the shared CLI/web core over the ODIN
engine + coverage.

Self-contained: every graph is synthetic, written into a tmp bundle laid out like
the real one (Model3/tasks entries + Model3/lib children), so nothing depends on
Tesla's ODIN bundle. Covers discovery (list_procedures: runnable vs blocked, both
TaskInfo title shapes) and a streamed run (run_procedure: on_event trace/metric/
done events + the RunResult-as-dict).
"""
from pathlib import Path

import odin_runner
import odin_service

from uds_local.node_config import NodeConfig
from uds_local.odj import FieldSpec, OdjEntry, SubSpec


def _write(bundle: Path, relbase: str, src: str) -> None:
    path = bundle / (relbase + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")


# A wired child graph using only handled node types (math.Multiply + SetOutput).
_CHILD = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "setout.set"}},
    "n": {"type": "networks.Input", "default": {"value": 0}},
    "mul": {"type": "math.Multiply", "a": {"connection": "n.value"}, "b": {"value": 2}},
    "setout": {"type": "networks.SetOutput", "key": "doubled",
               "value": {"connection": "mul.product"},
               "finished": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''

# Runnable entry: bare-task wrapper + a TaskInfo with the WRAPPED title shape
# ({'value': ...}) plus valid_states / principals.
_RUNNABLE = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "Model3/lib/dbl",
             "inputs": {"n": {"value": 5}},
             "outputs": {"exit_code": {"index": 0}, "doubled": {"index": 1}}},
    "info": {"type": "comments.TaskInfo", "title": {"value": "Double It"},
             "valid_states": ["StandStill|Parked"], "principals": ["tbx-internal"]},
}
'''

# Blocked entry: references an unhandled node type; TaskInfo uses the BARE title
# shape (a plain string), matching the real bundle.
_BLOCKED = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "x.run"}},
    "x": {"type": "custommod.CustomNode", "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
    "info": {"type": "comments.TaskInfo", "title": "Blocked One"},
}
'''

# Runnable entry with NO TaskInfo (metadata must degrade to None/empty).
_NOINFO = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''


def _make_bundle(tmp_path: Path) -> Path:
    _write(tmp_path, "Model3/lib/dbl", _CHILD)
    _write(tmp_path, "Model3/tasks/RUNNABLE-TASK", _RUNNABLE)
    _write(tmp_path, "Model3/tasks/BLOCKED-TASK", _BLOCKED)
    _write(tmp_path, "Model3/tasks/NOINFO-TASK", _NOINFO)
    return tmp_path


class TestListProcedures:
    def test_all_procs_annotated_with_runnable_and_metadata(self, tmp_path):
        procs = odin_service.list_procedures(bundle=_make_bundle(tmp_path),
                                             runnable_only=False)
        by_name = {p["name"]: p for p in procs}
        assert set(by_name) == {"RUNNABLE-TASK", "BLOCKED-TASK", "NOINFO-TASK"}

        run = by_name["RUNNABLE-TASK"]
        assert run["runnable"] is True
        assert run["missing_types"] == []
        assert run["basename"] == "Model3/tasks/RUNNABLE-TASK"
        assert run["title"] == "Double It"                 # wrapped {'value': ...}
        assert run["valid_states"] == ["StandStill|Parked"]
        assert run["principals"] == ["tbx-internal"]

    def test_blocked_proc_reports_missing_type_and_bare_title(self, tmp_path):
        procs = odin_service.list_procedures(bundle=_make_bundle(tmp_path),
                                             runnable_only=False)
        blocked = next(p for p in procs if p["name"] == "BLOCKED-TASK")
        assert blocked["runnable"] is False
        assert "custommod.CustomNode" in blocked["missing_types"]
        assert blocked["title"] == "Blocked One"           # bare-string title shape

    def test_no_taskinfo_degrades_gracefully(self, tmp_path):
        procs = odin_service.list_procedures(bundle=_make_bundle(tmp_path),
                                             runnable_only=False)
        noinfo = next(p for p in procs if p["name"] == "NOINFO-TASK")
        assert noinfo["runnable"] is True
        assert noinfo["title"] is None
        assert noinfo["valid_states"] == [] and noinfo["principals"] == []

    def test_runnable_only_filters_blocked(self, tmp_path):
        procs = odin_service.list_procedures(bundle=_make_bundle(tmp_path),
                                             runnable_only=True)
        names = {p["name"] for p in procs}
        assert names == {"RUNNABLE-TASK", "NOINFO-TASK"}   # BLOCKED-TASK dropped


# ---------------------------------------------------------------------------
# procedure_requirements -- "what must be on the bus before this proc runs"
# ---------------------------------------------------------------------------
# A lib graph exercising every extracted node kind: three CAN reads (read/monitor/
# compare) on ETH, one DYNAMIC (connection-sourced) signal, an alert inspection, an
# EnsureApplicationState (app-state precondition + a UDS target), a PowerContext, an
# odx UDS target, and a cid data-value read. None need to be *handled* -- the walk
# only visits node dicts.
_REQ_LIB = '''
network = {
    "read": {"type": "can.CANSignalRead",
             "signal_name": {"value": "GTW_drivetrainType"}, "bus_name": {"value": "ETH"}},
    "mon": {"type": "can.CANSignalMonitor",
            "signal_name": {"value": "DI_gear"}, "bus_name": {"value": "ETH"}},
    "cmp": {"type": "can.CANSignalValueComparison",
            "signal_name": {"value": "DIR_axleSpeed"}, "bus_name": {"value": "ETH"},
            "target": {"value": 560}, "comparator": {"value": 4}},
    "dyn": {"type": "can.CANSignalRead",
            "signal_name": {"connection": "src.out"}, "bus_name": {"value": "ETH"}},
    "def": {"type": "can.CANSignalRead",
            "signal_name": {"connection": "src.out", "value": "IBST_iBoosterStatus"},
            "bus_name": {"value": "ETH"}},
    "alert": {"type": "can.ActiveAlerts",
              "bus_name": {"value": "ETH"}, "prefix": {"value": "DI_a0"}},
    "boot": {"type": "vehiclecontrols.EnsureApplicationState",
             "node_name": {"value": "PMR"}, "application_state": {"value": "BOOTLOADER"}},
    "pwr": {"type": "vehiclecontrols.PowerContext", "power_state": {"value": "DRIVE"}},
    "uds": {"type": "odx.OdxStartRoutine", "node_name": {"value": "PMR"},
            "routine_name": {"value": "Foo"}},
    "cidv": {"type": "cid.GetDataValue", "data_name": {"value": "carVin"}},
}
'''
# Entry proc that references the lib (so the walk is transitive) + TaskInfo states.
_REQ_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "Model3/lib/reqlib"},
    "info": {"type": "comments.TaskInfo", "title": {"value": "Req Test"},
             "valid_states": ["Parked"]},
}
'''
# A read whose signal is literal but bus is absent -> grouped under the default bus.
_REQ_DEFBUS = '''
network = {
    "r": {"type": "can.CANSignalRead", "signal_name": {"value": "BMS_state"}},
}
'''


class TestProcedureRequirements:
    def _bundle(self, tmp_path):
        _write(tmp_path, "Model3/lib/reqlib", _REQ_LIB)
        _write(tmp_path, "Model3/tasks/REQ", _REQ_TASK)
        return tmp_path

    def test_groups_signals_by_bus_with_kind(self, tmp_path):
        req = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))
        assert req["basename"] == "Model3/tasks/REQ"
        # literal reads on ETH, sorted by (signal, kind); the pure-connection dyn read
        # is excluded, but the connection-with-default read IS enumerated at its default.
        assert req["signals"]["ETH"] == [
            {"signal": "DIR_axleSpeed", "kind": "compare"},
            {"signal": "DI_gear", "kind": "monitor"},
            {"signal": "GTW_drivetrainType", "kind": "read"},
            {"signal": "IBST_iBoosterStatus", "kind": "read"},   # from a conn+value default
        ]

    def test_dynamic_signal_counted_not_enumerated(self, tmp_path):
        req = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))
        assert req["dynamic_count"] == 1        # only the pure-connection read (no default)
        sigs = [s["signal"] for lst in req["signals"].values() for s in lst]
        assert "src.out" not in sigs            # the bare connection is never enumerated

    def test_alerts_and_uds_target_nodes(self, tmp_path):
        req = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))
        assert req["alerts"] == [{"bus": "ETH", "prefix": "DI_a0"}]
        assert req["nodes"] == ["PMR"]      # odx + EnsureApplicationState, de-duped

    def test_preconditions(self, tmp_path):
        pre = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))["preconditions"]
        assert pre["valid_states"] == ["Parked"]
        assert pre["application_state"] == "BOOTLOADER"
        assert pre["power_state"] == "DRIVE"
        assert pre["cid_values"] == ["carVin"]

    def test_absent_bus_falls_back_to_vehicle(self, tmp_path):
        _write(tmp_path, "Model3/tasks/DEFBUS", _REQ_DEFBUS)
        req = odin_service.procedure_requirements("Model3/tasks/DEFBUS", bundle=tmp_path)
        import config
        assert req["signals"][config.canonical_bus(None)] == [
            {"signal": "BMS_state", "kind": "read"}]

    def test_unknown_procedure_raises(self, tmp_path):
        import pytest
        with pytest.raises(FileNotFoundError):
            odin_service.procedure_requirements("Model3/tasks/NOPE", bundle=tmp_path)


# A tiny entry proc that captures one metric then exits 0.
_CAP_TASK = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "m"},
            "value": {"value": 42}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''


class TestRunProcedure:
    def test_returns_result_dict(self, tmp_path):
        _write(tmp_path, "Model3/tasks/CAP", _CAP_TASK)
        res = odin_service.run_procedure("Model3/tasks/CAP", backend="mock",
                                         bundle=tmp_path)
        assert res["exit_code"] == 0
        assert res["passed"] is True
        assert res["basename"] == "Model3/tasks/CAP"
        assert [m["value"] for m in res["metrics"]] == [42]

    def test_on_event_streams_trace_metric_and_done(self, tmp_path):
        _write(tmp_path, "Model3/tasks/CAP", _CAP_TASK)
        events: list[tuple] = []
        res = odin_service.run_procedure(
            "Model3/tasks/CAP", backend="mock", bundle=tmp_path,
            on_event=lambda kind, payload: events.append((kind, payload)))

        kinds = [k for k, _ in events]
        assert "trace" in kinds                            # node execution steps
        metrics = [p for k, p in events if k == "metric"]
        assert metrics and metrics[0]["metric"] == "m" and metrics[0]["value"] == 42
        # the terminal event carries the same dict run_procedure returns
        assert kinds[-1] == "done"
        assert events[-1][1] == res

    def test_passed_backend_instance_is_not_closed(self, tmp_path):
        _write(tmp_path, "Model3/tasks/CAP", _CAP_TASK)

        class _TrackingBackend(odin_runner.MockBackend):
            closed = False

            def close(self):
                self.closed = True

        be = _TrackingBackend("success")
        res = odin_service.run_procedure("Model3/tasks/CAP", backend=be,
                                         bundle=tmp_path)
        assert res["passed"] is True
        assert be.closed is False        # caller owns a passed-in backend instance

    def test_error_event_emitted_on_failure(self, tmp_path):
        # A missing basename makes Engine.run_procedure raise (file not found);
        # run_procedure must emit an 'error' event before propagating.
        events: list[tuple] = []
        raised = False
        try:
            odin_service.run_procedure(
                "Model3/tasks/DOES-NOT-EXIST", backend="mock", bundle=tmp_path,
                on_event=lambda kind, payload: events.append((kind, payload)))
        except Exception:  # noqa: BLE001
            raised = True
        assert raised
        assert events and events[-1][0] == "error"


# ---------------------------------------------------------------------------
# DID read/write helpers -- FakeSession + synthetic NodeConfig (no bench).
# ---------------------------------------------------------------------------
def _fs(bit_length, byte_position, bit_position=0, data_type="uint", enum=None):
    return FieldSpec(bit_length=bit_length, byte_position=byte_position,
                     bit_position=bit_position, data_type=data_type,
                     enum_map=enum or {})


_MODE = _fs(8, 0, 0, "uint", {"OFF": 0, "ON": 1})
# CFG: readable (sl0, MODE enum) + writable (sl5, MODE enum).
_CFG = OdjEntry(
    name="CFG", hex_id=0x0500,
    read=SubSpec(security_level=0, input={}, output={"MODE": _MODE},
                 input_size=0, output_size=1),
    write=SubSpec(security_level=5, input={"MODE": _MODE}, output={},
                  input_size=1, output_size=0))
# LOCK: read requires security level 3 (STATE byte).
_LOCK = OdjEntry(
    name="LOCK", hex_id=0x0600,
    read=SubSpec(security_level=3, input={}, output={"STATE": _fs(8, 0)},
                 input_size=0, output_size=1),
    write=None)
# SN: read-only ascii serial (sl0).
_SN = OdjEntry(
    name="SN", hex_id=0xF013,
    read=SubSpec(security_level=0, input={}, output_size=4, input_size=0,
                 output={"SN": _fs(32, 0, data_type="ascii")}),
    write=None)


def _did_cfg():
    return NodeConfig(name="DI", request_can_id=0x1, response_can_id=0x2,
                      security_algorithm="tesla_hash", security_buffer_size=16,
                      security_kw={}, dids={"CFG": _CFG, "LOCK": _LOCK, "SN": _SN})


class FakeSession:
    """Records UDS calls; returns canned bytes for the synthetic DIDs above."""

    def __init__(self, reads=None):
        self.calls = []
        self.reads = reads or {0x0500: b"\x01", 0x0600: b"\x07",
                               0xF013: b"ABCD"}

    def diagnostic_session(self, mode):
        self.calls.append(("diagnostic_session", mode))

    def security_access(self, level_idx=0, seed_level=None):
        self.calls.append(("security_access", level_idx, seed_level))

    def read_did(self, did):
        self.calls.append(("read_did", did))
        return self.reads.get(did, b"")

    def write_did(self, did, data):
        self.calls.append(("write_did", did, bytes(data)))


class TestListDids:
    def test_splits_readable_and_writable_with_metadata(self):
        dids = odin_service.list_dids(_did_cfg())
        assert {d["name"] for d in dids["read"]} == {"CFG", "LOCK", "SN"}
        assert {d["name"] for d in dids["write"]} == {"CFG"}    # only CFG is writable
        cfg_w = dids["write"][0]
        assert cfg_w["hex_id"] == "0x0500" and cfg_w["security_level"] == 5
        assert cfg_w["fields"] == ["MODE"]
        lock = next(d for d in dids["read"] if d["name"] == "LOCK")
        assert lock["security_level"] == 3


class TestReadDid:
    def test_decodes_enum_field(self):
        sess = FakeSession()
        res = odin_service.read_did(sess, _did_cfg(), "CFG")
        assert res["hex_id"] == "0x0500"
        assert res["fields"] == {"MODE": "ON"}    # 0x01 -> enum name
        assert res["raw"] == "01"
        assert ("read_did", 0x0500) in sess.calls

    def test_parsed_false_keeps_raw_number(self):
        res = odin_service.read_did(FakeSession(), _did_cfg(), "CFG", parsed=False)
        assert res["fields"] == {"MODE": 1}

    def test_read_runs_security_when_subspec_requires_it(self):
        sess = FakeSession()
        odin_service.read_did(sess, _did_cfg(), "LOCK")
        assert ("diagnostic_session", 0x02) in sess.calls
        assert ("security_access", 0, 3) in sess.calls    # seed_level = 3
        assert ("read_did", 0x0600) in sess.calls

    def test_resolves_by_hex_id(self):
        res = odin_service.read_did(FakeSession(), _did_cfg(), "0x0500")
        assert res["name"] == "CFG" and res["fields"] == {"MODE": "ON"}

    def test_raw_id_not_in_odj_returns_bytes_no_fields(self):
        sess = FakeSession(reads={0x1234: b"\xaa\xbb"})
        res = odin_service.read_did(sess, _did_cfg(), 0x1234)
        assert res["fields"] == {} and res["raw"] == "aabb"
        assert res["name"] == "0x1234"


class TestWriteDid:
    def test_encode_did_write_from_named_values(self):
        cfg = _did_cfg()
        assert odin_service.encode_did_write(cfg, "CFG", {"MODE": "ON"})[2] == b"\x01"
        assert odin_service.encode_did_write(cfg, "CFG", {"MODE": 1})[2] == b"\x01"

    def test_encode_did_write_raw_passthrough(self):
        cfg = _did_cfg()
        assert odin_service.encode_did_write(cfg, "CFG", "0a 0b")[2] == b"\x0a\x0b"
        assert odin_service.encode_did_write(cfg, "CFG", b"\xde\xad")[2] == b"\xde\xad"

    def test_write_encodes_runs_security_then_writes(self):
        sess = FakeSession()
        res = odin_service.write_did(sess, _did_cfg(), "CFG", {"MODE": "ON"})
        assert ("diagnostic_session", 0x02) in sess.calls
        assert ("security_access", 0, 5) in sess.calls     # write subspec sl = 5
        assert ("write_did", 0x0500, b"\x01") in sess.calls
        assert res["bytes"] == "01" and res["size"] == 1

    def test_security_can_be_skipped(self):
        sess = FakeSession()
        odin_service.write_did(sess, _did_cfg(), "CFG", {"MODE": "OFF"},
                               security=False)
        assert not any(c[0] == "security_access" for c in sess.calls)
        assert ("write_did", 0x0500, b"\x00") in sess.calls
