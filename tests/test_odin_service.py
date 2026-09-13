"""Tests for scripts/odin_service.py -- the shared CLI/web core over the ODIN engine.

Every graph is synthetic, written into a tmp bundle (Model3/tasks entries +
Model3/lib children).
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

# Runnable entry with a wrapped-title TaskInfo (title as {'value': ...}).
_RUNNABLE = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "Model3/lib/dbl",
             "inputs": {"n": {"value": 5}},
             "outputs": {"exit_code": {"index": 0}, "doubled": {"index": 1}}},
    "info": {"type": "comments.TaskInfo", "title": {"value": "Double It"},
             "valid_states": ["StandStill|Parked"], "principals": ["tbx-internal"],
             "description": "Double the input. - 1. Shift to D. 2. Coast to stop.",
             "user_facing_impact": "Wheels will physically rotate.",
             "additional_info": "Returns pass if the input is even.",
             "gtw_diag_level": ["Service"], "cancelable": True,
             "post_fusing_allowed": False},
}
'''

# Blocked entry: an unhandled node type; TaskInfo uses the bare (plain-string) title.
_BLOCKED = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "x.run"}},
    "x": {"type": "custommod.CustomNode", "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
    "info": {"type": "comments.TaskInfo", "title": "Blocked One"},
}
'''

# Runnable entry with no TaskInfo.
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


class TestVehicles:
    """A car's procedures are its own tree PLUS the shared ones it layers on.

    Only Model3/tasks was ever scanned, so the Gen3 tree -- where the DIR/DIF
    resolver-learn procs live, 625 entries to Model3's 569 on 2022.45.15 -- was
    invisible. Model3's own tasks reference Gen3/ 432 times and Common/ 84, so
    the trees are layered, not alternatives.
    """

    def _multi(self, tmp_path):
        _make_bundle(tmp_path)                             # Model3/tasks x3
        _write(tmp_path, "Gen3/tasks/GEN3-TASK", _RUNNABLE)
        _write(tmp_path, "Gen3/tasks/RUNNABLE-TASK", _BLOCKED)   # same NAME as Model3's
        _write(tmp_path, "Gen3/lib/dbl", _CHILD)
        _write(tmp_path, "Common/tasks/COMMON-TASK", _RUNNABLE)
        _write(tmp_path, "Common/lib/dbl", _CHILD)
        _write(tmp_path, "ModelY/tasks/Y-TASK", _RUNNABLE)
        _write(tmp_path, "Tutorials/tasks/DEMO", _RUNNABLE)
        _write(tmp_path, "Shared/lib/only", _CHILD)        # lib only, no entries
        return tmp_path

    def test_lists_only_the_cars(self, tmp_path):
        # Gen3 and Common carry entries but are not cars -- they are what a car
        # layers on. (Both DO have tasks/ in the real bundle: 625 and 212.)
        # Tutorials has entries too and is neither, so it is not offered as a car.
        assert odin_service.list_vehicles(bundle=self._multi(tmp_path)) == [
            "Model3", "ModelY"]

    def test_a_tutorials_tree_is_not_layered_onto_a_car(self, tmp_path):
        assert "Tutorials" not in odin_service.trees_for(
            "Model3", bundle=self._multi(tmp_path))

    def test_a_library_only_tree_is_not_a_tree(self, tmp_path):
        # No tasks/ -> pulled in by reference, never an entry point.
        assert "Shared" not in odin_service.list_trees(bundle=self._multi(tmp_path))

    def test_a_car_draws_from_its_own_tree_then_the_shared_ones(self, tmp_path):
        assert odin_service.trees_for("Model3", bundle=self._multi(tmp_path)) == [
            "Model3", "Gen3", "Common"]

    def test_procedures_union_the_cars_tree_with_the_shared_ones(self, tmp_path):
        procs = odin_service.list_procedures_for(
            "Model3", bundle=self._multi(tmp_path), runnable_only=False)
        assert {p["name"] for p in procs} == {
            "RUNNABLE-TASK", "BLOCKED-TASK", "NOINFO-TASK",   # Model3
            "GEN3-TASK",                                      # Gen3
            "COMMON-TASK",                                    # Common
        }

    def test_the_cars_own_version_wins_a_shared_name(self, tmp_path):
        # Model3 and Gen3 share 217 task names on 2022.45.15 and 172 DIFFER, so
        # this is not cosmetic: the car's own file is the one that must run.
        procs = {p["name"]: p for p in odin_service.list_procedures_for(
            "Model3", bundle=self._multi(tmp_path), runnable_only=False)}
        assert procs["RUNNABLE-TASK"]["tree"] == "Model3"
        assert procs["RUNNABLE-TASK"]["basename"] == "Model3/tasks/RUNNABLE-TASK"
        assert procs["RUNNABLE-TASK"]["runnable"] is True

    def test_runnability_is_filtered_after_the_tree_choice(self, tmp_path):
        # Model3's RUNNABLE-TASK is runnable and Gen3's namesake is blocked. The
        # reverse case is the trap: filtering first would let a blocked proc fall
        # through to another tree's version of the same name.
        names = {p["name"] for p in odin_service.list_procedures_for(
            "Model3", bundle=self._multi(tmp_path))}
        assert names == {"RUNNABLE-TASK", "NOINFO-TASK", "GEN3-TASK", "COMMON-TASK"}

    def test_a_bundle_without_the_shared_trees_still_works(self, tmp_path):
        _make_bundle(tmp_path)                             # Model3 only
        procs = odin_service.list_procedures_for("Model3", bundle=tmp_path)
        assert [p["name"] for p in procs] == ["NOINFO-TASK", "RUNNABLE-TASK"]

    def test_default_prefers_the_configured_product(self, tmp_path, monkeypatch):
        import config as _cfg
        monkeypatch.setattr(_cfg, "PRODUCT", "ModelY")
        assert odin_service.default_vehicle(bundle=self._multi(tmp_path)) == "ModelY"

    def test_default_falls_back_to_model3_when_the_product_is_absent(self, tmp_path,
                                                                     monkeypatch):
        import config as _cfg
        monkeypatch.setattr(_cfg, "PRODUCT", "ModelS")     # not in this bundle
        assert odin_service.default_vehicle(bundle=self._multi(tmp_path)) == "Model3"

    def test_default_takes_what_there_is_when_neither_matches(self, tmp_path,
                                                              monkeypatch):
        import config as _cfg
        monkeypatch.setattr(_cfg, "PRODUCT", "ModelS")
        _write(tmp_path, "ModelY/tasks/ONLY", _RUNNABLE)
        assert odin_service.default_vehicle(bundle=tmp_path) == "ModelY"


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
        assert run["description"] == "Double the input. - 1. Shift to D. 2. Coast to stop."
        assert run["user_facing_impact"] == "Wheels will physically rotate."
        assert run["additional_info"] == "Returns pass if the input is even."
        assert run["gtw_diag_level"] == ["Service"]
        assert run["cancelable"] is True and run["post_fusing_allowed"] is False

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
        assert noinfo["description"] is None and noinfo["user_facing_impact"] is None
        assert noinfo["additional_info"] is None
        assert noinfo["gtw_diag_level"] == []
        assert noinfo["cancelable"] is None and noinfo["post_fusing_allowed"] is None

    def test_runnable_only_filters_blocked(self, tmp_path):
        procs = odin_service.list_procedures(bundle=_make_bundle(tmp_path),
                                             runnable_only=True)
        names = {p["name"] for p in procs}
        assert names == {"RUNNABLE-TASK", "NOINFO-TASK"}   # BLOCKED-TASK dropped


# procedure_requirements: a lib graph exercising every extracted node kind -- three
# CAN reads (read/monitor/compare) on ETH, a dynamic (connection-sourced) signal, an
# alert inspection, EnsureApplicationState, PowerContext, an odx UDS target, and a cid
# data-value read.
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
# Entry proc referencing the lib (transitive walk) + TaskInfo states.
_REQ_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "Model3/lib/reqlib"},
    "info": {"type": "comments.TaskInfo", "title": {"value": "Req Test"},
             "valid_states": ["Parked"]},
}
'''
# Literal signal, no bus -> grouped under the default bus.
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
        # literal reads on ETH, sorted by (signal, kind)
        assert req["signals"]["ETH"] == [
            {"signal": "DIR_axleSpeed", "kind": "compare"},
            {"signal": "DI_gear", "kind": "monitor"},
            {"signal": "GTW_drivetrainType", "kind": "read"},
            {"signal": "IBST_iBoosterStatus", "kind": "read"},   # from a conn+value default
        ]

    def test_dynamic_signal_counted_not_enumerated(self, tmp_path):
        req = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))
        assert req["dynamic_count"] == 1
        sigs = [s["signal"] for lst in req["signals"].values() for s in lst]
        assert "src.out" not in sigs

    def test_alerts_and_uds_target_nodes(self, tmp_path):
        req = odin_service.procedure_requirements(
            "Model3/tasks/REQ", bundle=self._bundle(tmp_path))
        assert req["alerts"] == [{"bus": "ETH", "prefix": "DI_a0"}]
        assert req["nodes"] == ["PMR"]

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


# Following a connection to its source. The shared libs are written once and
# parameterised, so reading a field LOCALLY reports the lib's declared default
# -- which for the DI/PM pairs is the REAR unit, on the front task's readout.
# Modelled on Gen3/lib/DI_RESOLVER_LEARNING: a node_name Input defaulting to
# 'DIR', a UDS call connected to it, and a signal name concatenated from it.
_PARAM_LIB = '''
network = {
    "node_name": {"type": "networks.Input", "default": {"value": "DIR"}},
    "concat": {"type": "strings.Concat", "a": {"connection": "node_name.value"},
               "b": {"value": "_axleSpeed"}},
    "cmp": {"type": "can.CANSignalValueComparison",
            "signal_name": {"connection": "concat.c"}, "bus_name": {"value": "ETH"},
            "target": {"value": 560}, "comparator": {"value": 4}},
    "odx": {"type": "odx.OdxStartAndWaitResults", "routine_name": {"value": "R"},
            "node_name": {"connection": "node_name.value", "value": "DIR"}},
    "esp": {"type": "uds.UdsTesterPresent", "node_name": {"value": "ESP"}},
}
'''
# The front task binds the input as a BARE literal, which is how the real
# PROC_DIF_X_RESOLVER-LEARN writes it.
_FRONT_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork",
             "basename": "Model3/lib/paramlib", "inputs": {"node_name": "DIF"}},
}
'''
_REAR_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork",
             "basename": "Model3/lib/paramlib",
             "inputs": {"node_name": {"value": "DIR"}}},
}
'''
_UNBOUND_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork",
             "basename": "Model3/lib/paramlib"},
}
'''
# A node_name sourced from a constant.Constant -- 147 of the bundle's UDS calls
# are written this way, and a field-local read drops every one of them.
_CONST_LIB = '''
network = {
    "which": {"type": "constant.Constant", "value": {"value": "IBST"}},
    "uds": {"type": "uds.UdsReadDtcs", "node_name": {"connection": "which.out"}},
    "loop": {"type": "control.ForEachEntry", "data": {"value": {}}},
    "dyn": {"type": "can.CANSignalRead", "signal_name": {"connection": "loop.item"},
            "bus_name": {"value": "ETH"}},
}
'''
_CONST_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork", "basename": "Model3/lib/constlib"},
}
'''


# A pass-through lib between the task and the script it parameterises -- the
# shape of Gen3/lib/FIRMWARE_DOWNLOAD, which is nine Inputs relayed straight into
# UPDATE_MODULE. Without following those connections the component list a task
# binds is invisible one hop later.
_FLASH_LIB = '''
network = {
    "update_list": {"type": "networks.Input"},
    "hwid_list": {"type": "networks.Input"},
    "lock": {"type": "networks.Input", "default": {"value": "PMR"}},
    "task": {"type": "scripts.RunScriptTest", "script_name": "Model3/scripts/UPD",
             "inputs": {"update_component_list": {"connection": "update_list.value"},
                        "hwidacq_component_list": {"connection": "hwid_list.value"},
                        "node_to_lock": {"connection": "lock.value"}}},
}
'''
_FLASH_TASK = '''
network = {
    "task": {"type": "networks.RunReferencedSubnetwork",
             "basename": "Model3/lib/flashlib",
             "inputs": {"update_list": {"value": ["pmr", "dir"]},
                        "hwid_list": {"value": ["pmr"]}}},
}
'''


class TestFlashTargets:
    def _bundle(self, tmp_path):
        _write(tmp_path, "Model3/scripts/UPD", "network = " + repr("""
async def odin_script_test(api, update_component_list: list,
                           hwidacq_component_list: list, node_to_lock: str = ''):
    await api.cid.execute_application(path='/sbin/smashclicker', user='root',
                                      args=['-u', ','.join(update_component_list)])
    return 0
"""))
        _write(tmp_path, "Model3/lib/flashlib", _FLASH_LIB)
        _write(tmp_path, "Model3/tasks/UPDATE_PMR", _FLASH_TASK)
        _write(tmp_path, "Model3/tasks/PLAIN", _RUNNABLE)
        _write(tmp_path, "Model3/lib/dbl", _CHILD)
        return tmp_path

    def test_the_components_a_task_binds_survive_a_pass_through_lib(self, tmp_path):
        got = odin_service.flash_targets("Model3/tasks/UPDATE_PMR",
                                         bundle=self._bundle(tmp_path))
        assert got["update"] == ["pmr", "dir"]
        assert got["hwidacq"] == ["pmr"]
        assert got["node_to_lock"] == "PMR"      # from the lib's declared default

    def test_a_procedure_that_writes_no_firmware_has_no_targets(self, tmp_path):
        assert odin_service.flash_targets("Model3/tasks/PLAIN",
                                          bundle=self._bundle(tmp_path)) is None

    def test_listing_marks_which_procedures_flash(self, tmp_path):
        procs = {p["name"]: p for p in odin_service.list_procedures(
            bundle=self._bundle(tmp_path), entries="Model3/tasks",
            runnable_only=False)}
        assert procs["UPDATE_PMR"]["flashes"] is True
        assert procs["PLAIN"]["flashes"] is False

    def test_an_unknown_procedure_raises(self, tmp_path):
        import pytest
        with pytest.raises(FileNotFoundError):
            odin_service.flash_targets("Model3/tasks/NOPE",
                                       bundle=self._bundle(tmp_path))


class _ArmableBackend(odin_runner.MockBackend):
    """A mock that reports its arming the way BenchBackend.flash_preview does."""

    def __init__(self, allow_flash=False):
        super().__init__("success")
        self.allow_flash = allow_flash
        self.conditions: dict = {}

    def flash_preview(self, update=(), hwidacq=()):
        return {"plan": [], "conditions": dict(self.conditions), "choices": [],
                "blocked": [], "armed": self.allow_flash, "error": None}


class TestFlashPreflight:
    def test_a_non_flashing_procedure_needs_no_confirmation(self, tmp_path):
        _write(tmp_path, "Model3/tasks/PLAIN", _RUNNABLE)
        _write(tmp_path, "Model3/lib/dbl", _CHILD)
        got = odin_service.flash_preflight("Model3/tasks/PLAIN", bundle=tmp_path)
        assert got == {"basename": "Model3/tasks/PLAIN", "flashes": False}

    def test_a_backend_that_cannot_flash_says_so_before_the_run(self, tmp_path):
        # Better here than at the point of no return.
        bundle = TestFlashTargets()._bundle(tmp_path)
        got = odin_service.flash_preflight("Model3/tasks/UPDATE_PMR", bundle=bundle)
        assert got["flashes"] is True
        assert got["update"] == ["pmr", "dir"]
        assert got["plan"] == []
        assert got["blocked"] == ["pmr", "dir"]
        assert got["error"]

    def test_the_preview_reports_the_arming_the_run_will_use(self, tmp_path):
        # The preview builds its OWN backend, which starts disarmed. Without
        # this the modal said "flashing is not armed" for a bench the operator
        # had armed, and Confirm could never be enabled.
        bundle = TestFlashTargets()._bundle(tmp_path)
        be = _ArmableBackend(allow_flash=False)
        got = odin_service.flash_preflight("Model3/tasks/UPDATE_PMR", bundle=bundle,
                                           backend=be, allow_flash=True)
        assert got["armed"] is True
        assert be.allow_flash is True

    def test_arming_is_left_alone_when_the_caller_does_not_declare_it(self, tmp_path):
        bundle = TestFlashTargets()._bundle(tmp_path)
        be = _ArmableBackend(allow_flash=True)
        got = odin_service.flash_preflight("Model3/tasks/UPDATE_PMR", bundle=bundle,
                                           backend=be)
        assert got["armed"] is True


class TestConnectionResolution:
    def _bundle(self, tmp_path):
        _write(tmp_path, "Model3/lib/paramlib", _PARAM_LIB)
        _write(tmp_path, "Model3/tasks/FRONT", _FRONT_TASK)
        _write(tmp_path, "Model3/tasks/REAR", _REAR_TASK)
        _write(tmp_path, "Model3/tasks/UNBOUND", _UNBOUND_TASK)
        return tmp_path

    def test_the_caller_binding_wins_over_the_libs_declared_default(self, tmp_path):
        # The bug this fixes: the FRONT task reported the REAR inverter, because
        # the shared lib declares 'DIR' as its default and the readout never
        # looked at what the task bound.
        bundle = self._bundle(tmp_path)
        front = odin_service.procedure_requirements("Model3/tasks/FRONT", bundle=bundle)
        rear = odin_service.procedure_requirements("Model3/tasks/REAR", bundle=bundle)
        assert front["nodes"] == ["DIF", "ESP"]
        assert rear["nodes"] == ["DIR", "ESP"]

    def test_a_concatenated_signal_name_resolves_instead_of_counting_as_dynamic(
            self, tmp_path):
        bundle = self._bundle(tmp_path)
        front = odin_service.procedure_requirements("Model3/tasks/FRONT", bundle=bundle)
        rear = odin_service.procedure_requirements("Model3/tasks/REAR", bundle=bundle)
        assert front["signals"]["ETH"] == [{"signal": "DIF_axleSpeed", "kind": "compare"}]
        assert rear["signals"]["ETH"] == [{"signal": "DIR_axleSpeed", "kind": "compare"}]
        assert front["dynamic_count"] == 0

    def test_an_unbound_input_still_falls_back_to_its_default(self, tmp_path):
        req = odin_service.procedure_requirements("Model3/tasks/UNBOUND",
                                                  bundle=self._bundle(tmp_path))
        assert req["nodes"] == ["DIR", "ESP"]
        assert req["signals"]["ETH"] == [{"signal": "DIR_axleSpeed", "kind": "compare"}]

    def test_a_constant_sourced_node_name_is_listed(self, tmp_path):
        _write(tmp_path, "Model3/lib/constlib", _CONST_LIB)
        _write(tmp_path, "Model3/tasks/CONST", _CONST_TASK)
        req = odin_service.procedure_requirements("Model3/tasks/CONST", bundle=tmp_path)
        assert req["nodes"] == ["IBST"]

    def test_a_loop_item_is_still_dynamic(self, tmp_path):
        # Resolution follows pure-compute sources only. A per-iteration value is
        # genuinely unknowable statically, and stays counted, not guessed.
        _write(tmp_path, "Model3/lib/constlib", _CONST_LIB)
        _write(tmp_path, "Model3/tasks/CONST", _CONST_TASK)
        req = odin_service.procedure_requirements("Model3/tasks/CONST", bundle=tmp_path)
        assert req["dynamic_count"] == 1
        assert req["signals"] == {}


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
        assert "trace" in kinds
        metrics = [p for k, p in events if k == "metric"]
        assert metrics and metrics[0]["metric"] == "m" and metrics[0]["value"] == 42
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
        assert be.closed is False

    def test_error_event_emitted_on_failure(self, tmp_path):
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


# DID read/write helpers: FakeSession + synthetic NodeConfig.
def _fs(bit_length, byte_position, bit_position=0, data_type="uint", enum=None):
    return FieldSpec(bit_length=bit_length, byte_position=byte_position,
                     bit_position=bit_position, data_type=data_type,
                     enum_map=enum or {})


_MODE = _fs(8, 0, 0, "uint", {"OFF": 0, "ON": 1})
_CFG = OdjEntry(
    name="CFG", hex_id=0x0500,
    read=SubSpec(security_level=0, input={}, output={"MODE": _MODE},
                 input_size=0, output_size=1),
    write=SubSpec(security_level=5, input={"MODE": _MODE}, output={},
                  input_size=1, output_size=0))
_LOCK = OdjEntry(
    name="LOCK", hex_id=0x0600,
    read=SubSpec(security_level=3, input={}, output={"STATE": _fs(8, 0)},
                 input_size=0, output_size=1),
    write=None)
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
        assert {d["name"] for d in dids["write"]} == {"CFG"}
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
        assert ("security_access", 0, 3) in sess.calls
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
        assert ("security_access", 0, 5) in sess.calls
        assert ("write_did", 0x0500, b"\x01") in sess.calls
        assert res["bytes"] == "01" and res["size"] == 1

    def test_security_can_be_skipped(self):
        sess = FakeSession()
        odin_service.write_did(sess, _did_cfg(), "CFG", {"MODE": "OFF"},
                               security=False)
        assert not any(c[0] == "security_access" for c in sess.calls)
        assert ("write_did", 0x0500, b"\x00") in sess.calls
