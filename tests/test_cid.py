"""Tests for the CID emulation (Step 5): CidStore (data-value store) + CidFilesystem
(read-only firmware-dump view) + the Engine cid.* handlers.

Self-contained: unit tests use a tmp-dir fake root; the one real-dump test skips
when TM3_ROOT (config.ROOT) is unset. No bench / CAN bus needed.
"""
import hashlib
from pathlib import Path

import pytest
from odin_runner import BenchBackend, CidFilesystem, CidStore, Engine, MockBackend

import config
from vapi_registry import Registry

# Stands in for the firmware's alias table so the derive tests exercise the real
# registry path without a firmware dump: VAPI_shiftState is the ShiftStateNameMap
# enum over DI_gear, VAPI_driverPresent a bool over VCFRONT_driverPresent.
_TEST_VAPI_REGISTRY = Registry.from_dict({"aliases": {
    "VAPI_shiftState": {"source": "DI_gear", "render": "enum",
                        "enum": {"0": "Invalid", "1": "P", "2": "R", "3": "N",
                                 "4": "D", "5": "SNA"}},
    "VAPI_driverPresent": {"source": "VCFRONT_driverPresent", "render": "bool"},
}})


def lit(v):
    return {"value": v}


def conn(t):
    return {"connection": t}


def _cap(metric, value, done=None):
    node = {"type": "reporting.CaptureMetric", "metric_name": lit(metric),
            "value": value, "result_code": lit(0)}
    if done:
        node["done"] = done
    return node


# ---------------------------------------------------------------------------
# CidStore
# ---------------------------------------------------------------------------

class TestCidStore:
    def test_defaults_and_read_your_writes(self):
        s = CidStore()
        assert s.get("GUI_factoryMode") == "false"
        s.set("GUI_factoryMode", "true")
        assert s.get("GUI_factoryMode") == "true"
        assert s.get("UNKNOWN_NAME") is None

    def test_seed_overrides_defaults(self):
        assert CidStore(seed={"VAPI_countryCode": "DE"}).get("VAPI_countryCode") == "DE"

    def test_derive_wins_over_store(self):
        s = CidStore(derive=lambda n: "N" if n == "VAPI_shiftState" else None)
        s.set("VAPI_shiftState", "D")     # stored...
        assert s.get("VAPI_shiftState") == "N"   # ...but derive still wins

    def test_list_and_save_load(self):
        s = CidStore()
        s.set("a", "1")
        assert s.list(["a", "GUI_factoryMode"]) == {"a": "1", "GUI_factoryMode": "false"}
        s.save("/f.json", {"x": 1})
        assert s.load("/f.json") == {"x": 1}
        assert s.load("/missing") is None


# ---------------------------------------------------------------------------
# CidFilesystem (fake root)
# ---------------------------------------------------------------------------

def _fake_root(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "os-release").write_text("NAME=Buildroot\nID=buildroot\n")
    (tmp_path / "etc" / ".hidden").write_text("h")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_bytes(b"ABCD")
    return CidFilesystem(tmp_path)


class TestCidFilesystem:
    def test_hash_file(self, tmp_path):
        fs = _fake_root(tmp_path)
        assert fs.hash_file("/bin/tool", "sha256") == hashlib.sha256(b"ABCD").hexdigest()
        assert fs.hash_file("/bin/tool", "SHA-256") == hashlib.sha256(b"ABCD").hexdigest()
        assert fs.hash_file("/nope") is None

    def test_list_dir_hidden_and_details(self, tmp_path):
        fs = _fake_root(tmp_path)
        assert fs.list_dir("/etc") == ["os-release"]                 # hidden excluded
        assert set(fs.list_dir("/etc", show_hidden=True)) == {".hidden", "os-release"}
        assert fs.list_dir("/bin", details=True) == [
            {"name": "tool", "is_dir": False, "size": 4}]
        assert fs.list_dir("/does-not-exist") == []

    def test_grep_and_read_text(self, tmp_path):
        fs = _fake_root(tmp_path)
        assert fs.grep("Buildroot", "/etc/os-release") == ["NAME=Buildroot"]
        assert fs.grep("nomatch", "/etc/os-release") == []
        assert fs.grep("x", "/nope") == []
        assert fs.read_text("/etc/os-release").startswith("NAME=Buildroot")
        assert fs.read_text("/nope") is None

    def test_path_jail_rejects_escape(self, tmp_path):
        fs = _fake_root(tmp_path)
        assert fs.hash_file("/../../../etc/passwd") is None
        assert fs.read_text("../outside") is None
        assert fs.list_dir("/../..") == []

    def test_no_root_is_all_empty(self):
        fs = CidFilesystem(None)
        assert fs.hash_file("/x") is None
        assert fs.list_dir("/x") == []
        assert fs.grep("a", "/x") == []
        assert fs.read_text("/x") is None


@pytest.mark.skipif(config.ROOT is None or not config.ROOT.exists(),
                    reason="TM3_ROOT firmware dump not available")
class TestCidFilesystemRealDump:
    def test_serves_real_data_from_the_dump(self):
        fs = CidFilesystem(config.ROOT)
        assert fs.list_dir("/etc")                      # real rootfs listing, non-empty
        h = fs.hash_file("/sbin/autofuser.sh")          # a real regular file in the dump
        assert h is not None and len(h) == 64


# ---------------------------------------------------------------------------
# Engine cid.* handlers (mini-graphs)
# ---------------------------------------------------------------------------

def _run(graph, backend=None):
    eng = Engine(backend or MockBackend("success"), Path("."), time_scale=0.0)
    return eng.run_graph(graph, {})


class TestCidHandlers:
    def test_set_then_get_round_trip(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("set.run")},
            "set": {"type": "cid.SetDataValue", "data_name": lit("GUI_factoryMode"),
                    "value": lit("true"), "done": conn("cap.capture")},
            "cap": _cap("fm", conn("get.value"), done=conn("exit.exit")),
            "get": {"type": "cid.GetDataValue", "data_name": lit("GUI_factoryMode")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert _run(graph).metrics[0]["value"] == "true"

    def test_save_then_load_round_trip(self):
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("save.run")},
            "save": {"type": "cid.SaveData", "filename": lit("/f.json"),
                     "data": lit({"x": 1}), "done": conn("cap.capture")},
            "cap": _cap("loaded", conn("load.data"), done=conn("exit.exit")),
            "load": {"type": "cid.LoadData", "filename": lit("/f.json")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert _run(graph).metrics[0]["value"] == {"x": 1}

    def test_hashfile_via_bench_serves_real_hash(self, tmp_path):
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "t").write_bytes(b"ABCD")
        bb = BenchBackend("chan", firmware_root=tmp_path)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("cap.capture")},
            "cap": _cap("h", conn("hf.hash"), done=conn("exit.exit")),
            "hf": {"type": "cid.HashFile", "filepath": lit("/bin/t"),
                   "algorithm": lit("sha256")},
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        res = _run(graph, backend=bb)
        assert res.metrics[0]["value"] == hashlib.sha256(b"ABCD").hexdigest()

    def test_directory_contents_via_bench(self, tmp_path):
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "a").write_text("x")
        bb = BenchBackend("chan", firmware_root=tmp_path)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("gdc.run")},
            "gdc": {"type": "cid.GetDirectoryContents", "directory": lit("/etc"),
                    "done": conn("cap.capture")},
            "cap": _cap("ls", conn("gdc.result"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert _run(graph, backend=bb).metrics[0]["value"] == ["a"]

    def test_execute_application_is_stubbed_success(self, tmp_path):
        bb = BenchBackend("chan", firmware_root=tmp_path)
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("app.run")},
            "app": {"type": "cid.ExecuteApplication", "path": lit("/sbin/x.sh"),
                    "args": lit([]), "user": lit("root"), "done": conn("cap.capture")},
            "cap": _cap("status", conn("app.exit_status"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        assert _run(graph, backend=bb).metrics[0]["value"] == 0


# ---------------------------------------------------------------------------
# BenchBackend._derive_cid -- MCU-published CID names served off the live bus.
# A bench has no MCU, so DI_RESOLVER_LEARNING's dyno gate and its two shift waits
# read from the inverter's own DI_systemStatus echo instead, and the rotor/resolver
# learn scripts' drive-rail wait from VCFRONT_LVPowerState.
# ---------------------------------------------------------------------------

def _bench(tmp_path, signals, **kw):
    """BenchBackend whose bus reports `signals` ({name: raw int}); None = not seen.

    Carries the test alias table so a firmware-derived alias (VAPI_shiftState)
    resolves without a real dump; the computed values (rails, traction request)
    are hand-coded in the backend and need no registry."""
    kw.setdefault("vapi_registry", _TEST_VAPI_REGISTRY)
    bb = BenchBackend("chan", firmware_root=tmp_path, **kw)
    bb.can_read = lambda signal, bus=None: signals.get(signal)
    return bb


class TestBenchCidDerive:
    def test_dyno_mode_satisfies_the_resolver_learn_gate(self, tmp_path):
        bb = _bench(tmp_path, {"DI_tractionControlMode": 5})   # TC_DYNO_MODE
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Dyno"

    @pytest.mark.parametrize("tcm", [0, 1, 2, 3, 4])
    def test_other_traction_modes_are_not_dyno(self, tmp_path, tcm):
        bb = _bench(tmp_path, {"DI_tractionControlMode": tcm})
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Normal"

    @pytest.mark.parametrize("gear,expected",
                             [(1, "P"), (2, "R"), (3, "N"), (4, "D")])
    def test_gear_maps_to_shift_state(self, tmp_path, gear, expected):
        # The mapping is the firmware's ShiftStateNameMap over DI_gear, via the
        # alias table -- not a hand-written table in the backend.
        bb = _bench(tmp_path, {"DI_gear": gear})
        assert bb.cid_get("VAPI_shiftState") == expected

    def test_a_gear_outside_the_name_map_derives_nothing(self, tmp_path):
        # DI_gear 7 (SNA on the wire) has no ShiftStateNameMap entry -> unlisted,
        # so the alias yields None and the store/seed serves it.
        assert _bench(tmp_path, {"DI_gear": 7}).cid_get("VAPI_shiftState") is None

    def test_an_invalid_gear_gives_the_firmwares_label(self, tmp_path):
        # DI_gear 0 IS in the map (Invalid); that is the value the car reports.
        assert _bench(tmp_path, {"DI_gear": 0}).cid_get("VAPI_shiftState") == "Invalid"

    def test_float_physical_value_coerces(self, tmp_path):
        bb = _bench(tmp_path, {"DI_gear": 4.0})
        assert bb.cid_get("VAPI_shiftState") == "D"

    @pytest.mark.parametrize("raw,expected", [(1, "true"), (0, "false")])
    def test_a_bool_alias_renders_true_false(self, tmp_path, raw, expected):
        bb = _bench(tmp_path, {"VCFRONT_driverPresent": raw})
        assert bb.cid_get("VAPI_driverPresent") == expected

    @pytest.mark.parametrize("vps,rails", [
        (3, ["true", "true", "true"]),      # DRIVE
        (2, ["false", "true", "true"]),     # ACCESSORY
        (1, ["false", "false", "true"]),    # CONDITIONING
        (0, ["false", "false", "false"]),   # OFF
    ])
    def test_power_state_maps_to_rails(self, tmp_path, vps, rails):
        bb = _bench(tmp_path, {"VCFRONT_vehiclePowerState": vps})
        assert [bb.cid_get(n) for n in
                ("VAPI_driveRailOn", "VAPI_accRailOn", "VAPI_hvacRailOn")] == rails

    def test_quiet_bus_falls_back_to_seed(self, tmp_path):
        bb = _bench(tmp_path, {},
                    cid_values={"GUI_tractionControlModeRequest": "Dyno"})
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Dyno"
        assert bb.cid_get("VAPI_shiftState") is None
        assert bb.cid_get("VAPI_driveRailOn") is None

    def test_quiet_bus_falls_back_to_read_your_writes(self, tmp_path):
        bb = _bench(tmp_path, {})
        bb.cid_set("VAPI_shiftState", "D")
        assert bb.cid_get("VAPI_shiftState") == "D"

    def test_live_bus_wins_over_a_stale_seed(self, tmp_path):
        bb = _bench(tmp_path, {"DI_tractionControlMode": 0},
                    cid_values={"GUI_tractionControlModeRequest": "Dyno"})
        assert bb.cid_get("GUI_tractionControlModeRequest") == "Normal"

    def test_unopenable_bus_does_not_break_cid_reads(self, tmp_path):
        bb = BenchBackend("chan", firmware_root=tmp_path)

        def boom(signal, bus=None):
            raise OSError("no such device")

        bb.can_read = boom
        assert bb.cid_get("GUI_tractionControlModeRequest") is None
        assert bb.cid_get("GUI_factoryMode") == "false"   # defaults still served

    def test_unrelated_names_never_touch_can(self, tmp_path):
        reads = []
        bb = BenchBackend("chan", firmware_root=tmp_path)
        bb.can_read = lambda signal, bus=None: reads.append(signal)
        assert bb.cid_get("GUI_factoryMode") == "false"
        assert bb.cid_get("VAPI_countryCode") == "US"
        assert reads == []

    def test_gate_passes_through_the_real_node_types(self, tmp_path):
        """cid.GetDataValue -> logic.Compare -> control.IfThen, as
        Gen3/lib/DI_RESOLVER_LEARNING wires its dyno gate."""
        graph = {
            "enter": {"type": "networks.Enter", "start": conn("ifthen.run")},
            "ifthen": {"type": "control.IfThen", "expr": conn("compare1.result"),
                       "if_true": conn("cap_ok.capture"),
                       "if_false": conn("cap_no.capture")},
            "compare1": {"type": "logic.Compare", "a": conn("getdatavalue.value"),
                         "b": lit("Dyno")},
            "getdatavalue": {"type": "cid.GetDataValue",
                             "data_name": lit("GUI_tractionControlModeRequest")},
            "cap_ok": _cap("gate", lit("in dyno"), done=conn("exit.exit")),
            "cap_no": _cap("gate", lit("Not in Dyno Mode"), done=conn("exit.exit")),
            "exit": {"type": "networks.Exit", "exit_code": lit(0)},
        }
        dyno = _bench(tmp_path, {"DI_tractionControlMode": 5})
        assert _run(graph, backend=dyno).metrics[0]["value"] == "in dyno"
        normal = _bench(tmp_path, {"DI_tractionControlMode": 0})
        assert _run(graph, backend=normal).metrics[0]["value"] == "Not in Dyno Mode"


# ---------------------------------------------------------------------------
# BenchBackend.procedure_session -- a run holds the MCU's service mode on the bus
# (UI_serviceMode, 0x284), which the DI needs to start ROTOR/RESOLVER_LEARNING.
# Real HTTP into vehicle_sim's control server -> its UI node.
# ---------------------------------------------------------------------------

@pytest.fixture
def sim_ui():
    """(UI node, control URL) for a live vehicle_sim control server."""
    import sim_core
    import sim_registry
    import vehicle_sim

    ui = sim_registry.BY_NAME["UI"](sim_core.NodeContext(db=None))
    srv = vehicle_sim._start_control_server(vehicle_sim._ControlFacade({"UI": ui}), 0)
    yield ui, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _service_bit(ui):
    return {f.can_id: f for f in ui.frames()}[0x284].frame()[0] >> 3 & 1


def _session_bench(tmp_path, sim_url):
    bb = BenchBackend("chan", firmware_root=tmp_path, sim_url=sim_url)
    bb.events = []
    bb.on_event = lambda kind, payload: bb.events.append(payload["status"])
    return bb


class TestBenchProcedureSession:
    def test_run_holds_service_mode_then_restores(self, tmp_path, sim_ui):
        ui, url = sim_ui
        bb = _session_bench(tmp_path, url)
        with bb.procedure_session():
            assert _service_bit(ui) == 1
        assert _service_bit(ui) == 0

    def test_operator_set_service_mode_is_left_on(self, tmp_path, sim_ui):
        ui, url = sim_ui
        ui.set_ui("service_mode", "on")
        with _session_bench(tmp_path, url).procedure_session():
            pass
        assert _service_bit(ui) == 1

    def test_engine_wraps_the_whole_run(self, tmp_path, sim_ui, monkeypatch):
        ui, url = sim_ui
        seen = []
        monkeypatch.setattr(Engine, "_run_child_graph",
                            lambda self, g, i, depth: seen.append(_service_bit(ui)))
        monkeypatch.setattr("odin_runner.load_graph", lambda bundle, rel: {})
        Engine(_session_bench(tmp_path, url), Path(".")).run_procedure("x")
        assert seen == [1] and _service_bit(ui) == 0

    @pytest.mark.parametrize("url,why", [(None, "no vehicle_sim control URL"),
                                         ("http://127.0.0.1:9", "not on the bus")])
    def test_missing_sim_does_not_fail_the_run(self, tmp_path, url, why):
        bb = _session_bench(tmp_path, url)
        with bb.procedure_session():
            ran = True
        assert ran and why in bb.events[0]
