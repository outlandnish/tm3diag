"""Tests for scripts/odin_runner.py -- the ODIN node-graph interpreter.

Every graph here is synthetic, so nothing depends on Tesla's ODIN bundle.
"""
import base64
from pathlib import Path

import odin_runner
import pytest
from odin_runner import Backend, Engine, Frame, MockBackend


def _engine(bundle="."):
    return Engine(MockBackend("success"), Path(bundle))


def lit(v):
    """A literal data field: {'value': v}."""
    return {"value": v}


def conn(target):
    """A connection field: {'connection': 'node.port'}."""
    return {"connection": target}


def pull(node, extra=None, port="out"):
    """Build a one-node graph and pull the node's data handler."""
    graph = {"n": node}
    if extra:
        graph.update(extra)
    e = _engine()
    return e._pull(Frame(graph=graph, inputs={}, depth=0), conn(f"n.{port}"))


def run(graph, inputs=None):
    return _engine().run_graph(graph, inputs or {})


# Connection parser: node names never contain dots, so split on the first dot.
class TestConnectionParser:
    def test_two_dot_data_path_parses_node_then_portpath(self):
        graph = {
            "sub": {"type": "networks.ReferencedSubnetwork", "basename": "x",
                    "outputs": {"exit_code": {"index": 0}}},
        }
        e = _engine()
        frame = Frame(graph=graph, inputs={}, depth=0)
        frame.scratch["sub"] = odin_runner.RunResult(3, [], {})
        assert e._pull(frame, conn("sub.outputs.exit_code")) == 3


# Referenced subnetworks
_CHILD = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "setout.set"}},
    "n": {"type": "networks.Input", "default": {"value": 0}},
    "mul": {"type": "math.Multiply",
            "a": {"connection": "n.value"}, "b": {"value": 2}},
    "setout": {"type": "networks.SetOutput", "key": "doubled",
               "value": {"connection": "mul.product"},
               "finished": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"connection": "n.value"}},
}
'''

_BARE_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "child",
             "inputs": {"n": {"value": 5}},
             "outputs": {"exit_code": {"index": 0}, "doubled": {"index": 1}}},
    "info": {"type": "comments.TaskInfo", "title": {"value": "t"}},
}
'''

_WIRED = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "sub.slots.enter"}},
    "sub": {"type": "networks.ReferencedSubnetwork", "basename": "child",
            "inputs": {"n": {"value": 7}},
            "outputs": {"exit_code": {"index": 0}},
            "slots": {"enter": {"index": 0}},
            "signals": {"exit": {"index": 0, "connection": "cap.capture"}}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "code"},
            "value": {"connection": "sub.outputs.exit_code"},
            "result_code": {"value": 0}, "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''


def _bundle(tmp_path, **graphs):
    for stem, src in graphs.items():
        (tmp_path / f"{stem}.py").write_text(src, encoding="utf-8")
    return tmp_path


class TestReferencedSubnetwork:
    def test_bare_task_wrapper_resolves_inputs_and_outputs(self, tmp_path):
        _bundle(tmp_path, child=_CHILD, parent=_BARE_TASK)
        eng = Engine(MockBackend("success"), tmp_path)
        res = eng.run_procedure("parent")
        assert res.exit_code == 5           # child exits with exit_code = n
        assert res.outputs["doubled"] == 10  # SetOutput wrote n*2

    def test_inline_subnetwork_slots_signals_outputs(self, tmp_path):
        _bundle(tmp_path, child=_CHILD, parent=_WIRED)
        eng = Engine(MockBackend("success"), tmp_path)
        res = eng.run_procedure("parent")
        # signals.exit fired cap, which read sub.outputs.exit_code (=7) into a metric
        assert res.exit_code == 0
        assert [m["value"] for m in res.metrics] == [7]

    def test_child_metrics_bubble_up(self, tmp_path):
        child = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "inner"},
            "value": {"value": 42}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''
        _bundle(tmp_path, child=child, parent=_BARE_TASK)
        eng = Engine(MockBackend("success"), tmp_path)
        res = eng.run_procedure("parent")
        assert any(m["metric"] == "inner" and m["value"] == 42 for m in res.metrics)

    def test_none_input_binding_uses_child_default(self, tmp_path):
        # Binding a child input to None falls back to the child's Input default (ODIN: None == unset).
        child = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "inp": {"type": "networks.Input", "default": {"value": "VCLEFT"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "got"},
            "value": {"connection": "inp.value"}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''
        parent = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "child",
             "inputs": {"inp": {"value": None}}},
    "info": {"type": "comments.TaskInfo", "title": {"value": "t"}},
}
'''
        _bundle(tmp_path, child=child, parent=parent)
        res = Engine(MockBackend("success"), tmp_path).run_procedure("parent")
        assert res.metrics[0]["value"] == "VCLEFT"   # default, not None


class TestCompareOperator:
    def test_default_operator_is_equality(self):
        assert pull({"type": "logic.Compare", "a": lit(5), "b": lit(5)}) is True
        assert pull({"type": "logic.Compare", "a": lit(5), "b": lit(6)}) is False

    def test_not_equal(self):
        node = {"type": "logic.Compare", "a": lit(5), "b": lit(6), "operator": lit(1)}
        assert pull(node) is True

    def test_less_than(self):
        node = {"type": "logic.Compare", "a": lit(3), "b": lit(5), "operator": lit(2)}
        assert pull(node) is True
        node["a"] = lit(9)
        assert pull(node) is False


class TestGetItemListIndexing:
    def test_list_index(self):
        node = {"type": "collections.GetItem", "data": lit([10, 20, 30]), "key": lit(1)}
        assert pull(node) == 20

    def test_list_out_of_range_returns_default(self):
        node = {"type": "collections.GetItem", "data": lit([10]), "key": lit(9),
                "default": lit("d")}
        assert pull(node) == "d"

    def test_dict_still_works(self):
        node = {"type": "collections.GetItem", "data": lit({"k": 1}), "key": lit("k")}
        assert pull(node) == 1


class TestLogic:
    def test_is_in(self):
        assert pull({"type": "logic.IsIn", "a": lit("x"), "b": lit(["x", "y"])}) is True
        assert pull({"type": "logic.IsIn", "a": lit("z"), "b": lit(["x"])}) is False
        assert pull({"type": "logic.IsIn", "a": lit("z"), "b": lit(None)}) is False

    def test_is_not_in(self):
        assert pull({"type": "logic.IsNotIn", "a": lit("z"), "b": lit(["x"])}) is True

    def test_is_empty(self):
        assert pull({"type": "logic.IsEmpty", "a": lit([])}) is True
        assert pull({"type": "logic.IsEmpty", "a": lit(None)}) is True
        assert pull({"type": "logic.IsEmpty", "a": lit("x")}) is False

    def test_or_and_not(self):
        assert pull({"type": "logic.Or", "a": lit(False), "b": lit(3)}) == 3
        assert pull({"type": "logic.And", "a": lit(True), "b": lit(0)}) == 0
        assert pull({"type": "logic.Not", "a": lit(0)}) is True

    def test_is_none_nonzero(self):
        assert pull({"type": "logic.IsNone", "a": lit(None)}) is True
        assert pull({"type": "logic.IsNone", "a": lit(0)}) is False
        assert pull({"type": "logic.IsNonZero", "a": lit(0)}) is False
        assert pull({"type": "logic.IsNonZero", "a": lit(b"\x01")}) is True

    def test_between(self):
        node = {"type": "logic.Between", "a": lit(5), "low": lit(1), "high": lit(10)}
        assert pull(node) is True
        node["a"] = lit(11)
        assert pull(node) is False

    def test_less_than(self):
        assert pull({"type": "logic.LessThan", "a": lit(1), "b": lit(2)}) is True

    def test_multi_and_or(self):
        inputs = {"x": {"index": 0, "value": True}, "y": {"index": 1, "value": False}}
        assert pull({"type": "logic.MultiAndOr", "operator": lit("and"),
                     "inputs": inputs}) is False
        assert pull({"type": "logic.MultiAndOr", "operator": lit("or"),
                     "inputs": inputs}) is True


class TestDictsCollectionsLists:
    def test_set_item_immutable(self):
        base = {"a": 1}
        out = pull({"type": "dicts.SetItem", "data": lit(base),
                    "key": lit("b"), "value": lit(2)})
        assert out == {"a": 1, "b": 2}
        assert base == {"a": 1}  # original untouched

    def test_set_item_without_data(self):
        out = pull({"type": "dicts.SetItem", "key": lit("k"), "value": lit(9)})
        assert out == {"k": 9}

    def test_keys_values_merge_haskey(self):
        d = lit({"a": 1, "b": 2})
        assert pull({"type": "dicts.Keys", "data": d}) == ["a", "b"]
        assert pull({"type": "dicts.Values", "data": d}) == [1, 2]
        assert pull({"type": "dicts.Merge", "a": lit({"a": 1}), "b": lit({"b": 2})}) \
            == {"a": 1, "b": 2}
        assert pull({"type": "dicts.HasKey", "data": d, "key": lit("a")}) is True
        assert pull({"type": "dicts.HasKey", "data": d, "key": lit("z")}) is False

    def test_len_sort(self):
        assert pull({"type": "collections.Len", "data": lit([1, 2, 3])}) == 3
        assert pull({"type": "collections.Len", "data": lit(None)}) == 0
        assert pull({"type": "collections.Sort", "data": lit([3, 1, 2])}) == [1, 2, 3]

    def test_append_extend_any_splice(self):
        assert pull({"type": "lists.Append", "items": lit([1]), "value": lit(2)}) == [1, 2]
        assert pull({"type": "lists.Append", "value": lit("x")}) == ["x"]
        assert pull({"type": "lists.Extend", "a": lit([1]), "b": lit([2, 3])}) == [1, 2, 3]
        assert pull({"type": "lists.Any", "items": lit([0, "", 5])}) is True
        assert pull({"type": "lists.Any", "items": lit([0, ""])}) is False
        assert pull({"type": "lists.Splice", "items": lit([1, 2, 3, 4]),
                     "start": lit(1), "end": lit(3)}) == [2, 3]

    def test_math(self):
        assert pull({"type": "math.Add", "a": lit(2), "b": lit(3)}) == 5
        assert pull({"type": "math.Subtract", "a": lit(5), "b": lit(2)}) == 3
        assert pull({"type": "math.Multiply", "a": lit(4), "b": lit(3)}) == 12
        assert pull({"type": "math.Divide", "a": lit(9), "b": lit(3)}) == 3
        assert pull({"type": "math.Mod", "x": lit(7), "divisor": lit(3)}) == 1


class TestStringsBytesMisc:
    def test_format(self):
        node = {"type": "strings.Format", "text": lit("{a}/{b}"),
                "options": lit({"a": "x", "b": "y"})}
        assert pull(node) == "x/y"

    def test_split_join(self):
        assert pull({"type": "strings.Split", "text": lit("a-b-c"),
                     "separator": lit("-")}) == ["a", "b", "c"]
        assert pull({"type": "strings.Join", "items": lit(["a", "b"]),
                     "joiner": lit(",")}) == "a,b"

    def test_substring_strip(self):
        assert pull({"type": "strings.Substring", "string": lit("abcdef"),
                     "start": lit(1), "end": lit(3)}) == "bc"
        assert pull({"type": "strings.Rstrip", "str": lit("hi \n")}) == "hi"
        assert pull({"type": "strings.Strip", "str": lit("  hi  ")}) == "hi"
        assert pull({"type": "strings.Splitlines", "text": lit("a\nb")}) == ["a", "b"]

    def test_case(self):
        assert pull({"type": "strings.Case", "text": lit("Ab"), "case": lit(0)}) == "ab"
        assert pull({"type": "strings.Case", "text": lit("Ab"), "case": lit(1)}) == "AB"

    def test_bytes_append_encode_decode(self):
        assert pull({"type": "bytes.EncodeToBytes", "utf8_chars": lit("AB")}) == b"AB"
        assert pull({"type": "bytes.DecodeToString", "value": lit(b"AB")}) == "AB"

    def test_json_regex_sets(self):
        assert pull({"type": "json.Dumps", "json": lit({"a": 1})}) == '{"a": 1}'
        assert pull({"type": "json.Loads", "string": lit('{"a": 1}')}) == {"a": 1}
        assert pull({"type": "regex.Findall", "pattern": lit(r"\d+"),
                     "data": lit("a1b22")}) == ["1", "22"]
        assert set(pull({"type": "sets.Intersection", "a": lit([1, 2, 3]),
                         "b": lit([2, 3, 4])})) == {2, 3}
        assert set(pull({"type": "sets.Difference", "a": lit([1, 2, 3]),
                         "b": lit([2, 3])})) == {1}

    def test_sanitize_string(self):
        ok = {"type": "misc.SanitizeString", "input_text": lit("abc123"),
              "digits": lit(True), "ascii_letters": lit(True)}
        assert pull(ok) == "abc123"
        bad = {"type": "misc.SanitizeString", "input_text": lit("a b"),
               "ascii_letters": lit(True), "value_error_message": lit("nope")}
        with pytest.raises(ValueError, match="nope"):
            pull(bad)

    def test_variant_to_number(self):
        assert pull({"type": "types.VariantToNumber", "value": lit("42")}) == 42
        assert pull({"type": "types.VariantToNumber", "value": lit("3.5")}) == 3.5


def _cap(metric, value, done=None):
    node = {"type": "reporting.CaptureMetric", "metric_name": lit(metric),
            "value": value, "result_code": lit(0)}
    if done:
        node["done"] = done
    return node


class TestControlSwitch:
    def test_data_ternary(self):
        node = {"type": "control.Switch", "expr": lit(True),
                "if_true": lit("T"), "if_false": lit("F")}
        assert pull(node) == "T"
        node["expr"] = lit(False)
        assert pull(node) == "F"


class TestIteration:
    def test_for_each_binds_item_per_iteration(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fe.run")},
            "fe": {"type": "control.ForEach", "items": lit([1, 2, 3]),
                   "run_each": conn("cap.capture"), "done": conn("exit.exit")},
            "cap": _cap("m", conn("fe.item")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert res.exit_code == 0
        assert [m["value"] for m in res.metrics] == [1, 2, 3]

    def test_for_each_entry_binds_key_and_value(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fee.run")},
            "fee": {"type": "control.ForEachEntry", "data": lit({"a": 1, "b": 2}),
                    "run_each": conn("cap.capture"), "done": conn("exit.exit")},
            "cap": {"type": "reporting.CaptureMetric",
                    "metric_name": conn("fee.key"), "value": conn("fee.value"),
                    "result_code": lit(0)},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        got = {m["metric"]: m["value"] for m in res.metrics}
        assert got == {"a": 1, "b": 2}

    def test_for_loop_counts(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fl.run")},
            "fl": {"type": "control.ForLoop", "end": lit(3),
                   "run_each": conn("cap.capture"), "done": conn("exit.exit")},
            "cap": _cap("m", conn("fl.i")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert [m["value"] for m in res.metrics] == [0, 1, 2]

    def test_timeout_loop_condition_met_takes_passed(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("tl.run")},
            "tl": {"type": "control.TimeoutLoop", "timeout": lit(0),
                   "run_body": conn("noop.capture"), "condition": lit(True),
                   "passed": conn("ok.exit"), "timed_out": conn("bad.exit")},
            "noop": _cap("body", lit(1)),
            "ok": {"type": "networks.Exit", "exit_code": lit(0)},
            "bad": {"type": "networks.Exit", "exit_code": lit(1)},
        }
        assert run(graph).exit_code == 0

    def test_timeout_loop_condition_false_times_out(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("tl.run")},
            "tl": {"type": "control.TimeoutLoop", "timeout": lit(0),
                   "run_body": conn("noop.capture"), "condition": lit(False),
                   "passed": conn("ok.exit"), "timed_out": conn("bad.exit")},
            "noop": _cap("body", lit(1)),
            "ok": {"type": "networks.Exit", "exit_code": lit(0)},
            "bad": {"type": "networks.Exit", "exit_code": lit(1)},
        }
        assert run(graph).exit_code == 1


class TestCounter:
    def test_counter_increments_on_each_fire(self):
        # fire the counter from a ForLoop body 3x, then read its count into a metric.
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fl.run")},
            "fl": {"type": "control.ForLoop", "end": lit(3),
                   "run_each": conn("ctr.increment"), "done": conn("cap.capture")},
            "ctr": {"type": "control.Counter", "updated": None},
            "cap": _cap("count", conn("ctr.count"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert [m["value"] for m in res.metrics] == [3]


class TestTryExcept:
    def test_exception_routes_to_except_body(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("te.run")},
            "te": {"type": "control.TryExcept", "exception_class": lit("RuntimeError"),
                   "try_body": conn("boom.capture"),
                   "except_body": conn("caught.capture"),
                   "finally_body": conn("exit.exit")},
            "boom": _cap("boom", conn("div.result")),   # 1/0 raises inside the pull
            "div": {"type": "math.Divide", "a": lit(1), "b": lit(0)},
            "caught": _cap("caught", lit("ok")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert [m["metric"] for m in res.metrics] == ["caught"]

    def test_no_exception_runs_else_body(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("te.run")},
            "te": {"type": "control.TryExcept", "exception_class": lit("RuntimeError"),
                   "try_body": conn("ok.capture"), "else_body": conn("els.capture"),
                   "except_body": conn("caught.capture"),
                   "finally_body": conn("exit.exit")},
            "ok": _cap("ok", lit(1)),
            "els": _cap("else", lit(1)),
            "caught": _cap("caught", lit(1)),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        metrics = [m["metric"] for m in run(graph).metrics]
        assert metrics == ["ok", "else"]  # except_body never ran


class TestVehicleControls:
    def test_power_context_runs_body_then_done(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("pc.run")},
            "pc": {"type": "vehiclecontrols.PowerContext", "power_state": lit("DRIVE"),
                   "body": conn("body.capture"), "failure": conn("fail.capture"),
                   "done": conn("exit.exit")},
            "body": _cap("body", lit(1)),
            "fail": _cap("failure", lit(1)),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert res.exit_code == 0
        assert [m["metric"] for m in res.metrics] == ["body"]  # failure not fired

    def test_ensure_states_fire_done(self):
        for ntype, extra in (
            ("vehiclecontrols.EnsureApplicationState",
             {"node_name": lit("DI"), "application_state": lit("APPLICATION")}),
            ("vehiclecontrols.EnsurePowerState", {"power_state": lit("OFF")}),
            ("vehiclecontrols.McuScreenOn", {}),
        ):
            graph = {
                "enter": {"type": "networks.Enter", "start": conn("n.run")},
                "n": {"type": ntype, "done": conn("exit.exit"), **extra},
                "exit": {"type": "networks.Exit", "exit_code": lit(0)},
            }
            assert run(graph).exit_code == 0, ntype


class TestStep6Logic:
    def test_from_inputs_is_ordered_list(self):
        node = {"type": "dicts.FromInputs", "a": lit(1), "c": lit(3), "b": lit(2)}
        assert pull(node) == [1, 2, 3]   # ordered by letter, not insertion

    def test_base64(self):
        enc = pull({"type": "bytes.Base64Encode", "value": lit(b"AB")})
        assert enc == base64.b64encode(b"AB")
        assert pull({"type": "bytes.Base64Decode", "value": lit(enc)}) == b"AB"

    def test_random_bytes_length(self):
        assert len(pull({"type": "bytes.RandomBytes", "n": lit(8)})) == 8

    def test_bytes_to_int(self):
        assert pull({"type": "can.BytesToInt", "bytes": lit(b"\x01\x00")}) == 256
        assert pull({"type": "can.BytesToInt", "bytes": lit(5)}) == 5   # int passthrough

    def test_messages_fire_done(self):
        for ntype, extra in (
            ("messages.ProgressUpdate", {"value": lit(50)}),
            ("messages.StatusUpdate", {"status": lit("working")}),
            ("messages.Listen", {"message_type": lit("stop_now")}),
        ):
            graph = {
                "enter": {"type": "networks.Enter", "start": conn("n.run")},
                "n": {"type": ntype, "done": conn("exit.exit"), **extra},
                "exit": {"type": "networks.Exit", "exit_code": lit(0)},
            }
            assert run(graph).exit_code == 0, ntype


class _CanBackend(Backend):
    """can_read returns a constant, or pops successive values from a list."""

    def __init__(self, values):
        self._v = values

    def can_read(self, signal, bus=None):
        val = self._v.get(signal)
        if isinstance(val, list):
            return val.pop(0) if val else None
        return val


class TestLiveCan:
    def test_signal_read(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("cap.capture")},
            "cap": _cap("v", conn("csr.value"), done=conn("exit.exit")),
            "csr": {"type": "can.CANSignalRead", "signal_name": lit("X"),
                    "bus_name": lit("ETH")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        eng = Engine(_CanBackend({"X": 42}), Path("."), time_scale=0.0)
        assert eng.run_graph(graph, {}).metrics[0]["value"] == 42

    def test_monitor_change_fires_value_changed(self):
        graph = self._monitor_graph()
        eng = Engine(_CanBackend({"X": [1, 2]}), Path("."), time_scale=0.0)
        assert eng.run_graph(graph, {}).exit_code == 0   # value_changed -> exit 0

    def test_monitor_no_change_times_out(self):
        graph = self._monitor_graph()
        eng = Engine(_CanBackend({"X": [1, 1]}), Path("."), time_scale=0.0)
        assert eng.run_graph(graph, {}).exit_code == 1   # timed_out -> exit 1

    @staticmethod
    def _monitor_graph():
        return {
            "enter": {"type": "networks.Enter", "start": conn("mon.run")},
            "mon": {"type": "can.CANSignalMonitor", "signal_name": lit("X"),
                    "bus_name": lit("ETH"), "timeout": lit(0), "enabled": lit(True),
                    "value_changed": conn("chg.exit"), "timed_out": conn("to.exit")},
            "chg": {"type": "networks.Exit", "exit_code": lit(0)},
            "to": {"type": "networks.Exit", "exit_code": lit(1)},
        }


class TestInlineSubnetwork:
    def test_enter_exit_style_inputs_outputs_signal(self):
        # Inline Enter/Exit subnet: inner connections carry the 'sub.' prefix; the parent
        # continues via signals.exit after the child completes.
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("sub.slots.enter")},
            "seed": {"type": "constant.Constant", "value": lit(3)},
            "sub": {
                "type": "networks.Subnetwork",
                "slots": {"enter": {"index": 0}},
                "signals": {"exit": {"index": 0, "connection": "cap.capture"}},
                "inputs": {"n": {"connection": "seed.out"}},
                "outputs": {"exit_code": {"index": 0}, "tripled": {"index": 1}},
                "enter": {"type": "networks.Enter",
                          "start": {"connection": "sub.setout.set"}},
                "n": {"type": "networks.Input", "default": {"value": 0}},
                "mul": {"type": "math.Multiply", "a": {"connection": "sub.n.value"},
                        "b": {"value": 3}},
                "setout": {"type": "networks.SetOutput", "key": "tripled",
                           "value": {"connection": "sub.mul.product"},
                           "finished": {"connection": "sub.exit.exit"}},
                "exit": {"type": "networks.Exit",
                         "exit_code": {"connection": "sub.n.value"}},
            },
            "cap": _cap("t", conn("sub.outputs.tripled"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit",
                     "exit_code": conn("sub.outputs.exit_code")},
        }
        res = run(graph)
        assert res.exit_code == 3                     # inner exit_code = n
        assert [m["value"] for m in res.metrics] == [9]   # tripled = n*3

    def test_slot_signal_style_runs_inner_and_fires_exit(self):
        # Inline Slot/Signal subnet: networks.Slot is the entry relay, networks.Signal the exit relay.
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("sub.slots.enter")},
            "sub": {
                "type": "networks.Subnetwork",
                "slots": {"enter": {"index": 0}},
                "signals": {"exit": {"index": 0, "connection": "cap.capture"}},
                "slot": {"type": "networks.Slot",
                         "signal": {"connection": "sub.body.capture"}},
                "body": {"type": "reporting.CaptureMetric",
                         "metric_name": {"value": "inner"}, "value": {"value": 7},
                         "result_code": {"value": 0},
                         "done": {"connection": "sub.signal.emit"}},
                "signal": {"type": "networks.Signal"},
            },
            "cap": _cap("outer", lit("ok"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert res.exit_code == 0
        assert [m["metric"] for m in res.metrics] == ["inner", "outer"]


class TestControlFlowStep7:
    def test_break_stops_the_enclosing_loop(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fe.run")},
            "fe": {"type": "control.ForEach", "items": lit([1, 2, 3, 4]),
                   "run_each": conn("chk.run"), "done": conn("exit.exit")},
            "chk": {"type": "control.IfThen", "expr": conn("eq.result"),
                    "if_true": conn("brk.stop"), "if_false": conn("cap.capture")},
            "eq": {"type": "logic.Compare", "a": conn("fe.item"), "b": lit(3)},
            "brk": {"type": "control.Break"},
            "cap": _cap("seen", conn("fe.item")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert res.exit_code == 0
        assert [m["value"] for m in res.metrics] == [1, 2]  # 3 breaks, 4 unseen

    def test_multisplit_multimerge_join(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("ms.run")},
            "ms": {"type": "control.MultiSplit", "branches": {
                "A": {"index": 0, "connection": "capA.capture"},
                "B": {"index": 1, "connection": "capB.capture"}}},
            "capA": _cap("A", lit(1), done=conn("mm.dependencies.A")),
            "capB": _cap("B", lit(1), done=conn("mm.dependencies.B")),
            "mm": {"type": "control.MultiMerge",
                   "dependencies": {"A": {"index": 0}, "B": {"index": 1}},
                   "done": conn("final.capture")},
            "final": _cap("joined", lit(1), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert res.exit_code == 0
        assert [m["metric"] for m in res.metrics] == ["A", "B", "joined"]

    def test_merge_continues_on_first_arrival(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("ift.run")},
            "ift": {"type": "control.IfThen", "expr": lit(True),
                    "if_true": conn("mg.first"), "if_false": conn("mg.second")},
            "mg": {"type": "control.Merge", "done": conn("cap.capture")},
            "cap": _cap("merged", lit(1), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        assert [m["metric"] for m in res.metrics] == ["merged"]


class TestAccumulators:
    def test_append_output_builds_a_list(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fl.run")},
            "fl": {"type": "control.ForLoop", "end": lit(3),
                   "run_each": conn("ao.append"), "done": conn("exit.exit")},
            "ao": {"type": "networks.AppendOutput", "key": "items",
                   "value": conn("fl.i")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert run(graph).outputs["items"] == [0, 1, 2]

    def test_for_accumulate_collects_then_reads_results(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("fl.run")},
            "fl": {"type": "control.ForLoop", "end": lit(3),
                   "run_each": conn("fa.run"), "done": conn("cap.capture")},
            "fa": {"type": "control.ForAccumulate", "value": conn("fl.i")},
            "cap": _cap("acc", conn("fa.results"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert run(graph).metrics[0]["value"] == [0, 1, 2]


class TestLogicTail:
    def test_datetime_is_iso_string(self):
        v = pull({"type": "misc.DateTime"}, port="now")
        assert isinstance(v, str) and "T" in v

    def test_uuid_is_uuid_string(self):
        v = pull({"type": "misc.Uuid"}, port="uuid")
        assert isinstance(v, str) and len(v) == 36

    def test_series_sum_and_abs(self):
        assert pull({"type": "math.SeriesSum", "series": lit([1, 2, 3])},
                    port="sum") == 6
        assert pull({"type": "math.SeriesSum", "series": lit(None)}, port="sum") == 0
        assert pull({"type": "math.Abs", "value": lit(-5)}, port="result") == 5

    def test_active_alerts_default_empty(self):
        assert pull({"type": "can.ActiveAlerts", "bus_name": lit("ETH")},
                    port="alerts") == []


class TestInteropTail:
    def test_capture_connector_info_lookup_records_metric(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("cci.capture")},
            "cci": {"type": "reporting.CaptureConnectorInfoLookup",
                    "file_name": lit("tuner.json"), "exit_code": lit(0),
                    "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = run(graph)
        m = res.metrics[0]
        assert m["metric"] == "ConnectorInfoLookup"
        assert m["value"] == "tuner.json" and m["result_code"] == 0

    def test_save_authored_popup_stores_and_reports_success(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("sp.save")},
            "sp": {"type": "cid.SaveAuthoredPopup", "identifier": lit("id1"),
                   "data": lit("blob"), "done": conn("cap.capture")},
            "cap": _cap("ok", conn("sp.success"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        backend = MockBackend("success")
        res = Engine(backend, Path(".")).run_graph(graph, {})
        assert res.metrics[0]["value"] is True
        assert backend.cid_load("id1") == "blob"

    def test_messages_send_fires_done(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("snd.send")},
            "snd": {"type": "messages.Send", "payload": lit("X"),
                    "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert run(graph).exit_code == 0

    @staticmethod
    def _v2_graph():
        return {
            "enter": {"type": "networks.Enter", "start": conn("v2.run")},
            "v2": {"type": "odx.OdxStartAndWaitResults_V2", "node_name": lit("RADC"),
                   "routine_name": lit("R"), "status_parameter": lit("ST"),
                   "in_progress_statuses": lit(["BUSY"]), "max_runtime": lit(0),
                   "success": conn("ok.exit"), "failed": conn("bad.exit")},
            "ok": {"type": "networks.Exit", "exit_code": lit(0)},
            "bad": {"type": "networks.Exit", "exit_code": lit(1)},
        }

    def test_odx_v2_success_when_status_leaves_in_progress(self):
        assert Engine(_V2Backend("DONE"), Path(".")).run_graph(
            self._v2_graph(), {}).exit_code == 0

    def test_odx_v2_failed_when_still_in_progress(self):
        assert Engine(_V2Backend("BUSY"), Path(".")).run_graph(
            self._v2_graph(), {}).exit_code == 1

    def test_uds_io_control_routes_to_backend(self):
        backend = _IOBackend()
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("io.run")},
            "io": {"type": "uds.UdsIOControl", "node_name": lit("VCSEC"),
                   "control_id": lit("0x382"), "control_type": lit("SHORT_TERM_ADJUST"),
                   "input_payload": lit("0101"), "done": conn("cap.capture")},
            "cap": _cap("iodata", conn("io.data"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = Engine(backend, Path(".")).run_graph(graph, {})
        assert backend.calls == [("VCSEC", "0x382", "SHORT_TERM_ADJUST", "0101")]
        assert res.metrics[0]["value"] == b"\x01"


class _V2Backend(Backend):
    """odx.start_and_wait returns a fixed final status for the V2 branch test."""

    def __init__(self, final_status):
        self._final = final_status

    def odx(self, node_name):
        return self

    def start_and_wait(self, routine, status_param, in_progress, timeout, **_kw):
        return {status_param: self._final}


class _IOBackend(Backend):
    """Records uds.io_control calls and returns a canned response."""

    def __init__(self):
        self.calls = []

    def uds(self, node_name):
        outer = self

        class _A:
            def io_control(self, control_id, control_type=None, payload=None):
                outer.calls.append((node_name, control_id, control_type, payload))
                return b"\x01"

        return _A()


_SCRIPT = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "scripted"},
            "value": {"value": 99}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''

_SCRIPT_TASK = '''
network = {
    "task": {"type": "scripts.RunScriptTest", "script_name": "myscript"},
    "info": {"type": "comments.TaskInfo", "title": {"value": "t"}},
}
'''

_DYN_TASK = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "dyn.run"}},
    "which": {"type": "constant.Constant", "value": {"value": "child"}},
    "dyn": {"type": "networks.DynamicallyReferencedSubnetwork",
            "name": {"connection": "which.out"},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''

_DYN_CHILD = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "dynran"},
            "value": {"value": 5}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
'''


class TestScriptAndDynamicSubnet:
    def test_run_script_test_entry_runs_the_script_graph(self, tmp_path):
        _bundle(tmp_path, myscript=_SCRIPT, parent=_SCRIPT_TASK)
        res = Engine(MockBackend("success"), tmp_path).run_procedure("parent")
        assert any(m["metric"] == "scripted" and m["value"] == 99
                   for m in res.metrics)

    def test_dynamic_subnetwork_resolves_basename_at_runtime(self, tmp_path):
        _bundle(tmp_path, child=_DYN_CHILD, parent=_DYN_TASK)
        res = Engine(MockBackend("success"), tmp_path).run_procedure("parent")
        assert res.exit_code == 0
        assert any(m["metric"] == "dynran" for m in res.metrics)


class _IsotpBackend(Backend):
    """Records isotp_send calls and returns a fixed success flag."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def isotp_send(self, to_controller, from_controller, data, bus=None):
        self.sent.append((to_controller, from_controller, data, bus))
        return self.ok


class TestIsotpSend:
    @staticmethod
    def _graph():
        return {
            "enter": {"type": "networks.Enter", "start": conn("snd.transmit")},
            "snd": {"type": "isotp.Send", "to_controller": lit("0x480"),
                    "from_controller": lit(953), "data": lit("dead"),
                    "done": conn("cap.capture")},
            "cap": _cap("ok", conn("snd.success"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }

    def test_routes_ids_and_data_to_backend(self):
        backend = _IsotpBackend(ok=True)
        res = Engine(backend, Path(".")).run_graph(self._graph(), {})
        # to_controller '0x480' is coerced to int; from_controller passed through.
        assert backend.sent == [(0x480, 953, "dead", None)]
        assert res.metrics[0]["value"] is True

    def test_failure_is_reported_via_success_port(self):
        backend = _IsotpBackend(ok=False)
        res = Engine(backend, Path(".")).run_graph(self._graph(), {})
        assert res.metrics[0]["value"] is False

    def test_default_backend_reports_success(self):
        assert Backend().isotp_send(0x480, 953, b"\x01") is True


# ---------------------------------------------------------------------------
# The node types the native-script work also needed on the GRAPH side:
# reporting.ServiceOutput (the wired twin of a script's returned verdict),
# scripts.ScriptTest, cid.IsFused/SetFactoryMode/GetPlatform/SetVehicleConfig,
# cid.Start/StopHRL (a real bus capture), misc.WhitelistDict, enum.EnumInput,
# control.ConcurrentSplit, testing.PingOutcome, reporting.FileOutput.
# ---------------------------------------------------------------------------

class TestServiceOutputNode:
    @staticmethod
    def _graph(reason):
        return {
            "enter": {"type": "networks.Enter", "start": conn("so.run")},
            "reason": {"type": "reporting.ServiceOutputExitReason"},
            "so": {"type": "reporting.ServiceOutput",
                   "user_facing_msg": lit("data already present"),
                   "exit_code": conn(f"reason.{reason}"),
                   "data": {"Detail": lit({"k": 1})}},
        }

    def test_terminal_node_ends_the_graph_with_its_reason(self):
        res = run(self._graph("INVALID_INPUT"))
        assert res.exit_code == odin_runner.odin_script_api.ExitReason.INVALID_INPUT
        assert res.exit_code != 0
        assert res.outputs["service_output"]["user_facing_msg"] == "data already present"
        assert res.outputs["service_output"]["data"] == [
            {"name": "Detail", "type": None, "data": {"k": 1}}]

    def test_a_pass_verdict_is_exit_code_zero(self):
        assert run(self._graph("PASS")).exit_code == 0

    def test_an_unknown_member_reads_as_unknown(self):
        assert pull({"type": "reporting.ServiceOutputExitReason"}, port="NOPE") == \
            odin_runner.odin_script_api.ExitReason.UNKNOWN


class _FusedBackend(Backend):
    """A CID store just deep enough for the is-fused / factory-mode pair."""

    def __init__(self, **values):
        self.values = dict(values)

    def cid_get(self, name):
        return self.values.get(name)

    def cid_set(self, name, value):
        self.values[name] = value

    def cid_vitals(self):
        return {"info_hw": "infoz", "info_sw": "2022.45.15"}


class TestOperatorDeclaredCidState:
    def test_is_fused_reads_the_declared_value(self):
        # No bus reading establishes this, so the operator declares it (the web
        # UI's bench-state toggle writes GUI_isFused).
        for declared, expected in (("true", True), ("false", False)):
            e = Engine(_FusedBackend(GUI_isFused=declared), Path("."))
            frame = Frame(graph={"n": {"type": "cid.IsFused"}}, inputs={}, depth=0)
            assert e._pull(frame, conn("n.is_fused")) is expected

    def test_set_factory_mode_writes_the_cid_value(self):
        backend = _FusedBackend()
        Engine(backend, Path(".")).run_graph({
            "enter": {"type": "networks.Enter", "start": conn("fm.run")},
            "fm": {"type": "cid.SetFactoryMode", "factory_mode_state": lit(True),
                   "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }, {})
        assert backend.values["GUI_factoryMode"] == "true"

    def test_get_platform_comes_from_vitals(self):
        e = Engine(_FusedBackend(), Path("."))
        frame = Frame(graph={"n": {"type": "cid.GetPlatform"}}, inputs={}, depth=0)
        assert e._pull(frame, conn("n.info"))["info_hw"] == "infoz"

    def test_set_vehicle_config_records_the_write(self):
        backend = _FusedBackend()
        Engine(backend, Path(".")).run_graph({
            "enter": {"type": "networks.Enter", "start": conn("cfg.run")},
            "cfg": {"type": "cid.SetVehicleConfig", "configid": lit(15),
                    "data": lit("on"), "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }, {})
        assert backend.values["config_15"] == "on"


class _HrlBackend(Backend):
    """Records the high-rate-logging calls and hands back a canned path."""

    def __init__(self):
        self.calls = []

    def hrl_start(self, timeout=None, path=None):
        self.calls.append(("start", timeout))
        return "/tmp/hrl-vcan0.asc"

    def hrl_stop(self):
        self.calls.append(("stop", None))
        return "/tmp/hrl-vcan0.asc"

    def hrl_upload(self, hrl_type=None):
        self.calls.append(("upload", hrl_type))
        return {"path": "/tmp/hrl-vcan0.asc", "uploaded": False}


class TestHighRateLogging:
    def test_start_stop_upload_capture_to_a_file_and_report_the_path(self):
        backend = _HrlBackend()
        res = Engine(backend, Path(".")).run_graph({
            "enter": {"type": "networks.Enter", "start": conn("start.run")},
            "start": {"type": "cid.StartHRL", "timeout": lit(300),
                      "done": conn("stop.run")},
            "stop": {"type": "cid.StopHRL", "done": conn("up.run")},
            "up": {"type": "cid.StartHrlUploadService", "hrl_type": lit("gtw"),
                   "done": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }, {})
        assert backend.calls == [("start", 300), ("stop", None), ("upload", "gtw")]
        # The capture is a real local file, so the run says where it landed
        # rather than claiming an upload that has nowhere to go.
        assert res.outputs["hrl_logs"] == ["/tmp/hrl-vcan0.asc"]

    def test_default_backend_has_nothing_to_capture(self):
        assert Backend().hrl_start() is None
        assert Backend().hrl_stop() is None
        assert Backend().hrl_upload() == {"path": None, "uploaded": False}


class TestSmashclicker:
    """The MCU's ECU flasher, which all 55 UPDATE_* procedures reach through
    Gen3/scripts/UPDATE_MODULE. Its answer is parsed by the procedure itself, so
    the shape matters as much as the verdict."""

    ARGS = ["-h", "pmr", "-u", "pmr,dir", "-t", "+^=", "-j", "1234"]

    def test_args_are_parsed_the_way_update_module_writes_them(self):
        assert odin_runner._parse_smashclicker(self.ARGS) == {
            "update": ("pmr", "dir"), "hwidacq": ("pmr",),
            "job_id": "1234", "can_quiet": True,
        }

    def test_the_quiet_flag_is_optional(self):
        parsed = odin_runner._parse_smashclicker(["-h", "pcs", "-u", "pcs,pcscpu2",
                                                  "-j", "7"])
        assert parsed["can_quiet"] is False
        assert parsed["update"] == ("pcs", "pcscpu2")

    def test_only_the_flasher_path_is_intercepted(self):
        backend = Backend()
        # An ordinary shell-out keeps the canned success it always had.
        assert backend.cid_execute(path="/bin/ping", args=["-c", "1"])["exit_status"] == 0
        # The flasher does not.
        assert backend.cid_execute(path="/sbin/smashclicker",
                                   args=self.ARGS)["exit_status"] == 1

    def test_a_backend_that_cannot_flash_refuses_instead_of_faking_a_pass(self):
        # This is the bug the seam exists for: cid_execute's canned success made
        # every UPDATE_* procedure report a flash that never happened.
        res = Backend().flash_module(update=("pmr", "dir"))
        assert res["exit_status"] == 1
        assert "no flasher" in res["stderr"]

    def test_result_lines_are_shaped_for_the_procedures_own_parser(self):
        # UPDATE_MODULE.parse_log splits on '"': [1] is the module, and a later
        # piece holding 'code 1'/'code 2' is that module's pass.
        res = odin_runner._smashclicker_result(["pmr", "dir"])
        assert res["exit_status"] == 0
        for line, component in zip(res["stdout"].split("\r\n"), ("pmr", "dir"),
                                   strict=True):
            parts = line.split('"')
            assert parts[1] == component
            assert any("code 1" in p for p in parts)

    def test_a_failure_carries_its_reason_where_the_parser_looks(self):
        res = odin_runner._smashclicker_result(
            ["pmr"], [("dir", "no firmware for this ECU identity")])
        assert res["exit_status"] == 1          # a partial flash is not a pass
        fail = [x for x in res["stdout"].split("\r\n") if "dir" in x][0]
        code_piece = [p for p in fail.split('"') if "code" in p][0]
        assert "code 1" not in code_piece and "code 2" not in code_piece
        assert "no firmware" in code_piece

    def test_a_reason_cannot_smuggle_a_pass_code(self):
        # parse_log looks for the literal 'code 1' anywhere in the piece, so a
        # reason mentioning one would flip a failure into a pass.
        res = odin_runner._smashclicker_result([], [("dir", "rejected code 1x")])
        assert "code 1x" not in res["stdout"]
        assert res["exit_status"] == 1

    def test_nothing_flashed_is_not_a_pass(self):
        assert odin_runner._smashclicker_result([])["exit_status"] == 1


class _FlashBackend(Backend):
    """Records what the flasher was asked for and reports every image flashed."""

    def __init__(self):
        self.calls = []

    def flash_module(self, update=(), hwidacq=(), job_id="", can_quiet=False,
                     timeout=None):
        self.calls.append({"update": tuple(update), "hwidacq": tuple(hwidacq),
                           "job_id": job_id, "can_quiet": can_quiet})
        return odin_runner._smashclicker_result(list(update))


class TestRunCancel:
    def test_a_run_cancel_reaches_a_nested_frames_poll_loop(self):
        # The stop flag is shared by every frame of the run, so it ends a wait in
        # a subnetwork too -- not just the top-level graph.
        e = Engine(MockBackend("success"), Path("."))
        frame = Frame(graph={}, inputs={}, depth=0, run_cancel=e._cancel)
        assert frame.stopping() is False
        e.request_cancel()
        assert e.cancelled() is True
        assert frame.stopping() is True
        assert frame.wait(0.01) is True

    def test_a_finished_graph_does_not_stop_the_rest_of_the_run(self):
        # run_graph sets the FRAME's cancel in its finally (to join Split
        # branches). If that were the run flag, the first graph to finish would
        # kill everything after it.
        e = Engine(MockBackend("success"), Path("."))
        frame = Frame(graph={}, inputs={}, depth=0, run_cancel=e._cancel)
        frame.cancel.set()
        assert frame.stopping() is True
        assert e.cancelled() is False


class TestRemainingNodeTypes:
    def test_extend_multi_concatenates_an_indexed_list_map(self):
        assert pull({"type": "lists.ExtendMulti", "lists": {
            "2": {**lit(["c"]), "index": 1},
            "1": {**lit(["a", "b"]), "index": 0},
            "3": {**lit("bare"), "index": 2},   # a non-list entry is itself
        }}) == ["a", "b", "c", "bare"]

    def test_whitelist_dict_keeps_only_permitted_keys(self):
        assert pull({"type": "misc.WhitelistDict",
                     "input_data": lit({"a": 1, "b": 2, "c": 3}),
                     "whitelist": lit(["a", "c"])}) == {"a": 1, "c": 3}

    def test_enum_input_binds_like_an_input_with_a_default(self):
        graph = {"band": {"type": "enum.EnumInput", "default": lit("fm"),
                          "options": ["am", "fm"]}}
        e = _engine()
        bound = Frame(graph=graph, inputs={"band": "am"}, depth=0)
        unbound = Frame(graph=graph, inputs={}, depth=0)
        assert e._pull(bound, conn("band.value")) == "am"
        assert e._pull(unbound, conn("band.value")) == "fm"
        assert e._pull(unbound, conn("band.options")) == ["am", "fm"]

    def test_concurrent_split_runs_every_branch(self):
        res = run({
            "enter": {"type": "networks.Enter", "start": conn("cs.run")},
            "cs": {"type": "control.ConcurrentSplit", "branches": {
                "Master": {**conn("a.capture"), "index": 0},
                "Slave": {**conn("b.capture"), "index": 1}}},
            "a": _cap("master", lit(1)),
            "b": _cap("slave", lit(2)),
        })
        assert {m["metric"] for m in res.metrics} == {"master", "slave"}

    def test_ping_outcome_records_the_verdict(self):
        res = run({
            "enter": {"type": "networks.Enter", "start": conn("po.run")},
            "po": {"type": "testing.PingOutcome", "outcome": lit(True)},
        })
        assert res.metrics == [{"metric": "PingOutcome", "value": True,
                                "result_code": 0, "expected": True}]

    def test_file_output_attaches_the_blob_and_names_it(self):
        backend = _FusedBackend()
        res = Engine(backend, Path(".")).run_graph({
            "enter": {"type": "networks.Enter", "start": conn("fo.run")},
            "fo": {"type": "reporting.FileOutput", "file_name": lit("logs.gz"),
                   "data": lit("BLOB"), "mime_type": lit("application/gzip"),
                   "encoding": lit("Base64"), "finished": conn("exit.exit")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }, {})
        assert res.outputs["files"] == [{"file_name": "logs.gz",
                                         "mime_type": "application/gzip",
                                         "encoding": "Base64"}]

    def test_script_test_is_the_same_node_as_run_script_test(self, tmp_path):
        # scripts.ScriptTest is the older name for the same node. In 2022.45.15 it
        # is always INLINE in a wired graph (continuing via `done`), never a bare
        # task-wrapper entry -- so that is the shape tested here.
        _bundle(tmp_path, myscript=_SCRIPT, parent='''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "st.run"}},
    "st": {"type": "scripts.ScriptTest", "script_name": "myscript",
           "inputs": {}, "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
''')
        res = Engine(MockBackend("success"), tmp_path).run_procedure("parent")
        assert any(m["metric"] == "scripted" for m in res.metrics)
