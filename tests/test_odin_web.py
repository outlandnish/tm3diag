"""Tests for scripts/odin_web.py -- the aiohttp ODIN + DID endpoints.

Synthetic bundle, MockBackend, and a fake node_provider exercise every endpoint
against the aiohttp test client with no CAN bus. Each test runs under asyncio.run.
"""
import asyncio
import contextlib
import json
from pathlib import Path

import odin_web
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from odin_runner import MockBackend

from uds_local.node_config import NodeConfig
from uds_local.odj import FieldSpec, OdjEntry, SubSpec


# bundle fixtures
def _write(bundle: Path, relbase: str, src: str) -> None:
    path = bundle / (relbase + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")


_CAP = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "cap.capture"}},
    "cap": {"type": "reporting.CaptureMetric", "metric_name": {"value": "m"},
            "value": {"value": 42}, "result_code": {"value": 0},
            "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
    "info": {"type": "comments.TaskInfo", "title": "Cap It"},
}
'''
_BLOCKED = '''
network = {
    "enter": {"type": "networks.Enter", "start": {"connection": "x.run"}},
    "x": {"type": "custommod.CustomNode", "done": {"connection": "exit.exit"}},
    "exit": {"type": "networks.Exit", "exit_code": {"value": 0}},
    "info": {"type": "comments.TaskInfo", "title": "Blocked"},
}
'''
_REQ = '''
network = {
    "r": {"type": "can.CANSignalRead",
          "signal_name": {"value": "GTW_drivetrainType"}, "bus_name": {"value": "ETH"}},
    "info": {"type": "comments.TaskInfo", "title": "Req", "valid_states": ["Parked"]},
}
'''


def _bundle(tmp_path: Path) -> Path:
    _write(tmp_path, "Model3/tasks/CAP", _CAP)
    _write(tmp_path, "Model3/tasks/BLOCKED", _BLOCKED)
    return tmp_path


# DID stubs
def _fs(bit_length, byte_position, bit_position=0, data_type="uint", enum=None):
    return FieldSpec(bit_length=bit_length, byte_position=byte_position,
                     bit_position=bit_position, data_type=data_type, enum_map=enum or {})


_MODE = _fs(8, 0, 0, "uint", {"OFF": 0, "ON": 1})
_CFG = OdjEntry(
    name="CFG", hex_id=0x0500,
    read=SubSpec(security_level=0, input={}, output={"MODE": _MODE},
                 input_size=0, output_size=1),
    write=SubSpec(security_level=5, input={"MODE": _MODE}, output={},
                  input_size=1, output_size=0))


def _did_cfg():
    return NodeConfig(name="DI", request_can_id=1, response_can_id=2,
                      security_algorithm="tesla_hash", security_buffer_size=16,
                      security_kw={}, dids={"CFG": _CFG})


class FakeSession:
    def __init__(self):
        self.calls = []

    def diagnostic_session(self, mode):
        self.calls.append(("diagnostic_session", mode))

    def security_access(self, level_idx=0, seed_level=None):
        self.calls.append(("security_access", seed_level))

    def read_did(self, did):
        self.calls.append(("read_did", did))
        return b"\x01"

    def write_did(self, did, data):
        self.calls.append(("write_did", did, bytes(data)))

    # low-level UDS ops
    def ecu_reset(self, reset_type):
        self.calls.append(("ecu_reset", reset_type))

    def ecu_reset_no_wait(self, reset_type):
        self.calls.append(("ecu_reset_no_wait", reset_type))

    def start_tester_present(self):
        self.calls.append(("tp_start",))

    def stop_tester_present(self):
        self.calls.append(("tp_stop",))

    def routine_control(self, routine_id, arg=b"", subtype=0x01):
        self.calls.append(("routine_control", routine_id, bytes(arg), subtype))
        return b"\x01\x02"

    def read_dtcs(self, status_mask=0xFF):
        self.calls.append(("read_dtcs", status_mask))
        return {0xC00100: 0x2F}

    def clear_dtc(self, group=0xFFFFFF):
        self.calls.append(("clear_dtc", group))


class BootSession(FakeSession):
    """read_did answers like a node sitting in its bootloader: 0xF180 byte 8 == 0
    (fw_type BOOTLOADER) and 0xF181 (app-only) raising an NRC."""

    def read_did(self, did):
        self.calls.append(("read_did", did))
        if did == 0xF180:
            return bytes.fromhex("001122334455667700")
        if did == 0xF181:
            raise RuntimeError("NRC 0x31")
        return b"\xde\xad"


class FakeBackend:
    """Just the seam the bootloader ops need off the real BenchBackend."""

    def __init__(self):
        self.states = []

    def ensure_application_state(self, node, state):
        self.states.append((node, state))


def _mock_factory():
    return MockBackend("success")


@contextlib.asynccontextmanager
async def _client(**kw):
    app = web.Application()
    odin_web.setup_routes(app, **kw)
    async with TestClient(TestServer(app)) as client:
        yield client


class TestProcedures:
    def test_runnable_list(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path)) as client:
                r = await client.get("/api/odin/procedures")
                assert r.status == 200
                names = {p["name"] for p in await r.json()}
                assert names == {"CAP"}
        asyncio.run(body())

    def test_all_includes_blocked(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path)) as client:
                r = await client.get("/api/odin/procedures?all=1")
                procs = {p["name"]: p for p in await r.json()}
                assert set(procs) == {"CAP", "BLOCKED"}
                assert procs["BLOCKED"]["runnable"] is False
                assert "custommod.CustomNode" in procs["BLOCKED"]["missing_types"]
        asyncio.run(body())


class TestVehicles:
    """The picker's backing endpoints. Only Model3/tasks used to be reachable, so
    the Gen3 procedures a Model 3 actually runs were invisible."""

    def _multi(self, tmp_path):
        _bundle(tmp_path)                                  # Model3: CAP, BLOCKED
        _write(tmp_path, "Gen3/tasks/GEN3CAP", _CAP)
        _write(tmp_path, "ModelY/tasks/YCAP", _CAP)
        return tmp_path

    def test_lists_cars_and_a_default(self, tmp_path):
        async def body():
            async with _client(bundle=self._multi(tmp_path)) as client:
                data = await (await client.get("/api/odin/vehicles")).json()
                assert data["vehicles"] == ["Model3", "ModelY"]   # Gen3 is shared
                assert data["default"] == "Model3"
        asyncio.run(body())

    def test_a_car_gets_its_own_plus_the_shared_procedures(self, tmp_path):
        async def body():
            async with _client(bundle=self._multi(tmp_path)) as client:
                r = await client.get("/api/odin/procedures?vehicle=Model3")
                assert {p["name"] for p in await r.json()} == {"CAP", "GEN3CAP"}
        asyncio.run(body())

    def test_each_car_is_cached_separately(self, tmp_path):
        # One cache for all cars would have served Model3's list for ModelY.
        async def body():
            async with _client(bundle=self._multi(tmp_path)) as client:
                await client.get("/api/odin/procedures?vehicle=Model3")
                r = await client.get("/api/odin/procedures?vehicle=ModelY")
                assert {p["name"] for p in await r.json()} == {"YCAP", "GEN3CAP"}
        asyncio.run(body())

    def test_an_unknown_car_is_404_not_an_empty_list(self, tmp_path):
        async def body():
            async with _client(bundle=self._multi(tmp_path)) as client:
                r = await client.get("/api/odin/procedures?vehicle=ModelZ")
                assert r.status == 404
        asyncio.run(body())

    def test_no_car_falls_back_to_the_default(self, tmp_path):
        async def body():
            async with _client(bundle=self._multi(tmp_path)) as client:
                r = await client.get("/api/odin/procedures")
                assert {p["name"] for p in await r.json()} == {"CAP", "GEN3CAP"}
        asyncio.run(body())


class TestRequirements:
    def test_returns_signals_grouped_by_bus(self, tmp_path):
        async def body():
            _write(_bundle(tmp_path), "Model3/tasks/REQ", _REQ)
            async with _client(bundle=tmp_path) as client:
                r = await client.get("/api/odin/requirements?procedure=Model3/tasks/REQ")
                assert r.status == 200
                req = await r.json()
                assert req["signals"]["ETH"] == [
                    {"signal": "GTW_drivetrainType", "kind": "read"}]
                assert req["preconditions"]["valid_states"] == ["Parked"]
                assert req["dynamic_count"] == 0
        asyncio.run(body())

    def test_missing_procedure_param_is_400(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path)) as client:
                r = await client.get("/api/odin/requirements")
                assert r.status == 400
        asyncio.run(body())

    def test_unknown_procedure_is_404(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path)) as client:
                r = await client.get("/api/odin/requirements?procedure=Model3/tasks/NOPE")
                assert r.status == 404
        asyncio.run(body())


class TestRun:
    def test_run_returns_result(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=_mock_factory) as client:
                r = await client.post("/api/odin/run",
                                      json={"procedure": "Model3/tasks/CAP"})
                assert r.status == 200
                res = await r.json()
                assert res["passed"] is True and res["exit_code"] == 0
                assert [m["value"] for m in res["metrics"]] == [42]
        asyncio.run(body())

    def test_missing_procedure_is_400(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path)) as client:
                r = await client.post("/api/odin/run", json={})
                assert r.status == 400
        asyncio.run(body())

    def test_second_run_while_locked_is_409(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=_mock_factory) as client:
                svc = client.app[odin_web.ODIN_WEB]
                await svc._run_lock.acquire()      # simulate an in-flight run
                try:
                    r = await client.post("/api/odin/run",
                                          json={"procedure": "Model3/tasks/CAP"})
                    assert r.status == 409
                finally:
                    svc._run_lock.release()
        asyncio.run(body())

    def test_ws_streams_metric_and_done(self, tmp_path):
        async def body():
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=_mock_factory) as client:
                ws = await client.ws_connect("/ws/odin")
                r = await client.post("/api/odin/run",
                                      json={"procedure": "Model3/tasks/CAP"})
                assert r.status == 200
                events = []
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type == WSMsgType.TEXT:
                        d = json.loads(msg.data)
                        events.append(d)
                        if d["type"] == "done":
                            break
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                await ws.close()
                kinds = [e["type"] for e in events]
                assert "metric" in kinds and kinds[-1] == "done"
                metric = next(e for e in events if e["type"] == "metric")
                assert metric["metric"] == "m" and metric["value"] == 42
        asyncio.run(body())


class TestDid:
    def test_list(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.get("/api/did/DI")
                assert r.status == 200
                dids = await r.json()
                assert [d["name"] for d in dids["read"]] == ["CFG"]
                assert dids["write"][0]["security_level"] == 5
        asyncio.run(body())

    def test_read_decodes_enum(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/did/read",
                                      json={"node": "DI", "did": "CFG"})
                assert r.status == 200
                res = await r.json()
                assert res["fields"] == {"MODE": "ON"} and res["raw"] == "01"
        asyncio.run(body())

    def test_write_encodes_and_runs_security(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/did/write",
                                      json={"node": "DI", "did": "CFG",
                                            "values": {"MODE": "ON"}})
                assert r.status == 200
                assert ("security_access", 5) in sess.calls
                assert ("write_did", 0x0500, b"\x01") in sess.calls
        asyncio.run(body())

    def test_did_without_provider_is_503(self):
        async def body():
            async with _client() as client:      # no node_provider
                r = await client.get("/api/did/DI")
                assert r.status == 503
        asyncio.run(body())


class TestLowLevelUds:
    """The generic UDS primitives exposed alongside the packaged procedures."""

    def test_catalog_lists_ops_with_danger_flags(self):
        async def body():
            async with _client() as client:
                r = await client.get("/api/uds/ops")
                assert r.status == 200
                ops = await r.json()
                by_id = {o["op"]: o for o in ops}
                assert "enter_bootloader" in by_id and "probe_state" in by_id
                assert by_id["enter_bootloader"]["danger"] is True
                assert by_id["ecu_reset"]["danger"] is True
                assert "danger" not in by_id["probe_state"]
        asyncio.run(body())

    def test_probe_state_reads_fw_type_and_f181(self):
        async def body():
            sess = BootSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "probe_state"})
                assert r.status == 200
                res = (await r.json())["result"]
                assert res["fw_type"] == 0
                assert res["state"] == "BOOTLOADER"
                assert res["f181"] == "NRC (bootloader)"
        asyncio.run(body())

    def test_enter_bootloader_uses_backend_handover(self):
        async def body():
            sess, backend = BootSession(), FakeBackend()
            async with _client(backend_factory=lambda: backend,
                               node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "enter_bootloader"})
                assert r.status == 200
                assert backend.states == [("DI", "BOOTLOADER")]
                assert (await r.json())["result"]["probe"]["state"] == "BOOTLOADER"
        asyncio.run(body())

    def test_enter_bootloader_without_any_backend_is_400(self):
        async def body():
            sess = BootSession()
            # No backend_factory -> nothing to drive the handover with.
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "enter_bootloader"})
                assert r.status == 400
                assert "bench backend" in (await r.json())["error"]
        asyncio.run(body())

    def test_ecu_reset_honours_type_and_no_wait(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op", json={
                    "node": "DI", "op": "ecu_reset",
                    "args": {"type": "soft", "no_wait": True}})
                assert r.status == 200
                assert ("ecu_reset_no_wait", 0x03) in sess.calls

                await client.post("/api/uds/op", json={
                    "node": "DI", "op": "ecu_reset", "args": {"type": "hard"}})
                assert ("ecu_reset", 0x01) in sess.calls
        asyncio.run(body())

    def test_hex_fields_parse_as_hex_not_decimal(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op", json={
                    "node": "DI", "op": "routine_control",
                    "args": {"routine_id": "0403", "subtype": 1, "arg": "01 ff"}})
                assert r.status == 200
                # 0403 is hex 0x0403 (1027), NOT decimal 403
                assert ("routine_control", 0x0403, b"\x01\xff", 1) in sess.calls
                assert (await r.json())["result"]["routine"] == "0x0403"
        asyncio.run(body())

    def test_read_dtcs_and_clear(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "read_dtcs"})
                res = (await r.json())["result"]
                assert res["count"] == 1
                assert res["dtcs"] == [{"dtc": "0xC00100", "status": "0x2F"}]

                r = await client.post("/api/uds/op", json={
                    "node": "DI", "op": "clear_dtc", "args": {"group": "FFFFFF"}})
                assert r.status == 200
                assert ("clear_dtc", 0xFFFFFF) in sess.calls
        asyncio.run(body())

    def test_unknown_op_and_missing_node_are_400(self):
        async def body():
            sess = FakeSession()
            async with _client(node_provider=lambda n: (_did_cfg(), sess)) as client:
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "not-an-op"})
                assert r.status == 400
                r = await client.post("/api/uds/op", json={"op": "probe_state"})
                assert r.status == 400
        asyncio.run(body())

    def test_uds_op_without_provider_is_503(self):
        async def body():
            async with _client() as client:      # no node_provider
                r = await client.post("/api/uds/op",
                                      json={"node": "DI", "op": "probe_state"})
                assert r.status == 503
        asyncio.run(body())

    def test_nrc_from_the_ecu_becomes_400_not_500(self):
        async def body():
            class Boom(FakeSession):
                def read_did(self, did):
                    raise RuntimeError("NRC 0x33 securityAccessDenied")

            async with _client(node_provider=lambda n: (_did_cfg(), Boom())) as client:
                r = await client.post("/api/uds/op", json={
                    "node": "DI", "op": "read_did_raw", "args": {"did": "F190"}})
                assert r.status == 400
                assert "securityAccessDenied" in (await r.json())["error"]
        asyncio.run(body())


class TestCancel:
    """A long procedure needs a way out. Cancel ends the next WAIT -- it does not
    interrupt an operation in flight, and the response says so."""

    def test_cancel_with_no_run_is_a_conflict_not_a_silent_ok(self):
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/cancel", json={})
                assert r.status == 409
                assert "no run in progress" in (await r.json())["error"]
        asyncio.run(body())

    def test_cancel_sets_the_running_engines_flag(self, tmp_path):
        async def body():
            svc = None
            engines = []

            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=_mock_factory) as client:
                svc = client.app[odin_web.ODIN_WEB]
                # Stand in for a run in flight: hold the lock and register an
                # engine, exactly as _run does.
                import odin_runner
                eng = odin_runner.Engine(MockBackend("success"), _bundle(tmp_path))
                engines.append(eng)
                async with svc._run_lock:
                    svc._track_engine(eng)
                    r = await client.post("/api/odin/cancel", json={})
                    assert r.status == 200
                    data = await r.json()
                    assert data["cancelling"] is True
                    assert "next wait" in data["note"]
                svc._engine = None

            assert engines[0].cancelled() is True
        asyncio.run(body())


class TestBenchState:
    """Facts about the unit that no bus reading establishes, so the operator
    declares them and the runner seeds them into the CID store (cid.IsFused)."""

    def test_defaults_are_reported(self):
        async def body():
            async with _client() as client:
                r = await client.get("/api/odin/bench-state")
                assert r.status == 200
                body_ = await r.json()
                # Flashing is destructive and an UPDATE_* procedure asks for one
                # with no confirmation of its own, so it starts DISARMED.
                # ...while bootloader updates default ON: a -WITH-BOOTLOADER
                # procedure names its bu/bl by hand, so defaulting them off
                # would silently downgrade the run the operator picked.
                assert body_["state"] == {"is_fused": True, "allow_flash": False,
                                          "include_bootloaders": True,
                                          "ramapps": "include",
                                          "conditions": {}}
                assert body_["cid_values"] == {"GUI_isFused": "true"}
        asyncio.run(body())

    def test_arming_flashing_is_not_a_cid_value(self):
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state", json={"allow_flash": True})
                data = await r.json()
                assert data["state"]["allow_flash"] is True
                # It is a backend setting, not something a procedure reads.
                assert "allow_flash" not in data["cid_values"]
        asyncio.run(body())

    def test_a_run_arms_the_backend_from_the_declared_state(self, tmp_path):
        async def body():
            backend = MockBackend("success")
            backend.allow_flash = False
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state", json={"allow_flash": True})
                await client.post("/api/odin/run", json={"procedure": "Model3/tasks/CAP"})
                assert backend.allow_flash is True
                # ...and turning it back off disarms the next run.
                await client.post("/api/odin/bench-state", json={"allow_flash": False})
                await client.post("/api/odin/run", json={"procedure": "Model3/tasks/CAP"})
                assert backend.allow_flash is False
        asyncio.run(body())

    def test_preflight_arms_the_backend_the_same_way_a_run_does(self, tmp_path):
        # Preflight builds its own backend from the factory, which starts
        # disarmed. When only the run applied the declared state, the modal
        # reported "flashing is not armed" for an armed bench and Confirm was
        # never enabled -- the two paths have to answer alike.
        async def body():
            backend = MockBackend("success")
            backend.allow_flash = False
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state", json={"allow_flash": True})
                await client.post("/api/odin/flash-preflight",
                                  json={"procedure": "Model3/tasks/CAP"})
                assert backend.allow_flash is True
        asyncio.run(body())

    def test_declining_bootloaders_reaches_both_preflight_and_the_run(self, tmp_path):
        # Excluding them is a per-flash decision made in the dialog, so the
        # preview and the write it confirms must agree about it.
        async def body():
            backend = MockBackend("success")
            backend.include_bootloaders = True
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                r = await client.post("/api/odin/bench-state",
                                      json={"include_bootloaders": False})
                assert (await r.json())["state"]["include_bootloaders"] is False
                await client.post("/api/odin/flash-preflight",
                                  json={"procedure": "Model3/tasks/CAP"})
                assert backend.include_bootloaders is False
                await client.post("/api/odin/run", json={"procedure": "Model3/tasks/CAP"})
                assert backend.include_bootloaders is False
        asyncio.run(body())

    def test_the_ramapp_mode_is_one_of_three_named_states(self):
        # A checkbox cannot express "only", so this key is an enum, not a flag --
        # and an unknown value has to be rejected rather than coerced to a bool.
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state", json={"ramapps": "only"})
                assert (await r.json())["state"]["ramapps"] == "only"
                r = await client.post("/api/odin/bench-state", json={"ramapps": "yes"})
                assert r.status == 400
                r = await client.get("/api/odin/bench-state")
                assert (await r.json())["state"]["ramapps"] == "only"   # unchanged
        asyncio.run(body())

    def test_the_ramapp_mode_reaches_preflight_and_the_run(self, tmp_path):
        async def body():
            backend = MockBackend("success")
            backend.ramapps = "include"
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state", json={"ramapps": "only"})
                await client.post("/api/odin/flash-preflight",
                                  json={"procedure": "Model3/tasks/CAP"})
                assert backend.ramapps == "only"
                backend.ramapps = "include"
                await client.post("/api/odin/run", json={"procedure": "Model3/tasks/CAP"})
                assert backend.ramapps == "only"
        asyncio.run(body())

    def test_preflight_hands_over_the_declared_car_config_too(self, tmp_path):
        async def body():
            backend = MockBackend("success")
            backend.conditions = {}
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state",
                                  json={"conditions": {"vdcType": "1"}})
                await client.post("/api/odin/flash-preflight",
                                  json={"procedure": "Model3/tasks/CAP"})
                assert backend.conditions == {"vdcType": "1"}
        asyncio.run(body())

    def test_a_post_updates_it_and_maps_it_to_a_cid_value(self):
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state", json={"is_fused": False})
                assert r.status == 200
                assert (await r.json())["cid_values"] == {"GUI_isFused": "false"}
                # ...and it sticks for the next reader, leaving the rest alone.
                r = await client.get("/api/odin/bench-state")
                assert (await r.json())["state"] == {"is_fused": False,
                                                     "allow_flash": False,
                                                     "include_bootloaders": True,
                                                     "ramapps": "include",
                                                     "conditions": {}}
        asyncio.run(body())

    def test_declared_car_config_is_kept_as_strings(self):
        # A condition compares against the metadata's text, so 0 and "0" must
        # not be two different answers.
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state",
                                      json={"conditions": {"vdcType": 1,
                                                           "drivetrainType": "0",
                                                           "blank": ""}})
                assert r.status == 200
                assert (await r.json())["state"]["conditions"] == {
                    "vdcType": "1", "drivetrainType": "0"}
        asyncio.run(body())

    def test_car_config_must_be_an_object(self):
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state",
                                      json={"conditions": "vdcType=1"})
                assert r.status == 400
        asyncio.run(body())

    def test_a_run_hands_the_declared_car_config_to_the_backend(self, tmp_path):
        async def body():
            backend = MockBackend("success")
            backend.conditions = {}
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state",
                                  json={"conditions": {"vdcType": "1"}})
                await client.post("/api/odin/run", json={"procedure": "Model3/tasks/CAP"})
                assert backend.conditions == {"vdcType": "1"}
        asyncio.run(body())

    def test_an_unknown_key_is_rejected_rather_than_silently_ignored(self):
        async def body():
            async with _client() as client:
                r = await client.post("/api/odin/bench-state", json={"is_frobbed": True})
                assert r.status == 400
                assert "is_frobbed" in (await r.json())["error"]
        asyncio.run(body())

    def test_a_run_seeds_the_declared_state_into_the_cid_store(self, tmp_path):
        async def body():
            backend = MockBackend("success")
            async with _client(bundle=_bundle(tmp_path),
                               backend_factory=lambda: backend) as client:
                await client.post("/api/odin/bench-state", json={"is_fused": False})
                r = await client.post("/api/odin/run", json={"procedure":
                                                             "Model3/tasks/CAP"})
                assert r.status == 200
            assert backend.cid_get("GUI_isFused") == "false"
        asyncio.run(body())
