"""Vet ``[scenario.<NODE>]`` keys that set one DBC signal directly against the loaded CAN DB.

For each such key the signal must exist in the loaded database and the value must be one the
DBC defines for it. Values may be the sim's option names (``"rolls"``) or the DBC's value names
(``"ROLLS_MODE_SELECTED"``); keys may be the sim key or the DBC signal name. Names match ignoring
case and underscores. A sim option whose raw value the DBC names differently is warned about,
since that usually means the sim's table is wrong for this revision.

Keys that fan out to several signals or feed logic (ESP brake, BMS/HVP mode, SCCM gear, ...)
are passed through untouched; their nodes validate them. GTW car-config keys go through the
DBC already (gtw.py).
"""

from __future__ import annotations

from tesla_frames import UI_SETTINGS, VEHICLE_POWER_STATE

BOOL = "bool"  # 0/1 signal; the node takes a Python bool
_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"0", "off", "false", "no"}


def _esp_tables():
    from esp.esp import _ABS_EVENT, _STABILITY  # noqa: PLC0415 -- node module, lazy

    return _ABS_EVENT, _STABILITY


def _direct() -> dict[str, dict[str, tuple[str, object]]]:
    """node -> key -> (DBC signal, sim option table {name: raw} or BOOL)."""
    ui = {
        key: (sig, UI_SETTINGS[key]["options"])
        for key, sig in (
            ("pedal_map", "UI_pedalMap"),
            ("stopping_mode", "UI_stoppingMode"),
            ("motor_on_mode", "UI_motorOnMode"),
            ("traction_mode", "UI_tractionControlMode"),
            ("winch_mode", "UI_winchModeRequest"),
            ("track_mode", "UI_trackModeRequest"),
            ("trailer_mode", "UI_trailerMode"),
            ("service_mode", "UI_serviceMode"),
            ("development_car", "UI_developmentCar"),
        )
    }
    ui["charge_enable"] = ("UI_chargeEnableRequest", BOOL)
    abs_event, stability = _esp_tables()
    return {
        "UI": ui,
        "ESP": {
            "abs_event": ("ESP_absBrakeEvent2", abs_event),
            "stability": ("ESP_stabilityControlSts2", stability),
            "standstill_skid": ("ESP_ebrStandstillSkid", BOOL),
            "abs_fault_lamp": ("ESP_absFaultLamp", BOOL),
            "ebd_fault_lamp": ("ESP_ebdFaultLamp", BOOL),
            "esp_fault_lamp": ("ESP_espFaultLamp", BOOL),
        },
        "VCFRONT": {
            "lv_power_state": ("VCFRONT_vehiclePowerState", VEHICLE_POWER_STATE),
            "hv_charge_enable": ("VCFRONT_bmsHvChargeEnable", BOOL),
        },
    }


def norm(name: str) -> str:
    return str(name).replace("_", "").lower()


def _signal_index(db) -> dict[str, dict]:
    return {sn: s for msg in db.messages.values() for sn, s in msg["signals"].items()}


def _raw(node: str, key: str, value, signal: str, table, vd: dict) -> tuple[int, str | None]:
    """Resolve a TOML value to (raw, sim option name or None)."""
    if table is BOOL:
        if isinstance(value, bool):
            return int(value), None
        v = norm(value)
        if v in _TRUE:
            return 1, None
        if v in _FALSE:
            return 0, None
        for label, raw in vd.items():
            if norm(label) == v:
                return int(raw), None
        raise ValueError(f"{node}.{key}: {value!r} is not on/off or a {signal} value {list(vd)}")
    if isinstance(value, int) and not isinstance(value, bool):
        return value, None
    v = norm(value)
    for name, raw in table.items():
        if norm(name) == v:
            return raw, name
    for label, raw in vd.items():
        if norm(label) == v:
            return int(raw), None
    raise ValueError(
        f"{node}.{key}: {value!r} is not one of {list(table)} or {signal} {list(vd)}"
    )


def vet(node: str, settings: dict, db) -> tuple[dict, list[str]]:
    """Return (settings for node.configure, report lines). Raises ValueError on a mismatch."""
    spec = _direct().get(node)
    if not spec:
        return settings, []
    by_signal = {norm(sig): key for key, (sig, _t) in spec.items()}
    index = _signal_index(db)
    out: dict = {}
    lines: list[str] = []
    for key, value in settings.items():
        key = key if key in spec else by_signal.get(norm(key), key)
        if key not in spec:
            out[key] = value
            continue
        signal, table = spec[key]
        sig = index.get(signal)
        if sig is None:
            lines.append(f"  WARNING {node}.{key}: {signal} is not in the loaded CAN database")
            out[key] = value
            continue
        vd = sig.get("value_description") or {}
        raw, sim_name = _raw(node, key, value, signal, table, vd)
        if not 0 <= raw < (1 << sig["width"]):
            raise ValueError(f"{node}.{key}: {raw} does not fit {signal} ({sig['width']} bits)")
        by_raw = {int(r): label for label, r in vd.items()}
        if vd and raw not in by_raw:
            raise ValueError(f"{node}.{key}: {raw} is not a {signal} value {by_raw}")
        dbc_label = by_raw.get(raw, "")
        if table is BOOL:
            out[key] = bool(raw)
        else:
            names = [n for n, r in table.items() if r == raw]
            if not names:
                raise ValueError(
                    f"{node}.{key}: {signal} {raw} ({dbc_label}) is valid but the sim has no "
                    f"option for it; options: {table}"
                )
            out[key] = sim_name or names[0]
            if sim_name and dbc_label and norm(sim_name) not in norm(dbc_label) \
                    and norm(sim_name) not in _TRUE | _FALSE:
                lines.append(
                    f"  WARNING {node}.{key}: sim option {sim_name!r} = {raw}, which the DBC "
                    f"names {dbc_label}"
                )
        lines.append(f"  scenario {node}.{key} = {value!r} -> {signal} {raw} {dbc_label}".rstrip())
    return out, lines
