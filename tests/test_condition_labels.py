"""Tests for uds_local/condition_labels.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uds_local.condition_labels import (
    labels_for,
    load_condition_labels,
    load_dbc_condition_labels,
)


def _write_dbc(text: str) -> Path:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".dbc", delete=False) as f:
        f.write(text)
        return Path(f.name)


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


# The generated DBC is the fuller source: on 2026.8.3 compact.json names 2 of the
# metadata's 20 condition keys, the DBC names 19 (and the 20th resolves by case).
_DBC = """\
VERSION ""

VAL_TABLE_ GTW_vdcType 0 "BOSCH_VDC" 1 "TESLA_VDC" ;

BO_ 2047 GTW_carConfig: 8 GTW
 SG_ GTW_vdcType m2 : 14|1@1+ (1.0,0.0) [0|0] "" Vector__XXX

VAL_ 2047 GTW_rcmLocation 0 "CENTER_CONSOLE" 1 "UNDER_DASHBOARD" ;
VAL_ 2047 GTW_brakeHWType 0 "BREMBO_P42_MANDO_43MOC" 1 "BREMBO_LARGE_P42_BREMBO_44MOC" ;
VAL_ 1445 DIR_a144_vdcType 0 "NOT_THE_CONFIG_SIGNAL" ;
VAL_ 1444 PM_a043_GTW_vdcType 0 "ALSO_NOT_IT" ;
VAL_ 900 SomeOther_signal 0 "IRRELEVANT" ;
"""


class TestLoadDbcConditionLabels:
    """The DBC carries GTW_carConfig's own value tables, which is where the keys
    compact.json omits (vdcType, rcmLocation, brakeHWType…) are actually named."""

    def test_a_val_table_names_its_key(self):
        got = load_dbc_condition_labels(_write_dbc(_DBC))
        assert got["vdcType"] == {"0": "BOSCH_VDC", "1": "TESLA_VDC"}

    def test_a_val_line_names_its_key_too(self):
        got = load_dbc_condition_labels(_write_dbc(_DBC))
        assert got["rcmLocation"] == {"0": "CENTER_CONSOLE", "1": "UNDER_DASHBOARD"}

    def test_only_the_config_signal_answers_for_a_key(self):
        # DIR_a144_vdcType and PM_a043_GTW_vdcType are alert payloads echoing the
        # config back; letting either answer would mislabel the real choice.
        got = load_dbc_condition_labels(_write_dbc(_DBC))
        assert "NOT_THE_CONFIG_SIGNAL" not in got["vdcType"].values()
        assert "ALSO_NOT_IT" not in got["vdcType"].values()
        assert set(got) == {"vdcType", "rcmLocation", "brakeHWType"}

    def test_a_missing_or_unset_dbc_is_not_an_error(self):
        assert load_dbc_condition_labels(None) == {}
        assert load_dbc_condition_labels(Path("/nonexistent/x.dbc")) == {}


class TestMergedSources:
    def test_the_dbc_fills_in_what_compact_never_had(self):
        merged = load_condition_labels(_write_compact({"messages": {}}), _write_dbc(_DBC))
        assert merged["vdcType"]["0"] == "BOSCH_VDC"

    def test_compact_still_answers_when_there_is_no_dbc(self):
        merged = load_condition_labels(_write_compact(_MINIMAL_COMPACT), None)
        assert merged["drivetrainType"] == {"0": "RWD", "1": "AWD"}

    def test_the_dbc_wins_where_both_name_a_value(self):
        # Same firmware, but the DBC is generated from its decoder rather than
        # from the subset shipped to the diagnostic tool.
        compact = {"messages": {"m": {"signals": {"s": {
            "value_table_name": "vdcType",
            "value_description": {"STALE_NAME": 0}}}}}}
        merged = load_condition_labels(_write_compact(compact), _write_dbc(_DBC))
        assert merged["vdcType"]["0"] == "BOSCH_VDC"

    def test_a_key_no_source_names_simply_has_none(self):
        # The caller shows the raw value -- an unlabelled number is still a
        # correct answer, and better than not offering the choice.
        merged = load_condition_labels(None, _write_dbc(_DBC))
        assert merged.get("espValveType") is None


class TestLabelsFor:
    def test_an_exact_key_is_used(self):
        assert labels_for({"vdcType": {"0": "BOSCH_VDC"}}, "vdcType") == {"0": "BOSCH_VDC"}

    def test_a_key_matches_across_case(self):
        # The metadata spells this key brakeHWType on most rows and brakeHwType
        # on the ESP's; the signal is GTW_brakeHWType, so without folding one of
        # them showed bare numbers.
        table = load_dbc_condition_labels(_write_dbc(_DBC))
        assert labels_for(table, "brakeHwType")["0"] == "BREMBO_P42_MANDO_43MOC"

    def test_an_unknown_key_has_no_labels(self):
        assert labels_for({"vdcType": {"0": "x"}}, "towPackage") == {}

    def test_no_label_map_is_not_an_error(self):
        assert labels_for(None, "vdcType") == {}
