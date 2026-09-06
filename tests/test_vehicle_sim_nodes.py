"""Golden regression test for the node-centric bench (stateful Node model).

Locks the node registry to the exact frame inventory the pre-refactor flat ``frames``
list produced (captured from the original code): per-bus membership + each frame's
period / rolling-counter position / checksum position / counter width / DLC. If a node
edit changes what goes on the wire (drive scenario), this fails loudly.

CI-safe: instantiates the nodes with a stub NodeContext (a minimal DB just for the GTW
car-config), so nothing here needs the real compact DB or a CAN bus.
"""
from __future__ import annotations

import sim_core
import sim_registry

# (can_id, bus) -> (period_s, counter_start, cksum_start, counter_width, dlc)
# Keyed on the bus too, not the id alone: one id can legitimately be sent on BOTH
# buses (IBST_status 0x39D, below), which a can_id-keyed golden can't express.
# The MIA-owning party frames (rcm/esp/ibst/epas3p members) transmit at
# sim_core.PARTY_LIVENESS_S (the group2 CANB MIA-clear floor), a fixed per-frame constant
# now -- NOT the old global --party-period flatten (removed). Truly non-waited-on party frames
# (das 0x389/0x2B9) keep their native cycle; unknown 0x11D is at PARTY_LIVENESS_S because the DIR
# VDC freshness watchdog waits on it.
GOLDEN: dict[tuple[int, str], tuple] = {
    # ---- vehicle bus (group1 / CANA) ----
    (0x132, "vehicle"): (0.010, None, None, 4, 8),
    (0x212, "vehicle"): (0.100, None, None, 4, 8),
    (0x252, "vehicle"): (0.100, None, None, 4, 8),
    (0x2D2, "vehicle"): (0.100, None, None, 4, 8),
    (0x312, "vehicle"): (1.000, None, None, 4, 8),
    # CP_status ships on BOTH 0x210 (catalog id) and 0x25D (what the 2022 DIR actually
    # subscribes to -- the DIR gates on id 0x25D/dlc 8; 0x210 is not in the DIR's RX
    # table at all). Do not collapse these to one id -- see scripts/cp/cp.py.
    (0x210, "vehicle"): (0.100, None, None, 4, 8),
    (0x21D, "vehicle"): (0.100, None, None, 4, 8),  # CP_evseStatus (EVSE-connect report)
    (0x224, "vehicle"): (0.100, None, None, 4, 8),
    # 20Hz, not 10Hz: the 2022 CANData cycle_time is 50ms and under-sending aged out the
    # DIR freshness supervisor -> a155 vcfrontMIA (same for 0x3C2 below).
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
    # UI_vehicleModes must be DLC8: the DIR length-gate (run for every
    # bus-A RX id) raised a094 canDataBusA on every DLC-5 frame.
    (0x284, "vehicle"): (0.100, None, None, 4, 8),
    (0x293, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x313, "vehicle"): (0.100, 52, 56, 4, 8),
    (0x334, "vehicle"): (0.100, None, None, 4, 8),
    (0x333, "vehicle"): (0.500, None, None, 4, 4),  # UI_chargeRequest (user charge input)
    # 2022.45.15-only vehicle-bus members (fw_variants; default target = newest -> present here).
    # Firmware-confirmed absent in the 2020 DIR; gated so --fw 2020.8.1 omits them.
    (0x452, "vehicle"): (0.100, None, None, 4, 3),  # bms limits (torque-clamp input + bmsMIA)
    (0x2A7, "vehicle"): (0.100, None, None, 4, 8),  # cmp variant (config-selected alt of 0x247)
    (0x25C, "vehicle"): (0.100, None, None, 4, 1),  # app liveness (appMIA a108)
    # cp charge-cable state (cpMIA a105 + DI_a162_chargeCableConnected); DIR-only id.
    (0x25D, "vehicle"): (0.100, None, None, 4, 8),
    (0x3B3, "vehicle"): (0.100, None, None, 4, 8),  # UI_vehicleControl2 (uiMIA a088 member, drive mode)
    # IBST_status_A: the SAME id as the party 0x39D below, deliberately sent on bus A too.
    # The 2022 DIR validates 0x39D on CANA (cksumCtr @dir 0xab7fb -> a110_brakeMIA); 2020
    # wanted it on party only. Both copies ship at the 2022 target.
    (0x39D, "vehicle"): (0.010, 8, 0, 4, 5),        # ibst (2022.45.15 variant)
    # 0x392 is NOT here as an add: it stays in the inventory but reassigns EPAS3P_alertMatrix (2020,
    # epas3p) -> BMS_packConfig (2022, bms); newest target sources it from bms (see GOLDEN 0x392 above).
    # ---- UNKNOWN holding pen: undocumented PCS-context frames (awaiting PCS RE) ----
    (0x13D, "vehicle"): (0.010, None, None, 4, 6),
    (0x2B2, "vehicle"): (0.100, None, None, 4, 5),
    # ---- HVP (High Voltage Processor): commands the PCS + owns the contactors ----
    # Both plain (no counter/checksum in 2020 fw), 10ms, on the eth/vehicle bus.
    (0x22A, "vehicle"): (0.010, None, None, 4, 4),
    (0x20A, "vehicle"): (0.010, None, None, 4, 6),
    # ---- party bus (group2 / CANB) ----
    # MIA members -> PARTY_LIVENESS_S (0.010 = 100Hz, the confirmed floor); 83Hz left them in
    # steady-state MIA. das keeps native cycle; 0x11D is also at PARTY_LIVENESS_S (VDC freshness).
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
    (0x11D, "party"): (0.010, 8, 0, 4, 8),        # esp (re-homed from UNKNOWN); PARTY_LIVENESS_S -- DIR VDC freshness (a195/6/7,a210)
}


class _StubDb:
    """Minimal CAN DB for the GTW node's MuxedConfigTx (0x7FF) -- no real DB needed."""

    messages = {
        0x7FF: {
            "name": "GTW_carConfig",
            "signals": {
                "GTW_muxer": {"is_muxer": True},
                "GTW_chassisType": {"mux_id": 0, "width": 4},
                "GTW_drivetrainType": {"mux_id": 0, "width": 4},
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


# The drive bench marks the connected rear inverter real, so the SIMULATED inventory is the
# peers (the inverter nodes excluded). The GOLDEN dict below is that simulated peer set.
_INVERTER = ["DI", "DIR", "PMR"]


def _sim_frames():
    return _frames(sim_registry.select_nodes(real=_INVERTER))


def _no_send(_cid, _data):
    pass


def _rx(node, can_id, data, send=_no_send):
    """Deliver a frame to a node's registered rx handler -- the test-side of the engine's
    dispatch. A node maps id -> one callback (the engine aggregates across nodes to a list)."""
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
    # A frame dropped locally (send failed) must NOT consume a counter value, else the DIR sees a
    # jump and a validated-frame MIA won't reset until the sequence resyncs. note_send(False)
    # rolls the counter back so the dropped value is REUSED by the next (successful) frame ->
    # the on-wire sequence stays gapless (0,1,2 not 0,_,2).
    f = sim_core.SimFrame(
        "t", 0x101, 0.0125, lambda: bytearray(8), counter_start=52, cksum_start=56,
    )
    ctr = lambda b: (b[6] >> 4) & 0xF  # 4-bit counter @ bit52 = byte6 hi-nibble  # noqa: E731
    b0 = f.frame()          # counter 0 placed; _ctr -> 1
    f.note_send(True)       # delivered: keep it
    b1 = f.frame()          # counter 1 placed; _ctr -> 2  (this send will "fail")
    f.note_send(False)      # dropped: roll _ctr 2 -> 1
    b2 = f.frame()          # counter 1 again -- reuses the dropped value; _ctr -> 2
    assert (ctr(b0), ctr(b1), ctr(b2)) == (0, 1, 1)
    # a plain (counter-less) frame's note_send is a harmless no-op
    p = sim_core.SimFrame("p", 0x38E, 0.0125, lambda: bytearray(6))
    p.note_send(False)  # does not raise


def test_builder_managed_counter_also_rolls_back_on_drop():
    # J1850/E2E frames carry the counter INSIDE the builder (it's under a CRC), so counter_start
    # is None -- note_send must delegate the rollback to the builder via its bound-method __self__,
    # else those frames (IBST 0x38E, ESP 0x38D) keep desyncing on every drop while the additive
    # frames stay gapless. Covers all four stateful builders.
    from tesla_frames import J1850Frame, LvPowerState, SccmRightStalk

    j = J1850Frame(6)
    f = sim_core.SimFrame("ibst", 0x38E, 0.0125, j.frame)  # counter @ byte1 lo-nibble
    lo = lambda b: b[1] & 0xF  # noqa: E731
    b0 = f.frame(); f.note_send(True)     # noqa: E702  -- wire ctr 0
    b1 = f.frame(); f.note_send(False)    # noqa: E702  -- ctr 1 built then DROPPED -> rollback
    b2 = f.frame(); f.note_send(True)     # noqa: E702  -- ctr 1 reused (gapless)
    assert (lo(b0), lo(b1), lo(b2)) == (0, 1, 1)

    # SccmRightStalk (0x229) + LvPowerState (0x221) expose the same rollback contract.
    for owner in (SccmRightStalk(), LvPowerState()):
        start = owner._ctr
        owner.frame()
        assert owner._ctr == (start + 1) & 0xF
        owner.rollback()
        assert owner._ctr == start  # a drop leaves the counter where it was


def test_bus_membership_matches_firmware_groups():
    frames = _sim_frames()
    vehicle = {f.can_id for f in frames if f.bus == "vehicle"}
    party = {f.can_id for f in frames if f.bus == "party"}
    assert vehicle == {cid for cid, bus in GOLDEN if bus == "vehicle"}
    assert party == {cid for cid, bus in GOLDEN if bus == "party"}
    # Board-TX'd DIR/PMR IDs must never be simulated (they collide -> canDataBusB).
    assert 0x1E5 not in vehicle and 0x1E5 not in party
    assert 0x240 not in vehicle and 0x240 not in party


def test_collect_frames_accepts_a_node_subset():
    reg = sim_registry
    frames = reg.collect_frames(reg.instantiate([reg.BY_NAME["BMS"], reg.BY_NAME["CP"]], _ctx()))
    assert {f.can_id for f in frames} == {
        0x132, 0x212, 0x252, 0x2D2, 0x312, 0x452, 0x392, 0x210, 0x21D, 0x25D,
    }


def test_das_2022_variant_gates_the_new_dasmia_members():
    # 0x289/0x39B are firmware-CONFIRMED 2022-new: no CAN-id load in the 2020.8.1 DIR; both
    # received in the 2022.45.15 DIR. So they live in DAS.fw_variants()["2022.45.15"], NOT
    # the 2020 baseline -- a --fw 2020 bench must not see the two extra dasMIA members.
    reg = sim_registry
    das = [reg.BY_NAME["DAS"]]
    ids = lambda fw: {  # noqa: E731
        f.can_id for f in reg.collect_frames(reg.instantiate(das, _ctx()), fw=fw)
    }
    assert ids("2020.8.1") == {0x389, 0x2B9}
    assert ids("2022.45.15") == {0x389, 0x2B9, 0x289, 0x39B}
    assert ids("2024.8.9") == {0x389, 0x2B9, 0x289, 0x39B}  # persists forward
    assert ids(None) == {0x389, 0x2B9, 0x289, 0x39B}  # default = newest authored


def test_every_node_name_is_unique():
    names = [n.name for n in sim_registry.NODES]
    assert len(names) == len(set(names)), f"duplicate node names: {names}"


# ---- Phase 1: node selection + MIA coverage ------------------------------------


def test_select_nodes_real_drops_only_that_node():
    full = _ids(None)
    assert full - _ids(sim_registry.select_nodes(real=["GTW"])) == {0x7FF, 0x528, 0x3ED}


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
    assert _ids(sim_registry.select_nodes(real=["UI"])) == full - {
        0x82, 0x213, 0x284, 0x293, 0x313, 0x334, 0x333, 0x3B3,
    }


def test_mia_coverage_warnings_partial_full_absent():
    reg = sim_registry
    esp = set(reg.MIA_AGGREGATES["espMIA"])
    assert reg.mia_coverage_warnings(esp) == []  # full coverage -> silent
    assert reg.mia_coverage_warnings(set()) == []  # fully absent -> silent (intentional)
    warns = reg.mia_coverage_warnings(esp - {0x105})  # partial -> warns
    assert any("espMIA" in w and "0x105" in w for w in warns)


# ---- Phase 2: bench config (TOML) + bus normalization -------------------------


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


# ---- Orchestrator: [scenario] profiles applied via node.configure() -----------


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


# ---- Node model: stateful behavior (on_rx transitions) ------------------------


def test_epb_node_transitions_on_di_epb_request():
    epb = sim_registry.BY_NAME["EPB"](_ctx())
    sent = []
    park = (1 << 44).to_bytes(8, "little")  # DI_epbRequest=1 (PARK)
    unpark = (2 << 44).to_bytes(8, "little")  # DI_epbRequest=2 (UNPARK)
    _rx(epb, 0x118, park, lambda cid, d: sent.append(cid))
    assert epb.epb.status == 2  # EPB_PARKED
    _rx(epb, 0x118, unpark, lambda cid, d: sent.append(cid))
    assert epb.epb.status == 1  # EPB_RELEASED
    assert sent == []  # EPB reacts by state only, never TX's reactively


def test_hvp_node_idle_default_is_safe():
    """Default HVP state must be SHUTDOWN + contactors OPEN so a drive bench that never
    touches HVP asserts nothing aggressive on the DIR (no HV/energize command)."""
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

    # Never emit 0/SNA on 0x25D -- the DIR reads that as "cable connected".
    for connected in (False, True):
        cp.set_evse(connected)
        assert by_id[0x25D].frame()[1] & 0xC0 != 0

    # 2020 target: the DIR had no 0x25D subscription, so the frame must not appear.
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
    # default OFF: bmsHvChargeEnable(@0)=0, 12vStatusForDrive(@14w2)=0
    bword = int.from_bytes(base, "little")
    assert bword & 0x1 == 0 and (bword >> 14) & 0x3 == 0
    vc.set_charge_enable(True)
    word = int.from_bytes(by_id[0x3A1].frame(), "little")
    assert word & 0x1 == 1, "bmsHvChargeEnable set"
    assert (word >> 14) & 0x3 == 1, "12vStatusForDrive = READY"
    # drive-side signals still present (diPowerOnState @10 w3 == 3)
    assert (word >> 10) & 0x7 == 3


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


def test_vcsec_node_answers_immo_only_with_a_key():
    vcsec = sim_registry.BY_NAME["VCSEC"](_ctx())
    sent = []
    challenge = bytes([0, 1, 0, 2, 0, 3, 0, 0])
    _rx(vcsec, 0x276, challenge, lambda cid, d: sent.append((cid, d)))
    assert sent == []  # no key -> silent
    # Answering the challenge needs a user-supplied key-derivation provider; the
    # framework ships none, so skip the response half when none is configured.
    import pytest

    from uds_local.security_provider import get_key_derivation_provider

    if get_key_derivation_provider() is None:
        pytest.skip("no key-derivation provider configured")
    vcsec.immo_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    _rx(vcsec, 0x276, challenge, lambda cid, d: sent.append((cid, d)))
    assert len(sent) == 1 and sent[0][0] == 0x3D9  # answered on 0x3D9


# ---- Reactive inter-node comms (the charge-session cascade) --------------------


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


# ---- Drive-inverter nodes + drive scenario ------------------------------------


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
    assert 0x118 in dut_ids and 0x108 in dut_ids  # the inverter DOES source these
    # with the inverter marked real (drive bench), none of its IDs are simulated
    assert not (dut_ids & {f.can_id for f in _sim_frames()})


def test_load_drive_scenario_marks_inverter_real():
    from pathlib import Path

    p = Path(sim_registry.__file__).resolve().parents[1] / "scenarios" / "drive.toml"
    cfg = sim_registry.load_bench_config(p)
    assert set(cfg.real) == {"DI", "DIR", "PMR"}
    assert cfg.scenario["VCFRONT"] == {"lv_power_state": "drive"}
