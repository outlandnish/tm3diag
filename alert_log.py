"""Decode Tesla ECU ``<NODE>_alertLog`` CAN messages.

Every Tesla ECU broadcasts a ``<NODE>_alertLog`` frame (DIR 0x5A5, PCS 0x424,
DI 0x527, VCFRONT 0x534, ...). They share one framework:

    byte0 = alert code (the multiplexer -- which alert this frame logs)
    bytes 1..7 = that alert's log payload (alert-specific fields)

Alert code N under node X names alert ``X_aNNN`` (see :mod:`so_alerts`), whose
description says what the payload means. The *CAN-rationality* alerts
(``canDataBus*`` / ``canRationality``) carry the offending CAN id + an error
type; the exact packing differs by ECU firmware family (not by silicon -- DU
and PCS are both TI C28x, they just pack their logs differently):

  * Drive Unit (DU) ECUs -- DIR, DIF, DI, PMR, PMF, PM -- pack both into
    word1 (little-endian u16 at bytes 2-3): ``canID = word1 & 0x0FFF``,
    ``errorType = (word1 >> 12) & 7``; word2/word3 = badValue1/2. Confirmed
    on-vehicle: ``5E 80 A1 33 1C 00 00 00`` -> a094 canID 0x3A1
    (VCFRONT_vehicleStatus) errorType 3 (CHECKSUM).

  * PCS packs ``errorType = byte2 & 7`` and ``canID = byte3 | byte4<<8``
    (per Damien Maguire's openinverter ``PCSCan::handle424``).

Beyond the rationality alerts, one field is decodable for ANY alert: the first
log signal. ``a094`` pins the payload's first field to bit 16 (canID occupies
bits 16-27), so whatever signal the catalog lists first for an alert starts
there too. When that signal carries a value table -- 507 alerts do, and they are
overwhelmingly the ``*Reason`` / ``*Cause`` / ``*AbortReason`` fields that say
*why* the ECU raised the alert -- its low bits can be read and labelled without
knowing the field's true width: every value the table defines fits inside the
mask implied by the table's own maximum. Values outside the table are reported
raw rather than mislabelled. Example, real bench frame::

    0x527 A2 80 04 00 00 01 22 7C
      -> DI_a162_shiftDenied, DI_a162_shiftDeniedReason = SYS_STATE_NOT_ENABLED

The remaining log signals need per-alert field widths that no shipped artifact
carries (the .so catalog has names, units and value tables but no bit layout;
compact.json and the year DBCs have no alertLog message at all). For most alerts
they are therefore listed by name with the raw payload words rather than guessed
at. For drive-unit alerts whose firmware packer was recovered by the layout
extractor every field decodes, so the a162 frame above also yields
currentGear = N, requestedGear = D, essContClosed, pedalPos and the rest.
Those layouts are firmware-derived; point ``TM3_ALERTLOG_LAYOUTS`` at a rev-tagged
``alertlog_layouts_<rev>.json`` to enable them. A checkout without one decodes the
reason and nothing further.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# NODE_<type><code>[_suffix] -- the shape shared by alert-matrix signal names in
# the CAN DB and alert names in the MCU catalog. Same grammar as so_alerts, plus
# the trailing suffix so it can be prettified into a short human title.
_ALERT_NAME_RE = re.compile(r"^([A-Z][A-Z0-9]+)_([a-z]{1,2})(\d+)(?:_(.+))?$")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")
# Words that read as acronyms in a title rather than as words. Tesla alert
# suffixes are dense with them ("hwHvilNotPresent", "canDataBusA").
_ACRONYMS = frozenset((
    "can", "hw", "sw", "hv", "lv", "dc", "ac", "hvil", "mia", "crc", "id", "ui",
    "vdc", "abs", "esp", "epb", "abs", "pcs", "bms", "di", "dir", "dif", "pm",
    "pmr", "pmf", "uds", "pwm", "adc", "dac", "soc", "ptc", "cmp", "igbt", "isc",
    "obd", "ess", "hvp", "gtw", "das", "rcm", "sccm", "opd", "ibst",
))

ERROR_TYPES = {
    0: "NONE", 1: "LENGTH_SHORT", 2: "LENGTH_LONG", 3: "CHECKSUM",
    4: "SEQUENCE", 5: "DATA_INVALID", 6: "UNKNOWN_ID",
}

# ECU firmware family -> how a CAN-rationality alert packs
# (canID, errorType, bad1, bad2). DU and PCS are both C28x silicon but their
# alertLog payloads are laid out differently.
_DU_NODES = ("DIR", "DIF", "DI", "PMR", "PMF", "PM")


def _rat_du(d: bytes):
    """Drive Unit (DIR/DIF/DI/PMR/PMF/PM): word1 packs canID(0-11) | errorType(12-14)."""
    w1 = d[2] | (d[3] << 8)
    return (w1 & 0x0FFF, (w1 >> 12) & 0x7, d[4] | (d[5] << 8), d[6] | (d[7] << 8))


def _rat_pcs(d: bytes):
    """PCS (openinverter handle424): errorType=byte2&7, canID=byte3|byte4<<8."""
    return (d[3] | (d[4] << 8), d[2] & 0x7, None, None)


# byte1 bit7: SET on every alertLog frame captured so far (a094 x2, a162), and
# the catalog declares <NODE>_alertState (CLEARED/SET) right after <NODE>_alertID.
# The raw header byte is always surfaced alongside so a different pattern is
# visible rather than silently relabelled.
ALERT_STATES = {0: "CLEARED", 1: "SET"}


_RATIONALITY = dict.fromkeys(_DU_NODES, _rat_du)
_RATIONALITY["PCS"] = _rat_pcs


# alertLog field layouts, keyed (node, code) -> {field: (bit_offset, width,
# signed)} over the log-payload area (frame bytes 2-7) as a little-endian bit
# string -- offset 0 is byte2 bit0, the same view _decode_reason takes. The
# shipped artifacts carry field names/units/value-tables but NO bit positions
# (see the module docstring), so these are recovered from the drive-unit
# firmware: each alert's snapshot packer is parsed, and the field count is
# checked against the catalog's log_signals with a non-overlap/<=48-bit gate.
# Enum and unit labelling still comes from the per-revision catalog at decode
# time, so a layout serves every revision that keeps those fields; a field the
# catalog no longer lists is simply skipped. DI_a162 shiftDenied is the anchor,
# cross-checked against bench frame A2 80 04 00 00 01 46 7C (shiftDeniedReason
# SYS_STATE_NOT_ENABLED, currentGear N, requestedGear D).
#
# The layouts themselves are firmware-derived and are NOT shipped: point
# TM3_ALERTLOG_LAYOUTS at a rev-tagged alertlog_layouts_<rev>.json, or drop such
# files in an alertlog_layouts/ dir beside this module. Each file is tagged with
# the firmware rev it was extracted from, since alertLog packing shifts between
# builds.
_LAYOUTS_DIR = Path(__file__).with_name("alertlog_layouts")


def _layouts_path() -> Path:
    """Locate the rev-tagged layouts file.

    ``TM3_ALERTLOG_LAYOUTS`` wins. Otherwise prefer the rev in ``config.FW_VERSION``
    (the bench firmware), then fall back to the newest ``alertlog_layouts_*.json``.
    """
    env = os.environ.get("TM3_ALERTLOG_LAYOUTS")
    if env:
        return Path(env)
    try:                                          # optional: match the configured rev
        import config
        rev = getattr(config, "FW_VERSION", None)
    except Exception:
        rev = None
    if rev:
        p = _LAYOUTS_DIR / f"alertlog_layouts_{rev}.json"
        if p.exists():
            return p
    tagged = sorted(_LAYOUTS_DIR.glob("alertlog_layouts_*.json"))
    return tagged[-1] if tagged else _LAYOUTS_DIR / "alertlog_layouts.json"


def _load_layouts() -> dict[tuple[str, int], dict[str, tuple[int, int, bool]]]:
    """Load the extracted layouts, if any are configured.

    Absent -- the normal case unless TM3_ALERTLOG_LAYOUTS or a local
    alertlog_layouts/ file is provided -- means no layouts, and decode falls back
    to the leading-enum reason only. Never an error: the layouts are an
    enrichment, not a dependency.
    """
    try:
        raw = json.loads(_layouts_path().read_text())
    except (OSError, ValueError):
        return {}
    out: dict[tuple[str, int], dict[str, tuple[int, int, bool]]] = {}
    for node, alerts in raw.get("layouts", {}).items():
        for code, fields in alerts.items():
            m = re.search(r"(\d+)", code)
            if not m:
                continue
            out[(node, int(m.group(1)))] = {
                nm: tuple(spec) for nm, spec in fields.items()}
    return out


_LAYOUTS = _load_layouts()


def apply_layout(layout: dict[str, tuple[int, int, bool]], payload: int) -> dict[str, int]:
    """Extract each field's (signed) integer value from a log-payload int.

    ``payload`` is frame bytes 2-7 as a little-endian integer (bit 0 == byte2
    bit0). ``layout`` maps a field's suffix name to ``(bit_offset, width,
    signed)``. Pure bit math -- no catalog needed -- so it pins the reversed
    layouts in tests independently of the firmware libs.
    """
    out: dict[str, int] = {}
    for name, spec in layout.items():
        bit, width, signed = spec[0], spec[1], spec[2]
        raw = (payload >> bit) & ((1 << width) - 1)
        if signed and raw >= (1 << (width - 1)):
            raw -= 1 << width
        out[name] = raw
    return out


def split_alert_name(name: str) -> tuple[str | None, str | None, int | None, str]:
    """``"DIR_a094_canDataBusA"`` -> ``("DIR", "a", 94, "canDataBusA")``."""
    m = _ALERT_NAME_RE.match(name or "")
    if not m:
        return None, None, None, ""
    return m.group(1), m.group(2), int(m.group(3)), m.group(4) or ""


def pretty_alert_title(name: str) -> str:
    """Short human title from the alert name's camelCase suffix.

    ``DIR_a050_noStatorSensor`` -> ``"No stator sensor"``. Always available (it
    needs no catalog), so it's the fallback title when a build's catalog has no
    record for an alert the CAN DB knows about.
    """
    _node, _t, _code, suffix = split_alert_name(name)
    if not suffix:
        return name
    words = [w for w in _CAMEL_SPLIT_RE.split(suffix) if w]
    if not words:
        return suffix
    # Acronyms read as acronyms ("hwHvil" -> "HW HVIL"); ordinary CamelCase words
    # drop to lower case so the title reads as a sentence.
    out = [w.upper() if w.lower() in _ACRONYMS else (w if w.isupper() else w[0].lower() + w[1:])
           for w in words]
    head = out[0] if out[0].isupper() else out[0][0].upper() + out[0][1:]
    return " ".join([head, *out[1:]])


def alert_view(name: str, rec: dict | None = None) -> dict:
    """UI-ready view of one alert: short title + whatever catalog text exists.

    ``rec`` is a catalog record from :mod:`so_alerts` (via
    :meth:`AlertLogDecoder.describe`); ``None`` yields the name-only view, which
    is what every consumer gets when the MCU libs aren't available.
    """
    node, ctype, code, _suffix = split_alert_name(name)
    view = {
        "name": name,
        "node": node,
        "code": f"{ctype}{code:03d}" if ctype and code is not None else None,
        "title": pretty_alert_title(name),
        "catalog_name": None,
        "description": None,
        "cause": None,
        "clear": None,
        "effect": None,
        "audience": None,
        "log_signals": [],
    }
    if rec:
        view.update(
            catalog_name=rec.get("name"),
            description=rec.get("description") or None,
            cause=rec.get("cause") or None,
            clear=rec.get("clear") or None,
            effect=rec.get("effect") or None,
            audience=rec.get("audience") or None,
            log_signals=list(rec.get("log_signals") or ()),
        )
        # A catalog name with a richer suffix than the DB's wins the title.
        if rec.get("name") and rec["name"] != name:
            view["title"] = pretty_alert_title(rec["name"])
    return view


@dataclass
class AlertLogDecode:
    can_id: int
    node: str
    alert_code: int
    alert: str | None = None          # e.g. "DIR_a094_canDataBusA"
    description: str | None = None
    words: tuple = ()                 # the 3 little-endian payload u16 words
    # Populated for CAN-rationality alerts on known ECU families:
    rationality: bool = False
    offending_id: int | None = None   # the CAN id that failed validation
    offending_name: str | None = None
    error_type: int | None = None
    error_name: str | None = None
    bad_value1: int | None = None
    bad_value2: int | None = None
    log_signals: list = field(default_factory=list)
    # <NODE>_aNNN_<field> -> {"value": int, "label": str|None, "units": str|None}
    # for alerts whose bit layout has been reversed (see _LAYOUTS). Empty for the
    # rationality/reason-only paths.
    decoded: dict = field(default_factory=dict)
    view: dict = field(default_factory=dict)  # alert_view() of this alert
    header: int = 0                   # byte1 verbatim (state + unknown bits)
    state: str | None = None          # "SET" / "CLEARED" from byte1 bit7
    # The leading log signal, when it's an enum -- the alert's reason/cause.
    reason_signal: str | None = None
    reason_value: int | None = None
    reason_label: str | None = None   # None when the raw value isn't in the table

    def reason_text(self) -> str | None:
        """``"shiftDeniedReason: SYS_STATE_NOT_ENABLED"``, or None if undecoded.

        A field literally named ``reason``/``cause`` adds nothing as a prefix
        (``DIR_a090_reason`` -> just ``TOOSLOW``), so it's dropped.
        """
        if self.reason_signal is None:
            return None
        value = self.reason_label or self.reason_value
        short = split_alert_name(self.reason_signal)[3] or self.reason_signal
        if short.lower() in ("reason", "cause"):
            return str(value)
        return f"{short}: {value}"

    def _rat_value_for(self, suffix: str):
        """The rationality value that belongs to a log field, matched by NAME.

        The four rationality fields (offending id, error type, bad1, bad2) appear
        in different orders across builds and ECU families -- DIR ``a094`` lists
        ``[canID, errorType, ...]`` but ``a066`` lists ``[badValue1, badValue2,
        canID, errorType]``, and PCS ``a030`` lists ``[canRxErrorType, canID]``.
        Pairing by position mislabels all but one order, so match on the suffix.
        """
        s = (suffix or "").lower()
        if "errortype" in s or "rxerror" in s or "errorreason" in s:
            return self.error_name if self.error_name is not None else self.error_type
        if s == "canid" or s.endswith("canid") or s == "id":
            return self.offending_name or self.offending_id
        if "badvalue1" in s:
            return self.bad_value1
        if "badvalue2" in s:
            return self.bad_value2
        return None

    def log_values(self) -> list[dict]:
        """The alert's logged signals paired with whatever we can decode.

        Three tiers: an alert with a reversed bit layout (see ``_LAYOUTS``)
        resolves every field, labelled from the catalog and carrying its raw
        value + units; a CAN-rationality alert resolves its fields (matched to the
        offending id / error type / bad values by NAME, since their order varies);
        any other alert resolves its leading enum (the reason) and lists the rest
        by name with ``value: None`` -- the UI can still say *what* the payload
        words mean without inventing bit positions for them.
        """
        out = []
        for i, sig in enumerate(self.log_signals):
            short = sig.removeprefix("ETH_")
            entry: dict = {"name": short, "value": None}
            dv = self.decoded.get(short)
            if dv is not None:
                entry["value"] = dv["label"] if dv["label"] is not None else dv["value"]
                if dv["label"] is not None:
                    entry["raw"] = dv["value"]
                if dv.get("units"):
                    entry["units"] = dv["units"]
                if dv.get("inferred"):
                    entry["inferred"] = True    # value from a packing-convention fill
            elif self.rationality:
                rv = self._rat_value_for(split_alert_name(short)[3] or short)
                if rv is not None:
                    entry["value"] = rv
                elif i == 0 and self.reason_signal is not None:
                    entry["value"] = self.reason_label or self.reason_value
            elif i == 0 and self.reason_signal is not None:
                entry["value"] = self.reason_label or self.reason_value
            out.append(entry)
        return out

    def to_dict(self) -> dict:
        """JSON-friendly decode (what tm3web serves to the alert panels)."""
        d = dict(self.view) if self.view else alert_view(
            self.alert or f"{self.node}_a{self.alert_code:03d}")
        d.update(
            can_id=self.can_id,
            ecu=self.node,
            alert_code=self.alert_code,
            summary=self.summary(),
            words=list(self.words),
            header=self.header,
            state=self.state,
            reason_signal=self.reason_signal,
            reason_value=self.reason_value,
            reason_label=self.reason_label,
            reason_text=self.reason_text(),
            rationality=self.rationality,
            offending_id=self.offending_id,
            offending_name=self.offending_name,
            error_type=self.error_type,
            error_name=self.error_name,
            bad_value1=self.bad_value1,
            bad_value2=self.bad_value2,
            log_values=self.log_values(),
        )
        return d

    def summary(self) -> str:
        base = self.alert or f"{self.node}_a{self.alert_code:03d}"
        if self.rationality and self.offending_id is not None:
            who = self.offending_name or f"0x{self.offending_id:03X}"
            bad = (f", bad={self.bad_value1}/{self.bad_value2}"
                   if self.bad_value1 is not None else "")
            return f"{base}: {who} ({self.error_name}{bad})"
        reason = self.reason_text()
        if reason:
            return f"{base}: {reason}"
        w = " ".join(f"{x:04X}" for x in self.words)
        return f"{base}: [{w}]"


class AlertLogDecoder:
    """Loads the alert + CAN catalogs once and decodes alertLog frames."""

    def __init__(self, lib_dir: str | Path | None = None):
        self.alertlog_node: dict[int, str] = {}   # can_id -> node
        # (node, code_type, code) -> rec. Keyed on the type too: DIR_a094 and a
        # hypothetical DIR_w094 are different alerts that share a number.
        self.alert_by_nc: dict[tuple[str, str, int], dict] = {}
        self.alert_by_name: dict[str, dict] = {}   # exact catalog name -> rec
        self.msg_name: dict[int, str] = {}         # can_id -> message name
        # <NODE>_aNNN_<field> -> that log signal's catalog entry (units +
        # value_description). Bit layout is NOT in the catalog; see the module
        # docstring for what that does and doesn't allow.
        self.log_signal: dict[str, dict] = {}
        self._load(lib_dir)

    def _load(self, lib_dir):
        if lib_dir is None:
            import config as _cfg
            if _cfg.ROOT is None:
                raise RuntimeError("no lib_dir and config.ROOT (TM3_ROOT) unset")
            lib_dir = Path(_cfg.ROOT) / "usr/tesla/UI/lib"
        lib_dir = Path(lib_dir)
        import so_alerts
        import so_candata
        cat = so_candata.extract_catalog(lib_dir / "libQtCarCANData.so")
        for mn, m in cat.messages.items():
            self.msg_name[m["message_id"]] = mn
            if mn.endswith("_alertLog"):
                self.alertlog_node[m["message_id"]] = mn[:-len("_alertLog")]
                self.log_signal.update(m.get("signals", {}))
        for name, a in so_alerts.extract_alerts(
                lib_dir / "libQtCarAlerts.so").alerts.items():
            rec = dict(a, name=name)
            self.alert_by_name[name] = rec
            if a.get("node") is not None and a.get("code") is not None:
                key = (a["node"], a.get("code_type") or "a", a["code"])
                self.alert_by_nc.setdefault(key, rec)

    def is_alertlog(self, can_id: int) -> bool:
        return can_id in self.alertlog_node

    def describe(self, name: str) -> dict | None:
        """Catalog record for an alert name, or None if this build has none.

        Falls back from the exact name to (node, type, code) so a CAN DB whose
        suffix drifted from the catalog's -- different firmware revisions name
        the same alert number slightly differently -- still resolves.
        """
        rec = self.alert_by_name.get(name)
        if rec is not None:
            return rec
        node, ctype, code, _suffix = split_alert_name(name)
        if node is None:
            return None
        return self.alert_by_nc.get((node, ctype, code))

    def view(self, name: str) -> dict:
        """:func:`alert_view` for *name*, enriched from this build's catalog."""
        return alert_view(name, self.describe(name))

    def _decode_reason(self, out: AlertLogDecode, payload: int) -> None:
        """Decode the leading log signal when it's an enum -- the alert's *reason*.

        ``payload`` is bytes 2-7 as a little-endian integer, i.e. the log field
        area with bit 0 at frame bit 16 (where a094's canID starts). The first
        signal therefore sits at offset 0. Its true width isn't published, but
        every value its table defines fits within the table maximum's bit width,
        so masking to that width reads the value correctly; anything outside the
        table is left unlabelled rather than guessed.
        """
        if not out.log_signals:
            return
        first = out.log_signals[0].removeprefix("ETH_")
        meta = self.log_signal.get(first)
        vd = (meta or {}).get("value_description")
        if not vd:
            return
        width = max(1, max(vd.values()).bit_length())
        raw = payload & ((1 << width) - 1)
        out.reason_signal = first
        out.reason_value = raw
        for label, val in vd.items():
            if val == raw:
                out.reason_label = label
                break

    def _decode_layout(self, out: AlertLogDecode, payload: int) -> None:
        """Fully decode an alert whose field bit-layout has been reversed.

        ``payload`` is bytes 2-7 as a little-endian int (bit 0 == byte2 bit0).
        Fields absent from the layout -- or from this build's catalog -- are left
        undecoded; enum and unit labels come from the catalog so the decode stays
        correct per firmware revision.
        """
        layout = _LAYOUTS.get((out.node, out.alert_code))
        if not layout:
            return
        values = apply_layout(layout, payload)
        for sig in out.log_signals:
            short = sig.removeprefix("ETH_")
            fname = split_alert_name(short)[3] or short
            if fname not in values:
                continue
            raw = values[fname]
            meta = self.log_signal.get(short) or {}
            vd = meta.get("value_description") or {}
            label = next((lbl for lbl, val in vd.items() if val == raw), None)
            spec = layout[fname]
            out.decoded[short] = {
                "value": raw, "label": label, "units": meta.get("units"),
                # 4th spec element flags a value inferred from the packing
                # convention (a zero-init word / missing aggregator bit), not
                # recovered from a store -- lower confidence.
                "inferred": bool(len(spec) > 3 and spec[3])}

    def decode(self, can_id: int, data: bytes) -> AlertLogDecode | None:
        node = self.alertlog_node.get(can_id)
        if node is None or len(data) < 2:
            return None
        d = bytes(data) + b"\x00" * (8 - len(data))
        code = d[0]
        words = (d[2] | d[3] << 8, d[4] | d[5] << 8, d[6] | d[7] << 8)
        out = AlertLogDecode(can_id=can_id, node=node, alert_code=code, words=words)
        out.header = d[1]
        out.state = ALERT_STATES.get((d[1] >> 7) & 1)
        rec = self.alert_by_nc.get((node, "a", code))
        if rec:
            out.alert = rec.get("name")
            out.description = rec.get("description")
            out.log_signals = rec.get("log_signals", [])
        out.view = alert_view(out.alert or f"{node}_a{code:03d}", rec)
        # CAN-rationality alerts carry the offending id + error type.
        is_rat = bool(rec) and (
            "canData" in (out.alert or "") or "canRationality" in (out.alert or "")
            or "_canRationality" in (out.alert or ""))
        packer = _RATIONALITY.get(node)
        if is_rat and packer:
            cid, etype, bv1, bv2 = packer(d)
            out.rationality = True
            out.offending_id = cid
            out.offending_name = self.msg_name.get(cid)
            out.error_type = etype
            out.error_name = ERROR_TYPES.get(etype, f"?{etype}")
            out.bad_value1, out.bad_value2 = bv1, bv2
        else:
            # Read the leading enum (the reason) for every alert, then fully
            # decode the fields for alerts whose bit layout has been reversed.
            payload = int.from_bytes(d[2:8], "little")
            self._decode_reason(out, payload)
            self._decode_layout(out, payload)
        return out


_DEFAULT: AlertLogDecoder | None = None


def get_decoder(lib_dir=None) -> AlertLogDecoder:
    """Process-wide cached decoder (built from config.ROOT libs by default)."""
    global _DEFAULT
    if _DEFAULT is None or lib_dir is not None:
        dec = AlertLogDecoder(lib_dir)
        if lib_dir is None:
            _DEFAULT = dec
        return dec
    return _DEFAULT


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Decode a Tesla alertLog frame")
    ap.add_argument("can_id", help="hex CAN id, e.g. 5A5")
    ap.add_argument("data", help="8 hex bytes, e.g. 5E80A1331C000000 or '5E 80 ..'")
    ap.add_argument("--lib-dir", help="override UI lib dir")
    a = ap.parse_args()
    cid = int(a.can_id, 16)
    raw = bytes.fromhex(a.data.replace(" ", ""))
    dec = get_decoder(a.lib_dir)
    res = dec.decode(cid, raw)
    if res is None:
        print(f"0x{cid:X} is not a known *_alertLog message")
    else:
        print(res.summary())
        if res.description:
            print(f"  {res.description}")
