"""scripts/di/dir_learn.py against a fake tm3web (ODIN run + ws events + dash + UDS ops)."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from di import dir_learn


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the cal backup JSON lands here
    monkeypatch.setattr(dir_learn, "RESET_SETTLE_S", 0.05)
    monkeypatch.setattr(dir_learn, "RESET_BOOT_TIMEOUT_S", 2.0)


def _fake_tm3web(start_results=("SUCCESSFUL",), start_reason="INVALID_DI_STATE",
                 gear_delay_s=0.15, coast_s=0.3, learn="offset", di_state=2, dyno_available=1,
                 standby_after_s=0.3, spin_to=700.0, coast_rpm_per_tick=8.0, did_fail=False,
                 dir_fault=False):
    """A tm3web + DIR that follows the requested gear/traction/brake. In dyno + D the operator
    "spins" the axle to `spin_to`; in N it coasts down. 0x407 START is accepted once the car
    has been in N for `standby_after_s` and the axle is above the gate. `dir_fault`: the DIR
    faults in D (DI_systemState FAULT + an a016 alertLog) and the learn ends FAIL_DI_FAULT."""
    spec = dir_learn.LEARNS[learn]
    st = {"calls": [], "gear": "P", "brake": False, "speed": 0.0, "rpm": 0.0, "traction": 0,
          "state": di_state, "dyno": dyno_available, "ws": set(), "ui": [], "uds": [],
          "t_n": None, "polls": 0, "starts": list(start_results), "runs": 0,
          # tm3web's /api/alerts: an old DIR entry, a DI entry that gets logged again, and a
          # non-DU fault that must not be reported
          "alerts": {"faults": [{"name": "ESP_a001_x", "ecu": "ESP"}],
                     "log": [{"key": "5A5:old", "ecu": "DIR", "last": 1.0, "log_values": [],
                              "summary": "DIR_a070_udsTransactionInitiated: [030B]"},
                             {"key": "527:again", "ecu": "DI", "last": 1.0, "log_values": [],
                              "summary": "DI_a063_systemGracefulPowerOff: MOTOR_HALT_REQUEST"}]}}

    def now():
        return asyncio.get_running_loop().time()

    async def broadcast(ev):
        for ws in list(st["ws"]):
            await ws.send_str(json.dumps(ev))

    async def ws_odin(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        st["ws"].add(ws)
        async for _ in ws:
            pass
        return ws

    async def brake(request):
        body = await request.json()
        st["brake"] = body["pressed"]
        st["calls"].append(("brake", body["pressed"], st["gear"]))
        return web.json_response({"ok": True})

    async def spin():  # accelerate, then coast once in N
        r = 0.0
        while r < spin_to:
            r += 50.0
            st["rpm"] = r
            await asyncio.sleep(0.02)
        while st["rpm"] > 0:
            await asyncio.sleep(0.05)
            if st["gear"] == "N":
                st["rpm"] = max(0.0, st["rpm"] - coast_rpm_per_tick)

    async def gear(request):
        body = await request.json()
        st["calls"].append(("gear", body["value"], st["gear"], st["speed"]))

        async def engage():  # the DI takes a moment to report the new gear
            await asyncio.sleep(gear_delay_s)
            st["gear"] = body["value"]
            if body["value"] == "N" and st["rpm"] > 0:
                st["t_n"] = now()
            if body["value"] == "D" and st["traction"] == 5:
                asyncio.get_running_loop().create_task(spin())

        asyncio.get_running_loop().create_task(engage())
        return web.json_response({"ok": True})

    async def ui(request):
        body = await request.json()
        st["ui"].append((body["field"], body["value"]))
        if body["field"] == "traction_mode" and st["gear"] in ("P", "N") \
                and (body["value"] != "dyno" or st["dyno"]):
            st["traction"] = {"rolls": 4, "dyno": 5}[body["value"]]
        return web.json_response({"ok": True})

    async def uds(request):
        body = await request.json()
        op, args = body["op"], body.get("args") or {}
        st["uds"].append((op, args.get("subtype")))
        if op == "ecu_reset":
            st["state"], st["dyno"], st["traction"] = 2, 1, 0
            return web.json_response({"result": {"sent": "11 01"}})
        if op == "routine_control" and args.get("subtype") == 1:
            if st["t_n"] is None or now() - st["t_n"] < standby_after_s \
                    or st["rpm"] <= dir_learn.RESOLVER_START_RPM:
                return web.json_response({"error": "UdsError: NRC 0x22"}, status=400)
            return web.json_response({"result": {"result": ""}})
        if op == "routine_control" and args.get("subtype") == 3:
            st["polls"] += 1
            flags = 0x10 if st["polls"] < 3 else 0x00  # RUNNING, then LEARN_SUCCESS
            return web.json_response({"result": {"result": (bytes(184) + b"\x00\x40"
                                                           + bytes([flags])).hex()}})
        return web.json_response({"result": {}})

    async def did_read(request):
        if did_fail:
            return web.json_response({"error": "NRC 0x31"}, status=400)
        body = await request.json()
        return web.json_response({"hex_id": "0x0306", "raw": "00", "fields": {body["did"]: 0}})

    async def frames(_request):  # a healthy spin permit + the DIR's live state
        b108 = bytearray(8)
        b108[5:7] = int(st["rpm"] * 10).to_bytes(2, "little", signed=True)
        b118 = bytearray(8)
        b118[2] = st["state"]
        b118[5] = st["traction"] | st["dyno"] << 3
        b126 = (350 | 47 << 11).to_bytes(3, "little") + bytes(5)  # DIR_vBat 350 V, 47 A
        good = {"0x39D": "a00a010000", "0x2A8": "0100000000000000", "0x2E8": "0100000000000000",
                "0x185": "0000000000000000", "0x108": b108.hex(), "0x118": b118.hex(),
                "0x126": b126.hex()}
        return web.json_response(
            {"frames": {k: {"can0": {"data": v, "age_s": 0.05}} for k, v in good.items()}})

    async def dash(_request):
        return web.json_response({"gear": st["gear"], "speed_kph": st["speed"] or st["rpm"] * 0.1,
                                  "telltales": [{"id": "parkBrake", "lit": False}]})

    async def alerts(_request):
        return web.json_response(st["alerts"])

    async def wait_for(cond, tries=100):
        for _ in range(tries):
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False

    async def run(request):
        st["runs"] += 1
        st["body"] = await request.json()
        await broadcast({"type": "trace", "depth": 0, "message": "script odin_script_test"})
        started = st["starts"].pop(0) if st["starts"] else "SUCCESSFUL"
        results = {"START_ROUTINE_RESULTS": started}
        if started != "SUCCESSFUL":
            results["START_RESULTS_REASON"] = start_reason
        await broadcast({"type": "metric", "metric": spec.start_metric, "result_code": 2,
                         "metadata": {"results": results}})
        if started != "SUCCESSFUL":
            return web.json_response({"exit_code": 3, "passed": False})
        # the procedure waits for D with the brake released
        ok = await wait_for(lambda: st["gear"] == "D" and not st["brake"])
        st["speed"] = 40.0 if ok else 0.0  # spun up, now coasting down
        learn_result = "SUCCESS" if ok else "FAIL"
        if ok and dir_fault:
            st["state"] = 3
            log = st["alerts"]["log"]
            log[1]["last"] = 2.0  # logged again during the learn
            log.insert(0, {"key": "5A5:new", "ecu": "DIR", "last": 2.0, "state": "SET",
                           "summary": "DIR_a016_safeStateApplied: newSafeState: ALL_OFF",
                           "log_values": [{"name": "DIR_a016_motorRPM", "value": 4399},
                                          {"name": "DIR_a016_ascMonitor", "value": None}]})
            st["alerts"]["faults"].append({"name": "DIR_a016_safeStateApplied", "ecu": "DIR"})
            learn_result = "FAIL_DI_FAULT"
            await asyncio.sleep(0.3)  # the spin watcher sees the FAULT before the result

        async def coast():
            await asyncio.sleep(coast_s)
            st["speed"] = 0.0

        asyncio.get_running_loop().create_task(coast())
        await broadcast({"type": "metric", "metric": spec.result_metric, "result_code": 2,
                         "metadata": {"results": {"LEARN_RESULT": learn_result}}})
        if ok:  # teardown waits for P
            ok = await wait_for(lambda: st["gear"] == "P" and not st["brake"], tries=150)
        ok = ok and learn_result == "SUCCESS"
        await asyncio.sleep(0.05)
        return web.json_response({"exit_code": 0 if ok else 3, "passed": ok})

    app = web.Application()
    app.router.add_get("/ws/odin", ws_odin)
    app.router.add_post("/api/brake", brake)
    app.router.add_post("/api/gear", gear)
    app.router.add_post("/api/ui", ui)
    app.router.add_post("/api/uds/op", uds)
    app.router.add_post("/api/did/read", did_read)
    app.router.add_get("/api/dash", dash)
    app.router.add_get("/api/frames", frames)
    app.router.add_get("/api/alerts", alerts)
    app.router.add_post("/api/odin/run", run)
    return app, st


def _run(app, **kw):
    lines = []

    async def body():
        async with TestServer(app) as server:
            url = str(server.make_url("/"))
            return await dir_learn.run(url, brake_settle_s=0.05, standstill_timeout_s=5,
                                       log=lines.append, **kw)

    return asyncio.run(body()), lines


# -- rolls learns (ODIN) ------------------------------------------------------------------

def test_drive_then_park_once_the_motor_stops():
    app, st = _fake_tm3web()
    result, lines = _run(app)
    assert st["calls"] == [
        # to N before START (the rotor is still there)
        ("brake", True, "P"), ("gear", "N", "P", 0.0), ("brake", False, "N"),
        # after START: brake before D; released only after the DI reports D
        ("brake", True, "N"), ("gear", "D", "N", 0.0), ("brake", False, "D"),
        # after the result: P requested only once the motor stopped, brake released in P
        ("brake", True, "D"), ("gear", "P", "D", 0.0), ("brake", False, "P"),
    ]
    assert result["passed"]
    assert any("LEARN_RESULT SUCCESS" in line for line in lines)
    assert "spin check: OK" in lines
    # latched in N, before START; development_car off (a production car) for a rolls learn
    assert st["ui"] == [("development_car", "off"), ("traction_mode", "rolls")]
    assert "routine_params" not in st["body"]  # plain offset learn: Tesla's own START params


def test_start_gear_is_engaged_and_rolls_latched_before_the_procedure_starts():
    app, _ = _fake_tm3web()
    result, lines = _run(app)
    started = lines.index("script odin_script_test")
    assert lines.index("shift N") < lines.index("DI_tractionControlMode ROLLS") < started
    assert lines.index("shift D") > started
    assert result["passed"]


def test_flux_learn_sends_its_start_params():
    app, st = _fake_tm3web()
    result, _ = _run(app, learn="all", rotor_temp_c=21.0)
    assert st["body"]["routine_params"] == {
        "ROTOR_LEARNING": {"LEARN_SELECT": "ALL", "ROTOR_TEMPERATURE": 21.0}}
    assert result["passed"]


def test_resolver_error_learn_runs_its_procedure_with_its_spin_target():
    app, st = _fake_tm3web(learn="resolver-error")
    result, lines = _run(app, learn="resolver-error")
    assert st["body"] == {"procedure": "Gen3/tasks/PROC_DIR_X_RESOLVER-ERROR-LEARN"}
    assert any("past 609 axle rpm" in line for line in lines)
    assert result["passed"]


def test_no_park_leaves_p_to_the_operator():
    app, st = _fake_tm3web()
    result, _ = _run(app, park=False, start_gear=None)
    assert [c[:2] for c in st["calls"]] == [("brake", True), ("gear", "D"), ("brake", False)]
    assert not result["passed"]  # the fake procedure then times out waiting for P


def test_start_in_d_skips_the_shift_after_start():
    app, st = _fake_tm3web()
    result, _ = _run(app, start_gear="D")
    assert [c[:2] for c in st["calls"][:3]] == [("brake", True), ("gear", "D"), ("brake", False)]
    assert not any(c[:2] == ("gear", "N") for c in st["calls"])
    assert result["passed"]


def test_start_gear_failure_does_not_start_the_learn():
    app, st = _fake_tm3web(gear_delay_s=10)
    result, lines = _run(app, gear_timeout_s=0.2)
    assert st["calls"][-1][:2] == ("brake", False)
    assert "script odin_script_test" not in lines
    assert not result["passed"]


def test_no_shift_when_start_is_rejected():
    app, st = _fake_tm3web(start_results=("FAILED_INCORRECT_CONDITIONS",),
                           start_reason="NOT_IN_SERVICE_MODE")
    result, _ = _run(app, start_gear=None)
    assert st["calls"] == []
    assert st["runs"] == 1 and not st["uds"]  # only INVALID_DI_STATE earns a reset + retry
    assert not result["passed"]


def test_missing_calibration_is_named():
    app, _ = _fake_tm3web(learn="resolver-error", start_results=("FAILED_INCORRECT_CONDITIONS",),
                          start_reason="NO_FLUX_CALIBRATION")
    result, lines = _run(app, learn="resolver-error", start_gear=None)
    assert any("no stored offset/flux calibration" in line for line in lines)
    assert not result["passed"]


def test_brake_is_released_even_if_gear_never_engages():
    app, st = _fake_tm3web(gear_delay_s=10)
    result, lines = _run(app, gear_timeout_s=0.2, start_gear=None)
    assert st["calls"][-1][:2] == ("brake", False)
    assert any(line.startswith("shift failed") for line in lines)
    assert not any(c[:2] == ("gear", "P") for c in st["calls"])  # never reached D -> no park
    assert not result["passed"]


def test_backup_failure_does_not_start_the_learn():
    app, st = _fake_tm3web(did_fail=True)
    result, lines = _run(app)
    assert st["runs"] == 0 and st["calls"] == []
    assert any("calibration backup failed" in line for line in lines)
    assert not result["passed"]


def test_backup_is_saved_before_start(tmp_path):
    app, _ = _fake_tm3web()
    result, _ = _run(app)
    saved = json.loads(next(tmp_path.glob("dir_cal_backup_*.json")).read_text())
    assert set(saved) == set(dir_learn.CAL_DIDS)
    assert result["passed"]


# -- faults, alerts and parking -------------------------------------------------------------

def test_dir_fault_logs_the_state_change_and_the_alerts_from_the_learn():
    app, _ = _fake_tm3web(dir_fault=True)
    result, lines = _run(app)
    fault = next(line for line in lines if line.startswith("DI_systemState STANDBY -> FAULT"))
    assert "vBat 350 V" in fault and "motor 47 A" in fault
    assert "DU alerts set now: DIR_a016_safeStateApplied" in lines  # the ESP fault is left out
    logged = [line for line in lines if line.startswith("    alertLog")]
    assert logged == [
        # oldest first: re-logged during the learn, then new; the untouched old entry is not
        "    alertLog  DI_a063_systemGracefulPowerOff: MOTOR_HALT_REQUEST [527:again]",
        "    alertLog SET DIR_a016_safeStateApplied: newSafeState: ALL_OFF"
        " | DIR_a016_motorRPM=4399 [5A5:new]",
    ]
    assert any("LEARN_RESULT FAIL_DI_FAULT" in line for line in lines)
    assert not result["passed"]


def test_a_clean_learn_reports_no_new_alerts():
    app, _ = _fake_tm3web()
    result, lines = _run(app)
    assert "DU alerts set now: none" in lines
    assert "    no new DU alertLog payloads during the learn" in lines
    assert any("vBat 350 V motor 47 A" in line for line in lines)  # the spin profile
    assert result["passed"]


def _park(monkeypatch, standstill):
    """Run _park with wait_for_standstill answering from `standstill` in turn."""
    answers, calls, lines = list(standstill), [], []

    async def wait(*_a, **_k):
        return answers.pop(0)

    async def shift(_http, _url, gear, **_k):
        calls.append(gear)

    monkeypatch.setattr(dir_learn, "wait_for_standstill", wait)
    monkeypatch.setattr(dir_learn, "shift", shift)
    asyncio.run(dir_learn._park(None, "u", {}, 0.01, lines.append))
    return calls, lines


def test_park_waits_for_the_motor_to_stop_before_p(monkeypatch):
    calls, lines = _park(monkeypatch, [False, False, True])
    assert calls == ["P"]
    assert lines.count("    still spinning: not shifting to P yet") == 2


def test_park_never_shifts_to_p_while_still_spinning(monkeypatch):
    monkeypatch.setattr(dir_learn, "PARK_WAIT_ROUNDS", 3)
    calls, lines = _park(monkeypatch, [False] * 3)
    assert calls == []
    assert lines[-1].startswith("!!! motor still spinning: shift to P yourself")


# -- resets -------------------------------------------------------------------------------

def test_invalid_di_state_resets_the_dir_and_retries_once():
    app, st = _fake_tm3web(start_results=("FAILED_INCORRECT_CONDITIONS", "SUCCESSFUL"))
    result, lines = _run(app)
    assert st["runs"] == 2 and ("ecu_reset", None) in st["uds"]
    assert any("DIR ECU reset (START refused INVALID_DI_STATE)" in line for line in lines)
    # re-latched after the reset
    assert st["ui"] == [("development_car", "off"), ("traction_mode", "rolls")] * 2
    assert result["passed"]


def test_reset_never_does_not_retry():
    app, st = _fake_tm3web(start_results=("FAILED_INCORRECT_CONDITIONS", "SUCCESSFUL"))
    result, _ = _run(app, reset="never")
    assert st["runs"] == 1 and not st["uds"]
    assert not result["passed"]


def test_auto_reset_before_a_learn_while_the_dir_is_faulted():
    app, st = _fake_tm3web(di_state=3)
    result, lines = _run(app)
    assert st["uds"][0] == ("ecu_reset", None)
    assert any("DI_systemState FAULT" in line for line in lines)
    assert result["passed"]


def test_reset_before_and_after():
    app, st = _fake_tm3web()
    result, _ = _run(app, reset="both")
    assert [u for u in st["uds"] if u[0] == "ecu_reset"] == [("ecu_reset", None)] * 2
    assert result["passed"]


def test_healthy_dir_is_not_reset_by_default():
    app, st = _fake_tm3web()
    result, _ = _run(app)
    assert not st["uds"]
    assert result["passed"]


# -- dyno resolver learn (0x407, raw UDS) ---------------------------------------------------

def test_resolver_learn_arms_after_n_and_reads_the_result():
    app, st = _fake_tm3web(learn="resolver")
    result, lines = _run(app, learn="resolver")
    assert st["ui"] == [("development_car", "on"), ("traction_mode", "dyno")]
    ops = [u for u in st["uds"] if u[0] != "tester_present"]
    assert ops[0] == ("session", None)
    starts = [u for u in ops if u == ("routine_control", 1)]
    assert len(starts) >= 2  # refused until the inverter is in standby, then armed
    assert ("routine_control", 3) in ops
    # the D -> N shift at speed is made without the brake
    n_shift = next(i for i, c in enumerate(st["calls"]) if c[:2] == ("gear", "N") and c[2] == "D")
    assert st["calls"][n_shift - 1][:2] != ("brake", True)
    assert any("0x407 armed" in line for line in lines)
    assert any("LEARN_RESULT LEARN_SUCCESS" in line for line in lines)
    assert st["calls"][-2][:2] == ("gear", "P")
    assert result["passed"]


def test_resolver_learn_gives_up_when_it_coasts_below_the_gate():
    app, st = _fake_tm3web(learn="resolver", standby_after_s=10, coast_rpm_per_tick=40.0)
    result, lines = _run(app, learn="resolver")
    assert not any(u == ("routine_control", 3) for u in st["uds"])
    assert any("never armed" in line for line in lines)
    assert not result["passed"]


def test_resolver_learn_resets_a_tripped_dyno_latch_first():
    app, st = _fake_tm3web(learn="resolver", dyno_available=0)
    result, lines = _run(app, learn="resolver")
    assert st["uds"][0] == ("ecu_reset", None)
    assert any("dyno latched unavailable" in line for line in lines)
    assert result["passed"]


def test_resolver_learn_stops_if_dyno_is_not_confirmed():
    app, st = _fake_tm3web(learn="resolver", dyno_available=0)
    result, lines = _run(app, learn="resolver", reset="never")
    assert not any(u[0] == "routine_control" for u in st["uds"])
    assert any("DYNO not confirmed" in line for line in lines)
    assert not result["passed"]


# -- pure helpers -------------------------------------------------------------------------

def _frames(**over):
    data = {"0x39D": "a00a010000", "0x2A8": "0100000000000000", "0x2E8": "0100000000000000",
            "0x185": "0000000000000000", "0x108": "0000000000000000", **over}
    return {k: {"can0": {"data": v, "age_s": 0.05}} for k, v in data.items() if v is not None}


def test_spin_check_passes_the_healthy_sim_frames():
    chk = dir_learn.spin_check(_frames())
    assert chk["blockers"] == [] and chk["axle_rpm"] == 0.0 and chk["torque_nm"] == 0


def test_spin_check_names_each_blocker():
    chk = dir_learn.spin_check(_frames(**{
        "0x39D": "a00a020000",            # driverBrakeApply 2 = DRIVER_APPLYING_BRAKES
        "0x2A8": "0200000000000000",      # EPBL PARKED
        "0x185": "0100000000000000",      # a wheel brake torque
        "0x108": "0000000000e803",        # DIR_axleSpeed 1000 * 0.1 = 100 rpm
    }))
    assert [b.split()[0] for b in chk["blockers"]] == ["IBST_driverBrakeApply", "EPB", "ESP"]
    assert chk["axle_rpm"] == 100.0


def test_spin_check_decodes_0x118_traction_state_and_dyno_available():
    chk = dir_learn.spin_check(_frames(**{"0x118": "00000200000d0000"}))  # state 2, dyno + avail
    assert (chk["di_state"], chk["traction"], chk["dyno_available"]) == (2, 5, 1)


def test_spin_check_decodes_the_dir_bus_voltage_and_current():
    b126 = (281 | 14 << 11).to_bytes(3, "little") + bytes(5)  # DIR_vBat 0|10, motorCurrent 11|11
    chk = dir_learn.spin_check(_frames(**{"0x126": b126.hex()}))
    assert (chk["v_bat"], chk["i_motor"]) == (281, 14)
    assert dir_learn.spin_check(_frames())["v_bat"] is None  # no 0x126 on the bus


def test_axle_speed_sna_reads_as_unknown():
    # 0x8000 = SNA -- not -3276.8 rpm
    assert dir_learn.spin_check(_frames(**{"0x108": "00000000000080"}))["axle_rpm"] is None


def test_spin_check_falls_back_to_epbr_and_drops_stale_frames():
    f = _frames(**{"0x2A8": None, "0x2E8": "0100000000000000"})
    assert dir_learn.spin_check(f)["blockers"] == []           # EPBL absent -> EPBR used
    f["0x39D"]["can0"]["age_s"] = 2.0
    assert dir_learn.spin_check(f)["blockers"] == ["IBST 0x39D missing"]


def test_routine_params():
    assert dir_learn.routine_params("ROTOR_OFFSET") == {}
    assert dir_learn.routine_params(None, 20.0) == {}  # not a ROTOR_LEARNING run
    assert dir_learn.routine_params("ROTOR_FLUX", 20.0) == {
        "ROTOR_LEARNING": {"LEARN_SELECT": "ROTOR_FLUX", "ROTOR_TEMPERATURE": 20.0}}


def test_parse_resolver_result():
    r = dir_learn.parse_resolver_result(bytes(2) + b"\x00\x05" + bytes(180) + b"\x01\x00\x1b")
    assert r == {"running": True, "write_failed": True, "result": "START_SPEED",
                 "rms_error": 0.5, "table_nonzero": 1}
    assert "error" in dir_learn.parse_resolver_result(b"\x00")


def test_flux_learn_needs_a_rotor_temperature():
    with pytest.raises(SystemExit):
        dir_learn.main(["--learn", "flux"])
