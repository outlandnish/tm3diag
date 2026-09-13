"""Tests for vapi_registry: the firmware's VAPI DataValue alias table.

The runtime table (Registry) and its rendering, cache and fallback are tested
against hand-built data -- no firmware needed. Extraction itself (emulating the
lib's init_array) is exercised by the real-dump test, which skips when TM3_ROOT
is unset, the same way test_cid's CidFilesystem real-dump test does.
"""
import json

import pytest

import config
import vapi_registry
from vapi_registry import Registry

_ALIASES = {
    "VAPI_shiftState": {"source": "DI_gear", "render": "enum",
                        "enum": {"0": "Invalid", "1": "P", "4": "D"}},
    "VAPI_driverPresent": {"source": "VCFRONT_driverPresent", "render": "bool"},
    "VAPI_odometer": {"source": "DI_odo", "render": "num"},
}


def _reg():
    return Registry.from_dict({"lib": "libQtCarVAPI.so", "aliases": _ALIASES})


class TestRegistryValue:
    def test_an_enum_source_renders_to_its_label(self):
        assert _reg().value("VAPI_shiftState", {"DI_gear": 4}.get) == "D"

    def test_an_enum_value_absent_from_the_map_is_none(self):
        # DI_gear 7 has no entry -> None, so a caller falls back to seed/store.
        assert _reg().value("VAPI_shiftState", {"DI_gear": 7}.get) is None

    def test_a_float_raw_coerces_for_the_enum_lookup(self):
        assert _reg().value("VAPI_shiftState", {"DI_gear": 4.0}.get) == "D"

    @pytest.mark.parametrize("raw,expected", [(1, "true"), (0, "false"), (3, "true")])
    def test_a_bool_source_renders_true_false(self, raw, expected):
        assert _reg().value("VAPI_driverPresent", {"VCFRONT_driverPresent": raw}.get) == expected

    def test_a_numeric_source_passes_through(self):
        assert _reg().value("VAPI_odometer", {"DI_odo": 12345.6}.get) == 12345.6

    def test_a_name_that_is_not_an_alias_is_none(self):
        assert _reg().value("GUI_serviceMode", {"anything": 1}.get) is None

    def test_a_missing_source_signal_is_none(self):
        # The bus has not carried the source: None, never a guess.
        assert _reg().value("VAPI_shiftState", {}.get) is None

    def test_a_not_an_alias_never_reads_the_bus(self):
        # value() must short-circuit before touching read for a non-alias, so a
        # caller can tell "unknown name" from "known name, absent source".
        reads = []
        _reg().value("GUI_serviceMode", lambda s: reads.append(s))
        assert reads == []


class TestRegistryTable:
    def test_is_alias_and_source(self):
        r = _reg()
        assert r.is_alias("VAPI_shiftState") and r.source("VAPI_shiftState") == "DI_gear"
        assert not r.is_alias("GUI_serviceMode") and r.source("GUI_serviceMode") is None

    def test_len_counts_aliases(self):
        assert len(_reg()) == 3

    def test_round_trip_preserves_enum_with_int_keys(self):
        r = Registry.from_dict(_reg().to_dict())
        assert r.value("VAPI_shiftState", {"DI_gear": 1}.get) == "P"
        # JSON keys are strings; they must come back as ints for the lookup.
        assert r.to_dict()["aliases"]["VAPI_shiftState"]["enum"]["4"] == "D"

    def test_empty_registry_derives_nothing(self):
        r = Registry.empty()
        assert len(r) == 0
        assert r.value("VAPI_shiftState", {"DI_gear": 4}.get) is None


class TestLoadOrBuild:
    def test_none_lib_is_an_empty_registry(self, monkeypatch):
        monkeypatch.setattr(vapi_registry, "_MEMO", {})
        assert len(vapi_registry.load_or_build(None)) == 0

    def test_a_fresh_cache_is_loaded_without_building(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vapi_registry, "_MEMO", {})
        monkeypatch.setattr(vapi_registry, "extract",
                            lambda _lib: pytest.fail("should have loaded the cache"))
        lib = tmp_path / "libQtCarVAPI.so.1.0.0"
        lib.write_bytes(b"\x7fELF")
        cache = tmp_path / "reg.json"
        cache.write_text(json.dumps({"lib": lib.name, "aliases": _ALIASES}))
        import os
        os.utime(lib, (1, 1))                       # lib older than the cache
        reg = vapi_registry.load_or_build(lib, cache)
        assert reg.value("VAPI_shiftState", {"DI_gear": 4}.get) == "D"

    def test_a_stale_cache_is_rebuilt_and_rewritten(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vapi_registry, "_MEMO", {})
        built = {"lib": "x", "aliases": {"VAPI_x": {"source": "S", "render": "num"}}}
        monkeypatch.setattr(vapi_registry, "extract", lambda _lib: built)
        lib = tmp_path / "libQtCarVAPI.so.1.0.0"
        lib.write_bytes(b"\x7fELF")
        cache = tmp_path / "reg.json"
        reg = vapi_registry.load_or_build(lib, cache)
        assert reg.value("VAPI_x", {"S": 5}.get) == 5
        assert json.loads(cache.read_text()) == built     # cache was written

    def test_extraction_failure_yields_an_empty_registry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vapi_registry, "_MEMO", {})

        def boom(_lib):
            raise RuntimeError("no unicorn")

        monkeypatch.setattr(vapi_registry, "extract", boom)
        lib = tmp_path / "libQtCarVAPI.so.1.0.0"
        lib.write_bytes(b"\x7fELF")
        assert len(vapi_registry.load_or_build(lib, tmp_path / "reg.json")) == 0


@pytest.mark.skipif(config.vapi_libs() is None,
                    reason="TM3_ROOT firmware libs not available")
class TestExtractRealFirmware:
    """Extraction against the real libQtCarVAPI: the table the car actually ships."""

    @pytest.fixture(scope="class")
    def data(self):
        return vapi_registry.extract(config.vapi_libs()[0])

    def test_shift_state_aliases_di_gear_through_the_name_map(self, data):
        a = data["aliases"]["VAPI_shiftState"]
        assert a["source"] == "DI_gear" and a["render"] == "enum"
        assert a["enum"]["4"] == "D" and a["enum"]["1"] == "P"

    def test_a_known_bool_alias_is_typed_bool(self, data):
        assert data["aliases"]["VAPI_driverPresent"]["render"] == "bool"

    def test_the_table_covers_the_bulk_of_the_catalog(self, data):
        assert len(data["aliases"]) > 500

    def test_value_renders_a_live_gear(self, data):
        reg = Registry.from_dict(data)
        assert reg.value("VAPI_shiftState", {"DI_gear": 4}.get) == "D"
