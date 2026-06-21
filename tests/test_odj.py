"""Tests for uds_local/odj.py."""
from __future__ import annotations

import pytest
from pathlib import Path

import config as _cfg
from uds_local.odj import (
    FieldSpec, OdjEntry, RoutineEntry, IoControlEntry, load_odj,
)

_ODJ_DIR = _cfg.ODJ_DIR

if _ODJ_DIR is None or not _ODJ_DIR.exists():
    pytest.skip("TM3_ROOT firmware data not available — skipping odj tests", allow_module_level=True)


class TestLoadOdjBinFallback:
    def test_plain_missing_falls_back_to_bin(self):
        # CP.odj does not exist in 2026.8.3 — only CP.odj.bin does
        plain_path = _ODJ_DIR / "CP.odj"
        assert not plain_path.exists(), "test assumes CP.odj is absent"
        dids, routines, io_controls = load_odj(plain_path)
        assert len(dids) > 0

    def test_missing_both_returns_empty(self):
        dids, routines, io_controls = load_odj(_ODJ_DIR / "NONEXISTENT.odj")
        assert dids == {}
        assert routines == {}
        assert io_controls == {}


class TestOdjEntryFields:
    def test_cp_did_hex_id(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        entry = dids["DTC_TEST_RESULT"]
        assert entry.hex_id == 0x4FF

    def test_cp_did_write_not_none(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        entry = dids["DTC_TEST_RESULT"]
        assert entry.write is not None

    def test_cp_did_read_is_none_for_write_only(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        entry = dids["DTC_TEST_RESULT"]
        assert entry.read is None

    def test_cp_did_write_enum_field(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        entry = dids["DTC_TEST_RESULT"]
        field = entry.write.input["TEST_RESULT"]
        assert field.enum_map == {"TEST_FAILED": 2, "TEST_PASSED": 1}

    def test_field_with_no_map_has_empty_enum_map(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        entry = dids["DTC_TEST_RESULT"]
        field = entry.write.input["TEST_ID"]
        assert field.enum_map == {}
        assert isinstance(field.enum_map, dict)

    def test_field_spec_attributes(self):
        dids, _, _ = load_odj(_ODJ_DIR / "CP.odj")
        field = dids["DTC_TEST_RESULT"].write.input["TEST_ID"]
        assert field.bit_length == 16
        assert field.byte_position == 0
        assert field.bit_position == 0
        assert field.data_type == "uint"


class TestRoutineEntryFields:
    def test_routine_with_start_stop_results(self):
        # Use DI.odj which has routines with all three sub-sections
        path = _ODJ_DIR / "DI.odj"
        if not path.exists():
            path = _ODJ_DIR / "DI.odj.bin"
        if not path.exists():
            pytest.skip("DI.odj not available")
        _, routines, _ = load_odj(path)
        # ACCEL_PEDAL_SELF_TEST has results only
        entry = routines["ACCEL_PEDAL_SELF_TEST"]
        assert entry.results is not None
        assert entry.name == "ACCEL_PEDAL_SELF_TEST"


class TestIoControlEntryFields:
    def test_io_control_input_field(self):
        path = _ODJ_DIR / "DI.odj"
        if not path.exists():
            path = _ODJ_DIR / "DI.odj.bin"
        if not path.exists():
            pytest.skip("DI.odj not available")
        _, _, io_controls = load_odj(path)
        entry = io_controls["OIL_PUMP_FLOW_COMMAND_ADJUST"]
        assert entry.hex_id == 0x500
        assert "FLOW_COMMAND" in entry.input
        field = entry.input["FLOW_COMMAND"]
        assert field.bit_length == 8
        assert field.data_type == "uint"
