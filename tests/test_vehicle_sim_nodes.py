"""Golden regression test for the node-centric bench (stateful Node model).

Locks the node registry to a fixed frame inventory: per-bus membership plus each
frame's period / rolling-counter position / checksum position / counter width / DLC.
Instantiated with a stub NodeContext, so no real DB or CAN bus is needed.
"""
from __future__ import annotations

import pytest
import sim_core
import sim_registry

# (can_id, bus) -> (period_s, counter_start, cksum_start, counter_width, dlc)
# Keyed on (id, bus): id 0x39D (IBST_status) ships on both buses.
# MIA-owning party frames (rcm/esp/ibst/epas3p) transmit at sim_core.PARTY_LIVENESS_S
# (the group2 CANB MIA-clear floor); non-waited-on das 0x389/0x2B9 keep their native
# cycle; 0x11D is at PARTY_LIVENESS_S for the DIR VDC freshness watchdog.
GOLDEN: dict[tuple[int, str], tuple] = {
    # ---- vehicle bus (group1 / CANA) ----
    # 2026.8.3 shortens BMS_hvBusStatus to DLC 6 (BMS_chgTimeToFull dropped); the DIR's DLC
    # check is an exact match, so the old 8-byte build is rejected outright on a 2026 DU.
    (0x132, "vehicle"): (0.010, None, None, 4, 6),
    (0x212, "vehicle"): (0.100, None, None, 4, 8),
    (0x252, "vehicle"): (0.100, None, None, 4, 8),
    (0x2D2, "vehicle"): (0.100, None, None, 4, 8),
    (0x312, "vehicle"): (1.000, None, None, 4, 8),
    # CP_status ships on BOTH 0x210 (catalog id) and 0x25D (2022 DIR subscribes; gates
    # on id 0x25D/dlc 8; 0x210 is not in the DIR RX table). See scripts/cp/cp.py.
    (0x210, "vehicle"): (0.100, None, None, 4, 8),
    (0x21D, "vehicle"): (0.100, None, None, 4, 8),  # CP_evseStatus (EVSE-connect report)
    (0x224, "vehicle"): (0.100, None, None, 4, 8),
    # 2022 CANData cycle 50ms (20Hz); slower -> a155 vcfrontMIA (also 0x3C2 below).
    (0x3A1, "vehicle"): (0.050, 52, 56, 4, 8),
    (0x2E1, "vehicle"): (0.017, None, None, 4, 8),
    (0x241, "vehicle"): (0.100, None, None, 4, 7),
    # VCFRONT_sensors: the 2022 DIR gates 0x321 on a checksum+counter (plain in 2020).
    (0x321, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x102, "vehicle"): (0.100, None, None, 4, 8),
    (0x3C2, "vehicle"): (0.050, None, None, 4, 8),  # 2022 cycle=50ms (a155 member)
    (0x221, "vehicle"): (0.050, None, None, 4, 8),
    (0x103, "vehicle"): (0.100, None, None, 4, 8),
    (0x392, "vehicle"): (1.000, None, None, 4, 8),
    (0x229, "vehicle"): (0.100, None, None, 4, 3),
    (0x2A8, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x2E8, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x247, "vehicle"): (0.100, None, None, 4, 8),
    (0x207, "vehicle"): (0.100, None, None, 4, 8),
    (0x7FF, "vehicle"): (0.100, None, None, 4, 8),
    (0x528, "vehicle"): (0.100, None, None, 4, 4),
    (0x3ED, "vehicle"): (0.100, None, None, 4, 1),
    (0x082, "vehicle"): (1.000, None, None, 4, 8),
    (0x213, "vehicle"): (0.100, 4, 8, 4, 2),
    # UI_vehicleModes: DLC8 (DIR length-gate raises a094 canDataBusA on short frames). From
    # 2026.8.3 it also carries a counter+checksum the DIR enforces.
    (0x284, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x293, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x313, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x334, "vehicle"): (0.100, None, None, 4, 8),
    (0x333, "vehicle"): (0.500, None, None, 4, 4),  # UI_chargeRequest (user charge input)
    # 2022.45.15-only vehicle-bus members (fw_variants); gated so --fw 2020.8.1 omits them.
    (0x452, "vehicle"): (0.100, None, None, 4, 3),  # bms limits (torque-clamp input + bmsMIA)
    (0x2A7, "vehicle"): (0.100, None, None, 4, 8),  # cmp variant (config-selected alt of 0x247)
    # app liveness (appMIA a108). 2026.8.3 renumbers it 0x25C -> 0x25B APP_environment and
    # grows it to a gated DLC8 (counter@52 + checksum@56, reseeded magic 0x5B). GOLDEN is built
    # at the newest target, so 0x25B is the entry here; 0x25C is asserted in test_fw_versioning.
    (0x25B, "vehicle"): (0.100, 52, 56, 4, 8),
    # 2026.8.3-new rx ids the DIR MIA-supervises. All zeros(8) liveness; the two GATED ones
    # (0x238, 0x318) carry counter@52 + checksum@56 with the default id_lo+id_hi magic
    # (0x3A / 0x1B), the other two have no validator at all.
    (0x142, "vehicle"): (0.100, None, None, 4, 8),  # VCLEFT_liftgateStatus (ungated)
    (0x238, "vehicle"): (0.100, 52, 56, 4, 8),      # UI_driverAssistMapData (gated)
    (0x318, "vehicle"): (0.100, 52, 56, 4, 8),      # GTW_carState (gated)
    (0x3FD, "vehicle"): (0.100, None, None, 4, 8),  # UI_autopilotControl (ungated)
    # cp charge-cable state (cpMIA a105 + DI_a162_chargeCableConnected); DIR-only id.
    (0x25D, "vehicle"): (0.100, None, None, 4, 8),
    (0x3B3, "vehicle"): (0.100, None, None, 4, 8),  # UI_vehicleControl2 (uiMIA a088 member, drive mode)
    (0x353, "vehicle"): (0.100, None, None, 4, 8),  # UI_status: UI_developmentCar @40 -> DIR dyno-inhibit bypass
    # IBST_status_A: SAME id as the party 0x39D below, sent on bus A too. The 2022 DIR
    # validates 0x39D on CANA (cksum+counter -> a110_brakeMIA); 2020 wanted party only.
    (0x39D, "vehicle"): (0.010, 8, 0, 4, 5),        # ibst (2022.45.15 variant)
    # 0x392 stays in the inventory but reassigns EPAS3P_alertMatrix (2020, epas3p) ->
    # BMS_packConfig (2022, bms); newest target sources it from bms (GOLDEN 0x392 above).
    # undocumented PCS-context frames
    (0x13D, "vehicle"): (0.010, None, None, 4, 6),
    (0x2B2, "vehicle"): (0.100, None, None, 4, 5),
    # HVP (High Voltage Processor): commands the PCS + owns the contactors.
    # Both plain (no counter/checksum in 2020 fw), 10ms, on the vehicle bus.
    (0x22A, "vehicle"): (0.010, None, None, 4, 4),
    (0x20A, "vehicle"): (0.010, None, None, 4, 6),
    # party bus (group2 / CANB). MIA members -> PARTY_LIVENESS_S (0.010 = 100Hz floor);
    # das keeps native cycle; 0x11D also at PARTY_LIVENESS_S (VDC freshness).
    (0x3D1, "party"): (0.010, None, None, 4, 8),  # epas3p (native 1Hz)
    (0x370, "party"): (0.010, 48, 56, 4, 8),      # epas3p (native 10Hz)
    (0x145, "party"): (0.010, 8, 0, 4, 8),        # esp
    (0x105, "party"): (0.010, 52, 56, 4, 8),      # esp
    (0x155, "party"): (0.010, 52, 56, 4, 8),      # esp
    (0x175, "party"): (0.010, 52, 56, 4, 8),      # esp
    (0x185, "party"): (0.010, 52, 56, 4, 8),      # esp
    (0x38D, "party"): (0.010, None, None, 4, 7),  # esp
    (0x39D, "party"): (0.010, 8, 0, 4, 5),        # ibst
    (0x38E, "party"): (0.010, None, None, 4, 6),  # ibst
    (0x101, "party"): (0.010, 52, 56, 4, 8),      # rcm
    (0x111, "party"): (0.010, 52, 56, 4, 8),      # rcm
    (0x389, "party"): (0.500, 52, 56, 4, 8),      # das (non-MIA, native)
    (0x2B9, "party"): (0.040, 53, 56, 3, 8),      # das (non-MIA, native)
    (0x289, "party"): (0.100, 8, 0, 3, 3),        # das (dasMIA member; 2022.45.15 DIR-pinned)
    (0x39B, "party"): (0.100, 52, 56, 4, 8),      # das (dasMIA member; 2022.45.15 DIR-pinned)
    (0x11D, "party"): (0.010, 8, 0, 4, 8),        # esp; PARTY_LIVENESS_S -- DIR VDC freshness (a195/6/7, a210)
    # ---- DIF: the simulated FRONT drive unit of an AWD pair ----
    # The entire front->rear surface an AWD ("Master") rear rx's; a RWD rear rx's none of them.
    # Resolved at the node's newest authored revision (2026.8.3) like every other entry here --
    # the 2022.45.15 set differs and is locked separately below. Checksum byte 0 / counter byte 1
    # low nibble, which is the firmware's placement, not the byte7/byte6 one.
    (0x186, "party"): (0.010, 8, 0, 4, 8),        # DIF_torque
    (0x187, "party"): (0.010, 8, 0, 4, 8),        # DIF, in no ETH DBC at any revision
    (0x2D5, "party"): (0.010, 8, 0, 4, 8),        # DIF_status (DLC 7 on 2022.45.15 -- see below)
    (0x2E5, "vehicle"): (0.010, None, None, 4, 8),  # DIF_power; ungated, and the one on bus A
    # PMF: the front unit's CPU1, the other half of a simulated front. 3-BIT counter at 53 (not
    # the usual 4 at 52) -- a 4-bit one would run into the checksum byte.
    (0x1D5, "vehicle"): (0.010, 53, 56, 3, 8),    # PMF_state4
}


class _StubDb:
    """Minimal CAN DB for the GTW node's MuxedConfigTx (0x7FF) -- no real DB needed."""

    # value_description is what lets a scenario name an enum LABEL ("AWD") rather than a raw
    # number -- the shipped drive profiles do, since a label survives a revision renumbering.
    messages = {
        0x7FF: {
            "name": "GTW_carConfig",
            "signals": {
                "GTW_muxer": {"is_muxer": True},
                "GTW_chassisType": {
                    "mux_id": 0, "width": 4,
                    "value_description": {"3_CHASSIS": 2, "Y_CHASSIS": 3},
                },
                "GTW_drivetrainType": {
                    "mux_id": 0, "width": 4,
                    "value_description": {"RWD": 0, "AWD": 1},
                },
            },
        },
    }

    def encode_frame(self, msg_id, sv):
        return bytearray(8)


def _ctx() -> sim_core.NodeContext:
    return sim_core.NodeContext(db=_StubDb())


def _frames(classes=None):
    return sim_registry.collect_frames(sim_registry.instantiate(classes, _ctx()))


def _ids(classes) -> set[int]:
    return {f.can_id for f in _frames(classes)}


# Drive bench marks the inverter (DI/DIR/PMR) real; the simulated inventory is the peers,
# which is the GOLDEN set above.
_INVERTER = ["DI", "DIR", "PMR"]


def _sim_frames():
    return _frames(sim_registry.select_nodes(real=_INVERTER))


def _no_send(_cid, _data):
    pass


def _rx(node, can_id, data, send=_no_send):
    """Deliver a frame to a node's registered rx handler (test-side of the engine's dispatch)."""
    cb = node.rx_handlers().get(can_id)
    if cb is not None:
        cb(data, send)


def test_registry_expands_to_golden_inventory():
    frames = _sim_frames()
    by_key = {(f.can_id, f.bus): f for f in frames}
    assert len(frames) == len(GOLDEN), "frame count changed"
    assert len(by_key) == len(frames), "duplicate (arbitration ID, bus) in the registry"
    assert set(by_key) == set(GOLDEN), "arbitration-ID set changed"
    for (cid, bus), (period, ctr, cks, cw, dlc) in GOLDEN.items():
        f = by_key[(cid, bus)]
        where = f"0x{cid:03X} ({bus})"
        assert f.period_s == period, f"{where} period"
        assert f.counter_start == ctr, f"{where} counter_start"
        assert f.cksum_start == cks, f"{where} cksum_start"
        assert f.counter_width == cw, f"{where} counter_width"
        assert len(f.frame()) == dlc, f"{where} DLC"


def test_rolling_counter_rolls_back_on_a_dropped_send():
    # note_send(False) rolls the counter back so the dropped value is reused by the next
    # frame -> the on-wire sequence stays gapless (0,1,1 not 0,1,2).
    f = sim_core.SimFrame(
        "t", 0x101, 0.0125, lambda: bytearray(8), counter_start=52, cksum_start=56,
    )
    ctr = lambda b: (b[6] >> 4) & 0xF  # 4-bit counter @ bit52 = byte6 hi-nibble  # noqa: E731
    b0 = f.frame()          # counter 0
    f.note_send(True)       # delivered
    b1 = f.frame()          # counter 1
    f.note_send(False)      # dropped -> roll back
    b2 = f.frame()          # counter 1 reused
    assert (ctr(b0), ctr(b1), ctr(b2)) == (0, 1, 1)
    # counter-less frame: note_send is a no-op
    p = sim_core.SimFrame("p", 0x38E, 0.0125, lambda: bytearray(6))
    p.note_send(False)  # does not raise


def test_builder_managed_counter_also_rolls_back_on_drop():
    # J1850/E2E frames carry the counter inside the builder (under a CRC), so counter_start
    # is None; note_send delegates the rollback to the builder. Covers IBST 0x38E, ESP 0x38D.
    from tesla_frames import J1850Frame, LvPowerState, SccmRightStalk

    j = J1850Frame(6)
    f = sim_core.SimFrame("ibst", 0x38E, 0.0125, j.frame)  # counter @ byte1 lo-nibble
    lo = lambda b: b[1] & 0xF  # noqa: E731
    b0 = f.frame(); f.note_send(True)     # noqa: E702  -- ctr 0
    b1 = f.frame(); f.note_send(False)    # noqa: E702  -- ctr 1 dropped -> rollback
    b2 = f.frame(); f.note_send(True)     # noqa: E702  -- ctr 1 reused
    assert (lo(b0), lo(b1), lo(b2)) == (0, 1, 1)

    # SccmRightStalk (0x229) + LvPowerState (0x221) expose the same rollback contract.
    for owner in (SccmRightStalk(), LvPowerState()):
        start = owner._ctr
        owner.frame()
        assert owner._ctr == (start + 1) & 0xF
        owner.rollback()
        assert owner._ctr == start


def test_bus_membership_matches_firmware_groups():
    frames = _sim_frames()
    vehicle = {f.can_id for f in frames if f.bus == "vehicle"}
    party = {f.can_id for f in frames if f.bus == "party"}
    assert vehicle == {cid for cid, bus in GOLDEN if bus == "vehicle"}
    assert party == {cid for cid, bus in GOLDEN if bus == "party"}
    # Board-TX'd DIR/PMR ids never simulated (collision -> canDataBusB).
    assert 0x1E5 not in vehicle and 0x1E5 not in party
    assert 0x240 not in vehicle and 0x240 not in party


def test_collect_frames_accepts_a_node_subset():
    reg = sim_registry
    frames = reg.collect_frames(reg.instantiate([reg.BY_NAME["BMS"], reg.BY_NAME["CP"]], _ctx()))
    assert {f.can_id for f in frames} == {
        0x132, 0x212, 0x252, 0x2D2, 0x312, 0x452, 0x392, 0x210, 0x21D, 0x25D,
    }


def test_das_2022_variant_gates_the_new_dasmia_members():
    # 0x289/0x39B are 2022-new dasMIA members: absent in 2020.8.1, present in 2022.45.15;
    # they live in DAS.fw_variants()["2022.45.15"], not the 2020 baseline.
    reg = sim_registry
    das = [reg.BY_NAME["DAS"]]
    ids = lambda fw: {  # noqa: E731
        f.can_id for f in reg.collect_frames(reg.instantiate(das, _ctx()), fw=fw)
    }
    assert ids("2020.8.1") == {0x389, 0x2B9}
    assert ids("2022.45.15") == {0x389, 0x2B9, 0x289, 0x39B}
    assert ids("2024.8.9") == {0x389, 0x2B9, 0x289, 0x39B}  # persists forward
    assert ids(None) == {0x389, 0x2B9, 0x289, 0x39B}  # default = newest authored


def test_dif_0x2d5_is_dlc7_on_2022_and_dlc8_on_2026():
    # The DBC says DLC 8 for DIF_status at EVERY revision. The 2022.45.15 AWD DIR
    # gates it at 7, and the DLC check is an exact match -- a DLC-8 frame is rejected outright and
    # the frame goes MIA while looking healthy on the wire. It is 8 again on 2026.8.3. Both read
    # out of firmware; the DBC is not evidence here.
    reg = sim_registry
    dif = [reg.BY_NAME["DIF"]]
    dlc = lambda fw: {  # noqa: E731
        len(f.frame())
        for f in reg.collect_frames(reg.instantiate(dif, _ctx()), fw=fw)
        if f.can_id == 0x2D5
    }
    assert dlc("2022.45.15") == {7}
    assert dlc("2026.8.3") == {8}


def test_dif_frames_carry_the_firmware_checksum_seed_and_counter_placement():
    # Seed is the plain id_lo+id_hi rule (0x186 -> 0x87), checksum in byte 0, rolling counter in
    # byte 1's low nibble. With a zero payload the checksum IS seed+counter, so the first three
    # transmissions pin seed, placement and roll-forward in one go.
    reg = sim_registry
    f = next(
        x
        for x in reg.collect_frames(reg.instantiate([reg.BY_NAME["DIF"]], _ctx()), fw="2022.45.15")
        if x.can_id == 0x186
    )
    assert [tuple(f.frame()[:2]) for _ in range(3)] == [(0x87, 0), (0x88, 1), (0x89, 2)]


def test_awd_both_real_profile_stops_simulating_the_front():
    # A bench with BOTH physical units marks DIF+PMF `real` -- the same nodes the RWD profile
    # calls `absent`. That collision is why the two keys exist separately: one claims hardware,
    # the other denies it exists, and picking the wrong one on a two-unit bench means the sim
    # transmits over a real inverter.
    cfg, _nodes, frames = _load_scenario("drive-awd-both.toml")
    assert set(cfg.real) == {"DI", "DIR", "PMR", "DIF", "PMF"}
    assert not cfg.absent
    assert not (_FRONT_IDS & {f.can_id for f in frames})
    # The car is still an AWD car; only who simulates the front changed.
    assert cfg.scenario["GTW"]["drivetrain_type"] == "AWD"


def test_a_simulated_front_is_both_cores():
    # The front is ONE physical unit running two cores, so standing it in needs both nodes:
    # DIF (CPU2) sources the four DIF_* frames, PMF (CPU1) sources 0x1D5 PMF_state4. Simulating
    # only DIF leaves a 2026 rear in pmfMIA (DI_a042); the 2022 rear does not subscribe to 0x1D5.
    _cfg, _nodes, frames = _load_scenario("drive-awd.toml")
    ids = {f.can_id for f in frames}
    assert ids >= _FRONT_IDS
    pmf = next(f for f in frames if f.can_id == 0x1D5)
    # 3-bit counter: a 4-bit one would overflow bit 56 and corrupt the checksum byte.
    assert (pmf.counter_start, pmf.cksum_start, pmf.counter_width) == (53, 56, 3)
    assert pmf.bus == "vehicle"


def test_pmf_state4_carries_the_firmware_seed_and_3bit_counter():
    # Seed 0xD6 = id_lo + id_hi, checksum in byte 7, counter at 53 wrapping at 8 not 16. With a
    # zero payload the checksum is seed+counter, so eight sends pin the width: the 9th repeats.
    f = next(
        x
        for x in sim_registry.collect_frames(
            sim_registry.instantiate([sim_registry.BY_NAME["PMF"]], _ctx())
        )
        if x.can_id == 0x1D5
    )
    seen = [f.frame() for _ in range(9)]
    # counter n sits at bit 53, so it contributes n << 5 to byte 6; checksum is mod 256.
    assert [b[7] for b in seen] == [(0xD6 + 0x20 * n) & 0xFF for n in range(8)] + [0xD6]
    assert seen[8] == seen[0], "3-bit counter must wrap after 8, not 16"


def test_real_and_absent_are_mutually_exclusive():
    with pytest.raises(ValueError, match="both real and absent"):
        sim_registry.select_nodes(real=["DIF"], absent=["dif"])


def test_load_bench_config_rejects_a_node_that_is_both_real_and_absent(tmp_path):
    p = tmp_path / "sim.toml"
    p.write_text('[nodes]\nreal = ["DIF"]\nabsent = ["DIF"]\n')
    with pytest.raises(ValueError, match="both real and absent"):
        sim_registry.load_bench_config(p)


def test_front_unit_is_absent_from_a_rwd_bench_only_by_deselection():
    # A RWD ("Single") rear rx's none of the front's IDs, so they are harmless-but-unhandled
    # there. There is no RWD/AWD switch in the nodes themselves -- a RWD bench drops them via
    # `absent`, the same mechanism that keeps the sim off a connected physical inverter.
    ids = _ids(sim_registry.select_nodes(absent=["DIF", "PMF"]))
    assert not (_FRONT_IDS & ids)


# Everything a simulated FRONT drive unit sources: DIF (CPU2) + PMF (CPU1).
_FRONT_IDS = {0x186, 0x187, 0x2D5, 0x2E5, 0x1D5}


def _load_scenario(name):
    """Load a shipped scenario TOML and expand it the way vehicle_sim does."""
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scenarios" / name
    bc = sim_registry.load_bench_config(path)
    nodes = sim_registry.instantiate(
        sim_registry.select_nodes(bc.sim, bc.real, bc.absent), _ctx()
    )
    for n in nodes:
        n.fw = bc.fw
        if bc.scenario.get(n.name):
            n.configure(**dict(bc.scenario[n.name]))
    return bc, nodes, sim_registry.collect_frames(nodes)


def test_drive_scenarios_differ_only_in_drivetrain_and_the_front_unit():
    # The two shipped drive profiles bench the same rear hardware; what separates them is the
    # drivetrain the car config declares and whether the front unit is simulated. An AWD rear
    # raises difMIA without a front, and a RWD rear subscribes to none of the front's IDs.
    dif_ids = _FRONT_IDS

    rwd_cfg, rwd_nodes, rwd_frames = _load_scenario("drive.toml")
    awd_cfg, awd_nodes, awd_frames = _load_scenario("drive-awd.toml")

    gtw = lambda nodes: next(n for n in nodes if n.name == "GTW")  # noqa: E731
    assert gtw(rwd_nodes).config["drivetrain_type"] == 0  # RWD
    assert gtw(awd_nodes).config["drivetrain_type"] == 1  # AWD
    # Both are Model 3, and neither leaves the pairing-relevant keys unexpressible on this rev.
    for nodes in (rwd_nodes, awd_nodes):
        assert gtw(nodes).config["chassis_type"] == 2
        assert "drivetrain_type" not in gtw(nodes).unsupported

    assert not (dif_ids & {f.can_id for f in rwd_frames}), "RWD bench must not simulate a front"
    assert dif_ids <= {f.can_id for f in awd_frames}, "AWD bench must simulate the front"
    # Same rear hardware on both. The RWD car has no front at all -- `absent`, not `real` -- and
    # that means both of its cores.
    assert set(rwd_cfg.real) == set(awd_cfg.real) == {"DI", "DIR", "PMR"}
    assert set(rwd_cfg.absent) == {"DIF", "PMF"} and not awd_cfg.absent

    # Both pin the bench unit's revision -- load-bearing for the front, where DIF_status 0x2D5 is
    # DLC 7 on 2022.45.15 and every DBC says 8.
    assert rwd_cfg.fw == awd_cfg.fw == "2022.45.15"
    d5 = next(f for f in awd_frames if f.can_id == 0x2D5)
    assert len(d5.frame()) == 7 and d5.bus == "party"
    assert next(f for f in awd_frames if f.can_id == 0x2E5).bus == "vehicle"


def test_every_node_name_is_unique():
    names = [n.name for n in sim_registry.NODES]
    assert len(names) == len(set(names)), f"duplicate node names: {names}"


# Phase 1: node selection + MIA coverage


def test_select_nodes_real_drops_only_that_node():
    full = _ids(None)
    # 0x318 GTW_carState joins GTW's set at 2026.8.3 (the default target is the newest authored).
    assert full - _ids(sim_registry.select_nodes(real=["GTW"])) == {0x7FF, 0x528, 0x3ED, 0x318}


def test_select_nodes_sim_is_a_case_insensitive_whitelist():
    nodes = sim_registry.select_nodes(sim=["bms", "cp"])
    assert {n.name for n in nodes} == {"BMS", "CP"}
    assert _ids(nodes) == {
        0x132, 0x212, 0x252, 0x2D2, 0x312, 0x452, 0x392, 0x210, 0x21D, 0x25D,
    }


def test_select_nodes_unknown_name_raises():
    import pytest

    with pytest.raises(ValueError, match="unknown node"):
        sim_registry.select_nodes(real=["NOPE"])


def test_legacy_no_flags_alias_to_real_nodes():
    # --no-shifter/--no-gtw/--no-ui are documented aliases for --real SCCM/GTW/UI.
    full = _ids(None)
    assert _ids(sim_registry.select_nodes(real=["SCCM"])) == full - {0x229}
    # 0x238 + 0x3FD join UI's set at 2026.8.3 (the default target is the newest authored).
    assert _ids(sim_registry.select_nodes(real=["UI"])) == full - {
        0x82, 0x213, 0x284, 0x293, 0x313, 0x334, 0x333, 0x3B3, 0x353, 0x238, 0x3FD,
    }


def test_mia_coverage_warnings_partial_full_absent():
    reg = sim_registry
    esp = set(reg.MIA_AGGREGATES["espMIA"])
    assert reg.mia_coverage_warnings(esp) == []  # full coverage -> silent
    assert reg.mia_coverage_warnings(set()) == []  # fully absent -> silent
    warns = reg.mia_coverage_warnings(esp - {0x105})  # partial -> warns
    assert any("espMIA" in w and "0x105" in w for w in warns)


# Phase 2: bench config (TOML) + bus normalization


def test_canonical_bus_eth_and_unknown_map_to_vehicle():
    import config

    assert config.canonical_bus("eth") == "vehicle"
    assert config.canonical_bus("ETH") == "vehicle"
    assert config.canonical_bus(None) == "vehicle"
    assert config.canonical_bus("wat") == "vehicle"
    assert config.canonical_bus("party") == "party"
    assert config.canonical_bus("ch") == "charge"


def test_load_bench_config_parses_nodes_and_normalizes_bus(tmp_path):
    p = tmp_path / "bench.toml"
    p.write_text('[nodes]\nreal = ["PCS", "cp"]\n[bus]\n0x370 = "charge"\n0x241 = "eth"\n')
    cfg = sim_registry.load_bench_config(p)
    assert cfg.sim is None
    assert cfg.real == ["PCS", "cp"]
    assert cfg.bus == {0x370: "charge", 0x241: "vehicle"}  # eth -> vehicle


def test_load_bench_config_rejects_unknown_node(tmp_path):
    import pytest

    p = tmp_path / "bad.toml"
    p.write_text('[nodes]\nreal = ["NOPE"]\n')
    with pytest.raises(ValueError, match="unknown node"):
        sim_registry.load_bench_config(p)


def test_load_bench_config_rejects_non_integer_id_key(tmp_path):
    import pytest

    p = tmp_path / "bad.toml"
    p.write_text('[bus]\nnothex = "party"\n')
    with pytest.raises(ValueError, match="valid arbitration ID"):
        sim_registry.load_bench_config(p)


# Orchestrator: [scenario] profiles applied via node.configure()


def test_configure_base_rejects_unknown_keys():
    import pytest

    rcm = sim_registry.BY_NAME["RCM"](_ctx())  # a plain liveness node, no settable state
    with pytest.raises(ValueError, match="no configurable scenario state"):
        rcm.configure(bogus=1)


def test_configure_applies_charge_scenario_across_nodes():
    reg = sim_registry
    cp = reg.BY_NAME["CP"](_ctx())
    cp.configure(evse_connected=True, evse_limit_a=32)
    assert cp.evse_connected and cp.evse_limit_a == 32

    ui = reg.BY_NAME["UI"](_ctx())
    ui.configure(charge_enable=True, charge_limit_a=32, pedal_map="sport")
    assert ui.charge_enable and ui.charge_limit_a == 32
    assert ui.uicfg.pedal_map == 1  # sport

    vc = reg.BY_NAME["VCFRONT"](_ctx())
    vc.configure(lv_power_state="accessory", hv_charge_enable=True)
    assert vc.hv_charge_enable and vc.lv.vps == 2  # accessory

    bms = reg.BY_NAME["BMS"](_ctx())
    bms.configure(mode="charge")
    assert bms.mode == "charge"

    hvp = reg.BY_NAME["HVP"](_ctx())
    hvp.configure(mode="charge", hv_voltage=400)
    assert hvp.control == "SUPPORT" and hvp.charge_hw and hvp.hv_voltage == 400

    sccm = reg.BY_NAME["SCCM"](_ctx())
    sccm.configure(gear="D")
    assert sccm.last_gear_cmd == "D"


def test_configure_rejects_unknown_key_on_stateful_node():
    import pytest

    cp = sim_registry.BY_NAME["CP"](_ctx())
    with pytest.raises(ValueError, match="no configurable scenario state"):
        cp.configure(evse_connected=True, nonsense=1)


def test_load_bench_config_parses_scenario(tmp_path):
    p = tmp_path / "charge.toml"
    p.write_text(
        '[nodes]\nreal = ["PCS"]\n'
        '[scenario.CP]\nevse_connected = true\nevse_limit_a = 32\n'
        '[scenario.hvp]\nmode = "charge"\n'
    )
    cfg = sim_registry.load_bench_config(p)
    assert cfg.scenario["CP"] == {"evse_connected": True, "evse_limit_a": 32}
    assert cfg.scenario["HVP"] == {"mode": "charge"}  # node name upper-cased


def test_load_bench_config_rejects_unknown_scenario_node(tmp_path):
    import pytest

    p = tmp_path / "bad.toml"
    p.write_text('[scenario.NOPE]\nfoo = 1\n')
    with pytest.raises(ValueError, match="unknown node in \\[scenario\\]"):
        sim_registry.load_bench_config(p)


# Node model: stateful behavior (on_rx transitions)


def test_epb_node_transitions_on_di_epb_request():
    epb = sim_registry.BY_NAME["EPB"](_ctx())
    sent = []
    park = (1 << 44).to_bytes(8, "little")  # DI_epbRequest=1 (PARK)
    unpark = (2 << 44).to_bytes(8, "little")  # DI_epbRequest=2 (UNPARK)
    _rx(epb, 0x118, park, lambda cid, d: sent.append(cid))
    assert epb.epb.status == 2  # EPB_PARKED
    _rx(epb, 0x118, unpark, lambda cid, d: sent.append(cid))
    assert epb.epb.status == 1  # EPB_RELEASED
    assert sent == []  # EPB reacts by state, never TX reactively


def test_hvp_node_idle_default_is_safe():
    """Default HVP state: SHUTDOWN + contactors OPEN (no HV/energize command)."""
    hvp = sim_registry.BY_NAME["HVP"](_ctx())
    by_id = {f.can_id: f for f in hvp.frames()}
    ctrl = by_id[0x22A].frame()
    # HVP_pcsControlRequest@16w2 == SHUTDOWN(0); charge/dcdc HW enables (@18/@19) == 0
    word = int.from_bytes(ctrl, "little")
    assert (word >> 16) & 0x3 == 0, "pcsControlRequest should default to SHUTDOWN"
    assert (word >> 18) & 0x1 == 0 and (word >> 19) & 0x1 == 0, "HW enables should be off"
    cont = by_id[0x20A].frame()
    cword = int.from_bytes(cont, "little")
    assert cword & 0x7 == 1, "packContNegativeState should default to OPEN(1)"
    assert (cword >> 36) & 0x1 == 0, "dcLinkAllowedToEnergize should default to 0"


def test_hvp_set_mode_drives_control_and_contactors():
    hvp = sim_registry.BY_NAME["HVP"](_ctx())
    hvp.set_mode("charge")
    by_id = {f.can_id: f for f in hvp.frames()}
    ctrl = int.from_bytes(by_id[0x22A].frame(), "little")
    assert (ctrl >> 16) & 0x3 == 1, "SUPPORT"
    assert (ctrl >> 18) & 0x1 == 1, "charge HW enabled in charge mode"
    assert (ctrl >> 19) & 0x1 == 0, "dcdc HW off in charge mode"
    cont = int.from_bytes(by_id[0x20A].frame(), "little")
    assert cont & 0x7 == 6 and (cont >> 8) & 0xF == 5, "contactors CLOSED (neg ECON / set CLOSED)"
    import pytest

    with pytest.raises(ValueError, match="HVP mode"):
        hvp.set_mode("bogus")


def test_cp_evse_connect_state():
    cp = sim_registry.BY_NAME["CP"](_ctx())
    by_id = {f.can_id: f for f in cp.frames()}
    # default: unplugged -> 0x21D all-zero (no evseAccept/proximity/pilot)
    assert int.from_bytes(by_id[0x21D].frame(), "little") == 0
    cp.set_evse(True, limit_a=32)
    word = int.from_bytes(by_id[0x21D].frame(), "little")
    assert word & 0x1 == 1, "CP_evseAccept set when plugged"
    assert (word >> 2) & 0x3 == 3, "CP_proximity = LATCHED"
    assert (word >> 24) & 0x7F == 32, "CP_cableCurrentLimit = 32 A"
    assert (word >> 8) & 0xFF == 64, "CP_pilotCurrent = 32/0.5 = 64"
    cp.set_evse(False)
    assert int.from_bytes(by_id[0x21D].frame(), "little") == 0, "unplug clears it"


def test_cp_charge_cable_state_reaches_the_dir_on_0x25d():
    # The 2022 DIR reads charge-port state ONLY from 0x25D bits 14-15:
    # 1 = NOT_CONNECTED, 2 = CONNECTED, 0/absent = "cable connected" (DI_a162). 0x210 is
    # the catalog id and is not in the DIR's RX table -- both must ship.
    cp = sim_registry.BY_NAME["CP"](_ctx())
    by_id = {f.can_id: f for f in cp.frames_for("2022.45.15")}
    assert 0x25D in by_id and 0x210 in by_id, "both CP_status copies ship at the 2022 target"

    payload = by_id[0x25D].frame()
    assert len(payload) == 8, "DIR length-gate wants DLC 8 exactly (else a094 canDataBusA)"
    assert payload[1] & 0xC0 == 0x40, "bits 14-15 = 1 = NOT_CONNECTED (byte1 0x40)"
    assert (int.from_bytes(payload, "little") >> 14) & 0x3 == 1
    assert (int.from_bytes(by_id[0x210].frame(), "little") >> 16) & 0x3 == 1

    cp.set_evse(True, limit_a=32)
    assert (int.from_bytes(by_id[0x25D].frame(), "little") >> 14) & 0x3 == 2, "CONNECTED"
    assert (int.from_bytes(by_id[0x210].frame(), "little") >> 16) & 0x3 == 2

    # 0/SNA on 0x25D bits 14-15 reads as "cable connected"; never emit it.
    for connected in (False, True):
        cp.set_evse(connected)
        assert by_id[0x25D].frame()[1] & 0xC0 != 0

    # 2020 target: DIR has no 0x25D subscription.
    assert 0x25D not in {f.can_id for f in cp.frames_for("2020.8.1")}


def test_ui_charge_request_state():
    ui = sim_registry.BY_NAME["UI"](_ctx())
    by_id = {f.can_id: f for f in ui.frames()}
    # default: no charge request -> 0x333 all-zero
    assert int.from_bytes(by_id[0x333].frame(), "little") == 0
    ui.set_charge(enable=True, limit_a=32)
    word = int.from_bytes(by_id[0x333].frame(), "little")
    assert (word >> 2) & 0x1 == 1, "UI_chargeEnableRequest set"
    assert (word >> 8) & 0x7F == 32, "UI_acChargeCurrentLimit = 32 A"
    assert (word >> 16) & 0x3FF == 800, "UI_chargeTerminationPct defaults to 80.0% (raw 800)"


def test_vcfront_charge_enable_layers_onto_drive_status():
    vc = sim_registry.BY_NAME["VCFRONT"](_ctx())
    by_id = {f.can_id: f for f in vc.frames()}
    base = by_id[0x3A1].frame()
    # default: charging OFF (bmsHvChargeEnable@0=0) but LV READY for drive (12vStatusForDrive@14w2=1)
    bword = int.from_bytes(base, "little")
    assert bword & 0x1 == 0 and (bword >> 14) & 0x3 == 1
    vc.set_charge_enable(True)
    word = int.from_bytes(by_id[0x3A1].frame(), "little")
    assert word & 0x1 == 1, "bmsHvChargeEnable set"
    assert (word >> 14) & 0x3 == 1, "12vStatusForDrive still READY"
    # drive-side signals still present (diPowerOnState @10 w3 == 3)
    assert (word >> 10) & 0x7 == 3


def _esp_status_word(esp):
    by_id = {f.can_id: f for f in esp.frames()}
    return int.from_bytes(by_id[0x145].frame(), "little")


def test_esp_status_defaults_are_a_healthy_stationary_bench():
    """0x145 defaults: stability ON, no ABS event, no standstill skid, all four QF bits IN_SPEC."""
    esp = sim_registry.BY_NAME["ESP"](_ctx())
    w = _esp_status_word(esp)
    assert (w >> 14) & 0x3 == 1, "stabilityControlSts2 = ON (bench-confirmed drive gate)"
    assert (w >> 22) & 0x3 == 0, "absBrakeEvent2 = NOT_ACTIVE"
    assert (w >> 48) & 0x1 == 0, "ebrStandstillSkid = NO_STANDSTILL_SKID"
    for bit in (24, 25, 26, 27):
        assert (w >> bit) & 0x1 == 1, f"QF bit {bit} = IN_SPEC"
    assert (w >> 29) & 0x3 == 1, "driverBrakeApply = Not_Applied"
    assert (w >> 31) & 0x1 == 0, "brakeApply inactive"


def test_esp_status_brake_posture_and_flags_are_configurable():
    esp = sim_registry.BY_NAME["ESP"](_ctx())
    esp.configure(brake="applied", abs_event="front_rear", stability="faulted",
                  standstill_skid=True, qf_in_spec=False, esp_fault_lamp=True)
    w = _esp_status_word(esp)
    assert (w >> 29) & 0x3 == 2 and (w >> 31) & 0x1 == 1 and (w >> 21) & 0x1 == 1
    assert (w >> 22) & 0x3 == 1, "absBrakeEvent2 = ACTIVE_FRONT_REAR"
    assert (w >> 14) & 0x3 == 3, "stabilityControlSts2 = FAULTED"
    assert (w >> 48) & 0x1 == 1 and (w >> 18) & 0x1 == 1
    assert all((w >> b) & 0x1 == 0 for b in (24, 25, 26, 27)), "QF bits cleared together"


def test_ibst_brake_posture_drives_both_0x39d_copies():
    """IBST_driverBrakeApply must track ESP's — and the 2022 vehicle-bus copy shares the state."""
    ibst = sim_registry.BY_NAME["IBST"](_ctx())
    ibst.fw = "2022.45.15"
    ibst.configure(brake="applied")
    copies = [f for f in ibst.frames_for() if f.can_id == 0x39D]
    assert len(copies) == 2, "party + vehicle copies on 2022 fw"
    for f in copies:
        w = int.from_bytes(f.frame(), "little")
        assert (w >> 16) & 0x3 == 2, "IBST_driverBrakeApply = DRIVER_APPLYING_BRAKES"
        assert (w >> 18) & 0x7 == 2, "IBST_internalState = LOCAL_BRAKE_REQUEST"
        assert (w >> 21) & 0xFFF > 320, "rod travel past the released rest position"


def test_ibst_brake_released_is_the_default():
    ibst = sim_registry.BY_NAME["IBST"](_ctx())
    w = int.from_bytes({f.can_id: f for f in ibst.frames()}[0x39D].frame(), "little")
    assert (w >> 16) & 0x3 == 1, "BRAKES_NOT_APPLIED"
    assert (w >> 12) & 0x7 == 4, "iBoosterStatus = ACTIVE_GOOD_CHECK"


def test_esp_status_rejects_bad_scenario_values():
    import pytest

    esp = sim_registry.BY_NAME["ESP"](_ctx())
    for kwargs in ({"brake": "bogus"}, {"abs_event": "bogus"}, {"stability": "bogus"}):
        with pytest.raises(ValueError):
            esp.configure(**kwargs)
    with pytest.raises(ValueError):
        esp.configure(not_a_key=1)


def test_vcfront_12v_status_for_drive_is_independent_of_charging():
    """The DI/DIR a174 LV drive gate must not depend on HV charge-enable."""
    vc = sim_registry.BY_NAME["VCFRONT"](_ctx())
    by_id = {f.can_id: f for f in vc.frames()}
    vc.set_lv_ready_for_drive(False)
    word = int.from_bytes(by_id[0x3A1].frame(), "little")
    assert (word >> 14) & 0x3 == 0, "NOT_READY_FOR_DRIVE_12V"
    vc.configure(lv_ready_for_drive=True)
    word = int.from_bytes(by_id[0x3A1].frame(), "little")
    assert (word >> 14) & 0x3 == 1, "READY_FOR_DRIVE_12V"


def test_vcleft_owns_0x3c2_and_vcfront_does_not():
    """0x3C2 was split out of VCFRONT into its own VCLEFT node (distinct DBC sender)."""
    vcl = sim_registry.BY_NAME["VCLEFT"](_ctx())
    assert {f.can_id for f in vcl.frames()} == {0x3C2}
    assert 0x3C2 not in {f.can_id for f in sim_registry.BY_NAME["VCFRONT"](_ctx()).frames()}


def test_vcleft_switch_status_tracks_the_brake_switch():
    vcl = sim_registry.BY_NAME["VCLEFT"](_ctx())
    f = {x.can_id: x for x in vcl.frames()}[0x3C2]
    w = int.from_bytes(f.frame(), "little")
    assert (w >> 4) & 1 == 0 and (w >> 60) & 1 == 0, "default released"
    vcl.set_brake_switch(True)
    w = int.from_bytes(f.frame(), "little")
    assert (w >> 4) & 1 == 1 and (w >> 60) & 1 == 1, "VCLEFT_brakeSwitchPressed asserted"
    vcl.configure(brake_switch_pressed=False)
    assert not vcl.brake_switch_pressed


def test_brake_pressure_is_stored_and_validated():
    esp = sim_registry.BY_NAME["ESP"](_ctx())
    ibst = sim_registry.BY_NAME["IBST"](_ctx())
    esp.set_brake("applied", pressure=42)
    ibst.set_brake("applied", pressure=42)
    assert esp.brake_pressure == 42.0 and ibst.brake_pressure == 42.0
    esp.set_brake("released")  # pressure omitted -> cleared
    assert esp.brake_pressure is None
    import pytest

    for bad in (150, -1, "nope"):
        with pytest.raises(ValueError):
            esp.set_brake("applied", pressure=bad)


def test_esp_party3_emits_master_cyl_pressure():
    """0x38D carries the DI brake-vote VoteB: MC pressure (measured + virtual), QF=NORMAL,
    with a valid J1850 CRC + rolling counter."""
    from tesla_frames import j1850_crc8

    esp = sim_registry.BY_NAME["ESP"](_ctx())
    f = {x.can_id: x for x in esp.frames()}[0x38D]
    b = f.frame()
    v = int.from_bytes(b, "little")
    assert len(b) == 7 and b[0] == j1850_crc8(bytes(b[1:])), "DLC 7 + valid CRC"
    assert round(((v >> 44) & 0x3FF) * 0.3 - 30.0) == 0, "released -> 0 bar measured"
    assert (v >> 54) & 0x3 == 1 and (v >> 26) & 0x3 == 1, "both QF = NORMAL"
    esp.set_brake("applied", pressure=80)
    v = int.from_bytes(f.frame(), "little")
    assert round(((v >> 44) & 0x3FF) * 0.3 - 30.0) == 80, "80% -> ~80 bar measured"
    assert ((v >> 16) & 0x3FF) * 0.25 == 80.0, "80% -> 80 bar virtual"


def test_control_facade_brake_fans_out_to_esp_ibst_vcleft():
    """One dash brake toggle drives ESP 0x145 + IBST 0x39D posture and the VCLEFT 0x3C2 switch."""
    import vehicle_sim

    by = {n: sim_registry.BY_NAME[n](_ctx()) for n in ("ESP", "IBST", "VCLEFT")}
    fac = vehicle_sim._ControlFacade(by)
    out = fac.brake(True, pressure=42)
    assert out["pressed"] and sorted(out["nodes"]) == ["ESP", "IBST", "VCLEFT"]
    w145 = int.from_bytes({f.can_id: f for f in by["ESP"].frames()}[0x145].frame(), "little")
    w39d = int.from_bytes({f.can_id: f for f in by["IBST"].frames()}[0x39D].frame(), "little")
    w3c2 = int.from_bytes({f.can_id: f for f in by["VCLEFT"].frames()}[0x3C2].frame(), "little")
    assert (w145 >> 29) & 0x3 == 2, "ESP driverBrakeApply = applied"
    assert (w39d >> 16) & 0x3 == 2, "IBST driverBrakeApply = applied"
    assert (w3c2 >> 4) & 1 == 1, "VCLEFT brake switch asserted"
    assert fac.state()["brake_pressed"] is True and fac.state()["brake_pressure"] == 42.0
    fac.brake(False)
    w3c2 = int.from_bytes({f.can_id: f for f in by["VCLEFT"].frames()}[0x3C2].frame(), "little")
    assert (w3c2 >> 4) & 1 == 0 and fac.state()["brake_pressed"] is False


def _di_1d6(di):
    return {f.can_id: f for f in di.frames()}[0x1D6].frame()


def test_di_broadcasts_wired_brake_switch_on_0x1d6():
    di = sim_registry.BY_NAME["DI"](_ctx())
    assert (int.from_bytes(_di_1d6(di), "little") >> 33) & 1 == 0, "released default"
    di.set_brake_switch(True)
    assert (int.from_bytes(_di_1d6(di), "little") >> 33) & 1 == 1, "0x1D6 bit33 = pressed"


def test_vcleft_mirrors_di_wired_switch_when_shared_and_ignores_when_vc_only():
    di = sim_registry.BY_NAME["DI"](_ctx())
    vcl = sim_registry.BY_NAME["VCLEFT"](_ctx())
    di.set_brake_switch(True)

    def vc_bit():
        return (int.from_bytes({f.can_id: f for f in vcl.frames()}[0x3C2].frame(), "little") >> 4) & 1

    vcl.set_brake_line_switch_type("di_vc_shared")
    _rx(vcl, 0x1D6, _di_1d6(di))
    assert vc_bit() == 1, "DI_VC_SHARED: VCLEFT relays the DI wired switch onto 0x3C2"
    vcl.set_brake_line_switch_type("vc_only")  # the bench default
    vcl.set_brake_switch(False)
    _rx(vcl, 0x1D6, _di_1d6(di))
    assert vc_bit() == 0, "VC_ONLY: VCLEFT sources its own switch, ignores the DI report"


def test_vcleft_reads_brake_line_switch_type_from_carconfig_mux3():
    vcl = sim_registry.BY_NAME["VCLEFT"](_ctx())
    assert vcl.brake_line_switch_type == "vc_only", "bench default VC_ONLY"
    cc = bytearray(8)
    cc[0], cc[4] = 3, 0  # mux3, GTW_brakeLineSwitchType @39|1 = DI_VC_SHARED(0)
    _rx(vcl, 0x7FF, bytes(cc))
    assert vcl.brake_line_switch_type == "di_vc_shared"
    cc[4] = 1 << 7  # VC_ONLY(1)
    _rx(vcl, 0x7FF, bytes(cc))
    assert vcl.brake_line_switch_type == "vc_only"


def test_esp_and_ibst_mirror_the_di_wired_brake_switch():
    # DI_VC_SHARED: ESP/IBST follow the DI's 0x1D6 wired switch (the bench default is VC_ONLY).
    di = sim_registry.BY_NAME["DI"](_ctx())
    esp = sim_registry.BY_NAME["ESP"](_ctx())
    ibst = sim_registry.BY_NAME["IBST"](_ctx())
    esp.configure(brake_line_switch_type="di_vc_shared")
    ibst.configure(brake_line_switch_type="di_vc_shared")
    di.set_brake_switch(True)
    _rx(esp, 0x1D6, _di_1d6(di))
    _rx(ibst, 0x1D6, _di_1d6(di))
    assert esp.brake == "applied" and ibst.brake == "applied"
    di.set_brake_switch(False)
    _rx(esp, 0x1D6, _di_1d6(di))
    _rx(ibst, 0x1D6, _di_1d6(di))
    assert esp.brake == "released" and ibst.brake == "released"


def test_esp_and_ibst_ignore_the_di_switch_in_vc_only():
    """VC_ONLY (learned from GTW_carConfig mux3): ESP/IBST ignore 0x1D6 so UI/manual control wins."""
    di = sim_registry.BY_NAME["DI"](_ctx())
    esp = sim_registry.BY_NAME["ESP"](_ctx())
    ibst = sim_registry.BY_NAME["IBST"](_ctx())
    di.set_brake_switch(True)
    cc = bytearray(8)
    cc[0], cc[4] = 3, 1 << 7  # mux3, GTW_brakeLineSwitchType = VC_ONLY(1)
    for n in (esp, ibst):
        _rx(n, 0x7FF, bytes(cc))
        assert n.brake_line_switch_type == "vc_only"
        n.set_brake("applied")          # UI/manual request
        _rx(n, 0x1D6, _di_1d6(di))       # DI switch pressed -> must be ignored
        assert n.brake == "applied", "VC_ONLY: 0x1D6 does not override manual/UI control"


def test_control_facade_brake_drives_the_virtual_di_switch():
    import vehicle_sim

    by = {n: sim_registry.BY_NAME[n](_ctx()) for n in ("DI", "ESP", "IBST", "VCLEFT")}
    out = vehicle_sim._ControlFacade(by).brake(True)
    assert "DI" in out["nodes"] and by["DI"].brake_switch_pressed is True
    assert (int.from_bytes(_di_1d6(by["DI"]), "little") >> 33) & 1 == 1


# The 16 GTW_carConfig signals the DIR reads, as {scenario key: DBC signal}. Golden: the
# scenario keys are the node's public surface, and the signal names are what the loaded CAN
# database is asked to encode.
GTW_CARCONFIG_KEYS = {
    "country": "GTW_country",
    "brake_hw_type": "GTW_brakeHWType",
    "drivetrain_type": "GTW_drivetrainType",
    "tpms_type": "GTW_tpmsType",
    "vdc_type": "GTW_vdcType",
    "cabin_ptc_heater_type": "GTW_cabinPTCHeaterType",
    "spoiler_type": "GTW_spoilerType",
    "autopilot": "GTW_autopilot",
    "number_hvil_nodes": "GTW_numberHVILNodes",
    "performance_package": "GTW_performancePackage",
    "chassis_type": "GTW_chassisType",
    "pack_energy": "GTW_packEnergy",
    "pack_performance_deviation": "GTW_packPerformanceDeviation",
    "di_burn_in_type": "GTW_diBurnInType",
    "compressor_type": "GTW_compressorType",
    "brake_line_switch_type": "GTW_brakeLineSwitchType",  # 2022+; absent in 2020
}
# Only these two are non-zero by default -- the frame must stay what it was before the node
# had state (GTW_chassisType = 3_CHASSIS, everything else 0).
GTW_CARCONFIG_DEFAULTS = {"chassis_type": 2, "brake_line_switch_type": 1}  # VC_ONLY bench default


def _carcfg_db(omit=()):
    """A GTW_carConfig database shaped like CanDatabase.from_dbc's output, so the node runs
    through the real encoder. ``omit`` drops signals, standing in for an older revision that
    doesn't carry them (2020 has no compressorType/diBurnInType/packPerformanceDeviation/
    cabinPTCHeaterType/brakeLineSwitchType)."""
    from can_decoder import CanDatabase

    def sig(start, width, mux_id, is_muxer=False, vd=None):
        return {
            "start_position": start, "width": width, "mux_id": mux_id, "is_muxer": is_muxer,
            "value_description": vd, "endianness": "LITTLE", "signedness": "UNSIGNED",
            "scale": 1, "offset": 0,
        }

    labels = {
        "GTW_chassisType": {"3_CHASSIS": 2, "Y_CHASSIS": 3},
        "GTW_packEnergy": {"SR": 0, "LR": 1, "MR": 2},
        "GTW_drivetrainType": {"RWD": 0, "AWD": 1},
    }
    signals = {"GTW_carConfigMultiplexer": sig(0, 8, None, is_muxer=True)}
    for i, (key, name) in enumerate(GTW_CARCONFIG_KEYS.items()):
        if key in omit:
            continue
        # Spread across 3 mux pages; distinct byte per page so nothing overlaps.
        signals[name] = sig(8 + (i // 3) * 8, 5, 1 + i % 3, vd=labels.get(name))

    db = CanDatabase.__new__(CanDatabase)
    db.messages = {0x7FF: {"message_id": 0x7FF, "name": "GTW_carConfig",
                           "length_bytes": 8, "signals": signals}}
    db._by_node = {}
    db._cantools_db = None
    return db


def _gtw(omit=()):
    return sim_registry.BY_NAME["GTW"](sim_core.NodeContext(db=_carcfg_db(omit)))


def test_gtw_carconfig_stages_every_signal_the_database_carries():
    gtw = _gtw()
    assert set(gtw.config) == set(GTW_CARCONFIG_KEYS)
    assert gtw.unsupported == []
    for key in GTW_CARCONFIG_KEYS:
        assert gtw.config[key] == GTW_CARCONFIG_DEFAULTS.get(key, 0), key


def test_gtw_carconfig_defaults_leave_the_frame_byte_identical():
    """The node's idle frame matches an explicit MuxedConfigTx with the same defaults
    (chassisType 3_CHASSIS + brakeLineSwitchType VC_ONLY; everything else 0)."""
    from tesla_frames import GTW_CARCONFIG_ID, MuxedConfigTx

    db = _carcfg_db()
    old = MuxedConfigTx(db, GTW_CARCONFIG_ID,
                        defaults={"GTW_chassisType": 2, "GTW_drivetrainType": 0,
                                  "GTW_brakeLineSwitchType": 1})
    new = sim_registry.BY_NAME["GTW"](sim_core.NodeContext(db=db)).carcfg
    for _ in range(len(old.pages) * 2):  # two full mux cycles
        assert bytes(old.next_frame()) == bytes(new.next_frame())


def test_gtw_carconfig_takes_enum_labels_not_just_numbers():
    """A raw number means different things across revisions (performancePackage 4 is
    BASE_PLUS_AWD in 2020, BASE_2022 after), so labels are the portable spelling."""
    import pytest

    gtw = _gtw()
    assert gtw.set_config("pack_energy", "LR") == 1
    assert gtw.set_config("chassis_type", "Y_CHASSIS") == 3
    assert gtw.config["pack_energy"] == 1
    assert gtw.set_config("pack_energy", 2) == 2  # raw still accepted
    with pytest.raises(ValueError):
        gtw.set_config("pack_energy", "74_KWH")  # the 2020 spelling, absent here


def test_gtw_carconfig_skips_signals_the_revision_lacks():
    """2020 has no compressorType / brakeLineSwitchType; the node reports them rather than
    failing the bench."""
    import pytest

    gtw = _gtw(omit={"compressor_type", "di_burn_in_type", "brake_line_switch_type"})
    assert set(gtw.unsupported) == {"compressor_type", "di_burn_in_type", "brake_line_switch_type"}
    assert "compressor_type" not in gtw.config
    with pytest.warns(UserWarning, match="not in the loaded CAN database"):
        assert gtw.set_config("compressor_type", 3) is None


def test_gtw_carconfig_rejects_an_unknown_parameter():
    import pytest

    gtw = _gtw()
    with pytest.raises(ValueError, match="unknown car-config parameter"):
        gtw.set_config("nonsense", 1)
    with pytest.raises(ValueError):
        gtw.configure(nonsense=1)


def test_gtw_carconfig_configure_applies_scenario_keys():
    gtw = _gtw()
    gtw.configure(chassis_type="Y_CHASSIS", pack_energy="MR", autopilot=3)
    assert gtw.config["chassis_type"] == 3
    assert gtw.config["pack_energy"] == 2
    assert gtw.config["autopilot"] == 3


def test_gtw_2022_variant_sends_a_real_clock():
    # 2022 DIR brake-temp estimator needs 0x528 now > the time saved at power-off; 0 -> DI_a228.
    import time

    gtw = _gtw()
    t20 = {f.can_id: f for f in gtw.frames_for("2020.8.1")}[0x528].frame()
    assert bytes(t20) == bytes(4)  # 2020 baseline unchanged
    t22 = {f.can_id: f for f in gtw.frames_for("2022.45.15")}[0x528].frame()
    assert len(t22) == 4 and abs(int.from_bytes(t22, "big") - time.time()) < 5


def test_bms_status_mode_drive_vs_charge():
    bms = sim_registry.BY_NAME["BMS"](_ctx())
    by_id = {f.can_id: f for f in bms.frames()}
    drive = int.from_bytes(by_id[0x212].frame(), "little")
    assert (drive >> 32) & 0xF == 1 and (drive >> 16) & 0x7 == 3  # BMS_DRIVE / HV_UP_FOR_DRIVE
    bms.set_mode("charge")
    chg = int.from_bytes(by_id[0x212].frame(), "little")
    assert (chg >> 32) & 0xF == 3, "BMS_state = BMS_CHARGE"
    assert (chg >> 16) & 0x7 == 4, "BMS_hvState = HV_UP_FOR_CHARGE"
    assert (chg >> 29) & 0x1 == 1, "BMS_chargeRequest = 1"
    import pytest

    with pytest.raises(ValueError, match="BMS mode"):
        bms.set_mode("bogus")


def test_bms_2022_variant_uses_2022_dbc_scaling():
    # 2020 LSBs on a 2022 DI read minBusVoltage 300 V as 600 V > the 373 V pack -> DI_a125.
    import pytest

    bms = sim_registry.BY_NAME["BMS"](_ctx())

    def fields(fw, cid, *spans):
        v = int.from_bytes({f.can_id: f for f in bms.frames_for(fw)}[cid].frame(), "little")
        return [(v >> s) & ((1 << w) - 1) for s, w in spans]

    # 2020 baseline stays byte-identical
    by20 = {f.can_id: bytes(f.frame()) for f in bms.frames_for("2020.8.1")}
    assert by20[0x2D2].hex() == "3075409c0000420f"
    assert by20[0x252].hex() == "7017983a00000100"
    assert by20[0x132].hex() == "b491000010270000"
    # 2022.45.15 DBC scaling -> same engineering values
    vmin, vmax, idis = fields("2022.45.15", 0x2D2, (0, 16), (16, 16), (48, 14))
    assert (vmin * 0.02, vmax * 0.02) == pytest.approx((300.0, 400.0))
    assert idis * 0.15 == pytest.approx(500.0, abs=0.15)
    (pdis,) = fields("2022.45.15", 0x252, (16, 16))
    assert pdis * 0.013 == pytest.approx(150.0, abs=0.013)
    (iunf,) = fields("2022.45.15", 0x132, (32, 16))
    assert iunf * 0.05 - 822.0 == pytest.approx(0.0)


def test_vcsec_node_answers_immo_only_with_a_key():
    vcsec = sim_registry.BY_NAME["VCSEC"](_ctx())
    sent = []
    challenge = bytes([0, 1, 0, 2, 0, 3, 0, 0])
    _rx(vcsec, 0x276, challenge, lambda cid, d: sent.append((cid, d)))
    assert sent == []  # no key -> silent
    # The response half needs a user-supplied key-derivation provider; skip if none.
    import pytest

    from uds_local.security_provider import get_key_derivation_provider

    if get_key_derivation_provider() is None:
        pytest.skip("no key-derivation provider configured")
    vcsec.immo_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    _rx(vcsec, 0x276, challenge, lambda cid, d: sent.append((cid, d)))
    assert len(sent) == 1 and sent[0][0] == 0x3D9  # answered on 0x3D9


# Reactive inter-node comms (the charge-session cascade)


def test_rx_handler_registration_builds_the_dispatch_table():
    reg = sim_registry
    # Each reactive node declares exactly the IDs it handles; fixed-liveness nodes register none.
    assert set(reg.BY_NAME["VCFRONT"](_ctx()).rx_handlers()) == {0x333, 0x21D}
    assert set(reg.BY_NAME["BMS"](_ctx()).rx_handlers()) == {0x3A1}
    assert set(reg.BY_NAME["HVP"](_ctx()).rx_handlers()) == {0x3A1}
    assert set(reg.BY_NAME["EPB"](_ctx()).rx_handlers()) == {0x118}
    assert set(reg.BY_NAME["VCSEC"](_ctx()).rx_handlers()) == {0x276}
    assert reg.BY_NAME["RCM"](_ctx()).rx_handlers() == {}
    # The engine's global {id: [handlers]} table: 0x3A1 fans to BOTH BMS and HVP.
    table: dict[int, list] = {}
    for n in reg.instantiate(reg.NODES, _ctx()):
        for cid, cb in n.rx_handlers().items():
            table.setdefault(cid, []).append(cb)
    assert len(table[0x3A1]) == 2 and len(table[0x276]) == 1


def test_vcfront_reacts_to_ui_charge_request_and_cp_evse():
    vc = sim_registry.BY_NAME["VCFRONT"](_ctx())
    _rx(vc, 0x333, (1 << 2).to_bytes(4, "little"))  # UI_chargeEnableRequest
    assert not vc.hv_charge_enable  # needs an EVSE too
    _rx(vc, 0x21D, (1).to_bytes(8, "little"))  # CP_evseAccept
    assert vc.hv_charge_enable  # both present -> authorize HV charge
    _rx(vc, 0x21D, (0).to_bytes(8, "little"))  # unplug
    assert not vc.hv_charge_enable


def test_bms_and_hvp_react_to_vcfront_charge_enable():
    bms = sim_registry.BY_NAME["BMS"](_ctx())
    hvp = sim_registry.BY_NAME["HVP"](_ctx())
    on = (1).to_bytes(8, "little")   # VCFRONT bmsHvChargeEnable @0 = 1
    off = (0).to_bytes(8, "little")
    _rx(bms, 0x3A1, on)
    _rx(hvp, 0x3A1, on)
    assert bms.mode == "charge"
    assert hvp.control == "SUPPORT" and hvp.charge_hw and hvp.contactor_stage == "closed"
    _rx(bms, 0x3A1, off)
    _rx(hvp, 0x3A1, off)
    assert bms.mode == "drive" and hvp.control == "SHUTDOWN"


def test_charge_session_cascades_from_externalities():
    reg = sim_registry
    nodes = [reg.BY_NAME[n](_ctx()) for n in ("CP", "UI", "VCFRONT", "BMS", "HVP")]
    by = {n.name: n for n in nodes}
    # Only the EXTERNALITIES are set: plug in an EVSE + ask to charge.
    by["CP"].set_evse(True, 32)
    by["UI"].set_charge(enable=True, limit_a=32)
    # Simulate the engine's dispatch: each node broadcasts, delivered to the matching handlers.
    for _ in range(4):
        for src in nodes:
            for f in src.frames():
                data = f.frame()
                for dst in nodes:
                    _rx(dst, f.can_id, data)
    # The charge state EMERGED -- nothing set VCFRONT/BMS/HVP charge directly.
    assert by["VCFRONT"].hv_charge_enable
    assert by["BMS"].mode == "charge"
    assert by["HVP"].control == "SUPPORT" and by["HVP"].charge_hw


# Drive-inverter nodes + drive scenario


def test_inverter_nodes_source_expected_ids():
    reg = sim_registry
    di = {f.can_id for f in reg.collect_frames([reg.BY_NAME["DI"](_ctx())])}
    dir_ = {f.can_id for f in reg.collect_frames([reg.BY_NAME["DIR"](_ctx())])}
    pmr = {f.can_id for f in reg.collect_frames([reg.BY_NAME["PMR"](_ctx())])}
    assert 0x118 in di    # DI_systemStatus (the vehicle-level aggregate)
    assert 0x108 in dir_  # DIR_torque (the rear physical inverter)
    assert pmr == {0x385, 0x6D4}  # PMR_alertMatrix1 + PMR_info


def test_drive_config_marks_inverter_real_and_excludes_it():
    reg = sim_registry
    dut_ids = _ids([reg.BY_NAME[n] for n in ("DI", "DIR", "PMR")])
    assert 0x118 in dut_ids and 0x108 in dut_ids
    # inverter marked real (drive bench) -> none of its ids are simulated
    assert not (dut_ids & {f.can_id for f in _sim_frames()})


def test_load_drive_scenario_marks_inverter_real():
    from pathlib import Path

    p = Path(sim_registry.__file__).resolve().parents[1] / "scenarios" / "drive.toml"
    cfg = sim_registry.load_bench_config(p)
    assert set(cfg.real) == {"DI", "DIR", "PMR"}
    # No front drive unit on a RWD car -- absent, not real, and that means both of its cores.
    assert set(cfg.absent) == {"DIF", "PMF"}
    assert cfg.scenario["VCFRONT"] == {"lv_power_state": "drive"}
    assert cfg.scenario["GTW"] == {"drivetrain_type": "RWD", "chassis_type": "3_CHASSIS"}
