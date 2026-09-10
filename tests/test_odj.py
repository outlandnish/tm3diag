"""Tests for uds_local/odj.py."""
from __future__ import annotations

from pathlib import Path

import pytest

import config as _cfg
from uds_local.odj import load_odj

_ODJ_DIR = _cfg.ODJ_DIR

if _ODJ_DIR is None or not _ODJ_DIR.exists():
    pytest.skip("TM3_ROOT firmware data not available — skipping odj tests", allow_module_level=True)


def _odj_path(stem: str) -> Path:
    """Path to an ODJ for `stem`, preferring plaintext .odj over .odj.bin.

    Skips when neither form is present in the active firmware dump.
    """
    plain = _ODJ_DIR / f"{stem}.odj"
    if plain.exists() or (_ODJ_DIR / f"{stem}.odj.bin").exists():
        return plain
    pytest.skip(f"{stem}.odj[.bin] not available in this firmware dump")


class TestLoadOdjBinFallback:
    def test_bin_only_stem_falls_back_to_bin(self):
        bin_only = None
        for p in _ODJ_DIR.glob("*.odj.bin"):
            stem = p.name[: -len(".odj.bin")]
            if not (_ODJ_DIR / f"{stem}.odj").exists():
                bin_only = stem
                break
        if bin_only is None:
            pytest.skip("no bin-only ODJ in this dump (all have plaintext)")
        # Some ODJs hold only routines or io-controls, so assert something parsed.
        dids, routines, io_controls = load_odj(_ODJ_DIR / f"{bin_only}.odj")
        assert dids or routines or io_controls, (
            f"{bin_only}.odj.bin fallback parsed nothing"
        )

    def test_missing_both_returns_empty(self):
        dids, routines, io_controls = load_odj(_ODJ_DIR / "NONEXISTENT.odj")
        assert dids == {}
        assert routines == {}
        assert io_controls == {}


class TestOdjEntryFields:
    # The CP ODJ varies across dumps, so assert the shape of parsed entries,
    # not specific DID/field names or ids.

    def test_cp_dids_have_valid_hex_ids(self):
        dids, _, _ = load_odj(_odj_path("CP"))
        assert dids, "CP.odj parsed no DIDs"
        for name, entry in dids.items():
            assert isinstance(name, str) and name
            assert 0 <= entry.hex_id <= 0xFFFF

    def test_cp_dids_have_read_or_write(self):
        dids, _, _ = load_odj(_odj_path("CP"))
        for name, entry in dids.items():
            assert entry.read is not None or entry.write is not None, name

    def test_writable_did_input_fields_are_well_formed(self):
        dids, _, _ = load_odj(_odj_path("CP"))
        writable = [e for e in dids.values()
                    if e.write is not None and e.write.input]
        assert writable, "CP.odj has no writable DID with input fields"
        for entry in writable:
            for fname, field in entry.write.input.items():
                assert isinstance(fname, str) and fname
                assert isinstance(field.enum_map, dict)
                assert field.bit_length > 0
                assert field.byte_position >= 0
                assert 0 <= field.bit_position < 8
                assert field.data_type in ("uint", "int", "ascii", "bytes")


class TestRoutineEntryFields:
    def test_routine_subsections_parse(self):
        _, routines, _ = load_odj(_odj_path("DI"))
        assert routines, "DI.odj parsed no routines"
        for name, entry in routines.items():
            assert entry.name == name
        with_sub = [e for e in routines.values()
                    if e.start is not None or e.stop is not None
                    or e.results is not None]
        assert with_sub, "no DI routine has a start/stop/results sub-section"


class TestIoControlEntryFields:
    def test_io_control_input_field(self):
        _, _, io_controls = load_odj(_odj_path("DI"))
        with_input = [(n, e) for n, e in io_controls.items() if e.input]
        assert with_input, "DI.odj has no io-control with input fields"
        for name, entry in with_input:
            assert 0 <= entry.hex_id <= 0xFFFF
            for fname, field in entry.input.items():
                assert isinstance(fname, str) and fname
                assert field.bit_length > 0
                assert field.data_type in ("uint", "int", "ascii", "bytes")
