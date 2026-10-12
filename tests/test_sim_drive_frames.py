"""Sim frames the DI's drive-mode plausibility checks read: ESP wheel speeds/pulses and brake
pressure, RCM accelerations, UI summon state, and drive.toml's startup brake."""
from __future__ import annotations

import sys
import types

import pytest
import sim_registry

from tests.test_vehicle_sim_nodes import _ctx, _load_scenario, _rx


def _node(name):
    return sim_registry.BY_NAME[name](_ctx())


def _frame(node, can_id, fw=None):
    frames = node.frames_for(fw) if fw else node.frames()
    return int.from_bytes(bytes({f.can_id: f for f in frames}[can_id].frame()), "little")


def _field(v, start, width):
    return v >> start & ((1 << width) - 1)


def _axle(rpm):  # DIR_torque 0x108: DIR_axleSpeed @40 s16, 0.1 rpm; None = SNA
    raw = -0x8000 if rpm is None else round(rpm * 10)
    return bytes(5) + raw.to_bytes(2, "little", signed=True) + bytes(1)


@pytest.fixture
def clock(monkeypatch):
    """A settable monotonic clock for the ESP/RCM modules."""
    t = {"now": 100.0}
    fake = types.SimpleNamespace(monotonic=lambda: t["now"])
    for name in ("ESP", "RCM"):
        monkeypatch.setattr(sys.modules[type(_node(name)).__module__], "time", fake)
    return t


# -- ESP 0x175 wheel speeds ----------------------------------------------------------------

def _wheels(esp):
    v = _frame(esp, 0x175)
    return [_field(v, s, 13) for s in (0, 13, 26, 39)]  # FrL FrR ReL ReR


@pytest.mark.parametrize(("mode", "fronts_turn", "rears_turn"),
                         [("zero", False, False), ("axle", False, True), ("road", True, True)])
def test_wheel_speed_modes(mode, fronts_turn, rears_turn):
    esp = _node("ESP")
    esp.configure(wheel_speeds=mode)
    _rx(esp, 0x108, _axle(600))
    raw = round(600 * 2.136 * 60 / 1000 / 0.042)  # ESP's Model 3 circumference
    assert _wheels(esp) == [raw if fronts_turn else 0] * 2 + [raw if rears_turn else 0] * 2


def test_axle_speed_sna_makes_the_turning_wheels_sna():
    esp = _node("ESP")
    esp.configure(wheel_speeds="axle")
    _rx(esp, 0x108, _axle(None))
    assert _wheels(esp) == [0, 0, 0x1FFF, 0x1FFF]


def test_unknown_wheel_speed_mode_is_rejected():
    with pytest.raises(ValueError):
        _node("ESP").configure(wheel_speeds="fast")


# -- ESP 0x155 wheel pulses + direction ----------------------------------------------------

def _rotation(esp):
    v = _frame(esp, 0x155)
    pulses = [_field(v, 8 * i, 8) for i in range(4)]
    dirs = [_field(v, 32 + 2 * i, 2) for i in range(4)]
    return pulses, dirs, _field(v, 40, 2)


def _pulses_for(rpm, secs, circ=2.136):  # DIR: fixed 17.4 mm/pulse, from the sent 0x175 speed
    raw = round(rpm * circ * 60 / 1000 / 0.042)
    return raw * 0.042 / 3.6 * secs / (0x4737 * 4 / 2**22)


def test_pulses_follow_the_sent_wheel_speed(clock):
    esp = _node("ESP")
    esp.configure(wheel_speeds="road")
    _rx(esp, 0x108, _axle(300))
    _rotation(esp)  # first frame: no elapsed time yet
    clock["now"] += 0.25
    pulses, dirs, avail = _rotation(esp)
    assert pulses == [int(_pulses_for(300, 0.25))] * 4 == [153] * 4  # ~123 pulses/rev
    assert dirs == [0] * 4 and avail == 3  # forward (default code 0), both availability bits
    clock["now"] += 0.5
    assert _rotation(esp)[0] == [int(_pulses_for(300, 0.75) % 255)] * 4  # rolling, mod 255


def _carconfig_mux3(chassis):  # GTW_carConfig 0x7FF mux3, GTW_chassisType @18|3
    return (3 | chassis << 18).to_bytes(8, "little")


@pytest.mark.parametrize(("chassis", "circ"), [(2, 2.136), (3, 2.276), (0, 2.136)])
def test_wheel_speed_uses_the_esp_circumference_for_the_chassis(clock, chassis, circ):
    # The ESP's calibrated circumference: per chassis, not per wheel size. Pulses follow.
    esp = _node("ESP")
    esp.configure(wheel_speeds="road")
    _rx(esp, 0x7FF, _carconfig_mux3(chassis))
    _rx(esp, 0x108, _axle(300))
    assert _wheels(esp) == [round(300 * circ * 60 / 1000 / 0.042)] * 4
    _rotation(esp)
    clock["now"] += 0.25
    assert _rotation(esp)[0] == [int(_pulses_for(300, 0.25, circ=circ))] * 4


def test_axle_mode_leaves_the_fronts_still(clock):
    esp = _node("ESP")
    esp.configure(wheel_speeds="axle", forward_direction=2)
    _rx(esp, 0x108, _axle(300))
    _rotation(esp)
    clock["now"] += 0.25
    pulses, dirs, _ = _rotation(esp)
    assert pulses == [0, 0, 153, 153] and dirs == [0, 0, 2, 2]


def test_direction_codes(clock):
    esp = _node("ESP")
    esp.configure(wheel_speeds="road", forward_direction=2)
    for rpm, want in ((300, 2), (-300, 1), (0, 0), (None, 0)):  # SNA reads as not moving
        _rx(esp, 0x108, _axle(rpm))
        assert _rotation(esp)[1] == [want] * 4, rpm


def test_sna_holds_the_pulse_counters(clock):
    esp = _node("ESP")
    esp.configure(wheel_speeds="road")
    _rx(esp, 0x108, _axle(600))
    _rotation(esp)
    clock["now"] += 0.1
    held = _rotation(esp)[0]
    _rx(esp, 0x108, _axle(None))
    clock["now"] += 0.1
    assert _rotation(esp)[0] == held


def test_forward_direction_is_0_or_2():
    with pytest.raises(ValueError):
        _node("ESP").configure(forward_direction=1)  # 1 is backward


# -- ESP 0x105 master-cylinder pressure agrees with 0x38D ----------------------------------

def _mc_pressures(esp):
    p105 = _field(_frame(esp, 0x105), 36, 10)
    p38d = _field(int.from_bytes(bytes(esp._esp_party3()), "little"), 44, 10)
    return p105, p38d


@pytest.mark.parametrize(("posture", "pressure", "raw"), [
    ("released", None, 100),        # 0 bar
    ("applied", None, 267),         # default 50% -> 50 bar
    ("applied", 20, 167),           # 20 bar
    ("released", 29, 100),          # a pressure left over after release is not sent
])
def test_0x105_and_0x38d_report_the_same_pressure(posture, pressure, raw):
    # A mismatch between the two raises DI_a198.
    esp = _node("ESP")
    esp.set_brake("applied", pressure)
    esp.set_brake(posture, pressure)
    assert _mc_pressures(esp) == (raw, raw)


def test_0x105_keeps_its_availability_bits():
    v = _frame(_node("ESP"), 0x105)
    assert [_field(v, b, 1) for b in (13, 14, 15, 33, 34, 35)] == [1] * 6


# -- RCM 0x111 -----------------------------------------------------------------------------

def _accels(rcm):
    v = _frame(rcm, 0x111)
    signed = [(_field(v, s, 16) ^ 0x8000) - 0x8000 for s in (0, 16, 32)]
    return [a * 0.00125 for a in signed], _field(v, 48, 3)


def test_vertical_accel_is_gravity_compensated_by_default():
    (long_a, lat_a, vert_a), qf = _accels(_node("RCM"))
    assert (long_a, lat_a, vert_a) == (0.0, 0.0, 0.0)  # +/-9.81 trips DI_a216 VERT_ACCEL
    assert qf == 0b111


def test_vertical_accel_is_settable():
    rcm = _node("RCM")
    rcm.configure(vertical_accel=-9.81)
    assert _accels(rcm)[0][2] == pytest.approx(-9.81, abs=0.00125)


def test_longitudinal_accel_follows_the_axle_speed(clock):
    rcm = _node("RCM")
    _rx(rcm, 0x108, _axle(0))
    clock["now"] += 0.1
    _rx(rcm, 0x108, _axle(100))  # 100 rpm * 2.1 m / 60 s = 3.5 m/s in 0.1 s
    assert _accels(rcm)[0][0] == pytest.approx(35.0, abs=0.01)
    _rx(rcm, 0x108, _axle(None))  # SNA holds it
    assert _accels(rcm)[0][0] == pytest.approx(35.0, abs=0.01)


# -- UI 0x3B3 ------------------------------------------------------------------------------

def test_summon_state_is_idle_from_2022():
    ui = _node("UI")
    for fw in ("2022.45.15", "2026.8.3"):
        assert _field(_frame(ui, 0x3B3, fw), 16, 2) == 1, fw  # IDLE; SNA/ACTIVE upset the DIR
    assert 0x3B3 not in {f.can_id for f in ui.frames_for("2020.8.1")}


# -- DAS 0x2B9 -----------------------------------------------------------------------------

def _das_limits(v):
    assert _field(v, 16, 2) == 0  # no AEB event
    assert _field(v, 35, 9) * 0.04 - 15 == pytest.approx(-15)   # accelMin
    assert _field(v, 44, 9) * 0.04 - 15 == pytest.approx(5.4)   # accelMax
    assert _field(v, 18, 9) == 0 and _field(v, 27, 8) == 0xFE   # jerk limits, not SNA


def test_das_control_is_manual_by_default():
    v = _frame(_node("DAS"), 0x2B9)
    assert _field(v, 12, 4) == 13  # ACC_CANCEL_GENERIC_SILENT
    assert _field(v, 0, 12) == 0
    _das_limits(v)


def test_das_control_acc_on():
    das = _node("DAS")
    das.configure(acc_state="ACC_ON")
    v = _frame(das, 0x2B9)
    assert _field(v, 12, 4) == 4  # ACC_ON; APC_* states would start autopark
    assert _field(v, 0, 12) * 0.1 == pytest.approx(52.5)
    _das_limits(v)


def test_das_acc_state_takes_a_raw_value():
    das = _node("DAS")
    das.configure(acc_state=3)  # ACC_HOLD
    assert _field(_frame(das, 0x2B9), 12, 4) == 3


@pytest.mark.parametrize("bad", ["ON", 16])
def test_das_acc_state_rejects_unknown_values(bad):
    with pytest.raises(ValueError):
        _node("DAS").configure(acc_state=bad)


# -- BMS 0x2D2 --------------------------------------------------------------------------------

@pytest.mark.parametrize(("amps", "raw"), [(None, 0), (150, 1500), (1638.3, 0x3FFF)])
def test_bms_max_charge_current(amps, raw):
    bms = _node("BMS")
    if amps is not None:
        bms.configure(max_charge_current=amps)
    assert _field(_frame(bms, 0x2D2, "2022.45.15"), 32, 14) == raw  # 0.1 A; 0 = no regen


@pytest.mark.parametrize("amps", [-1, 5000])
def test_bms_max_charge_current_rejects_out_of_range(amps):
    with pytest.raises(ValueError):
        _node("BMS").configure(max_charge_current=amps)


# -- tm3web BRAKE telltale -----------------------------------------------------------------

def _brake_sig(esp, ibst, vcl):  # *_driverBrakeApply 1 not applied / 2 applying; None = absent
    sig = {"DI_brakePedalState": 2}  # 2022+ DIRs always send INVALID
    for name, v in (("ESP_driverBrakeApply", esp), ("IBST_driverBrakeApply", ibst),
                    ("VCLEFT_brakeSwitchPressed", vcl)):
        if v is not None:
            sig[name] = v
    return sig


@pytest.mark.parametrize(("esp", "ibst", "vcl", "vote"), [
    (2, 2, 1, "ON"), (1, 1, 0, "OFF"), (2, 2, 0, "MISMATCH"), (2, 3, 1, "MISMATCH"),
    (2, None, 1, "MISMATCH"), (None, None, None, None),
])
def test_brake_telltale_follows_the_brake_sources_not_di_brake_pedal_state(esp, ibst, vcl, vote):
    import tm3web
    snap = tm3web._build_dash_snapshot(_brake_sig(esp, ibst, vcl), None)
    assert snap["brake_vote"] == vote
    brake = next(t for t in snap["telltales"] if t["id"] == "brake")
    assert brake["lit"] is (vote == "MISMATCH")
    assert snap["brake_pedal_state"] == 2


# -- drive.toml ----------------------------------------------------------------------------

def test_drive_scenario_starts_released_on_every_brake_source():
    # ESP, IBST and VCLEFT are the DI's brake-vote inputs; they must agree.
    _, nodes, _ = _load_scenario("drive.toml")
    by = {n.name: n for n in nodes}
    assert by["ESP"].brake == by["IBST"].brake == "released"
    assert by["VCLEFT"].brake_switch_pressed is False
    assert (by["ESP"].wheel_speeds, by["ESP"].forward_direction) == ("road", 0)
    assert by["DAS"].acc_state == 13  # ACC_CANCEL_GENERIC_SILENT: manual control
