"""Headless flashing: pick firmware by COMPONENT NAME and flash it, no prompts.

dfu.py's flow is interactive by design -- it reads an ECU's identity, offers the
matching firmware, and asks before touching flash. ODIN drives the same thing
non-interactively: `Gen3/scripts/UPDATE_MODULE` shells out to the MCU's flasher
as

    /sbin/smashclicker -h <hwidacq list> -u <update list> -j <job id>

where `-h` names the ECU whose identity is read and `-u` names the images to
write -- `UPDATE_PMR` is `-h pmr -u pmr,dir`. Those component names are exactly
FirmwareEntry.component, so the two models join without a translation table.

This module is the seam odin_runner's BenchBackend calls in smashclicker's place
(see BenchBackend.flash_module). It reuses dfu's own selection and flash phases;
what it adds is choosing entries by component instead of by prompt, and saying
precisely which requested components it could not place -- an unknown component
is reported, never quietly skipped, because a flasher that silently does nothing
is the failure mode this whole path exists to remove.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

from ._dual_cpu import find_dual_cpu_pair, secondary_fallback_module_byte
from ._ecu_map import get_script
from ._groups import (
    find_bootloader_entries,
    parent_node_for_bootloader,
    parent_node_for_subcomponent,
)


class QuietDisplay:
    """StatusDisplay's interface with no terminal control.

    The flash scripts narrate through a display; driven from ODIN there is no
    terminal to narrate to, so the lines are collected instead and handed back
    as the run's log. `on_event(kind, payload)` -- when given -- forwards them
    live, so a flash that takes minutes reports where it is instead of going
    quiet: 'status' for each step, 'progress' for the transfer's byte counts.
    """

    # A 216 KB image is ~850 TransferData blocks. Emitting one event per block
    # floods the listener -- and where that listener is a websocket pump the run
    # cannot finish until the whole backlog has drained, so the operator watches
    # a completed flash sit there. Report on a real change or a real interval,
    # never per block.
    _PROGRESS_MIN_DELTA = 1.0     # percent
    _PROGRESS_MIN_INTERVAL = 0.25  # seconds

    def __init__(self, on_event=None, label: str = "") -> None:
        self.lines: list[str] = []
        self._on_event = on_event
        self.label = label
        self._last_pct: float | None = None
        self._last_at = 0.0

    def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        # A listener must never fail the flash it is only watching.
        with contextlib.suppress(Exception):
            self._on_event(kind, {"source": self.label or "flash", **payload})

    def set_header(self, header: str) -> None:
        self.lines.append(str(header))
        self._last_pct = None       # a new image restarts the progress span
        self._emit("status", {"status": str(header)})

    def set_detail(self, detail: str) -> None:
        self.lines.append(f"  {detail}")
        self._emit("status", {"status": str(detail)})

    def set_progress(self, current: int, total: int, label: str = "") -> None:
        # Deliberately NOT recorded in `lines`: it is one step redrawing, not a
        # step of its own.
        pct = round(100.0 * current / total, 1) if total else None
        now = time.monotonic()
        done = total and current >= total
        if (pct is not None and self._last_pct is not None and not done
                and pct - self._last_pct < self._PROGRESS_MIN_DELTA
                and now - self._last_at < self._PROGRESS_MIN_INTERVAL):
            return
        self._last_pct, self._last_at = pct, now
        self._emit("progress", {"current": int(current), "total": int(total),
                                "value": pct, "label": label, "units": "bytes"})

    def start_step(self, label: str) -> None:
        """Begin a new progress span, so its first report is not throttled."""
        self._last_pct = None

    def finalize(self) -> None:
        pass


def uds_node_for(component: str, default: str | None = None) -> str | None:
    """The ECU a component is flashed THROUGH.

    Usually itself, but a bootloader image goes through its parent app's node
    (`vcsecbl` -> VCSEC) and a subcomponent through the ECU that gateways it
    (`lumbarl` -> VCLEFT, `hcml` -> VCFRONT) -- which is why ODIN carries a
    separate `node_to_lock` alongside the component list.
    """
    key = component.lower()
    node = (parent_node_for_bootloader(key)
            or parent_node_for_subcomponent(key)
            or default
            or key)
    return node.upper()   # node names are upper-case everywhere in the runner


def _image_of(entry) -> tuple:
    """What actually gets written -- conditions excluded on purpose."""
    return (entry.component.lower(), entry.src_path, entry.dest_name, entry.crc)


def select_entries(entries: list, components) -> tuple[list, list, list]:
    """(selected, unplaced, ambiguous) for `components`.

    ONE entry per component. The signed metadata carries a row per condition
    combination, and most of those rows name the SAME image: pmr:440467458 has
    two `pmr` rows differing only in vdcType, both pointing at the same .bhx.
    Taking every match would
    flash that ECU twice in a row -- which is not harmless, since a flash burns
    a count against FLASH_COUNT_LIMITS -- so identical images collapse to one.

    Rows that name DIFFERENT images for the same component are a real choice we
    cannot make (dfu prompts for it); those come back as `ambiguous` rather than
    picking one arbitrarily. Order follows `components`, preserving ODIN's
    bu -> bl -> app sequence; find_bootloader_entries re-imposes it anyway.
    """
    by_component: dict[str, list] = {}
    for e in entries:
        by_component.setdefault(e.component.lower(), []).append(e)
    selected, unplaced, ambiguous = [], [], []
    for name in components:
        found = by_component.get(str(name).lower())
        if not found:
            unplaced.append(str(name))
            continue
        images = {_image_of(e): e for e in found}
        if len(images) > 1:
            ambiguous.append(str(name))
            continue
        selected.append(next(iter(images.values())))
    return selected, unplaced, ambiguous


def condition_choices(matches: list, components, conditions=None,
                      label_map: dict | None = None) -> list[dict]:
    """The car-config choices that actually decide THIS flash.

    Only the keys whose value differs across the candidate rows are asked about:
    the map has nineteen condition keys, but for one ECU's identity at most a
    couple of them change which image applies. Values carry the firmware's own
    labels where a source names them (vdcType 0 -> BOSCH_VDC), so the operator
    picks a car, not a number; a key nothing names keeps its raw value, which is
    still answerable -- see uds_local.condition_labels.
    """
    from uds_local.condition_labels import labels_for

    wanted = {str(c).lower() for c in components}
    rows = [e for e in matches if e.component.lower() in wanted]
    labels = label_map or {}
    out = []
    for key in _varying_keys(rows):
        table = labels_for(labels, key)
        values = sorted({e.conditions[key] for e in rows if key in e.conditions})
        out.append({
            "key": key,
            "value": (conditions or {}).get(key),
            "options": [{"value": v, "label": table.get(v) or v} for v in values],
        })
    return out


def _varying_keys(rows: list) -> list[str]:
    from uds_local.metadata import varying_condition_keys
    return varying_condition_keys(rows)


def bootloader_choice(selected: list, include: bool) -> dict:
    """What the bootloader entries in `selected` are, and what skipping them means.

    dfu.py asks this at the terminal (_prompt_bootloader_choice) and defaults to
    NO, because there a bootloader turns up as a surprise from metadata matching.
    Driven from ODIN it is the opposite: an operator who ran
    UPDATE_VCSEC-WITH-BOOTLOADER asked for one by name, so the default is to
    honour that and the preflight dialog is where it gets confirmed -- silently
    downgrading a -WITH-BOOTLOADER run to a plain app update would be the same
    quiet no-op this path exists to remove.

    Either way the consequence is spelled out rather than left to be recalled:
    after bu+bl the app slot holds the update agent, so whether a regular app
    reflashes afterwards decides what the ECU boots.
    """
    bus, bls, apps = find_bootloader_entries(selected)
    entries = bus + bls
    if not entries:
        return {"available": [], "included": bool(include), "note": None}
    warn = ("Bootloader flashing can brick the ECU if interrupted. After bu+bl "
            "the app slot holds the update agent")
    if apps:
        note = f"{warn}; these restore it: {', '.join(e.dest_name for e in apps)}."
    else:
        note = (f"{warn}, and no app entry follows to restore it -- the ECU "
                "boots the update agent until one is flashed.")
    return {
        "available": [
            {"component": e.component, "dest": e.dest_name,
             "kind": "updater" if e in bus else "image",
             "parent": parent_node_for_bootloader(e.component.lower())}
            for e in entries
        ],
        "included": bool(include),
        "note": note,
    }


RAMAPP_MODES = ("include", "skip", "only")


def ramapp_choice(selected: list, mode: str = "include") -> dict:
    """The RAM apps in `selected`, and how they are to be flashed.

    Three states rather than a checkbox, because "just the RAM app" is a real
    bench request: a RAM app is not part of a normal app update, so pushing one
    WITHOUT rewriting the app it rides on is often exactly what is wanted.
    dfu.py offers the same three.

    `only` is offered solely when there is something else to exclude -- when the
    procedure names nothing but RAM apps, "include" and "only" are the same list
    and a choice between them is a choice about nothing.
    """
    from ._groups import find_ramapp_entries

    rams, others = find_ramapp_entries(selected)
    if not rams:
        return {"available": [], "mode": "include", "options": [], "note": None}

    note = ("RAM apps run from RAM and are not part of a normal app update.")
    # The OPC caveat is the one that actually costs time on a bench: pushing
    # pmramapp to service an OPC is a wasted step on gen26, where the CAN<->LIN
    # gateway is already resident in the PMR app -- opc/opcs flash THROUGH it.
    if any(e.component.lower() in ("pmramapp", "pmsramapp") for e in rams):
        note += (" The OPC CAN<->LIN gateway is already resident in the gen26 "
                 "PMR app, so this is generally not needed to service an OPC -- "
                 "flash opc/opcs directly instead.")
    options = [
        {"value": "include", "label": "Write them, as the procedure names"},
        {"value": "skip", "label": "Skip the RAM apps"},
    ]
    if others:
        options.append({"value": "only", "label": "RAM apps only"})
    valid = {o["value"] for o in options}
    return {
        "available": [{"component": e.component, "dest": e.dest_name} for e in rams],
        "mode": mode if mode in valid else "include",
        "options": options,
        "note": note,
    }


def flash_components(
    sess,
    artifacts_dir: Path | str,
    ecu_name: str,
    components,
    *,
    channel: str | None = None,
    interface: str | None = None,
    display=None,
    conditions: dict | None = None,
    dry_run: bool = False,
    on_event=None,
    label_map: dict | None = None,
    include_bootloaders: bool = True,
    ramapps: str = "include",
) -> dict:
    """Flash `components` onto `ecu_name`, reading its identity to pick firmware.

    Returns {'flashed': [...], 'unplaced': [...], 'ambiguous': [...],
             'no_script': [...], 'log': [...], 'ok': bool}. `unplaced` are
    components the signed metadata has no entry for at this ECU's identity;
    `ambiguous` are ones whose rows name more than one image, a choice dfu
    prompts for and this cannot make; `no_script` are ones dfu has firmware for
    but no validated flash sequence. Any of them leaves ok=False -- a partial
    flash is not a success.

    `include_bootloaders=False` drops the bu/bl entries, and `ramapps` in
    ('skip', 'only') narrows the RAM apps. Those are narrowings the operator
    ASKED for, not components that could not be placed, so they land in
    `excluded` and leave ok=True -- see bootloader_choice for why the defaults
    run the other way from dfu's.
    """
    from uds_local.identity import parse_f180
    from uds_local.metadata import find_firmware, load_metadata

    display = display or QuietDisplay(on_event, label=ecu_name)
    artifacts_dir = Path(artifacts_dir)
    tsv = artifacts_dir / "signed_metadata_map.tsv"
    if not tsv.exists():
        raise FileNotFoundError(f"signed_metadata_map.tsv not found in {artifacts_dir}")

    ident = parse_f180(sess.read_did(0xF180), ecu_name)
    display.set_header(f"identity {ecu_name} key={ident.lookup_key}")
    entries = load_metadata(tsv)
    # Unfiltered first: the choices an operator can still make are read off the
    # candidate rows BEFORE the car config narrows them.
    all_matches = find_firmware(entries, ecu_name, ident.packed_key)
    matches = find_firmware(entries, ecu_name, ident.packed_key, conditions)
    selected, unplaced, ambiguous = select_entries(matches, components)

    # The bootloader and RAM-app questions, answered BEFORE the plan is built so
    # what the dialog shows is what would actually be written. Order follows
    # dfu's: bootloaders, then RAM apps.
    bootloaders = bootloader_choice(selected, include_bootloaders)
    excluded: list[str] = []
    if bootloaders["available"] and not include_bootloaders:
        bus, bls, apps = find_bootloader_entries(selected)
        excluded = [e.component for e in bus + bls]
        selected = apps

    ramapp = ramapp_choice(selected, ramapps)
    if ramapp["available"] and ramapp["mode"] != "include":
        from ._groups import find_ramapp_entries
        rams, others = find_ramapp_entries(selected)
        dropped, selected = ((rams, others) if ramapp["mode"] == "skip"
                             else (others, rams))
        excluded += [e.component for e in dropped]

    # An entry we have firmware for but no validated sequence must not be
    # attempted; report it rather than guessing at a flash script.
    runnable, no_script = [], []
    for e in selected:
        try:
            get_script(e.component.lower())
        except KeyError:
            no_script.append(e.component)
        else:
            runnable.append(e)

    flashed = [e.component for e in runnable]
    # Nothing is written if any requested component could not be placed: a
    # half-flashed set is a worse state to leave an ECU in than an untouched one.
    blocked = unplaced or ambiguous or no_script
    plan = [{"component": e.component, "image": e.src_path, "dest": e.dest_name,
             "crc": e.crc, "conditions": dict(e.conditions)} for e in runnable]
    # The plan goes out BEFORE anything is written, so what is about to be
    # flashed -- and the car config that chose it -- is visible, not inferred
    # afterwards from a log.
    if on_event is not None:
        with contextlib.suppress(Exception):
            on_event("flash_plan", {"ecu": ecu_name, "identity": ident.lookup_key,
                                    "conditions": dict(conditions or {}),
                                    "plan": plan, "blocked": list(blocked),
                                    "excluded": list(excluded),
                                    "dry_run": bool(dry_run or blocked)})
    if not dry_run and runnable and not blocked:
        _flash(sess, artifacts_dir, runnable, display, channel, interface)
    elif blocked:
        flashed = []

    return {
        "flashed": flashed,
        "unplaced": unplaced,
        "ambiguous": ambiguous,
        "no_script": no_script,
        "excluded": excluded,
        "bootloaders": bootloaders,
        "ramapps": ramapp,
        "identity": ident.lookup_key,
        "conditions": dict(conditions or {}),
        "choices": condition_choices(all_matches, components, conditions, label_map),
        "plan": plan,
        "log": list(getattr(display, "lines", [])),
        "ok": not blocked and bool(flashed),
    }


def _flash(sess, artifacts_dir: Path, selected: list, display,
           channel: str | None, interface: str | None) -> None:
    """dfu's phase-4 sequence: bu, then bl, then apps -- with the dual-CPU pair
    (PCS/PM family) run through its own two-CPU path after any bootloader."""
    import dfu

    bus, bls, apps = find_bootloader_entries(selected)
    pair = find_dual_cpu_pair(apps)
    singles = [e for e in bus + bls + apps if pair is None or e not in pair]

    for entry in singles:
        display.set_header(f"flash {entry.dest_name}")
        ecu_type = entry.component.lower()
        script, module_byte = get_script(ecu_type)
        script.module_byte = module_byte
        script.run(
            sess,
            dfu._parse_firmware(artifacts_dir / entry.src_path),
            entry,
            channel=channel,
            interface=interface,
            display=display,
            fallback_module_byte=secondary_fallback_module_byte(ecu_type),
        )

    if pair is not None:
        from ._dual_cpu import run_pcs_dual_cpu
        primary, secondary = pair
        display.set_header(f"flash (dual-CPU) {primary.dest_name} + {secondary.dest_name}")
        run_pcs_dual_cpu(
            sess,
            dfu._parse_firmware(artifacts_dir / primary.src_path), primary,
            dfu._parse_firmware(artifacts_dir / secondary.src_path), secondary,
            display=display,
        )
