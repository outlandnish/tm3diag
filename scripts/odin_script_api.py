#!/usr/bin/env python3
"""odin_script_api.py -- the `api` facade for ODIN NATIVE SCRIPT tests.

A bundle graph file is usually a wired node graph: its `network` is a dict of
nodes that odin_runner.Engine walks. **154 of the 1,955 files on 2022.45.15 are
not** -- their `network` is a Python SOURCE STRING defining

    async def odin_script_test(api, <declared inputs>): ...

which drives the same vehicle interop the graph nodes do, but as plain awaited
calls on an `api` facade instead of wired ports. `Gen3/scripts/PROC_DIX_X_
RESOLVER-ERROR-LEARN` (the REAR/FRONT resolver-error learn procedures) is one.

This module is that facade, mapped onto the same odin_runner.Backend the graph
handlers use, so both forms run against one bench with one set of adapters.

NOT IMPLEMENTED, deliberately: `api.http.*`, `api.modem.*` and
`api.cid.car_server_request` (which is an HTTP call to the car server). They
reach the Tesla mothership or the car's LTE modem -- neither exists on a bench,
and a stub would make a connectivity test pass for the wrong reason. Scripts
using them stay reported as blocked: `required()` names every `api.*` a script
touches and `supported()` names what is here, so odin_service filters them out
of the runnable list the same way it filters an unhandled node type.

Return shapes match what the scripts destructure (all read off the bundle):
    get_data_value        -> {'value': ...}
    get_data_value_until  -> {'passed': bool, 'value': ...}
    set_data_value        -> {'done': bool}
    uds_read_data         -> {'output_payload': bytes}
    odx_read_data         -> {'data': {param: value}}
    odx_*_routine/results -> {'results': {param: value}}
    execute_application   -> {'stdout': str, 'stderr': str, 'exit_status': int}
    can_signal_read       -> {'value': ...}
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import enum
import json
import re
import time as _time_mod
import types

# --------------------------------------------------------------------------------------
# enums the scripts read off the facade
# --------------------------------------------------------------------------------------
# ODIN's own numbering for these is not in the bundle (the framework ships as a
# PyInstaller binary), and only PASS == 0 is load-bearing -- odin_service reports
# `passed = exit_code == 0`, matching networks.Exit's integer exit codes. The
# non-zero ordering below is ours.


class ExitReason(enum.IntEnum):
    """api.service_output_exit_reason -- a script's terminal verdict."""

    PASS = 0
    FAIL = 1
    ERROR = 2
    TIMEOUT = 3
    INVALID_INPUT = 4
    INVALID_VEHICLE_STATE = 5
    UNKNOWN = 6


class MetricResult(enum.IntEnum):
    """api.metric_result -- per-metric verdict.

    Pass/Fail are 0/1 to match the graph form, whose result codes come from
    reporting.BoolToResultCode (0 on true, 1 on false).
    """

    Pass = 0
    Fail = 1
    Skip = 2


class PowerState(enum.IntEnum):
    """api.power_state_enum -- the rail a script needs the car brought to."""

    OFF = 0
    ACCESSORY = 1
    ACCESSORY_PLUS = 2
    DRIVE_RAIL = 3
    CONDITIONING = 4
    CONTACTORS_OPEN = 5


class OdinStatus(enum.IntEnum):
    """api.odin_status -- the run's UI state, attached to a status update."""

    RUNNING = 0
    WAITING_FOR_USER_INPUT = 1
    COMPLETE = 2


class ISOTPError(Exception):
    """odin.core.isotp.error.ISOTPError -- an ISO-TP transport failure.

    Two scripts catch this by name. The framework class lives on the MCU; the
    transport that would raise it here is uds_local's, so the type is defined
    with the facade and served to a script's import (see ScriptClock.modules).
    """


class TimeoutBs(ISOTPError):
    """odin.core.isotp.error.TimeoutBs -- no flow control within N_Bs."""


class ServiceOutput:
    """api.service_output -- what a script returns: a user-facing message, an
    ExitReason, and named data blocks. Built either fully-formed
    (`output(user_facing_msg=..., exit_code=...)`) or empty and then mutated."""

    def __init__(self, user_facing_msg: str = "", exit_code=ExitReason.UNKNOWN):
        self.user_facing_msg = user_facing_msg
        self.exit_code = exit_code
        self.data: list[dict] = []
        self.exceptions: list[str] = []

    def set_user_facing_msg(self, msg) -> None:
        self.user_facing_msg = msg

    def set_exit_code(self, code) -> None:
        self.exit_code = code

    def add_data(self, name=None, data_type=None, data=None) -> None:
        self.data.append({
            "name": name,
            "type": getattr(data_type, "__name__", data_type),
            "data": data,
        })

    def add_exception(self, exc) -> None:
        self.exceptions.append(f"{type(exc).__name__}: {exc}")

    def clear_data(self) -> None:
        self.data.clear()

    def as_dict(self) -> dict:
        out = {"user_facing_msg": self.user_facing_msg,
               "exit_code": int(self.exit_code) if isinstance(self.exit_code, int)
               else self.exit_code,
               "data": list(self.data)}
        if self.exceptions:
            out["exceptions"] = list(self.exceptions)
        return out

    def __repr__(self) -> str:
        return f"ServiceOutput(exit_code={self.exit_code!r}, msg={self.user_facing_msg!r})"


# --------------------------------------------------------------------------------------
# virtual clock: scripts `from asyncio import sleep` and deadline on `time()`
# --------------------------------------------------------------------------------------
class ScriptClock:
    """A time base for one script run, honouring Engine's `time_scale`.

    Scripts import `sleep` and `time` themselves and write real deadline loops
    (`while time() < start + 95: ... await sleep(3)`). Speeding up `sleep`
    alone would leave such a loop spinning against the wall clock for the full
    95 seconds; so a sleep that is not taken in real time is instead ADDED to a
    virtual offset that `time()`/`monotonic()` report. At time_scale 1.0 this is
    the identity; at 0.0 (the mock/test default) the script's clock jumps and
    the loop exits after one pass.
    """

    def __init__(self, time_scale: float = 0.0):
        self.time_scale = time_scale
        self.offset = 0.0

    async def sleep(self, delay=0, result=None):
        delay = delay or 0
        if self.time_scale:
            await asyncio.sleep(delay * self.time_scale)
        self.offset += delay * (1.0 - self.time_scale)
        return result

    async def wait_for(self, aw, timeout):
        # A scaled-to-zero timeout would fire before the awaitable ever ran, so
        # in fast mode wait without one -- nothing genuinely blocks there.
        scaled = timeout * self.time_scale if timeout is not None else None
        return await asyncio.wait_for(aw, scaled or None)

    def time(self):
        return _time_mod.time() + self.offset

    def monotonic(self):
        return _time_mod.monotonic() + self.offset

    def _module(self, real, **overrides):
        mod = types.SimpleNamespace(**{k: getattr(real, k) for k in dir(real)
                                       if not k.startswith("__")})
        for k, v in overrides.items():
            setattr(mod, k, v)
        return mod

    def modules(self) -> dict:
        """The modules a script's imports are redirected to, keyed by their full
        dotted name: the two stdlib ones the virtual clock owns, plus the ODIN
        framework's ISO-TP error types (a couple of scripts catch them by name;
        the transport that raises them is ours, so they are defined here rather
        than pulled off an MCU we do not have)."""
        return {
            "asyncio": self._module(asyncio, sleep=self.sleep, wait_for=self.wait_for),
            "time": self._module(_time_mod, time=self.time, monotonic=self.monotonic),
            "odin.core.isotp.error": types.SimpleNamespace(
                ISOTPError=ISOTPError, TimeoutBs=TimeoutBs),
        }

    def builtins(self) -> dict:
        """A `__builtins__` whose __import__ serves the shimmed modules.

        `from asyncio import sleep` compiles to a call on the executing frame's
        __import__, so overriding it in the script's own globals redirects that
        one script without touching sys.modules or any other thread. A non-empty
        fromlist means the caller wants the DEEPEST module named, which is what
        the shim table is keyed by; a bare `import x.y` wants the top package,
        which nothing shimmed here is imported as.
        """
        ns = dict(vars(builtins))
        shims = self.modules()
        real_import = builtins.__import__

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and name in shims:
                return shims[name]
            return real_import(name, globals, locals, fromlist, level)

        ns["__import__"] = _import
        return ns


# --------------------------------------------------------------------------------------
# the facade
# --------------------------------------------------------------------------------------
class _Ns:
    """One `api.<name>` namespace. Its public coroutines ARE the surface: they
    name the api functions, so `supported()` is derived from them and cannot
    drift from what is actually implemented."""

    def __init__(self, api: ScriptApi):
        self._api = api
        self._engine = api._engine
        self._frame = api._frame

    def _log(self, msg) -> None:
        self._engine._log(self._frame.depth, f"     {msg}")

    @property
    def _backend(self):
        return self._engine.backend

    @property
    def _scale(self) -> float:
        return self._api.clock.time_scale


class _Reporting(_Ns):
    async def capture_metric(self, metric_name=None, value=None, result_code=None,
                             expected_value=None, metadata=None, high_limit=None):
        metric = {"metric": metric_name, "value": value,
                  "result_code": result_code, "expected": expected_value}
        if metadata is not None:
            metric["metadata"] = metadata
        if high_limit is not None:
            metric["high_limit"] = high_limit
        self._frame.metrics.append(metric)
        self._log(f"metric {metric_name} = {value!r} rc={result_code}")
        self._engine._emit("metric", metric)
        return {"done": True}

    async def debug_print(self, value=None):
        self._log(f"print {value!r}")
        return {"done": True}


class _Cid(_Ns):
    async def get_data_value(self, data_name=None, timeout=None):
        return {"value": self._backend.cid_get(data_name)}

    async def get_data_value_until(self, data_name=None, pass_value=None,
                                   operator=0, timeout=10, sleep=0.25):
        """Poll a CID data value until it compares equal (or `operator`) to
        `pass_value`. Mirrors cid.GetDataValueUntil, but returns the verdict
        instead of firing a passed/timed_out port."""
        deadline = self._api.clock.monotonic() + (timeout or 0) * self._scale
        v = None
        first = True
        while first or self._api.clock.monotonic() < deadline:
            first = False
            v = self._backend.cid_get(data_name)
            if self._engine._cmp(operator, v, pass_value):
                self._log(f"cid {data_name}={v!r} == {pass_value!r} -> passed")
                return {"passed": True, "value": v}
            if self._frame.cancel.is_set():
                break
            await self._api.clock.sleep(sleep or 0.25)
        self._log(f"cid {data_name}={v!r} != {pass_value!r} -> timed out")
        return {"passed": False, "value": v}

    async def set_data_value(self, data_name=None, value=None):
        self._backend.cid_set(data_name, value)
        return {"done": True}

    async def list_data_values(self, data_names=None, names=None):
        return {"values": self._backend.cid_list_values(data_names or names)}

    async def save_data(self, filename=None, data=None):
        self._backend.cid_save(filename, data)
        return {"done": True}

    async def load_data(self, filename=None):
        blob = self._backend.cid_load(filename)
        if blob is None:
            raise FileNotFoundError(filename)
        return {"data": blob}

    async def load_bytes(self, filename=None):
        blob = self._backend.cid_load(filename)
        if blob is None:
            raise FileNotFoundError(filename)
        return {"data": blob if isinstance(blob, bytes) else str(blob).encode()}

    async def get_vin(self, in_hex=False):
        return {"vin": self._backend.cid_vin(in_hex)}

    async def get_vitals(self):
        return {"vitals": self._backend.cid_vitals()}

    async def get_platform(self):
        """The MCU generation a script branches on (`info_hw`: 'infoz', …).
        Served from vitals, which BenchBackend fills from the firmware dump."""
        vitals = self._backend.cid_vitals() or {}
        return {"info": {"info_hw": vitals.get("info_hw"),
                         "info_sw": vitals.get("info_sw")}}

    async def is_fused(self):
        return {"fused": bool(self._backend.cid_get("GUI_isFused") == "true")}

    async def get_directory_contents(self, directory=None, show_hidden=False,
                                     details=False):
        return {"result": self._backend.cid_list_dir(
            directory, show_hidden=bool(show_hidden), details=bool(details)),
            "error": ""}

    async def rmi_gtw_config_allowlist(self):
        """Which vehicle configs a non-internal principal may write. A bench
        runs as tbx-internal (see odin.get_caller_principals), which bypasses
        the allowlist, so an empty one is never consulted."""
        return {"allowlist": {}}

    async def set_vehicle_config(self, config_id=None, value=None, **_kw):
        self._backend.cid_set(f"config_{config_id}", value)
        return {"done": True}

    # -- shell execution: the firmware dump's binaries can't run, so these are
    # the Backend's canned success (same as the graph's cid.Execute* nodes).
    async def execute_application(self, path=None, args=None, user=None,
                                  whitelist_chars=None, timeout=None):
        return self._backend.cid_execute(kind="application", path=path,
                                         command=None, args=args, user=user)

    async def execute_script(self, path=None, args=None, user=None,
                             whitelist_chars=None, timeout=None):
        return self._backend.cid_execute(kind="script", path=path, command=None,
                                         args=args, user=user)

    async def cid_command(self, command=None, args=None, user=None, timeout=None):
        return self._backend.cid_execute(kind="command", path=None,
                                         command=command, args=args, user=user)

    async def sv_command(self, service=None, action=None, timeout=None):
        self._log(f"sv {action} {service}")
        return {"stdout": "", "stderr": "", "exit_status": 0}

    async def reboot_cid(self, **_kw):
        self._log("reboot cid (no-op on a bench)")
        return {"done": True}

    async def emit_reboot_gateway(self, **_kw):
        self._log("reboot gateway (no-op on a bench)")
        return {"done": True}

    async def clear_cache(self, **_kw):
        return {"done": True}


class _VehicleControls(_Ns):
    @contextlib.asynccontextmanager
    async def power_context(self, power_state=None, allow_higher=False, **_kw):
        self._backend.ensure_power_state(power_state)
        self._log(f"power context {power_state!r}")
        yield {"power_state": power_state}

    async def ensure_power_state(self, power_state=None, **_kw):
        self._backend.ensure_power_state(power_state)
        return {"done": True}

    async def ensure_application_state(self, node_name=None, application_state=None):
        self._backend.ensure_application_state(node_name, application_state)
        return {"done": True}


class _Uds(_Ns):
    def _sess(self, node_name):
        return self._backend.uds(node_name)

    @contextlib.asynccontextmanager
    async def uds_tester_present_context(self, uds_node_name=None, node_name=None,
                                         interval=None, **_kw):
        node = uds_node_name or node_name
        self._sess(node).tester_present()
        self._log(f"tester-present context {node}")
        yield {"node_name": node}

    @contextlib.asynccontextmanager
    async def uds_node_lock_context(self, node_name=None, **_kw):
        # Exclusive access to one node. A bench runs one procedure at a time,
        # so the lock is uncontended; the context just scopes the session.
        self._log(f"node lock {node_name}")
        yield {"node_name": node_name}

    async def uds_tester_present(self, node_name=None, **_kw):
        self._sess(node_name).tester_present()
        return {"done": True}

    async def uds_diagnostic_session(self, node_name=None, session_type=None,
                                     response_required=None):
        self._sess(node_name).diagnostic_session(session_type)
        return {"done": True}

    async def uds_security_access(self, node_name=None, security_level=None):
        self._sess(node_name).security_access(security_level)
        return {"done": True}

    async def uds_routine_control(self, node_name=None, routine_id=None,
                                  input_payload=None, routine_type=None):
        return {"output_payload": self._sess(node_name).routine_control(
            routine_id, input_payload, routine_type)}

    async def uds_read_data(self, node_name=None, data_id=None):
        return {"output_payload": self._sess(node_name).read_data(data_id)}

    async def uds_write_data(self, node_name=None, data_id=None, input_payload=None):
        self._sess(node_name).write_data(data_id, input_payload)
        return {"done": True}

    async def udsio_control(self, node_name=None, control_id=None,
                            control_type=None, input_payload=None):
        return {"output_payload": self._sess(node_name).io_control(
            control_id, control_type, input_payload)}

    async def uds_ecu_reset(self, node_name=None, reset_type=None,
                            response_required=None):
        self._sess(node_name).ecu_reset(reset_type, response_required)
        return {"done": True}

    async def uds_clear_dtcs(self, node_name=None, dtc_mask=None):
        self._sess(node_name).clear_dtcs(dtc_mask)
        return {"done": True}

    async def uds_read_dtcs(self, node_name=None, dtc_mask=None):
        return {"dtcs": self._sess(node_name).read_dtcs(dtc_mask)}


class _Odx(_Ns):
    def _sess(self, node_name):
        return self._backend.odx(node_name)

    async def odx_start_routine(self, node_name=None, routine_name=None,
                                input_parameters=None, params=None):
        # Scripts read startRoutineResult['results']['START_ROUTINE_RESULTS'] --
        # a field of the StartRoutine (0x31 01) RESPONSE, not RequestRoutineResults
        # (0x31 03, whose record is ROUTINE_STATUS / LEARN_RESULT). start_routine
        # returns that start response already decoded, so hand it straight back.
        return {"results": self._sess(node_name).start_routine(
            routine_name, input_parameters or params)}

    async def odx_stop_routine(self, node_name=None, routine_name=None,
                               input_parameters=None, params=None):
        self._sess(node_name).stop_routine(routine_name, input_parameters or params)
        return {"done": True}

    async def odx_request_results(self, node_name=None, routine_name=None,
                                  params=None):
        return {"results": self._sess(node_name).request_results(routine_name, params)}

    async def odx_start_and_wait_results(self, node_name=None, routine_name=None,
                                         status_parameter=None,
                                         in_progress_statuses=None, timeout=None,
                                         input_parameters=None, stop_routine=False,
                                         diagnostic_session=None, **_kw):
        return {"results": self._sess(node_name).start_and_wait(
            routine_name, status_parameter, in_progress_statuses or [True],
            timeout or 1, input_parameters=input_parameters,
            stop_routine=bool(stop_routine), cancel=self._frame.cancel,
            time_scale=self._scale)}

    async def odx_start_and_wait_results_v2(self, node_name=None, routine_name=None,
                                            status_parameter=None,
                                            in_progress_statuses=None,
                                            max_runtime=None, should_stop=False,
                                            diagnostic_session=None,
                                            input_parameters=None, **_kw):
        in_prog = in_progress_statuses or [True]
        try:
            results = self._sess(node_name).start_and_wait(
                routine_name, status_parameter, in_prog, max_runtime or 1,
                stop_routine=bool(should_stop), cancel=self._frame.cancel,
                time_scale=self._scale)
            final = results.get(status_parameter) if status_parameter else None
            ok = final not in in_prog
        except Exception as e:  # noqa: BLE001  (a routine/transport error is a fail)
            results, ok = {"error": str(e)}, False
        return {"results": results, "success": ok,
                "results_control_type": "REQUEST_ROUTINE_RESULTS"}

    async def odx_read_data(self, node_name=None, data_name=None):
        return {"data": self._sess(node_name).read_data(data_name)}

    async def odx_write_data(self, node_name=None, data_name=None, data=None,
                             input_parameters=None):
        self._sess(node_name).write_data(data_name, data if data is not None
                                         else input_parameters)
        return {"done": True}


class _Can(_Ns):
    async def can_signal_read(self, signal_name=None, bus_name=None, timeout=None):
        return {"value": self._backend.can_read(signal_name, bus_name)}

    async def can_signal_monitor(self, signal_name=None, bus_name=None, timeout=None):
        """Wait for the signal to CHANGE; report the value either way."""
        deadline = self._api.clock.monotonic() + (timeout or 10) * self._scale
        initial = self._backend.can_read(signal_name, bus_name)
        v = initial
        first = True
        while first or self._api.clock.monotonic() < deadline:
            first = False
            v = self._backend.can_read(signal_name, bus_name)
            if v is not None and v != initial:
                return {"value": v, "changed": True}
            if self._frame.cancel.is_set():
                break
            await self._api.clock.sleep(0.05)
        return {"value": v, "changed": False}

    async def active_alerts(self, bus=None, bus_name=None, prefix=None, audience=None):
        alerts = self._backend.can_active_alerts(bus or bus_name, prefix, audience)
        # Scripts do `res.get('alerts', {}).keys()`, so a mapping is the shape;
        # a Backend that hands back a bare list is normalised here.
        if isinstance(alerts, dict):
            return {"alerts": alerts}
        return {"alerts": dict.fromkeys(alerts or [], True)}


class _Messages(_Ns):
    async def status_update(self, status=None, odin_status=None, **_kw):
        self._log(f"status: {status!r}")
        self._engine._emit("status", {"status": status, "odin_status": odin_status,
                                      "source": "procedure"})
        return {"listen_id": f"status-{id(self._frame):x}", "done": True}

    async def progress_update(self, value=None, **_kw):
        self._log(f"progress {value}%")
        self._engine._emit("progress", {"value": value, "total": 100,
                                        "current": value, "units": "percent",
                                        "source": "procedure"})
        return {"done": True}

    async def send(self, payload=None, **_kw):
        self._log(f"msg.send {payload!r}")
        return {"done": True}

    async def broadcast(self, payload=None, **_kw):
        self._log(f"msg.broadcast {payload!r}")
        return {"done": True}

    @contextlib.asynccontextmanager
    async def listen(self, message_type=None, **_kw):
        # No operator UI on a bench, so an awaited message is treated as already
        # delivered -- the same call the graph's messages.Listen node makes.
        event = asyncio.Event()
        event.set()
        yield event


class _Proto(_Ns):
    async def read_file(self, filepath=None, mode="r", **_kw):
        return {"data": self._backend.cid_read_file(filepath, mode or "r")}

    async def save_image(self, filepath=None, data=None, **_kw):
        self._backend.cid_save(filepath, data)
        return {"done": True}


class _Odin(_Ns):
    async def is_remote_network_request(self):
        # A bench run is local by construction (no Tesla service network).
        return {"remote_request": False}

    async def get_caller_principals(self):
        return {"caller_principals": list(self._api.principals)}

    async def trigger_data_upload(self, node_name=None, timeout=None):
        # Uploading an ECU log to Tesla needs the service network; report the
        # no-op rather than a fabricated success id.
        self._log(f"data upload {node_name} (no service network on a bench)")
        return {"result": None}


class _Networks(_Ns):
    async def set_output(self, key=None, value=None):
        self._frame.outputs[key] = value
        self._log(f"output[{key!r}] = {value!r}")
        return {"done": True}

    async def dynamically_referenced_subnetwork(self, name=None, basename=None, **inputs):
        return await self._api._call_child(name or basename, inputs)


class _Subnetwork(_Ns):
    async def run_reference(self, basename=None, **inputs):
        return await self._api._call_child(basename, inputs)


class _Scripts(_Ns):
    async def script_test(self, script_name=None, **inputs):
        return await self._api._call_child(script_name, inputs)


class _CidUpdater(_Ns):
    async def command(self, command=None, **_kw):
        # The CID's updater daemon. Nothing to drive on a bench -- reported the
        # same way sv_command is, so a script's control flow still advances.
        self._log(f"cidupdater {command!r} (no-op on a bench)")
        return {"stdout": "", "stderr": "", "exit_status": 0}


_NAMESPACES = {
    "reporting": _Reporting,
    "cid": _Cid,
    "vehiclecontrols": _VehicleControls,
    "uds": _Uds,
    "odx": _Odx,
    "can": _Can,
    "messages": _Messages,
    "proto": _Proto,
    "odin": _Odin,
    "networks": _Networks,
    "subnetwork": _Subnetwork,
    "scripts": _Scripts,
    "cidupdater": _CidUpdater,
}

# Namespaces that are a value rather than a set of coroutines. A script reaching
# any member of one (api.metric_result.Fail) is satisfied by the namespace.
_VALUE_NAMESPACES = {
    "service_output", "service_output_exit_reason", "metric_result",
    "power_state_enum", "odin_status", "json", "misc",
}

# Default principals a bench run presents. tbx-internal is the unrestricted one
# (see cid.rmi_gtw_config_allowlist); the resolver-learn tasks require it or
# tbx-external in their TaskInfo.
DEFAULT_PRINCIPALS = ("tbx-internal",)


class ScriptApi:
    """The object bound to a native script's `api` parameter."""

    def __init__(self, engine, frame, principals=DEFAULT_PRINCIPALS):
        self._engine = engine
        self._frame = frame
        self.principals = tuple(principals)
        self.clock = ScriptClock(getattr(engine, "_time_scale", 0.0) or 0.0)
        for attr, cls in _NAMESPACES.items():
            setattr(self, attr, cls(self))
        # value namespaces
        self.service_output = ServiceOutput
        self.service_output_exit_reason = ExitReason
        self.metric_result = MetricResult
        self.power_state_enum = PowerState
        self.odin_status = OdinStatus
        self.json = json
        self.misc = types.SimpleNamespace(whitelist_regex=re)

    async def _call_child(self, basename, inputs):
        """Run another bundle graph (wired or native-script) and return its
        outputs plus exit_code, the dict shape the callers destructure."""
        result = await self._engine._run_child_async(
            basename, inputs, self._frame.depth + 1)
        self._frame.metrics.extend(result.metrics)
        return {**result.outputs, "exit_code": result.exit_code}


# --------------------------------------------------------------------------------------
# static analysis: what a script needs vs. what is implemented
# --------------------------------------------------------------------------------------
_API_RE = re.compile(r"\bapi\.(\w+)(?:\.(\w+))?")
_REF_RE = re.compile(r"""run_reference\s*\(\s*['"]([^'"]+)['"]""")
_SCRIPT_RE = re.compile(r"""script_name\s*=\s*['"]([^'"]+)['"]""")
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)

# Non-stdlib modules the facade itself serves to a script's import.
_SHIMMED_MODULES = {"odin.core.isotp.error"}

# Modules a script may import. Everything else (the rest of `odin.*`, the ODIN
# framework's own package) does not exist outside the MCU.
_STDLIB_OK = {
    "asyncio", "time", "enum", "math", "json", "re", "base64", "random",
    "datetime", "collections", "itertools", "functools", "struct", "string",
    "typing", "dataclasses", "hashlib", "binascii", "copy", "os", "sys",
    "statistics", "operator", "uuid", "textwrap", "urllib",
}


def supported() -> set[str]:
    """Every `api.*` name this facade implements, as coverage pseudo-types."""
    out = {f"api.{n}" for n in _VALUE_NAMESPACES}
    for ns, cls in _NAMESPACES.items():
        out.add(f"api.{ns}")
        for attr in dir(cls):
            if not attr.startswith("_") and callable(getattr(cls, attr)):
                out.add(f"api.{ns}.{attr}")
    return out


def required(source: str) -> set[str]:
    """Every capability a native script needs, in coverage-pseudo-type form.

    `api.<ns>.<fn>` for facade calls, `import.<mod>` for a module the bench
    cannot provide, and `script.syntax-error` for a source the firmware itself
    ships broken (one on 2022.45.15). Members of a value namespace collapse to
    the namespace, so `api.metric_result.Fail` needs only `api.metric_result`.
    """
    out: set[str] = set()
    try:
        compile(source, "<odin-script>", "exec")
    except SyntaxError:
        return {"script.syntax-error"}
    for ns, fn in _API_RE.findall(source):
        out.add(f"api.{ns}" if (not fn or ns in _VALUE_NAMESPACES)
                else f"api.{ns}.{fn}")
    for frm, plain in _IMPORT_RE.findall(source):
        mod = frm or plain
        if mod in _SHIMMED_MODULES:
            continue
        if mod and mod.split(".")[0] not in _STDLIB_OK:
            out.add(f"import.{mod}")
    return out


def referenced(source: str) -> set[str]:
    """Bundle basenames a native script runs, where they are string literals.

    Lets odin_coverage keep descending through a script into the graphs it
    calls; a computed target is invisible here, the same blind spot the graph
    walker has for a connection-sourced basename.
    """
    return set(_REF_RE.findall(source)) | set(_SCRIPT_RE.findall(source))


# The api calls that constitute a requirement, and the field each reads it from.
_REQ_CAN_KINDS = {"can.can_signal_read": "read", "can.can_signal_monitor": "monitor"}
_REQ_CID_READS = ("cid.get_data_value", "cid.get_data_value_until")
_REQ_NODE_PREFIXES = ("uds.", "odx.")


def _api_path(node, aliases: dict) -> str | None:
    """The `<ns>.<fn>` a call's callee names -- written out (`api.cid.set_data_value`)
    or through the alias scripts bind at the top (`sdv = api.cid.set_data_value`)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "api" and parts:
        return ".".join(reversed(parts))
    return None


def script_requirements(source: str, inputs: dict | None = None) -> dict:
    """What a native script needs on the bus / in the environment before it runs.

    The static counterpart of procedure_requirements' node walk: scripts have no
    nodes, so this reads the api calls instead -- resolving the aliases they bind
    up front (`gdv = api.cid.get_data_value`) so a call through one still counts.
    A non-literal signal name is counted as dynamic rather than guessed at, so
    the signal list stays a lower bound exactly as it is for a graph.

    `inputs` are the literals the CALLING task bound to the script's parameters.
    They matter: PROC_DIR_/PROC_DIF_X_RESOLVER-ERROR-LEARN are the same script
    with nodeName='DIR'/'DIF', and every UDS call inside names that parameter --
    so without the binding the readout would omit the one ECU the task is about.
    """
    req = {"signals": [], "alerts": [], "nodes": set(), "cid_values": set(),
           "app_states": [], "power_states": [], "dynamic": 0}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return req

    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                isinstance(n.targets[0], ast.Name):
            path = _api_path(n.value, {})
            if path:
                aliases[n.targets[0].id] = path

    # Parameter name -> the literal the caller bound (or the declared default).
    consts = {k: v for k, v in (inputs or {}).items() if isinstance(v, str)}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "odin_script_test":
            args = fn.args.args[1:]  # [0] is `api`
            for arg, default in zip(args[len(args) - len(fn.args.defaults):],
                                    fn.args.defaults, strict=False):
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    consts.setdefault(arg.arg, default.value)

    def kw(call, *names):
        """A keyword's literal string, None if absent, or `False` if it is present
        but computed -- which is a dynamic value. A bare name that IS one of the
        script's bound parameters resolves to what the caller passed."""
        for k in call.keywords:
            if k.arg not in names:
                continue
            if isinstance(k.value, ast.Constant):
                return k.value.value
            if isinstance(k.value, ast.Name) and k.value.id in consts:
                return consts[k.value.id]
            return False
        return None

    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        path = _api_path(call.func, aliases)
        if path is None:
            continue
        if path in _REQ_CAN_KINDS:
            sig = kw(call, "signal_name")
            if not sig:
                req["dynamic"] += 1
                continue
            req["signals"].append((sig, kw(call, "bus_name") or None,
                                   _REQ_CAN_KINDS[path]))
        elif path == "can.active_alerts":
            req["alerts"].append((kw(call, "bus", "bus_name") or None,
                                  kw(call, "prefix") or None))
        elif path in _REQ_CID_READS:
            dn = kw(call, "data_name")
            if dn:
                req["cid_values"].add(dn)
        elif path == "vehiclecontrols.ensure_application_state":
            st = kw(call, "application_state")
            if st and st not in req["app_states"]:
                req["app_states"].append(st)
            nn = kw(call, "node_name")
            if nn:
                req["nodes"].add(nn)
        elif path in ("vehiclecontrols.power_context",
                      "vehiclecontrols.ensure_power_state"):
            ps = kw(call, "power_state")
            if ps is False:  # power_state=api.power_state_enum.X -- read the member
                for k in call.keywords:
                    if k.arg == "power_state" and isinstance(k.value, ast.Attribute):
                        ps = k.value.attr
            if ps and ps not in req["power_states"]:
                req["power_states"].append(ps)
        elif path.startswith(_REQ_NODE_PREFIXES):
            nn = kw(call, "node_name", "uds_node_name")
            if nn:
                req["nodes"].add(nn)
    return req


def compile_script(source: str, clock: ScriptClock | None = None):
    """Compile a native script and return its `odin_script_test` coroutine.

    The function is defined in a namespace whose `__builtins__` carries the
    clock's __import__, so the `from asyncio import sleep` / `from time import
    time` that scripts do INSIDE the function body resolve to the shimmed
    modules -- per script, without touching sys.modules.
    """
    ns: dict = {"__builtins__": (clock or ScriptClock(1.0)).builtins()}
    exec(compile(source, "<odin-script>", "exec"), ns)  # noqa: S102
    fn = ns.get("odin_script_test")
    if fn is None:
        raise ValueError("script defines no `odin_script_test`")
    return fn
