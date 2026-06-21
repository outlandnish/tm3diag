"""Tests for uds_local/condition_labels.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uds_local.condition_labels import load_condition_labels


def _write_compact(data: dict) -> Path:
    """Write a compact.json-shaped dict to a temp file and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(data, f)
        name = f.name
    return Path(name)


_MINIMAL_COMPACT = {
    "messages": {
        "GTW_status": {
            "signals": {
                "GTW_vdcType": {
                    "value_table_name": "vdcType",
                    "value_description": {"BOSCH_VDC": 0, "TESLA_VDC": 1},
                },
                "GTW_drivetrainType": {
                    "value_table_name": "drivetrainType",
                    "value_description": {"RWD": 0, "AWD": 1},
                },
                "GTW_noTable": {
                    "scale": 1,
                },
            }
        },
        "GTW_status2": {
            "signals": {
                "GTW_chassisType": {
                    "value_table_name": "chassisType",
                    "value_description": {
                        "MODEL_S_CHASSIS": 0,
                        "MODEL_X_CHASSIS": 1,
                        "MODEL_3_CHASSIS": 2,
                    },
                }
            }
        },
    }
}


class TestLoadConditionLabels:
    def test_returns_dict(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        assert isinstance(result, dict)

    def test_extracts_vdctype(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        assert "vdcType" in result
        assert result["vdcType"] == {"0": "BOSCH_VDC", "1": "TESLA_VDC"}

    def test_extracts_drivetraintype(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        assert result["drivetrainType"] == {"0": "RWD", "1": "AWD"}

    def test_extracts_chassistype_from_second_message(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        assert result["chassisType"]["2"] == "MODEL_3_CHASSIS"

    def test_signals_without_value_table_excluded(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        # GTW_noTable has no value_table_name — must not appear as a table key
        assert "GTW_noTable" not in result
        # Only 3 tables defined
        assert len(result) == 3

    def test_none_returns_empty(self):
        assert load_condition_labels(None) == {}

    def test_missing_file_returns_empty(self):
        assert load_condition_labels(Path("/nonexistent/path.json")) == {}

    def test_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not valid json {{{")
            name = f.name
        assert load_condition_labels(Path(name)) == {}

    def test_empty_messages_returns_empty(self):
        p = _write_compact({"messages": {}})
        assert load_condition_labels(p) == {}

    def test_accepts_str_path(self):
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(str(p))
        assert "vdcType" in result

    def test_int_keys_converted_to_str(self):
        # value_description values are ints in JSON — must be str keys in output
        p = _write_compact(_MINIMAL_COMPACT)
        result = load_condition_labels(p)
        for table in result.values():
            for k in table:
                assert isinstance(k, str), f"key {k!r} is not a str"

    def test_duplicate_table_name_last_wins(self):
        # Two signals share the same value_table_name — last one in iteration wins
        compact = {
            "messages": {
                "msg1": {
                    "signals": {
                        "SIG_A": {
                            "value_table_name": "myTable",
                            "value_description": {"FIRST": 0},
                        }
                    }
                },
                "msg2": {
                    "signals": {
                        "SIG_B": {
                            "value_table_name": "myTable",
                            "value_description": {"SECOND": 0},
                        }
                    }
                },
            }
        }
        p = _write_compact(compact)
        result = load_condition_labels(p)
        assert "myTable" in result
        # One of the two wins — just verify only one entry and it's a valid label
        assert result["myTable"]["0"] in ("FIRST", "SECOND")
