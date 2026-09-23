"""Tests for scripts/odin_script_api.py -- ODIN's OTHER graph form.

154 of the 1,955 files in the 2022.45.15 bundle are not wired node graphs at all:
their `network` is a Python source string defining
`async def odin_script_test(api, ...)`, driving the same vehicle interop through
an `api` facade. Gen3/scripts/PROC_DIX_X_RESOLVER-ERROR-LEARN (behind both the
front and rear resolver-error learn tasks) is one of them.

Self-contained: every script and graph here is synthetic, written into a tmp
bundle laid out like the real one, so nothing depends on Tesla's ODIN bundle.
Covers running a script (input binding, ServiceOutput/int returns, metrics,
outputs, chaining), the virtual clock that keeps a deadline loop from spinning
against the wall clock, and the static analysis coverage/requirements read off a
script instead of off nodes.
"""
from pathlib import Path

import odin_coverage
import odin_runner
import odin_script_api
import odin_service
import pytest


def _write(bundle: Path, relbase: str, src: str) -> None:
    path = bundle / (relbase + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")


def _script(bundle: Path, relbase: str, body: str) -> None:
    """Write a native script file -- `network` is the SOURCE of the coroutine."""
    _write(bundle, relbase, "network = " + repr(body))


def _engine(bundle: Path, backend=None, **kw) -> odin_runner.Engine:
    return odin_runner.Engine(backend or odin_runner.MockBackend(), bundle, **kw)


# A bare task wrapper whose entry node calls a script, the shape every one of the
# real script-backed tasks has (PROC_DIR_X_RESOLVER-ERROR-LEARN included).
def _wrapper(script: str, inputs: str = "{}") -> str:
    return f'''
network = {{
    "task": {{"type": "scripts.RunScriptTest", "script_name": {script!r},
              "inputs": {inputs}}},
}}
'''


class TestRunScript:
    def test_task_wrapper_runs_a_script_and_returns_its_service_output(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, nodeName: str):
    out = api.service_output()
    await api.reporting.capture_metric(metric_name='NODE',
                                       result_code=api.metric_result.Pass,
                                       value=nodeName)
    out.set_user_facing_msg(f"ran on {nodeName}")
    out.set_exit_code(api.service_output_exit_reason.PASS)
    return out
""")
        _write(tmp_path, "T/tasks/E",
               _wrapper("T/scripts/S", '{"nodeName": {"value": "DIR"}}'))

        res = _engine(tmp_path).run_procedure("T/tasks/E")

        assert res.exit_code == 0                       # PASS is 0: `passed` reads it
        assert res.outputs["service_output"]["user_facing_msg"] == "ran on DIR"
        assert [(m["metric"], m["value"]) for m in res.metrics] == [("NODE", "DIR")]

    def test_a_failing_verdict_is_a_non_zero_exit_code(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    return api.service_output(user_facing_msg="nope",
                              exit_code=api.service_output_exit_reason.INVALID_VEHICLE_STATE)
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        res = _engine(tmp_path).run_procedure("T/tasks/E")

        assert res.exit_code == odin_script_api.ExitReason.INVALID_VEHICLE_STATE
        assert res.exit_code != 0

    @pytest.mark.parametrize(("returned", "expected"), [
        ("return 0", 0), ("return 7", 7), ("return None", 0), ("pass", 0),
    ])
    def test_a_bare_int_or_no_return_is_the_exit_code(self, tmp_path, returned, expected):
        _script(tmp_path, "T/scripts/S",
                f"async def odin_script_test(api):\n    {returned}\n")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        assert _engine(tmp_path).run_procedure("T/tasks/E").exit_code == expected

    def test_set_output_lands_in_the_run_outputs(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, value: int):
    await api.networks.set_output(key='doubled', value=value * 2)
    return 0
""")
        _write(tmp_path, "T/tasks/E",
               _wrapper("T/scripts/S", '{"value": {"value": 21}}'))

        assert _engine(tmp_path).run_procedure("T/tasks/E").outputs["doubled"] == 42


class TestInputBinding:
    def _run(self, tmp_path, inputs: str):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, required_in: str, optional_in: int = 9):
    await api.networks.set_output(key='seen', value=[required_in, optional_in])
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S", inputs))
        return _engine(tmp_path).run_procedure("T/tasks/E")

    def test_declared_default_fills_an_unbound_parameter(self, tmp_path):
        res = self._run(tmp_path, '{"required_in": {"value": "x"}}')
        assert res.outputs["seen"] == ["x", 9]

    def test_a_bound_none_means_unset_and_falls_back_to_the_default(self, tmp_path):
        # ODIN spells "unset" as a None-valued binding (see _data_networks_Input),
        # so a caller passing None must NOT shadow the script's own default.
        res = self._run(tmp_path,
                        '{"required_in": {"value": "x"}, "optional_in": {"value": None}}')
        assert res.outputs["seen"] == ["x", 9]

    def test_a_parameter_with_no_value_and_no_default_is_none(self, tmp_path):
        # ODIN's own semantics: an unbound networks.Input reads as None, and the
        # scripts are written for it -- UPDATE_MODULE takes `job_id: str` with no
        # default and normalises a non-numeric one to ''. Refusing here would
        # block procedures the real tool runs.
        res = self._run(tmp_path, "{}")
        assert res.outputs["seen"] == [None, 9]


class TestVirtualClock:
    def test_a_wall_clock_deadline_loop_ends_without_sleeping(self, tmp_path):
        # The real resolver-error-learn script polls for up to 95 s in a
        # `while time() < start + TIMEOUT` loop with `await sleep(3)` inside. With
        # only sleep sped up, that loop would spin against the wall clock for the
        # full 95 s; the clock's virtual offset is what makes it terminate.
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    from asyncio import sleep
    from time import time
    start = time()
    passes = 0
    while time() < start + 95.0:
        passes += 1
        await sleep(3.0)
    await api.networks.set_output(key='passes', value=passes)
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        import time as _t
        began = _t.monotonic()
        res = _engine(tmp_path, time_scale=0.0).run_procedure("T/tasks/E")

        assert res.outputs["passes"] == 32          # ceil(95/3): the loop DID run
        assert _t.monotonic() - began < 5.0         # ...but took no real time

    def test_real_timings_are_preserved_at_scale_one(self):
        clock = odin_script_api.ScriptClock(1.0)
        before = clock.time()
        assert clock.offset == 0.0
        assert abs(clock.time() - before) < 1.0     # tracks the real clock


class TestChaining:
    def test_a_script_runs_another_script_and_reads_its_outputs(self, tmp_path):
        _script(tmp_path, "T/scripts/CHILD", """
async def odin_script_test(api, value: int):
    await api.networks.set_output(key='multiplied', value=value * 2)
    return 0
""")
        _script(tmp_path, "T/scripts/PARENT", """
async def odin_script_test(api):
    child = await api.scripts.script_test(script_name='T/scripts/CHILD', value=4)
    await api.networks.set_output(key='from_child', value=child['multiplied'])
    await api.networks.set_output(key='child_exit', value=child['exit_code'])
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/PARENT"))

        res = _engine(tmp_path).run_procedure("T/tasks/E")

        assert res.outputs["from_child"] == 8
        assert res.outputs["child_exit"] == 0

    def test_a_script_runs_a_wired_graph_and_its_metrics_bubble_up(self, tmp_path):
        _write(tmp_path, "T/lib/G", '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "n": {"type": "networks.Input", "default": {"value": 0}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "GRAPH"},
            "value": {"connection": "n.value"},
            "done": {"connection": "setout.set"}},
    "setout": {"type": "networks.SetOutput", "key": "echoed",
               "value": {"connection": "n.value"},
               "finished": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
}
''')
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    res = await api.subnetwork.run_reference('T/lib/G', n=11)
    await api.networks.set_output(key='echoed', value=res['echoed'])
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        res = _engine(tmp_path).run_procedure("T/tasks/E")

        assert res.outputs["echoed"] == 11
        assert [(m["metric"], m["value"]) for m in res.metrics] == [("GRAPH", 11)]

    def test_a_script_reached_through_a_wired_graph_still_runs(self, tmp_path):
        # script -> graph -> task-wrapper -> script: the middle hop is synchronous,
        # so the inner script cannot re-enter the outer event loop (run_coroutine
        # gives it its own). Regression guard for that nesting.
        _script(tmp_path, "T/scripts/INNER", """
async def odin_script_test(api):
    await api.networks.set_output(key='inner', value='ran')
    return 0
""")
        _write(tmp_path, "T/lib/MID", '''
network = {
    "task": {"type": "scripts.RunScriptTest", "script_name": "T/scripts/INNER",
             "inputs": {}},
}
''')
        _script(tmp_path, "T/scripts/OUTER", """
async def odin_script_test(api):
    res = await api.subnetwork.run_reference('T/lib/MID')
    await api.networks.set_output(key='inner', value=res.get('inner'))
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/OUTER"))

        assert _engine(tmp_path).run_procedure("T/tasks/E").outputs["inner"] == "ran"


class TestFacadeInterop:
    def test_cid_and_uds_calls_route_to_the_backend(self, tmp_path):
        calls: list = []

        class _Uds:
            def __init__(self, node):
                self.node = node

            def diagnostic_session(self, session_type):
                calls.append(("session", self.node, session_type))

            def security_access(self, level):
                calls.append(("secacc", self.node, level))

            def tester_present(self):
                calls.append(("tp", self.node))

        class _Backend(odin_runner.Backend):
            def __init__(self):
                self.values = {"VAPI_driveRailOn": "true"}

            def uds(self, node_name):
                return _Uds(node_name)

            def cid_get(self, name):
                return self.values.get(name)

            def cid_set(self, name, value):
                self.values[name] = value

        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, nodeName: str):
    rail = await api.cid.get_data_value_until(data_name='VAPI_driveRailOn',
                                              pass_value='true', timeout=30)
    if not rail['passed']:
        return 1
    await api.cid.set_data_value(data_name='GUI_serviceMode', value=1)
    async with api.uds.uds_tester_present_context(uds_node_name=nodeName, interval=0.1):
        await api.uds.uds_diagnostic_session(node_name=nodeName,
                                             session_type='EXTENDED_DIAGNOSTIC_SESSION')
        await api.uds.uds_security_access(node_name=nodeName, security_level='LEVEL_5')
    return 0
""")
        _write(tmp_path, "T/tasks/E",
               _wrapper("T/scripts/S", '{"nodeName": {"value": "DIR"}}'))
        backend = _Backend()

        res = _engine(tmp_path, backend).run_procedure("T/tasks/E")

        assert res.exit_code == 0
        assert calls == [("tp", "DIR"),
                         ("session", "DIR", "EXTENDED_DIAGNOSTIC_SESSION"),
                         ("secacc", "DIR", "LEVEL_5")]
        assert backend.values["GUI_serviceMode"] == 1

    def test_tester_present_context_applies_the_script_interval(self, tmp_path):
        periods: list = []

        class _Uds:
            period = 0.5

            def tester_present(self):
                periods.append(("tp", _Uds.period))

            def tester_present_interval(self, seconds):
                previous, _Uds.period = _Uds.period, seconds
                return previous

            def diagnostic_session(self, session_type):
                periods.append(_Uds.period)

        class _Backend(odin_runner.Backend):
            def uds(self, node_name):
                return _Uds()

        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    async with api.uds.uds_tester_present_context(uds_node_name='DIR', interval=0.1):
        await api.uds.uds_diagnostic_session(node_name='DIR', session_type='DEFAULT_SESSION')
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        assert _engine(tmp_path, _Backend()).run_procedure("T/tasks/E").exit_code == 0
        # one sent at the new period before the block's first request; restored after
        assert periods == [("tp", 0.1), 0.1] and _Uds.period == 0.5

    def test_get_data_value_until_reports_a_timeout_rather_than_raising(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    res = await api.cid.get_data_value_until(data_name='VAPI_driveRailOn',
                                             pass_value='true', timeout=30)
    await api.networks.set_output(key='passed', value=res['passed'])
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        # MockBackend has no VAPI_driveRailOn -> the poll times out, which is the
        # script's own "operator did not turn the drive rail on" branch.
        assert _engine(tmp_path).run_procedure("T/tasks/E").outputs["passed"] is False

    def test_a_narrating_script_reports_progress_on_the_event_stream(self, tmp_path):
        # A long procedure that says nothing looks hung. ODIN's own narration
        # (messages.progress_update / status_update) goes out as events so a
        # caller can show a bar and a step name instead of a blank pane.
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    await api.messages.status_update(status='Erasing')
    await api.messages.progress_update(value=40)
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))
        events = []

        odin_runner.Engine(odin_runner.MockBackend(), tmp_path,
                           on_event=lambda k, p: events.append((k, p))
                           ).run_procedure("T/tasks/E")

        kinds = {k for k, _ in events}
        assert {"status", "progress"} <= kinds
        progress = next(p for k, p in events if k == "progress")
        assert (progress["value"], progress["units"]) == (40, "percent")
        assert next(p for k, p in events if k == "status")["status"] == "Erasing"

    def test_caller_principals_come_from_the_engine(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    who = (await api.odin.get_caller_principals())['caller_principals']
    await api.networks.set_output(key='who', value=who)
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        res = _engine(tmp_path, principals=("tbx-external",)).run_procedure("T/tasks/E")
        assert res.outputs["who"] == ["tbx-external"]

    def test_the_odin_isotp_error_types_are_importable(self, tmp_path):
        # Two real scripts catch these by name; the framework class only exists on
        # the MCU, so the facade serves its own to their import.
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    from odin.core.isotp.error import ISOTPError, TimeoutBs
    try:
        raise TimeoutBs('no flow control')
    except ISOTPError:
        return 0
    return 1
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        assert _engine(tmp_path).run_procedure("T/tasks/E").exit_code == 0


class TestStaticAnalysis:
    def test_supported_covers_the_facade_and_excludes_http_and_modem(self):
        supported = odin_script_api.supported()
        assert "api.cid.get_data_value_until" in supported
        assert "api.uds.uds_routine_control" in supported
        assert "api.odx.odx_request_results" in supported
        assert "api.service_output_exit_reason" in supported
        # Deliberately absent: they reach the Tesla mothership / the LTE modem,
        # and a stub would make a connectivity test pass for the wrong reason.
        assert not any(s.startswith(("api.http.", "api.modem.")) for s in supported)
        assert "api.cid.car_server_request" not in supported

    def test_required_names_the_calls_a_script_makes(self):
        req = odin_script_api.required("""
async def odin_script_test(api):
    await api.cid.set_data_value(data_name='x', value=1)
    await api.http.request(url='https://tesla')
    code = api.metric_result.Fail
    return 0
""")
        assert "api.cid.set_data_value" in req
        assert "api.http.request" in req
        # An enum MEMBER collapses to its namespace -- api.metric_result is the
        # capability, not each of Pass/Fail/Skip.
        assert "api.metric_result" in req and "api.metric_result.Fail" not in req

    def test_required_flags_an_unavailable_import(self):
        assert odin_script_api.required(
            "from odin.private.thing import X\nasync def odin_script_test(api):\n"
            "    return 0\n") == {"import.odin.private.thing"}

    def test_required_flags_a_source_the_firmware_ships_broken(self):
        # One script on 2022.45.15 (PROC_IBST_X_READ-RPS-AT-IDLE) does not parse.
        assert odin_script_api.required("def x():\n  a = 1\n   b = 2\n") == \
            {"script.syntax-error"}

    def test_referenced_finds_the_graphs_a_script_calls(self):
        assert odin_script_api.referenced("""
async def odin_script_test(api):
    await api.subnetwork.run_reference('Common/lib/THING', a=1)
    await api.scripts.script_test(script_name="T/scripts/OTHER")
""") == {"Common/lib/THING", "T/scripts/OTHER"}

    def test_script_requirements_resolves_aliases(self):
        # Scripts bind the api functions to locals up front and call through those,
        # so a requirements scan that only matched `api.x.y(` would see nothing.
        req = odin_script_api.script_requirements("""
async def odin_script_test(api, nodeName: str):
    read = api.can.can_signal_read
    gdvu = api.cid.get_data_value_until
    await read(signal_name='DI_gear', bus_name='ETH')
    await read(signal_name=some_variable)
    await gdvu(data_name='VAPI_driveRailOn', pass_value='true')
    await api.uds.uds_security_access(node_name='DIR', security_level='LEVEL_5')
    await api.vehiclecontrols.ensure_application_state(node_name='ESP',
                                                       application_state='APPLICATION')
""")
        assert ("DI_gear", "ETH", "read") in req["signals"]
        assert req["dynamic"] == 1              # the computed signal name
        assert req["cid_values"] == {"VAPI_driveRailOn"}
        assert req["nodes"] == {"DIR", "ESP"}
        assert req["app_states"] == ["APPLICATION"]

    def test_script_requirements_reads_a_power_state_enum_member(self):
        req = odin_script_api.script_requirements("""
async def odin_script_test(api):
    async with api.vehiclecontrols.power_context(power_state=api.power_state_enum.ACCESSORY_PLUS):
        pass
""")
        assert req["power_states"] == ["ACCESSORY_PLUS"]


class TestCoverageAndDiscovery:
    def test_a_script_task_is_runnable_when_the_facade_covers_it(self, tmp_path):
        _script(tmp_path, "T/scripts/OK", """
async def odin_script_test(api):
    await api.cid.set_data_value(data_name='GUI_serviceMode', value=1)
    return 0
""")
        _write(tmp_path, "T/tasks/OK", _wrapper("T/scripts/OK"))

        procs = odin_service.list_procedures(bundle=tmp_path, entries="T/tasks",
                                             runnable_only=False)
        assert [(p["name"], p["runnable"], p["missing_types"]) for p in procs] == \
            [("OK", True, [])]

    def test_a_script_needing_http_is_blocked_not_silently_broken(self, tmp_path):
        # Before the facade existed these reported runnable and then died at run
        # time with "'str' object has no attribute 'values'".
        _script(tmp_path, "T/scripts/WEB", """
async def odin_script_test(api):
    await api.http.request(url='https://tesla')
    return 0
""")
        _write(tmp_path, "T/tasks/WEB", _wrapper("T/scripts/WEB"))

        procs = odin_service.list_procedures(bundle=tmp_path, entries="T/tasks",
                                             runnable_only=False)
        assert procs[0]["runnable"] is False
        assert procs[0]["missing_types"] == ["api.http.request"]

    def test_coverage_descends_through_a_script_into_the_graphs_it_calls(self, tmp_path):
        _write(tmp_path, "T/lib/BAD", '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "x.run"}},
    "x": {"type": "custommod.CustomNode"},
}
''')
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    await api.subnetwork.run_reference('T/lib/BAD')
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        procs = odin_service.list_procedures(bundle=tmp_path, entries="T/tasks",
                                             runnable_only=False)
        assert procs[0]["missing_types"] == ["custommod.CustomNode"]

    def test_handled_types_includes_the_facade(self):
        handled = odin_coverage.handled_types()
        assert "api.uds.uds_routine_control" in handled
        assert "cid.GetDataValueUntil" in handled       # the graph side still there

    def test_requirements_reads_a_script_reached_from_a_task(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api):
    await api.can.can_signal_read(signal_name='DI_gear', bus_name='ETH')
    await api.uds.uds_security_access(node_name='DIR', security_level='LEVEL_5')
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        req = odin_service.procedure_requirements("T/tasks/E", bundle=tmp_path)

        assert req["nodes"] == ["DIR"]
        assert req["signals"]["ETH"] == [{"signal": "DI_gear", "kind": "read"}]

    def test_the_ecu_a_task_binds_is_named_in_its_requirements(self, tmp_path):
        # The front and rear resolver-error-learn tasks are the SAME script with
        # nodeName='DIF'/'DIR'. Every UDS call inside names that parameter, so
        # without the caller's binding the readout would omit the one ECU the
        # task is actually about -- which is what the UI shows the operator.
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, nodeName: str, hasEsp: bool = True):
    await api.uds.uds_security_access(node_name=nodeName, security_level='LEVEL_5')
    await api.uds.uds_clear_dtcs(node_name='ESP')
    return 0
""")
        for task, node in (("REAR", "DIR"), ("FRONT", "DIF")):
            _write(tmp_path, f"T/tasks/{task}",
                   _wrapper("T/scripts/S", f'{{"nodeName": {{"value": "{node}"}}}}'))
            req = odin_service.procedure_requirements(f"T/tasks/{task}",
                                                      bundle=tmp_path)
            assert req["nodes"] == sorted({node, "ESP"})

    def test_a_declared_default_names_the_ecu_when_the_task_binds_nothing(self, tmp_path):
        _script(tmp_path, "T/scripts/S", """
async def odin_script_test(api, nodeName: str = 'PMR'):
    await api.uds.uds_ecu_reset(node_name=nodeName, reset_type='HARD_RESET')
    return 0
""")
        _write(tmp_path, "T/tasks/E", _wrapper("T/scripts/S"))

        assert odin_service.procedure_requirements(
            "T/tasks/E", bundle=tmp_path)["nodes"] == ["PMR"]
