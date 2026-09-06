"""Tests for alert_log field packing (firmware-independent).

The catalog-backed name lookups need the UI .so files, so those are exercised
by the integration tests; here we pin the wire bit-packing that was reversed
from the DIR firmware and Damien Maguire's openinverter PCS decoder, using the
real on-vehicle frames captured for a094.
"""

import pytest

from alert_log import (
    _LAYOUTS,
    ERROR_TYPES,
    AlertLogDecode,
    _rat_du,
    _rat_pcs,
    alert_view,
    apply_layout,
    pretty_alert_title,
    split_alert_name,
)

# The firmware-derived alertLog bit-layouts are not shipped; tests that need them
# run only when TM3_ALERTLOG_LAYOUTS (or a local alertlog_layouts/ file) supplies
# them. Absent -- the normal public case -- they skip.
needs_layouts = pytest.mark.skipif(
    not _LAYOUTS, reason="alertlog layouts not configured (TM3_ALERTLOG_LAYOUTS)"
)


def test_du_a094_checksum_frame():
    # can1 5A5 [8] 5E 80 A1 33 1C 00 00 00  (DIR a094)
    d = bytes([0x5E, 0x80, 0xA1, 0x33, 0x1C, 0x00, 0x00, 0x00])
    canid, etype, bv1, bv2 = _rat_du(d)
    assert canid == 0x3A1            # VCFRONT_vehicleStatus
    assert etype == 3                # CHECKSUM
    assert ERROR_TYPES[etype] == "CHECKSUM"
    assert (bv1, bv2) == (28, 0)


def test_du_a094_length_short_frame():
    # can1 5A5 [8] 5E 80 84 12 05 00 08 00  (DIR a094)
    d = bytes([0x5E, 0x80, 0x84, 0x12, 0x05, 0x00, 0x08, 0x00])
    canid, etype, bv1, bv2 = _rat_du(d)
    assert canid == 0x284            # UI_vehicleModes
    assert etype == 1                # LENGTH_SHORT
    assert (bv1, bv2) == (5, 8)


def test_pcs_layout_damien():
    # PCS handle424: errorType = byte2 & 7, canID = byte3 | byte4<<8
    d = bytes([0x1E, 0x00, 0x03, 0x21, 0x03, 0x00, 0x00, 0x00])
    canid, etype, bv1, bv2 = _rat_pcs(d)
    assert canid == 0x321
    assert etype == 3
    assert bv1 is None and bv2 is None


def test_error_type_table():
    assert ERROR_TYPES[0] == "NONE"
    assert ERROR_TYPES[4] == "SEQUENCE"
    assert ERROR_TYPES[6] == "UNKNOWN_ID"


# --- reversed field layouts (firmware-independent bit math) --------------------


@needs_layouts
def test_a162_shift_denied_layout():
    """Pin the DI_a162 packing reversed from the DIR firmware against the bench
    frame 0x527 A2 80 04 00 00 01 46 7C (a refused N->D shift)."""
    payload = int.from_bytes(bytes([0x04, 0x00, 0x00, 0x01, 0x46, 0x7C]), "little")
    v = apply_layout(_LAYOUTS[("DI", 162)], payload)
    assert v == {
        "shiftDeniedReason": 4,        # SYS_STATE_NOT_ENABLED
        "motorSpeed": 0,
        "essContClosed": 1,
        "pedalPos": 0,
        "currentGear": 3,              # DI_GEAR_N
        "requestedGear": 4,            # DI_GEAR_D
        "timeSinceCruiseCancel": 120,
        "chargeCableConnected": 1,
        "frunkOpen": 0,
    }


@needs_layouts
def test_apply_layout_sign_extends():
    """A signed field reads negative once its top bit is set (motorSpeed, 16b)."""
    # motorSpeed occupies bits 8-23; set it to -1 (all ones) and nothing else.
    payload = (0xFFFF << 8)
    v = apply_layout(_LAYOUTS[("DI", 162)], payload)
    assert v["motorSpeed"] == -1
    assert v["shiftDeniedReason"] == 0


@needs_layouts
def test_pcs_layouts_anchor():
    """PCS alertLog layouts loaded and anchored: a030 canRationality matches Damien's
    handle424 (errorType=byte2&7, canID=byte3|byte4<<8), a034 hvBusOv decodes, a029 has 4."""
    assert ("PCS", 30) in _LAYOUTS                   # a030 anchor
    a030 = _LAYOUTS[("PCS", 30)]
    assert tuple(a030["canRxErrorType"][:2]) == (0, 3)
    assert tuple(a030["canID"][:2]) == (8, 16)
    # a034 hvBusOv: single 12-bit HV_busV at bit 0
    a034 = _LAYOUTS[("PCS", 34)]
    assert tuple(next(iter(a034.values()))[:2]) == (0, 12)
    assert apply_layout(a034, 0x123)["HV_busV"] == 291
    # a029 chgPowerRationality: 4 fields
    assert len(_LAYOUTS[("PCS", 29)]) == 4


@needs_layouts
def test_layouts_artifact_loaded_and_sane():
    """The bundled alertlog_layouts_<rev>.json loads, and every layout is in-bounds and
    non-overlapping (the extractor's gate must hold for what shipped)."""
    assert len(_LAYOUTS) >= 20                       # the extracted set
    assert ("DI", 162) in _LAYOUTS                   # anchor alert present
    for (node, code), fields in _LAYOUTS.items():
        assert node in ("DI", "DIR", "PCS")
        # specs are [bit, width, signed] or [bit, width, signed, inferred]
        spans = sorted((s[0], s[0] + s[1]) for s in fields.values())
        assert spans[-1][1] <= 48, f"{node}_a{code} exceeds 48 bits"
        for i in range(1, len(spans)):
            assert spans[i][0] >= spans[i - 1][1], f"{node}_a{code} overlaps"
        for _nm, spec in fields.items():
            _b, w, signed = spec[0], spec[1], spec[2]
            if signed:                               # narrow fields never signed
                assert w >= 6
            if len(spec) > 3:                        # 4th element is the inferred flag
                assert isinstance(spec[3], bool)


@needs_layouts
def test_inferred_fill_marked_and_anchored():
    """The consecutive-packing fill supplies missing fields flagged inferred,
    while recovered fields carry no such flag."""
    # a162's fully-recovered fields are never flagged inferred
    for spec in _LAYOUTS[("DI", 162)].values():
        assert len(spec) == 3 or spec[3] is False
    # a133 badValue1 is a zero-init middle word -> inferred full word at bit 16
    a133 = _LAYOUTS.get(("DIR", 133))
    if a133:                                          # present only when layouts are configured
        bv1 = a133["badValue1"]                       # keys are field suffixes
        assert tuple(bv1[:2]) == (16, 16) and len(bv1) > 3 and bool(bv1[3])
        assert len(a133["capacitorTemp"]) == 3        # recovered, unflagged


def test_log_values_uses_decoded_layout():
    """When a frame carried a reversed layout, every field resolves (labelled)."""
    d = AlertLogDecode(
        can_id=0x527, node="DI", alert_code=162,
        log_signals=["ETH_DI_a162_shiftDeniedReason", "ETH_DI_a162_currentGear",
                     "ETH_DI_a162_motorSpeed"],
        decoded={
            "DI_a162_shiftDeniedReason": {
                "value": 4, "label": "SYS_STATE_NOT_ENABLED", "units": None},
            "DI_a162_currentGear": {"value": 3, "label": "N", "units": None},
            "DI_a162_motorSpeed": {"value": 0, "label": None, "units": "MPH"},
        })
    vals = {v["name"]: v for v in d.log_values()}
    assert vals["DI_a162_shiftDeniedReason"]["value"] == "SYS_STATE_NOT_ENABLED"
    assert vals["DI_a162_shiftDeniedReason"]["raw"] == 4
    assert vals["DI_a162_currentGear"]["value"] == "N"
    assert vals["DI_a162_motorSpeed"]["value"] == 0
    assert vals["DI_a162_motorSpeed"]["units"] == "MPH"


# --- name parsing / human titles (no firmware libs needed) --------------------


@pytest.mark.parametrize(("name", "parts"), [
    ("DIR_a094_canDataBusA", ("DIR", "a", 94, "canDataBusA")),
    ("BMS_a089_SW_VcFront_MIA", ("BMS", "a", 89, "SW_VcFront_MIA")),
    ("APP_sw191_abortReason", ("APP", "sw", 191, "abortReason")),
    ("DI_a126_limpMode", ("DI", "a", 126, "limpMode")),
    ("not_an_alert", (None, None, None, "")),
])
def test_split_alert_name(name, parts):
    assert split_alert_name(name) == parts


@pytest.mark.parametrize(("name", "title"), [
    ("DIR_a050_noStatorSensor", "No stator sensor"),
    ("DIR_a007_hwHvilNotPresent", "HW HVIL not present"),
    ("DIR_a094_canDataBusA", "CAN data bus A"),
    ("DI_a162_shiftDenied", "Shift denied"),
])
def test_pretty_alert_title(name, title):
    assert pretty_alert_title(name) == title


def test_alert_view_without_catalog():
    """No catalog record -> name-derived title, no invented prose."""
    v = alert_view("DIR_a050_noStatorSensor")
    assert v["node"] == "DIR" and v["code"] == "a050"
    assert v["title"] == "No stator sensor"
    assert v["description"] is None and v["cause"] is None
    assert v["log_signals"] == []


def test_alert_view_prefers_catalog_suffix():
    """A CAN DB name that drifted from the catalog's still titles from the catalog."""
    rec = {"name": "DIR_a050_noStatorSensor", "description": "no stator sensor available"}
    v = alert_view("DIR_a050_noStatorSensorTemp", rec)
    assert v["name"] == "DIR_a050_noStatorSensorTemp"       # what the bus called it
    assert v["catalog_name"] == "DIR_a050_noStatorSensor"
    assert v["title"] == "No stator sensor"


# --- log payload rendering ----------------------------------------------------


def test_log_values_fills_rationality_then_names():
    d = AlertLogDecode(
        can_id=0x5A5, node="DIR", alert_code=94, rationality=True,
        offending_name="VCFRONT_vehicleStatus", error_name="CHECKSUM",
        bad_value1=28, bad_value2=0,
        log_signals=["ETH_DIR_a094_canID", "ETH_DIR_a094_errorType",
                     "ETH_DIR_a094_badValue1", "ETH_DIR_a094_badValue2"])
    vals = d.log_values()
    assert [v["name"] for v in vals] == [
        "DIR_a094_canID", "DIR_a094_errorType", "DIR_a094_badValue1", "DIR_a094_badValue2"]
    assert [v["value"] for v in vals] == ["VCFRONT_vehicleStatus", "CHECKSUM", 28, 0]


def test_log_values_leaves_unreversed_fields_undecoded():
    """Only the leading reason resolves; the rest must stay None, not be guessed."""
    d = AlertLogDecode(
        can_id=0x527, node="DI", alert_code=162,
        reason_signal="DI_a162_shiftDeniedReason", reason_value=4,
        reason_label="SYS_STATE_NOT_ENABLED",
        log_signals=["ETH_DI_a162_shiftDeniedReason", "ETH_DI_a162_motorSpeed",
                     "ETH_DI_a162_currentGear"])
    vals = d.log_values()
    assert vals[0]["value"] == "SYS_STATE_NOT_ENABLED"
    assert vals[1]["value"] is None and vals[2]["value"] is None
    assert d.reason_text() == "shiftDeniedReason: SYS_STATE_NOT_ENABLED"


def test_summary_prefers_reason_over_raw_words():
    d = AlertLogDecode(can_id=0x527, node="DI", alert_code=162,
                       alert="DI_a162_shiftDenied", words=(4, 0x100, 0x7C22))
    assert d.summary() == "DI_a162_shiftDenied: [0004 0100 7C22]"
    d.reason_signal, d.reason_value = "DI_a162_shiftDeniedReason", 4
    d.reason_label = "SYS_STATE_NOT_ENABLED"
    assert d.summary() == "DI_a162_shiftDenied: shiftDeniedReason: SYS_STATE_NOT_ENABLED"
