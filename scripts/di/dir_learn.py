#!/usr/bin/env python3
"""Run a DIR motor learn through tm3web and do the brake, shifts, traction mode and resets for you.

    --learn offset          rotor offset (ROTOR_LEARNING 0x406, Tesla's default)
    --learn flux | all      rotor flux / offset + flux (same routine; needs --rotor-temp)
    --learn resolver-error  resolver error table in rolls mode (RESOLVER_LEARNING 0x409)
    --learn resolver        resolver error table in dyno mode (LEGACY_RESOLVER_LEARNING 0x407)

Rolls learns (offset/flux/all/resolver-error) run Tesla's ODIN procedure: shift N (an
un-learned offset creeps the motor in P/D and START refuses MOTOR_SPEED_DETECTED), latch
ROLLS, START, then brake on -> D -> brake off the moment DI_gear reads D. The DIR cancels
(FAIL_BRAKES) if the axle passes ~11 rpm while the CAN brake reads applied. Accelerate past
the learn's spin target, lift off when told, and let it coast.

The dyno learn (0x407) arms only at START, and only while the car is in N with the inverter
already in standby and the axle above ~551 rpm. So: latch DYNO in N, shift D, accelerate past
the target, and the script shifts N and retries START until the DIR accepts it. Both resolver
learns capture the table while coasting from ~470 to ~332 axle rpm with zero torque.

--reset (default auto) resets the DIR: before a dyno learn whose one-shot dyno latch has
tripped (DI_dynoModeAvailable 0) or while DI_systemState is FAULT, and once more if START is
refused with INVALID_DI_STATE. before/after/both force it; never turns it off.

    python scripts/di/dir_learn.py [--url http://localhost:8765] [--learn offset]
    python scripts/di/dir_learn.py --learn all --rotor-temp auto
    python scripts/di/dir_learn.py --learn resolver --reset before

Needs tm3web --control with vehicle_sim running (scenarios/drive-learn.toml).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp


@dataclass(frozen=True)
class Learn:
    procedure: str | None      # ODIN task; None = the dyno learn, driven over raw UDS
    start_metric: str | None
    result_metric: str | None
    learn_select: str | None   # ROTOR_LEARNING LEARN_SELECT
    spin_rpm: float            # axle rpm to pass before lifting off
    overspeed_rpm: float | None
    traction: int              # DI_tractionControlMode the learn needs
    needs_temp: bool = False


_ROTOR = ("Gen3/tasks/PROC_DIR_X_ROTOR-OFFSET-LEARN", "DIAGCODE_START_ROTOR_LEARNING_ROUTINE",
          "DIAGCODE_ROTOR_LEARNING_ROUTINE_RESULTS")
TRACTION_ROLLS, TRACTION_DYNO = 4, 5
# Spin targets: the learn job waits for the axle to pass its threshold (offset 443, resolver
# error 609 rpm), then for torque 0 within ~1 s. Rolls learns fail above ~720 rpm. The dyno
# learn needs > ~551 rpm once in N with the inverter in standby, so spin well past it.
LEARNS = {
    "offset": Learn(*_ROTOR, "ROTOR_OFFSET", 443.0, 720.0, TRACTION_ROLLS),
    "flux": Learn(*_ROTOR, "ROTOR_FLUX", 443.0, 720.0, TRACTION_ROLLS, needs_temp=True),
    "all": Learn(*_ROTOR, "ALL", 443.0, 720.0, TRACTION_ROLLS, needs_temp=True),
    "resolver-error": Learn("Gen3/tasks/PROC_DIR_X_RESOLVER-ERROR-LEARN",
                            "DIAGCODE_START_RESOLVER_LEARNING_ROUTINE",
                            "DIAGCODE_RESOLVER_LEARNING_ROUTINE_RESULTS",
                            None, 609.0, 720.0, TRACTION_ROLLS),
    "resolver": Learn(None, None, None, None, 650.0, None, TRACTION_DYNO),
}
# Back-compat names (offset learn).
PROCEDURE, START_METRIC, RESULT_METRIC = _ROTOR
LEARN_SPIN_RPM = LEARNS["offset"].spin_rpm
OVERSPEED_RPM = LEARNS["offset"].overspeed_rpm

RID_RESOLVER_LEARN = 0x0407
RESOLVER_START_RPM = 551.0   # 0x407 START gate (filtered axle speed)
RESOLVER_CAPTURE_RPM = (332.0, 470.0)
RESOLVER_RESULT = {0: "LEARN_SUCCESS", 1: "TEST_FAILED", 2: "OFF_LIMITS", 3: "START_SPEED",
                   4: "SPEED_RANGE", 5: "TORQUE", 6: "BRAKE", 7: "FAULT"}

# The DIR's spin permit during a learn (2022.45.15): above ~11 axle rpm it cancels with
# FAIL_BRAKES (dyno learn: BRAKE) unless IBST_driverBrakeApply == 1, EPB systemStatus == 1
# (RELEASED) and the ESP per-wheel brake torques (0x185) are 0. Decoded by the firmware's
# layout, not the DBC (which puts EPBL at 0x2A9).
SPIN_IDS = (0x39D, 0x2A8, 0x2E8, 0x185, 0x108, 0x118, 0x128, 0x126)
# 0x128 DI_systemLimits: what caps drive torque/speed right now.
_LIMIT_BITS = {0: "regenPower", 1: "iBat", 2: "vBatLow", 3: "vBatHigh", 4: "obstacle",
               5: "limp", 6: "GPO", 7: "shift", 8: "driveTorque", 9: "regenTorque",
               10: "vehicleSpeed", 11: "bmsMiaFreeze", 12: "spinDownLearning"}
_SPEED_LIMIT_REASON = {0: "NONE", 1: "DRIVE_UNIT_LIMIT", 2: "FACTORY_LOW_BRAKE_FLUID",
                       3: "FACTORY_MODE", 4: "SERVICE_MODE", 5: "MAX_CAR_CONFIG",
                       6: "TRAILER_MODE", 7: "TAS", 8: "LOAD_SHED", 9: "FRUNK_OPEN", 10: "USER"}
FAIL_BRAKES_RPM = 11.0
STALE_S = 0.5
# DI_tractionControlMode (0x118 bit40 w3). The DIR confirms ROLLS/DYNO only at standstill in
# P/N, then holds it through the shift to D; dyno is one-shot (a drop latches it unavailable
# until a DIR reset) unless UI_developmentCar is set.
_TRACTION = {0: "NORMAL", 1: "SLIP_START", 4: "ROLLS", 5: "DYNO", 6: "OFFROAD"}
_TRACTION_UI = {TRACTION_ROLLS: "rolls", TRACTION_DYNO: "dyno"}
_DI_STATE = {0: "UNAVAILABLE", 1: "IDLE", 2: "STANDBY", 3: "FAULT", 4: "ABORT", 5: "ENABLE"}
DI_STATE_FAULT = 3


def spin_check(frames: dict) -> dict:
    """What the DIR's spin permit sees, from /api/frames' "frames" map."""
    def fresh(can_id):
        return [bytes.fromhex(c["data"]) for c in frames.get(f"0x{can_id:03X}", {}).values()
                if c.get("age_s", 0) <= STALE_S]

    blockers = []
    ibst = [d[2] & 3 for d in fresh(0x39D) if len(d) >= 3]
    if not ibst:
        blockers.append("IBST 0x39D missing")
    elif any(v != 1 for v in ibst):
        blockers.append(f"IBST_driverBrakeApply {ibst} (need 1 BRAKES_NOT_APPLIED)")
    epb = [d[0] & 0xF for d in fresh(0x2A8) if d] or [d[0] & 0xF for d in fresh(0x2E8) if d]
    if not epb:
        blockers.append("EPB 0x2A8/0x2E8 missing")
    elif any(v != 1 for v in epb):
        blockers.append(f"EPB systemStatus {epb} (need 1 RELEASED)")
    torques = [int.from_bytes(d[:6], "little") for d in fresh(0x185)]  # 4 x 12-bit
    if not torques:
        blockers.append("ESP 0x185 missing")
    elif any(torques):
        blockers.append("ESP 0x185 brake torque nonzero")
    raw = next((int.from_bytes(d[5:7], "little", signed=True)  # DIR_axleSpeed 40|16, 0.1 rpm
                for d in fresh(0x108) if len(d) >= 7), None)
    axle = None if raw in (None, -0x8000) else raw * 0.1  # 0x8000 = SNA
    # DIR_torqueActual 27|13 signed, 2 Nm. The learns need it back at exactly 0.
    torque = None
    for d in fresh(0x108):
        if len(d) >= 5:
            t = (int.from_bytes(d[:5], "little") >> 27) & 0x1FFF
            torque = (t - 0x2000 if t & 0x1000 else t) * 2
            break
    traction = next((d[5] & 7 for d in fresh(0x118) if len(d) >= 6), None)
    dyno_ok = next((d[5] >> 3 & 1 for d in fresh(0x118) if len(d) >= 6), None)  # bit 43
    state = next((d[2] & 7 for d in fresh(0x118) if len(d) >= 3), None)  # DI_systemState 16|3
    limits = None
    for d in fresh(0x128):
        if len(d) >= 4:
            v = int.from_bytes(d[:4], "little")
            on = [n for b, n in _LIMIT_BITS.items() if v >> b & 1]
            cap, why = v >> 16 & 0x1FF, v >> 25 & 0xF
            if cap:
                on.append(f"speed<={cap}kph({_SPEED_LIMIT_REASON.get(why, why)})")
            limits = ",".join(on) or "none"
            break
    # 0x126 DIR_hvStatus (2022+): DIR_vBat 0|10 V, DIR_motorCurrent 11|11 A.
    v_bat = i_motor = None
    for d in fresh(0x126):
        if len(d) >= 3:
            v = int.from_bytes(d[:3], "little")
            v_bat, i_motor = v & 0x3FF, v >> 11 & 0x7FF
            break
    return {"blockers": blockers, "axle_rpm": axle, "traction": traction, "di_state": state,
            "dyno_available": dyno_ok, "torque_nm": torque, "limits": limits,
            "v_bat": v_bat, "i_motor": i_motor}


async def watch_spin_permit(http, url, stop: asyncio.Event, *, period_s=0.1, log=print,
                            learn: Learn = LEARNS["offset"]) -> None:
    """Log each change in the spin permit, when the axle spins while it is blocked, the
    lift-off point and the once-a-second spin profile."""
    last, warned, traction_warned, last_state = None, False, False, None
    params = {"ids": ",".join(f"0x{i:X}" for i in SPIN_IDS)}
    loop = asyncio.get_running_loop()
    next_speed_log, peak, lift_until = loop.time(), 0.0, 0.0
    while True:  # at least one check, even if the learn ends at once
        try:
            async with http.get(url + "/api/frames", params=params) as r:
                if r.status != 200:
                    log(f"spin check unavailable (/api/frames: HTTP {r.status})")
                    return
                chk = spin_check((await r.json(content_type=None)).get("frames", {}))
        except aiohttp.ClientError as e:
            log(f"spin check unavailable: {e}")
            return
        blockers = tuple(chk["blockers"])
        if blockers != last:
            log("spin check: " + ("OK" if not blockers else "BLOCKED: " + "; ".join(blockers)))
            last = blockers
        rpm = chk["axle_rpm"]
        state = chk.get("di_state")
        if state is not None and state != last_state:
            if last_state is not None:
                log(f"DI_systemState {_DI_STATE.get(last_state, last_state)} -> "
                    f"{_DI_STATE.get(state, state)} (axle {rpm} rpm, torque "
                    f"{chk.get('torque_nm')} Nm, vBat {chk.get('v_bat')} V, "
                    f"motor {chk.get('i_motor')} A)")
            last_state = state
        if blockers and rpm is not None and abs(rpm) > FAIL_BRAKES_RPM and not warned:
            log(f"!!! axle {rpm:.0f} rpm while blocked -> the DIR cancels the learn (brakes)")
            warned = True
        if rpm is not None:
            if abs(rpm) > learn.spin_rpm >= peak:
                log(f">>> axle {rpm:.0f} rpm > {learn.spin_rpm:.0f}: LIFT OFF THE ACCELERATOR NOW")
                lift_until = loop.time() + 2.0
            if loop.time() < lift_until:  # the learn needs torque at 0 within ~1 s
                log(f"    lift-off: axle {rpm:.0f} rpm torque {chk.get('torque_nm')} Nm")
            over = learn.overspeed_rpm
            if over is not None and abs(rpm) > over - 20 >= peak:
                log(f"!!! axle {rpm:.0f} rpm: near the {over:.0f} rpm overspeed limit")
            peak = max(peak, abs(rpm))
        traction = chk.get("traction")
        if traction is not None and traction != learn.traction and not traction_warned:
            log(f"!!! DI_tractionControlMode is {_TRACTION.get(traction, traction)}, not "
                f"{_TRACTION[learn.traction]} -- the learn will fail; the DIR only confirms it "
                "at standstill in P/N")
            traction_warned = True
        if loop.time() >= next_speed_log:  # the spin-up/coast-down profile, once a second
            tm = _TRACTION.get(traction, traction)
            log(f"    axle {'SNA' if rpm is None else f'{rpm:.0f}'} rpm (peak {peak:.0f}) "
                f"torque {chk.get('torque_nm')} Nm vBat {chk.get('v_bat')} V motor "
                f"{chk.get('i_motor')} A traction {tm} "
                f"state {_DI_STATE.get(state, state)} limits {chk.get('limits')}")
            next_speed_log = loop.time() + 1.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), period_s)
        if stop.is_set():
            return


async def _post(http: aiohttp.ClientSession, url: str, path: str, body: dict) -> dict:
    async with http.post(url + path, json=body) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RuntimeError(f"{path}: {data.get('error', data) if isinstance(data, dict) else data}")
        return data


async def _dash(http: aiohttp.ClientSession, url: str) -> dict:
    async with http.get(url + "/api/dash") as r:
        return await r.json(content_type=None)


async def uds_op(http, url, op: str, args: dict | None = None, node: str = "DIR") -> dict:
    """One tm3web low-level UDS op (/api/uds/op); raises RuntimeError on an NRC/error."""
    return (await _post(http, url, "/api/uds/op",
                        {"node": node, "op": op, "args": args or {}})).get("result") or {}


async def _log_tp_health(http, url, log) -> None:
    """Print the DIR keep-alive stats -- did WE stall, or did the DIR miss frames we sent?"""
    try:
        async with http.get(url + "/api/uds-health", params={"node": "DIR"}) as r:
            tp = (await r.json(content_type=None)).get("tp") if r.status == 200 else None
    except aiohttp.ClientError:
        tp = None
    if tp:
        log(f"    TesterPresent: sent {tp['sent']}, failed {tp['failed']}, "
            f"largest gap {tp['max_gap_s']} s, last {tp.get('since_last_s')} s ago | "
            f"session {tp.get('session')} thread_alive {tp.get('thread_alive')} "
            f"phase {tp.get('phase')!r} interval {tp.get('interval_s')} s")
        if tp.get("thread_alive") is False:
            log("!!! DIR TesterPresent keep-alive thread is NOT running -> FAIL_NO_TESTER_PRESENT")
    else:
        log("    TesterPresent: no DIR session in tm3web's ODIN backend")


_DU_ECUS = {"DIR", "DI", "PMR"}


async def _alerts(http, url) -> dict | None:
    try:
        async with http.get(url + "/api/alerts") as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except aiohttp.ClientError:
        return None


async def alert_log_keys(http, url) -> dict | None:
    """alertLog payload -> last seen, to diff against after the learn."""
    a = await _alerts(http, url)
    return None if a is None else {e.get("key"): e.get("last") for e in a.get("log", [])}


async def log_du_alerts(http, url, before: dict | None, *, log=print) -> None:
    """Print the drive-unit alerts that are set now and the alertLog payloads first seen or
    seen again since ``before`` -- the why behind FAIL_DI_FAULT."""
    a = await _alerts(http, url)
    if a is None:
        log("DU alerts: /api/alerts unavailable")
        return
    faults = [f for f in a.get("faults", []) if f.get("ecu") in _DU_ECUS]
    before = before or {}
    new = [e for e in a.get("log", []) if e.get("ecu") in _DU_ECUS
           and (e.get("key") not in before or e.get("last") != before[e.get("key")])]
    log(f"DU alerts set now: {', '.join(f['name'] for f in faults) or 'none'}")
    for e in reversed(new):  # oldest first
        vals = ", ".join(f"{v['name']}={v['value']}" for v in e.get("log_values", [])
                         if v.get("value") is not None)
        log(f"    alertLog {e.get('state') or ''} {e.get('summary')}"
            + (f" | {vals}" if vals else "") + f" [{e.get('key')}]")
    if not new:
        log("    no new DU alertLog payloads during the learn")


BRAKE_VOTE_TIMEOUT_S = 3.0
GEAR_RETRY_S = 2.0


async def shift(http, url, gear, *, brake_settle_s=0.5, gear_timeout_s=8.0, brake=True,
                log=print) -> dict:
    """Brake on, request `gear`, wait for DI_gear to match, brake off (always). brake=False
    skips the brake (D -> N while spinning: a CAN brake then would trip the brake checks).
    Returns the dash snapshot."""
    loop = asyncio.get_running_loop()
    if brake:
        await _post(http, url, "/api/brake", {"pressed": True})
        log("brake on")
    try:
        if brake:
            # The shift interlock goes by the DI's debounced brake vote, not the raw CAN
            # brake: wait for DI_brakePedalState ON (older tm3web: just settle).
            await asyncio.sleep(brake_settle_s)
            deadline = loop.time() + BRAKE_VOTE_TIMEOUT_S
            while (state := (await _dash(http, url)).get("brake_pedal_state", 1)) != 1:
                if loop.time() > deadline:
                    log(f"DI_brakePedalState {state} (not ON) after {BRAKE_VOTE_TIMEOUT_S} s; "
                        "shifting anyway")
                    break
                await asyncio.sleep(0.05)
        deadline = loop.time() + gear_timeout_s
        next_request = loop.time()
        while True:
            if loop.time() >= next_request:  # re-send if the gesture didn't commit
                await _post(http, url, "/api/gear", {"value": gear})
                log(f"shift {gear}")
                next_request = loop.time() + GEAR_RETRY_S
            dash = await _dash(http, url)
            if dash.get("gear") == gear:
                return dash
            if loop.time() > deadline:
                raise TimeoutError(
                    f"DI_gear still {dash.get('gear')!r} {gear_timeout_s} s after {gear} "
                    f"(DI_brakePedalState {dash.get('brake_pedal_state')})")
            await asyncio.sleep(0.02)  # short: in D the creep builds while the brake is on
    finally:
        if brake:
            await _post(http, url, "/api/brake", {"pressed": False})
            log("brake off")


# Coast-down is over once the speed is back at creep; an un-learned offset keeps the motor
# creeping (~2 km/h) in P/D, so it never reads 0 before the learn succeeds.
async def wait_for_standstill(http, url, *, below_kph=3.0, hold_s=1.0, timeout_s=20.0,
                              log=print) -> bool:
    """True once DI_vehicleSpeed stays at/below `below_kph` for `hold_s`; False on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    still_since = speed = None
    while loop.time() < deadline:
        speed = (await _dash(http, url)).get("speed_kph")
        now = loop.time()
        if speed is not None and abs(speed) <= below_kph:
            still_since = still_since if still_since is not None else now
            if now - still_since >= hold_s:
                return True
        else:
            still_since = None
        await asyncio.sleep(0.1)
    log(f"motor not confirmed stopped after {timeout_s} s (speed_kph {speed})")
    return False


def _describe(ev: dict) -> str | None:
    kind = ev.get("type")
    if kind == "trace":
        return "  " * int(ev.get("depth", 0)) + str(ev.get("message", ""))
    if kind == "metric":
        meta = f" · {json.dumps(ev['metadata'])}" if "metadata" in ev else ""
        return f"● {ev.get('metric')} (rc {ev.get('result_code')}){meta}"
    if kind == "error":
        return f"✖ {ev.get('error')}"
    return None


async def _spin_now(http, url) -> dict | None:
    params = {"ids": ",".join(f"0x{i:X}" for i in SPIN_IDS)}
    try:
        async with http.get(url + "/api/frames", params=params) as r:
            if r.status != 200:
                return None
            return spin_check((await r.json(content_type=None)).get("frames", {}))
    except aiohttp.ClientError:
        return None


STILL_RPM = 1.0
STILL_KPH = 0.3


async def wait_spin_ok(http, url, *, still=False, hold_s=1.0, timeout_s=10.0, log=print) -> bool:
    """True once the spin permit is OK (and, with `still`, the axle below STILL_RPM) for
    `hold_s`. True if the check is unavailable (old tm3web)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    ok_since = chk = None
    while loop.time() < deadline:
        chk = await _spin_now(http, url)
        if chk is None:
            log("spin check unavailable; starting without it")
            return True
        moving = False
        if still:
            rpm = chk["axle_rpm"]
            if rpm is not None:
                moving = abs(rpm) >= STILL_RPM
            else:  # axle speed SNA (N): go by DI_vehicleSpeed instead
                kph = (await _dash(http, url)).get("speed_kph")
                moving = kph is None or abs(kph) >= STILL_KPH
        now = loop.time()
        if not chk["blockers"] and not moving:
            ok_since = ok_since if ok_since is not None else now
            if now - ok_since >= hold_s:
                state = chk.get("di_state")
                log(f"DI_systemState {_DI_STATE.get(state, state)}")
                return True
        else:
            ok_since = None
        await asyncio.sleep(0.1)
    log(f"not ready to start after {timeout_s} s: {chk}")
    return False


async def set_traction(http, url, mode: int, *, timeout_s=5.0, log=print) -> bool:
    """Request `mode` from the sim UI and wait for DI_tractionControlMode to confirm it. Call at
    standstill in P/N: that is the only place the DIR confirms rolls/dyno. UI_developmentCar is
    on only for dyno (it keeps the one-shot dyno latch from tripping; the DIR reads it for
    nothing else), so rolls learns run as a production car would."""
    await _post(http, url, "/api/ui", {"field": "development_car",
                                       "value": "on" if mode == TRACTION_DYNO else "off"})
    await _post(http, url, "/api/ui", {"field": "traction_mode", "value": _TRACTION_UI[mode]})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    chk = None
    while loop.time() < deadline:
        chk = await _spin_now(http, url)
        if chk is None:
            log("traction check unavailable; continuing")
            return True
        if chk.get("traction") == mode:
            log(f"DI_tractionControlMode {_TRACTION[mode]}")
            return True
        await asyncio.sleep(0.1)
    got = None if chk is None else chk.get("traction")
    log(f"DI_tractionControlMode {_TRACTION.get(got, got)} after {timeout_s} s, wanted "
        f"{_TRACTION[mode]} (DI_dynoModeAvailable {None if chk is None else chk.get('dyno_available')})")
    return False


RESET_BOOT_TIMEOUT_S = 30.0
RESET_SETTLE_S = 2.0  # let the pre-reset 0x118 go stale before waiting for the DIR


async def reset_dir(http, url, *, why: str, log=print) -> bool:
    """ECUReset (hard) the DIR and wait for it to broadcast 0x118 again, out of FAULT. Clears
    its latched states: the one-shot dyno latch, fault op-states behind INVALID_DI_STATE, a
    stuck learn job."""
    log(f"DIR ECU reset ({why})")
    try:
        await uds_op(http, url, "ecu_reset", {"type": "hard"})
    except (RuntimeError, aiohttp.ClientError) as e:
        log(f"DIR reset failed: {e}")
        return False
    await asyncio.sleep(RESET_SETTLE_S)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESET_BOOT_TIMEOUT_S
    chk = None
    while loop.time() < deadline:
        chk = await _spin_now(http, url)
        state = None if chk is None else chk.get("di_state")
        if state is not None and state not in (0, DI_STATE_FAULT):
            log(f"DIR back: DI_systemState {_DI_STATE.get(state, state)}")
            return True
        await asyncio.sleep(0.2)
    log(f"DIR not back {RESET_BOOT_TIMEOUT_S:.0f} s after reset: {chk}")
    return False


# Rotor-temperature stand-ins for a flux learn (no magnet sensor): older units report stator
# temperature (0x315 DIR_temperature mux 0, byte 3); newer ones only oil (0x395 DIR_oilPump
# byte 3, DIR_oilPumpFluidTQF bit 3 = HIGH_CONFIDENCE). Both 1 C/bit, -40 offset. Only
# valid on a cold-soaked unit, where magnets, stator and oil sit at ambient.
TEMP_PLAUSIBLE_C = (-30.0, 60.0)


def _temp(raw: int) -> float | None:
    t = raw - 40.0
    lo, hi = TEMP_PLAUSIBLE_C
    return t if raw not in (0, 0xFF) and lo <= t <= hi else None


def temp_readings(frames: dict) -> dict:
    """Stator (mux 0 only) and high-confidence oil temperatures from /api/frames' map."""
    def payloads(can_id):
        return [bytes.fromhex(c["data"]) for c in frames.get(f"0x{can_id:03X}", {}).values()
                if c.get("age_s", 0) <= 2.5]

    out = {}
    for d in payloads(0x315):
        if len(d) >= 4 and d[0] & 7 == 0:
            out["stator"] = _temp(d[3])
    for d in payloads(0x395):
        if len(d) >= 4 and d[0] >> 3 & 1:
            out["oil"] = _temp(d[3])
    return out


async def read_rotor_temp(http, url, *, timeout_s=5.0, log=print) -> float | None:
    """Stator temperature if the unit has the sensor, else oil pump fluid temperature."""
    params = {"ids": "0x315,0x395"}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    seen: dict = {}
    while loop.time() < deadline:  # 0x315 is 1 s and multiplexed: wait for a mux-0 frame
        try:
            async with http.get(url + "/api/frames", params=params) as r:
                frames = (await r.json(content_type=None)).get("frames", {}) \
                    if r.status == 200 else {}
        except aiohttp.ClientError:
            frames = {}
        seen.update(temp_readings(frames))
        if seen.get("stator") is not None:
            break
        await asyncio.sleep(0.2)
    stator, oil = seen.get("stator"), seen.get("oil")
    log(f"rotor temperature stand-ins: stator {stator} C, oil {oil} C")
    return stator if stator is not None else oil


# What the learns overwrite (offset, flux, resolver table): saved before START so a bad learn
# can be written back (DID writes need SecurityAccess L5; reads need none).
CAL_DIDS = ("SPEED_SENSOR_ANGLE_OFFSET", "MOTOR_FLUX_REFERENCE", "RESOLVER_CALIBRATION_DATA")


async def backup_cal_dids(http, url, *, node="DIR", log=print) -> Path | None:
    """Read CAL_DIDS from `node` and save them to dir_cal_backup_<time>.json; None on failure."""
    saved = {}
    for did in CAL_DIDS:
        try:
            async with http.post(url + "/api/did/read", json={"node": node, "did": did}) as r:
                res = await r.json(content_type=None)
        except aiohttp.ClientError as e:
            log(f"backup: {did} read failed: {e}")
            return None
        if r.status != 200:
            log(f"backup: {did} read failed: {res.get('error', res)}")
            return None
        saved[did] = {"hex_id": res.get("hex_id"), "raw": res.get("raw"),
                      "fields": res.get("fields")}
    path = Path(f"dir_cal_backup_{time.strftime('%Y%m%d-%H%M%S')}.json")
    path.write_text(json.dumps(saved, indent=1))
    for did, v in saved.items():
        shown = {k: val for k, val in (v["fields"] or {}).items() if k != "ANGLE_ERROR_TABLE"}
        log(f"backup: {did} {v['hex_id']} {shown or v['raw']}")
    log(f"backup saved to {path.resolve()}")
    return path


def routine_params(learn_select: str | None = "ROTOR_OFFSET",
                   rotor_temp_c: float | None = None) -> dict:
    """START-param overrides for ROTOR_LEARNING. Tesla's script sends ROTOR_OFFSET at a fake
    -40 C; a flux learn (ROTOR_FLUX / ALL) corrects the measured flux to 25 C with this
    temperature, so it needs the real rotor temperature. None: not a ROTOR_LEARNING run."""
    if learn_select is None:
        return {}
    over = {}
    if learn_select != "ROTOR_OFFSET":
        over["LEARN_SELECT"] = learn_select
    if rotor_temp_c is not None:
        over["ROTOR_TEMPERATURE"] = rotor_temp_c
    return {"ROTOR_LEARNING": over} if over else {}


def parse_resolver_result(data: bytes) -> dict:
    """LEGACY_RESOLVER_LEARNING (0x407) results, 187 B: 92 x int16 BE error table,
    RMSERROR int16 BE @184 (1/512), flags @186: LEARN_RESULT b0-2, WRITE_FAILED b3, RUNNING b4."""
    if len(data) < 187:
        return {"error": f"short result ({len(data)} B): {data.hex()}"}
    code = data[186] & 7
    return {"running": bool(data[186] >> 4 & 1), "write_failed": bool(data[186] >> 3 & 1),
            "result": RESOLVER_RESULT.get(code, code),
            "rms_error": struct.unpack_from(">h", data, 184)[0] / 512.0,
            "table_nonzero": sum(1 for v in struct.unpack_from(">92h", data, 0) if v)}


PARK_WAIT_ROUNDS = 15  # x --standstill-timeout (20 s default): 5 min


async def _park(http, url, timing, standstill_timeout_s, log) -> None:
    log(">>> waiting for the motor to stop")
    # Never shift to P while spinning: a free DU coasts for minutes. Keep waiting, then leave
    # P to the operator.
    for _ in range(PARK_WAIT_ROUNDS):
        if await wait_for_standstill(http, url, timeout_s=standstill_timeout_s, log=log):
            break
        log("    still spinning: not shifting to P yet")
    else:
        log("!!! motor still spinning: shift to P yourself once it has stopped")
        return
    try:
        await shift(http, url, "P", **timing)
        log(">>> In P.")
    except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
        log(f"shift to P failed: {e}")


async def _run_odin(http, ws, url, learn: Learn, body: dict, *, timing, park, start_gear,
                    standstill_timeout_s, log) -> tuple[dict, str | None, str | None]:
    """One ODIN learn procedure with the brake/shift choreography.
    Returns (run result, LEARN_RESULT, START_RESULTS_REASON if START was refused)."""
    in_drive = False
    if start_gear:
        try:
            await shift(http, url, start_gear, **timing)
        except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
            log(f"shift to {start_gear} failed: {e}; not starting")
            return {"exit_code": None, "passed": False}, None, None
        in_drive = start_gear == "D"
        log(f"In {start_gear}; waiting for the spin permit"
            + ("" if in_drive else " and a still axle") + " before starting the learn")
        if not await wait_spin_ok(http, url, still=not in_drive, log=log):
            return {"exit_code": None, "passed": False}, None, None
        if not in_drive and not await set_traction(http, url, learn.traction, log=log):
            log("traction mode not confirmed; starting anyway (the learn will say why)")
    run_task = asyncio.create_task(_post(http, url, "/api/odin/run", body))
    learn_result = refused = None
    shifted = False
    watch_stop = asyncio.Event()
    watcher = None
    go = (f"Release the pedal and press the accelerator now: past {learn.spin_rpm:.0f} axle "
          f"rpm (under {learn.overspeed_rpm:.0f}), then lift off at once when told.")
    while not run_task.done():
        try:
            msg = await ws.receive(timeout=0.2)
        except TimeoutError:
            continue
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            continue
        ev = json.loads(msg.data)
        line = _describe(ev)
        if line:
            log(line)
        if ev.get("type") != "metric":
            continue
        results = (ev.get("metadata") or {}).get("results") or {}
        if ev.get("metric") == learn.result_metric:
            learn_result = results.get("LEARN_RESULT")
            await _log_tp_health(http, url, log)
            watch_stop.set()
            if park and in_drive:
                # The procedure now waits for P; only shift once the motor has stopped.
                log(f">>> learn finished ({learn_result})")
                await _park(http, url, timing, standstill_timeout_s, log)
                in_drive = False
        if ev.get("metric") == learn.start_metric and not shifted:
            shifted = True
            started = results.get("START_ROUTINE_RESULTS")
            await _log_tp_health(http, url, log)
            if started != "SUCCESSFUL":
                refused = results.get("START_RESULTS_REASON") or str(started)
                log(f"START {started!r} ({refused}): not shifting")
                if refused in ("NO_OFFSET_CALIBRATION", "NO_FLUX_CALIBRATION"):
                    log("    the DIR has no stored offset/flux calibration: restore it "
                        "(0x306/0x309) or run --learn offset/all first")
                continue
            watcher = asyncio.create_task(
                watch_spin_permit(http, url, watch_stop, log=log, learn=learn))
            if in_drive:
                log(">>> Learn running in D. " + go)
                continue
            log(">>> Shifting to D: keep OFF the accelerator until told (the DI refuses the "
                "shift with the pedal pressed)")
            try:
                dash = await shift(http, url, "D", **timing)
            except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
                log(f"shift failed: {e} (the procedure times out waiting for D)")
                continue
            in_drive = True
            park_lit = next(
                (t["lit"] for t in dash.get("telltales", []) if t["id"] == "parkBrake"), None)
            log(f">>> In D, CAN brake released (park-brake request lit: {park_lit}). " + go)
    watch_stop.set()
    if watcher is not None:
        await watcher
    return await run_task, learn_result, refused


START_WINDOW_S = 30.0
RESULTS_TIMEOUT_S = 90.0


async def _run_resolver_dyno(http, url, learn: Learn, *, timing, park, standstill_timeout_s,
                             log) -> tuple[dict, str | None]:
    """LEGACY_RESOLVER_LEARNING (0x407): dyno in N, spin in D, then N and START until armed."""
    fail = {"exit_code": None, "passed": False}
    try:
        await shift(http, url, "N", **timing)
    except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
        log(f"shift to N failed: {e}; not starting")
        return fail, None
    if not await wait_spin_ok(http, url, still=True, log=log):
        return fail, None
    if not await set_traction(http, url, TRACTION_DYNO, log=log):
        log("DYNO not confirmed: 0x407 START would be refused (try --reset before)")
        return fail, None
    try:
        await uds_op(http, url, "session", {"mode": "extended"})
        await uds_op(http, url, "tester_present", {"on": True})
    except (RuntimeError, aiohttp.ClientError) as e:
        log(f"DIR extended session failed: {e}")
        return fail, None
    try:
        await shift(http, url, "D", **timing)
    except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
        log(f"shift to D failed: {e}")
        return fail, None
    log(f">>> In D (dyno). Press the accelerator past {learn.spin_rpm:.0f} axle rpm; the script "
        "shifts to N there. Then lift off and let it coast.")
    loop = asyncio.get_running_loop()
    watch_stop = asyncio.Event()
    watcher = asyncio.create_task(watch_spin_permit(http, url, watch_stop, log=log, learn=learn))
    result_name = None
    try:
        deadline = loop.time() + 60.0
        while True:  # wait for the spin
            chk = await _spin_now(http, url) or {}
            rpm = chk.get("axle_rpm")
            if rpm is not None and abs(rpm) >= learn.spin_rpm:
                break
            if loop.time() > deadline:
                log(f"axle never passed {learn.spin_rpm:.0f} rpm in 60 s")
                return fail, None
            await asyncio.sleep(0.05)
        try:
            await shift(http, url, "N", brake=False, **timing)
        except (TimeoutError, RuntimeError, aiohttp.ClientError) as e:
            log(f"shift to N failed: {e}")
            return fail, None
        t_n = loop.time()
        log(f">>> In N at {rpm:.0f} rpm. Retrying START (0x407) until the inverter is in "
            f"standby; it must still be above {RESOLVER_START_RPM:.0f} rpm then.")
        last_err, next_err_log = None, 0.0
        while True:
            try:
                await uds_op(http, url, "routine_control",
                             {"routine_id": f"{RID_RESOLVER_LEARN:04X}", "subtype": 1})
                chk = await _spin_now(http, url) or {}
                log(f">>> 0x407 armed {loop.time() - t_n:.1f} s after N "
                    f"(axle {chk.get('axle_rpm')} rpm). Capturing {RESOLVER_CAPTURE_RPM[1]:.0f}"
                    f" -> {RESOLVER_CAPTURE_RPM[0]:.0f} rpm: no brake, no accelerator.")
                break
            except (RuntimeError, aiohttp.ClientError) as e:
                last_err = str(e)
            now = loop.time()
            if now >= next_err_log:
                chk = await _spin_now(http, url) or {}
                log(f"    START refused ({last_err}) {now - t_n:.1f} s after N, "
                    f"axle {chk.get('axle_rpm')} rpm")
                next_err_log = now + 1.0
            rpm = (await _spin_now(http, url) or {}).get("axle_rpm")
            if (rpm is not None and abs(rpm) < RESOLVER_START_RPM - 10) \
                    or now - t_n > START_WINDOW_S:
                log(f"!!! 0x407 never armed ({now - t_n:.1f} s after N, axle {rpm} rpm): the "
                    "inverter reached standby too late or the gate failed. Spin higher.")
                return fail, None
            await asyncio.sleep(0.1)
        deadline = loop.time() + RESULTS_TIMEOUT_S
        last = None
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                res = await uds_op(http, url, "routine_control",
                                   {"routine_id": f"{RID_RESOLVER_LEARN:04X}", "subtype": 3})
            except (RuntimeError, aiohttp.ClientError) as e:
                log(f"    results: {e}")
                continue
            r = parse_resolver_result(bytes.fromhex(res.get("result") or ""))
            state = (r.get("running"), r.get("result"))
            if state != last:
                log(f"    0x407 {r}")
                last = state
            if "error" not in r and not r["running"]:
                result_name = r["result"]
                break
        else:
            log(f"0x407 still running after {RESULTS_TIMEOUT_S:.0f} s")
    finally:
        watch_stop.set()
        await watcher
    if park:
        await _park(http, url, timing, standstill_timeout_s, log)
    ok = result_name == "LEARN_SUCCESS"
    return {"exit_code": 0 if ok else 1, "passed": ok}, result_name


RESET_MODES = ("auto", "before", "after", "both", "never")


async def run(url: str, *, learn: str = "offset", brake_settle_s=0.5, gear_timeout_s=8.0,
              park=True, standstill_timeout_s=20.0, start_gear="N",
              learn_select: str | None = None, rotor_temp_c=None, backup=True,
              reset: str = "auto", log=print) -> dict:
    """start_gear (rolls learns): gear to START in. "N" (default): the only gear where an
    un-learned offset doesn't creep the rotor (START refuses MOTOR_SPEED_DETECTED); D follows
    START with a fast brake release. "D": START in D. None: START in the current gear.
    learn_select overrides the learn's LEARN_SELECT; rotor_temp_c: see routine_params ("auto"
    reads the stator, else oil, temperature from the bus before START). reset: RESET_MODES."""
    spec = LEARNS[learn]
    if learn_select is None:
        learn_select = spec.learn_select
    url = url.rstrip("/")
    timing = {"brake_settle_s": brake_settle_s, "gear_timeout_s": gear_timeout_s, "log": log}
    fail = {"exit_code": None, "passed": False}
    async with aiohttp.ClientSession() as http, http.ws_connect(url + "/ws/odin") as ws:
        if backup and await backup_cal_dids(http, url, log=log) is None:
            log("calibration backup failed; not starting (--no-backup to skip)")
            return fail
        if rotor_temp_c == "auto":
            rotor_temp_c = await read_rotor_temp(http, url, log=log)
            if rotor_temp_c is None:
                log("no plausible stator or oil temperature on the bus; not starting "
                    "(pass --rotor-temp C)")
                return fail
        chk = await _spin_now(http, url) or {}
        why = None
        if reset in ("before", "both"):
            why = "--reset " + reset
        elif reset == "auto" and chk.get("di_state") == DI_STATE_FAULT:
            why = "DI_systemState FAULT"
        elif reset == "auto" and spec.traction == TRACTION_DYNO \
                and chk.get("dyno_available") == 0:
            why = "dyno latched unavailable"
        if why and not await reset_dir(http, url, why=why, log=log):
            return fail
        alerts_before = await alert_log_keys(http, url)
        if spec.procedure is None:
            result, learn_result = await _run_resolver_dyno(
                http, url, spec, timing=timing, park=park,
                standstill_timeout_s=standstill_timeout_s, log=log)
        else:
            body = {"procedure": spec.procedure}
            if over := routine_params(learn_select, rotor_temp_c):
                body["routine_params"] = over
                log(f"START params override: {over['ROTOR_LEARNING']}")
            kw = {"timing": timing, "park": park, "start_gear": start_gear,
                  "standstill_timeout_s": standstill_timeout_s, "log": log}
            result, learn_result, refused = await _run_odin(http, ws, url, spec, body, **kw)
            if refused == "INVALID_DI_STATE" and reset != "never" and await reset_dir(
                    http, url, why="START refused INVALID_DI_STATE", log=log):
                result, learn_result, _ = await _run_odin(http, ws, url, spec, body, **kw)
        await log_du_alerts(http, url, alerts_before, log=log)
        if reset in ("after", "both"):
            await reset_dir(http, url, why="--reset " + reset, log=log)
    log(f"exit {result.get('exit_code')} · LEARN_RESULT {learn_result}")
    return result


async def _cancel(url: str) -> None:
    async with aiohttp.ClientSession() as http, \
            http.post(url.rstrip("/") + "/api/odin/cancel", json={}) as r:
        print(f"cancel: {await r.text()}")


def _rotor_temp_arg(s: str) -> float | str:
    if s.strip().lower() == "auto":
        return "auto"
    t = float(s)
    if not -40 <= t <= 215:
        raise argparse.ArgumentTypeError("rotor temperature must be -40..215 C")
    return t


def main(argv=None, *, default_learn: str = "offset") -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8765", help="tm3web URL")
    p.add_argument("--learn", choices=list(LEARNS), default=default_learn,
                   help="offset (Tesla's default), flux, all (offset + flux), resolver-error "
                        "(0x409, rolls) or resolver (0x407, dyno)")
    p.add_argument("--rotor-temp", type=_rotor_temp_arg, default=None,
                   help="rotor temperature in C for the START, or 'auto' (stator temperature, "
                        "else oil pump fluid temperature; cold-soaked unit only). Required for "
                        "--learn flux/all (the flux is corrected to 25 C with it)")
    p.add_argument("--reset", choices=RESET_MODES, default="auto",
                   help="DIR ECU reset: auto (when a latched state would block the learn), "
                        "before, after, both, never")
    p.add_argument("--spin-rpm", type=float, default=None,
                   help="axle rpm to reach before lifting off (default: the learn's target)")
    p.add_argument("--brake-settle", type=float, default=0.5,
                   help="seconds between brake on and the gear request (default 0.5)")
    p.add_argument("--gear-timeout", type=float, default=8.0,
                   help="seconds to wait for DI_gear to change before releasing the brake anyway")
    p.add_argument("--no-park", action="store_true", help="leave the shift to P to the operator")
    p.add_argument("--start-gear", choices=["N", "D", "current"], default="N",
                   help="rolls learns: gear to START in (default N: the rotor is still there)")
    p.add_argument("--standstill-timeout", type=float, default=20.0,
                   help="seconds per standstill check before P (retried; P only once stopped)")
    p.add_argument("--no-backup", action="store_true",
                   help="skip saving the offset/flux/resolver DIDs before START")
    args = p.parse_args(argv)
    if LEARNS[args.learn].needs_temp and args.rotor_temp is None:
        p.error("--learn flux/all needs --rotor-temp (the motor's actual temperature)")
    if args.spin_rpm is not None:
        spec = LEARNS[args.learn]
        LEARNS[args.learn] = Learn(**{**spec.__dict__, "spin_rpm": args.spin_rpm})
    try:
        result = asyncio.run(run(args.url, learn=args.learn, brake_settle_s=args.brake_settle,
                                 gear_timeout_s=args.gear_timeout, park=not args.no_park,
                                 standstill_timeout_s=args.standstill_timeout,
                                 start_gear=None if args.start_gear == "current"
                                 else args.start_gear,
                                 rotor_temp_c=args.rotor_temp, backup=not args.no_backup,
                                 reset=args.reset))
    except KeyboardInterrupt:
        asyncio.run(_cancel(args.url))
        return 130
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
