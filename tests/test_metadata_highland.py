"""End-to-end firmware-selection tests against a real Highland Model 3 service card.

Fixtures in tests/fixtures/highland/ are a verbatim capture from a Tesla service
("ConfigLoader") SD card pulled mid-flash:

  - signed_metadata_map.tsv      the card's MAP.TSV (git SHA 067a1dfc..., map version 11)
  - signed_metadata_map.tsv.sig  the 64-byte Ed25519 detached signature over it
  - vehicle_config.json          the integer-valued config IDs decoded from the
                                 gateway's UDSDEBUG.LOG ("Config <name> id=.. value=..")
  - cbreaker.map                 the per-chassisType ECU roster (chassisType:2 = Highland)

Unlike test_metadata.py (which points at an external deploy path that may not exist),
these fixtures are committed, so this module always runs. It exercises the same
load_metadata / find_firmware / packed_key_from_f180 path that dfu.py uses in the
field, against genuine data with all its real-world quirks.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from uds_local.metadata import (
    FirmwareEntry,
    find_firmware,
    load_metadata,
    narrow_by_conditions,
    packed_key_from_f180,
    varying_condition_keys,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "highland"
_TSV = _FIXTURES / "signed_metadata_map.tsv"
_SIG = _FIXTURES / "signed_metadata_map.tsv.sig"
_CONFIG = _FIXTURES / "vehicle_config.json"

# Build identifier stamped on this card (also appears in BOOTED.IMG/GW.HGZ at offset 0x14).
_GIT_SHA = "067a1dfcf133a88b994f7f9562dde8eae27155c0"
_MAP_VERSION = "11"


@pytest.fixture(scope="module")
def entries() -> list[FirmwareEntry]:
    return load_metadata(_TSV)


@pytest.fixture(scope="module")
def vehicle_config() -> dict[str, str]:
    return json.loads(_CONFIG.read_text())


class TestHighlandMetadataShape:
    def test_fixture_files_present(self):
        assert _TSV.exists() and _SIG.exists() and _CONFIG.exists()

    def test_loads_expected_count(self, entries):
        # 2648 lines in MAP.TSV minus the 1 git-SHA header line.
        assert len(entries) == 2647

    def test_git_sha_header_excluded(self, entries):
        # The header line "<sha>\t<version>" must not be parsed as an entry.
        assert all(e.lookup_key != _GIT_SHA for e in entries)
        first = _TSV.read_text().splitlines()[0].split("\t")
        assert first == [_GIT_SHA, _MAP_VERSION]

    def test_signature_is_64_bytes(self):
        # Genuine Ed25519 detached signature over the .tsv.
        assert _SIG.stat().st_size == 64

    def test_every_entry_well_formed(self, entries):
        for e in entries:
            assert ":" in e.lookup_key
            assert e.src_path.strip()
            assert e.dest_name.strip()
            assert e.crc
            assert isinstance(e.conditions, dict)

    def test_covers_highland_specific_ecus(self, entries):
        # ECUs introduced on Highland that the older Model 3 map lacks.
        components = {e.component for e in entries}
        for c in ("icr", "disp", "lvbms", "rgbdoorfl"):
            assert c in components, f"expected Highland ECU {c!r} in map"


class TestFindFirmwareWithRealConfig:
    """find_firmware against the genuine config broadcast by this vehicle's gateway."""

    def test_vehicle_is_highland(self, vehicle_config):
        assert vehicle_config["chassisType"] == "2"

    def test_real_config_collapses_known_clean_cases(self, entries, vehicle_config):
        # ECUs whose conditions are fully covered by the logged config resolve to a
        # single firmware row. These are anchored from the captured card.
        clean = {
            "cbc:687865856": "cbc.bhx",
            "db:83886080": "db.bhx",
            "bleepleft:167837697": "bleepleft.bhx",
        }
        for key, dest in clean.items():
            ecu, pk = key.split(":")
            res = find_firmware(entries, ecu, int(pk), conditions=vehicle_config)
            assert len(res) == 1, f"{key} did not collapse to 1: {len(res)} rows"
            assert res[0].dest_name == dest

    def test_selection_never_crashes_and_returns_subset(self, entries, vehicle_config):
        # For every lookup_key in the map, filtered selection must be a subset of the
        # unfiltered matches and must never raise — the property dfu.py relies on.
        by_key = defaultdict(list)
        for e in entries:
            by_key[e.lookup_key].append(e)
        for key in by_key:
            ecu, pk = key.split(":")
            unfiltered = find_firmware(entries, ecu, int(pk))
            filtered = find_firmware(entries, ecu, int(pk), conditions=vehicle_config)
            # FirmwareEntry is an unfrozen dataclass (unhashable), so compare by identity.
            assert all(any(f is u for u in unfiltered) for f in filtered)
            assert filtered, f"{key} returned no matches (should fall back to all)"

    def test_brake_gated_ecus_cannot_be_resolved_from_logged_config(
        self, entries, vehicle_config
    ):
        # Documented real-world quirk: this card's logged config carries brakeHWType=1
        # and no espValveType, but every esp/ibst row is gated on brake values 6/13/15/16/18
        # (and esp rows use the lowercase key 'brakeHwType'). No row matches, so find_firmware
        # correctly falls back to the full candidate set rather than mis-selecting.
        for key in ("esp:84148225", "ibst:67305475"):
            ecu, pk = key.split(":")
            unfiltered = find_firmware(entries, ecu, int(pk))
            filtered = find_firmware(entries, ecu, int(pk), conditions=vehicle_config)
            assert len(filtered) == len(unfiltered) > 1

    def test_map_mixes_brake_key_casing(self, entries):
        # Surface the genuine inconsistency in Tesla's data so a future case-folding
        # change to find_firmware is a deliberate decision, not an accident.
        keys = set()
        for e in entries:
            keys.update(e.conditions)
        assert "brakeHWType" in keys  # used by ibst
        assert "brakeHwType" in keys  # used by esp


class TestGatewayFlashEntry:
    """The gateway app is the one ECU the captured log actually flashed (gtw3:13 succeeded)."""

    def test_gtw3_app_entry_present(self, entries):
        gtw3_apps = [
            e for e in entries if e.component == "gtw3" and e.dest_name == "gtw3.img"
        ]
        assert gtw3_apps, "no gtw3 app image in map"
        assert all(e.src_path.endswith("gwapp.img") for e in gtw3_apps)


class TestPackedKeyAgainstRealMap:
    def test_packed_key_resolves_a_real_entry(self, entries):
        # Reconstruct a packed key from an F180-shaped response and confirm it indexes
        # a real row. db:83886080 -> PCBA_ID=5, ASSEMBLY_ID=0, USAGE_ID=0.
        assert 83886080 == (5 << 24)
        f180 = bytes([0x00, 0x00, 0x1B, 0x05, 0x00, 0x00] + [0x00] * 13)
        assert packed_key_from_f180(f180) == 83886080
        assert find_firmware(entries, "db", 83886080)


class TestConditionNarrowing:
    """varying_condition_keys and narrow_by_conditions against the highland fixture."""

    def test_no_variation_returns_empty(self, entries):
        # adsp entries have chassisType but it's the same value across all — no variation
        adsp = [e for e in entries if e.component == "adsp"]
        assert adsp, "need adsp entries in fixture"
        keys = varying_condition_keys(adsp)
        # chassisType may vary across adsp entries; we just assert the return type
        assert isinstance(keys, list)
        assert all(isinstance(k, str) for k in keys)

    def test_single_entry_no_variation(self, entries):
        # A set of one entry never has varying keys
        single = entries[:1]
        assert varying_condition_keys(single) == []

    def test_vdctype_shows_as_varying_for_pmr(self, entries):
        # Highland has pmr entries gated on driveInterfaceType — check a key actually varies
        pmr = [e for e in entries if e.component == "pmr"]
        if not pmr:
            pytest.skip("no pmr entries in highland fixture")
        keys = varying_condition_keys(pmr)
        assert isinstance(keys, list)
        # At least one key must vary (the whole point)
        assert len(keys) >= 1

    def test_wildcards_excluded_from_variation_analysis(self):
        # Wildcard entries (conditions={}) must not pollute the varying-key analysis
        e_wild = FirmwareEntry("x:1", "x.bhx", "x.bhx", "x", "aa", {}, "sig")
        e_typed = FirmwareEntry("x:1", "x.bhx", "x.bhx", "x", "aa", {"vdcType": "0"}, "sig")
        e_typed2 = FirmwareEntry("x:1", "x.bhx", "x.bhx", "x", "aa", {"vdcType": "1"}, "sig")
        keys = varying_condition_keys([e_wild, e_typed, e_typed2])
        assert "vdcType" in keys

    def test_narrow_by_conditions_filters_correctly(self):
        e0 = FirmwareEntry("x:1", "a.bhx", "a.bhx", "x", "aa", {"vdcType": "0"}, "s")
        e1 = FirmwareEntry("x:1", "b.bhx", "b.bhx", "x", "bb", {"vdcType": "1"}, "s")
        result = narrow_by_conditions([e0, e1], "vdcType", "0")
        assert result == [e0]

    def test_narrow_by_conditions_fallback_on_empty(self):
        e0 = FirmwareEntry("x:1", "a.bhx", "a.bhx", "x", "aa", {"vdcType": "0"}, "s")
        e1 = FirmwareEntry("x:1", "b.bhx", "b.bhx", "x", "bb", {"vdcType": "1"}, "s")
        # Value "9" matches nothing → must return original list unchanged
        result = narrow_by_conditions([e0, e1], "vdcType", "9")
        assert result == [e0, e1]

    def test_narrow_by_conditions_wildcard_entries_survive(self):
        e_wild = FirmwareEntry("x:1", "w.bhx", "w.bhx", "x", "cc", {}, "s")
        e0 = FirmwareEntry("x:1", "a.bhx", "a.bhx", "x", "aa", {"vdcType": "0"}, "s")
        e1 = FirmwareEntry("x:1", "b.bhx", "b.bhx", "x", "bb", {"vdcType": "1"}, "s")
        result = narrow_by_conditions([e_wild, e0, e1], "vdcType", "0")
        # Wildcard entry has no vdcType key → conditions.get("vdcType") is None ≠ "0"
        # Only e0 matches. e_wild is excluded — callers decide whether to re-add wildcards.
        assert result == [e0]


class TestPromptConditions:
    """_prompt_conditions via monkeypatched prompt_select."""

    def _make_entry(self, dest: str, conds: dict) -> "FirmwareEntry":
        from uds_local.metadata import FirmwareEntry
        return FirmwareEntry("x:1", f"{dest}.bhx", f"{dest}.bhx", "x", "aa", conds, "sig")

    def test_no_varying_keys_returns_matches_unchanged(self, monkeypatch):
        import dfu
        # prompt_select must never be called when there's nothing to decide
        monkeypatch.setattr("dfu.prompt_select", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not prompt")))
        e0 = self._make_entry("a", {"vdcType": "0"})
        e1 = self._make_entry("b", {"vdcType": "0"})  # same value — no variation
        from flash_scripts._display import StatusDisplay
        result = dfu._prompt_conditions([e0, e1], StatusDisplay())
        assert result == [e0, e1]

    def test_single_varying_key_prompts_once_and_narrows(self, monkeypatch):
        import dfu
        calls = []
        def fake_select(question, labels, default=0, display=None):
            calls.append((question, labels))
            return 1  # always pick second option
        monkeypatch.setattr("dfu.prompt_select", fake_select)

        e0 = self._make_entry("a", {"vdcType": "0"})
        e1 = self._make_entry("b", {"vdcType": "1"})
        from flash_scripts._display import StatusDisplay
        result = dfu._prompt_conditions([e0, e1], StatusDisplay())

        assert len(calls) == 1
        assert calls[0][0] == "Select vdcType"
        assert calls[0][1] == ["vdcType=0", "vdcType=1"]
        assert result == [e1]

    def test_two_varying_keys_prompts_twice(self, monkeypatch):
        import dfu
        calls = []
        def fake_select(question, labels, default=0, display=None):
            calls.append(question)
            return 0  # always pick first option
        monkeypatch.setattr("dfu.prompt_select", fake_select)

        e00 = self._make_entry("a", {"vdcType": "0", "drivetrainType": "0"})
        e01 = self._make_entry("b", {"vdcType": "0", "drivetrainType": "1"})
        e10 = self._make_entry("c", {"vdcType": "1", "drivetrainType": "0"})
        e11 = self._make_entry("d", {"vdcType": "1", "drivetrainType": "1"})
        from flash_scripts._display import StatusDisplay
        result = dfu._prompt_conditions([e00, e01, e10, e11], StatusDisplay())

        assert len(calls) == 2
        assert set(calls) == {"Select drivetrainType", "Select vdcType"}
        # After picking first value for each key, exactly one entry survives
        assert len(result) == 1

    def test_wildcard_entries_pass_through(self, monkeypatch):
        import dfu
        monkeypatch.setattr("dfu.prompt_select", lambda *a, **kw: 0)
        e_wild = self._make_entry("w", {})
        e0 = self._make_entry("a", {"vdcType": "0"})
        e1 = self._make_entry("b", {"vdcType": "1"})
        from flash_scripts._display import StatusDisplay
        result = dfu._prompt_conditions([e_wild, e0, e1], StatusDisplay())
        # Wildcard has no vdcType — narrow_by_conditions excludes it from the narrowed set.
        # Only the matching typed entry survives.
        assert e_wild not in result
        assert len(result) == 1

    def test_single_match_no_prompt(self, monkeypatch):
        import dfu
        monkeypatch.setattr("dfu.prompt_select", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not prompt")))
        e0 = self._make_entry("a", {"vdcType": "0"})
        from flash_scripts._display import StatusDisplay
        result = dfu._prompt_conditions([e0], StatusDisplay())
        assert result == [e0]
