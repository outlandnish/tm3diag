"""Firmware-revision message selection (sim_core.FirmwareVersion / Node.frames_for).

Fallback policy: a node emits the newest message set authored at/below the target
revision, else the last version it has (clamped to the oldest); no target => newest.
"""
from __future__ import annotations

import pytest
import sim_core
import sim_registry
from sim_core import BASELINE_FW, FirmwareVersion, Node, SimFrame, resolve_fw_variants


def _sf(name: str, can_id: int) -> SimFrame:
    return SimFrame(name, can_id, 0.1, lambda: bytearray(8))


def test_firmware_version_parses_raw_extraction_dir_names():
    assert str(FirmwareVersion("2020.8.1-9-ae1963092f.model3")) == "2020.8.1"
    assert str(FirmwareVersion("2024.8.9.ice.extracted")) == "2024.8.9"
    assert str(FirmwareVersion("2026.8.3")) == "2026.8.3"


def test_firmware_version_ordering_and_padding():
    assert FirmwareVersion("2020.8.1") < FirmwareVersion("2024.8.9")
    assert FirmwareVersion("2024.8.9") < FirmwareVersion("2025.26.8")
    assert FirmwareVersion("2024.8.9") < FirmwareVersion("2024.10.1")
    assert FirmwareVersion("2020.8") == FirmwareVersion("2020.8.0")
    assert FirmwareVersion("2020.8") < FirmwareVersion("2020.8.1")


def test_firmware_version_equal_hashes_for_zero_padded():
    assert hash(FirmwareVersion("2020.8")) == hash(FirmwareVersion("2020.8.0"))
    assert FirmwareVersion("2020.8.0.0") == FirmwareVersion("2020.8")


def test_firmware_version_accepts_a_firmware_version():
    v = FirmwareVersion("2024.8.9")
    assert FirmwareVersion(v) == v


def test_firmware_version_rejects_non_numeric():
    with pytest.raises(ValueError):
        FirmwareVersion("not-a-version")
    with pytest.raises(ValueError):
        FirmwareVersion("")


# resolve_fw_variants — the fallback policy
_VARIANTS = {"2020.8.1": "base", "2024.8.9": "mid", "2026.8.3": "new"}


def test_resolve_none_picks_newest_authored():
    assert resolve_fw_variants(_VARIANTS, None) == "new"


def test_resolve_exact_match():
    assert resolve_fw_variants(_VARIANTS, "2024.8.9") == "mid"


def test_resolve_floors_to_newest_at_or_below_target():
    assert resolve_fw_variants(_VARIANTS, "2025.26.8") == "mid"
    assert resolve_fw_variants(_VARIANTS, "2022.45.15") == "base"


def test_resolve_clamps_to_oldest_when_target_predates_all():
    assert resolve_fw_variants(_VARIANTS, "2019.1.1") == "base"


def test_resolve_target_above_all_uses_newest():
    assert resolve_fw_variants(_VARIANTS, "2099.1.1") == "new"


def test_resolve_single_variant_always_wins():
    only = {"2020.8.1": "only"}
    assert resolve_fw_variants(only, None) == "only"
    assert resolve_fw_variants(only, "2030.0") == "only"
    assert resolve_fw_variants(only, "2000.0") == "only"


def test_resolve_empty_raises():
    with pytest.raises(ValueError):
        resolve_fw_variants({}, "2020.8.1")


# Node.frames_for / fw_variants / resolved_fw
class _PlainNode(Node):
    name = "PLAIN"

    def frames(self) -> list[SimFrame]:
        return [_sf("base", 0x100)]


class _VersionedNode(Node):
    """A node whose message set changed at 2024.8.9 (an extra frame appears)."""

    name = "VERS"

    def frames(self) -> list[SimFrame]:
        return [_sf("v20", 0x100)]

    def _frames_2024(self) -> list[SimFrame]:
        return [_sf("v24", 0x100), _sf("v24_extra", 0x101)]

    def fw_variants(self):
        return {"2020.8.1": self.frames, "2024.8.9": self._frames_2024}


def test_plain_node_defaults_to_baseline_for_every_target():
    n = _PlainNode()
    assert n.fw_variants() == {BASELINE_FW: n.frames}
    for target in (None, "2020.8.1", "2026.8.3", "2019.1.1"):
        assert [f.name for f in n.frames_for(target)] == ["base"]
        assert str(n.resolved_fw(target)) == "2020.8.1"


def test_versioned_node_selects_the_right_set():
    n = _VersionedNode()
    assert [f.name for f in n.frames_for(None)] == ["v24", "v24_extra"]  # newest
    assert [f.name for f in n.frames_for("2024.8.9")] == ["v24", "v24_extra"]  # exact
    assert [f.name for f in n.frames_for("2025.26.8")] == ["v24", "v24_extra"]  # floor -> 2024
    assert [f.name for f in n.frames_for("2022.45.15")] == ["v20"]  # floor -> 2020
    assert [f.name for f in n.frames_for("2019.1")] == ["v20"]  # clamp -> oldest


def test_versioned_node_resolved_fw_labels():
    n = _VersionedNode()
    assert str(n.resolved_fw(None)) == "2024.8.9"
    assert str(n.resolved_fw("2022.45.15")) == "2020.8.1"
    assert str(n.resolved_fw("2026.8.3")) == "2024.8.9"


# self.fw: the per-node target the driver sets
def test_node_defaults_fw_to_none():
    assert _VersionedNode().fw is None


def test_frames_for_inherits_self_fw_by_default():
    n = _VersionedNode()
    n.fw = "2022.45.15"
    assert [f.name for f in n.frames_for()] == ["v20"]
    assert str(n.resolved_fw()) == "2020.8.1"
    assert [f.name for f in n.frames_for("2024.8.9")] == ["v24", "v24_extra"]
    assert [f.name for f in n.frames_for(None)] == ["v24", "v24_extra"]  # None = newest


def test_fw_inherit_sentinel_is_distinct_from_none():
    # FW_INHERIT (default) means "use self.fw"; None means "newest authored".
    n = _VersionedNode()
    n.fw = "2022.45.15"
    assert n.frames_for(sim_core.FW_INHERIT)[0].name == "v20"  # inherit -> floored
    assert n.frames_for(None)[0].name == "v24"  # explicit newest


def test_collect_frames_inherits_each_nodes_fw():
    a, b = _VersionedNode(), _VersionedNode()
    a.fw = "2022.45.15"  # -> baseline set (1 frame)
    b.fw = "2024.8.9"  # -> 2024 set (2 frames)
    ids = sorted(f.can_id for f in sim_registry.collect_frames([a, b]))
    assert ids == [0x100, 0x100, 0x101]


# Registry threading + the "baseline is stable across fw" invariant
def _db_free_nodes():
    ctx = sim_core.NodeContext()
    # GTW's builder needs the compact DB; exclude it (the golden test covers GTW).
    classes = [
        c
        for c in sim_registry.select_nodes(real=["DI", "DIR", "PMR"])
        if c.name != "GTW"
    ]
    return sim_registry.instantiate(classes, ctx)


def test_2022_variant_adds_exactly_the_fw_confirmed_new_ids():
    nodes = _db_free_nodes()
    ids_2020 = {f.can_id for f in sim_registry.collect_frames(nodes, "2020.8.1")}
    ids_2022 = {f.can_id for f in sim_registry.collect_frames(nodes, "2022.45.15")}
    # Nodes with a 2022.45.15 variant add ONLY firmware-confirmed 2022-new IDs (absent in the
    # 2020 DIR): DAS 0x289/0x39B (party) + BMS 0x452, CMP 0x2A7, APP 0x25C, CP 0x25D,
    # UI 0x3B3 UI_vehicleControl2 + 0x353 UI_status (UI_developmentCar -> dyno bypass) (vehicle).
    # Everything else is baseline, so this is the exact 2020->2022 delta; nothing is dropped
    # between those two (2026 IS the first revision that drops one -- see the next test).
    # 0x392 is NOT in the delta: it reassigns owner across fw (epas3p EPAS3P_alertMatrix in 2020 ->
    # bms BMS_packConfig in 2022) but stays in the inventory at both targets.
    assert ids_2022 - ids_2020 == {0x289, 0x39B, 0x452, 0x2A7, 0x25C, 0x25D, 0x3B3, 0x353}
    assert ids_2020 - ids_2022 == set()
    assert 0x392 in ids_2020 and 0x392 in ids_2022, "0x392 must persist across the reassignment"
    assert ids_2020, "expected a non-empty simulated inventory"


def test_2026_variant_swaps_the_app_liveness_id():
    """2026.8.3 is the first revision that DROPS an id. Enumerating both DIRs' rx sets from
    firmware shows 0x25C is the only frame the 2026 DIR lost, and
    0x25B APP_environment is new -- the app-liveness message was renumbered. Everything else
    the sim sends carries forward."""
    nodes = _db_free_nodes()
    ids_none = {f.can_id for f in sim_registry.collect_frames(nodes, None)}
    ids_2022 = {f.can_id for f in sim_registry.collect_frames(nodes, "2022.45.15")}
    ids_2026 = {f.can_id for f in sim_registry.collect_frames(nodes, "2026.8.3")}
    assert ids_none == ids_2026  # newest == any target >= 2026.8.3
    # 0x25B replaces 0x25C; 0x142/0x238/0x3FD are new rx ids the 2026 DIR MIA-supervises.
    # GTW's new 0x318 GTW_carState is NOT here because _db_free_nodes drops GTW (needs a DBC);
    # it is covered by the golden inventory in test_vehicle_sim_nodes.
    # (0x1D5 PMF_state4 and 0x2E5 DIF_power are also new to the DIR but AWD-only -- not simulated.)
    assert ids_2026 - ids_2022 == {0x25B, 0x142, 0x238, 0x3FD}
    assert ids_2022 - ids_2026 == {0x25C}


def test_only_fw_varied_nodes_diverge_from_baseline_today():
    # Nodes with a 2022.45.15 fw_variant. IBST's variant adds no new id -- it
    # re-sends IBST_status 0x39D on the VEHICLE bus as well, because the 2022 DIR
    # validates it on bus A (a110_brakeMIA) where 2020 only wanted it on party.
    # DIF is in BOTH sets: DIF_status 0x2D5 is DLC 7 on 2022.45.15 and DLC 8 again on 2026.8.3,
    # both read out of firmware. (PMF is baseline-only -- 0x1D5's placement does not move.)
    # VCFRONT's 2022 set multiplexes 0x3A1 (VCFRONT_vehicleStatus pages 0/1).
    varied_2022 = {"DAS", "BMS", "CMP", "APP", "UI", "EPAS3P", "IBST", "CP", "DIF", "VCFRONT"}
    # Nodes that additionally author a 2026.8.3 set: BMS re-lays 0x132 (DLC 8->6) / 0x212 / 0x252,
    # UI 0x284 gains a now-ENFORCED counter+checksum, APP swaps 0x25C -> 0x25B, and VCFRONT
    # reseeds the 0x3A1 checksum magic (0x2A -> 0xC0).
    varied_2026 = {"BMS", "UI", "APP", "VCFRONT", "VCLEFT", "GTW", "DIF"}
    for node in _db_free_nodes():
        if node.name in varied_2026:
            expected = "2026.8.3"
        elif node.name in varied_2022:
            expected = "2022.45.15"
        else:
            expected = BASELINE_FW
        assert str(node.resolved_fw("2026.8.3")) == expected, node.name
        # a 2022 bench must keep resolving to the 2022 set -- adding a 2026 set must not
        # drag newer messages onto an older target.
        expected_2022 = "2022.45.15" if node.name in varied_2022 else BASELINE_FW
        assert str(node.resolved_fw("2022.45.15")) == expected_2022, node.name


# BenchConfig / load_bench_config — [firmware] version
def test_bench_config_default_fw_is_none():
    assert sim_registry.BenchConfig().fw is None


def test_load_bench_config_parses_firmware_version(tmp_path):
    p = tmp_path / "sim.toml"
    p.write_text('[firmware]\nversion = "2024.8.9"\n[nodes]\nreal = ["PCS"]\n')
    cfg = sim_registry.load_bench_config(str(p))
    assert cfg.fw == "2024.8.9"
    assert cfg.real == ["PCS"]


def test_load_bench_config_absent_firmware_is_none(tmp_path):
    p = tmp_path / "sim.toml"
    p.write_text('[nodes]\nreal = ["PCS"]\n')
    assert sim_registry.load_bench_config(str(p)).fw is None


def test_load_bench_config_rejects_unparseable_firmware_version(tmp_path):
    p = tmp_path / "sim.toml"
    p.write_text('[firmware]\nversion = "not-a-version"\n')
    with pytest.raises(ValueError, match="firmware"):
        sim_registry.load_bench_config(str(p))
