"""Detection helpers for special multi-entry firmware groupings.

Two groupings are detected here, both used by `dfu.py phase 2` to gate
prompts and reorder the selected entry list:

* **Bootloader pairs** — `*bu` (updater) + `*bl` (image), keyed under a
  parent ECU. Flashed via the parent's UDS endpoint; bu first, then bl.
* **Subcomponents** — e.g. `cpPlcFw` and `cpPlcPib` flashed via the CP MCU
  to reach the PLC modem on the same board.
"""


# ---------------------------------------------------------------------------
# Bootloader pairs
# ---------------------------------------------------------------------------

from ._ecu_map import BL_PARENT_ECUS

# ecu_type → parent ECU node name, for both the updater (`<parent>bu`) and the
# image (`<parent>bl`). Built from the authoritative BL_PARENT_ECUS list in
# _ecu_map so the set of recognised bootloader types stays in one place.
#
# Detection is driven off this explicit dict — NOT an `endswith('bl')` string
# test — because some real app ECUs end in "bl" (e.g. `epbl`, the EPB-left
# app). `epbl` is a parent here, so its *children* `epblbu`/`epblbl` are
# recognised as bootloaders while `epbl` itself maps to nothing and is
# correctly treated as a regular app.
_BL_PARENT_NODE: dict[str, str] = {}
for _parent in BL_PARENT_ECUS:
    _BL_PARENT_NODE[f"{_parent}bu"] = _parent
    _BL_PARENT_NODE[f"{_parent}bl"] = _parent
del _parent


def is_bootloader_ecu_type(ecu_type: str) -> bool:
    """True if ecu_type names a known bootloader updater/image (`*bu`/`*bl`).

    Recognised via the explicit BL_PARENT_ECUS-derived set, so app ecu_types
    that happen to end in "bl" (e.g. `epbl`) are NOT misclassified.
    """
    return ecu_type.lower() in _BL_PARENT_NODE


def parent_node_for_bootloader(ecu_type: str) -> str | None:
    """Return the parent ECU node name for a bootloader ecu_type, or None."""
    return _BL_PARENT_NODE.get(ecu_type.lower())


def find_bootloader_entries(selected: list) -> tuple[list, list, list]:
    """Split `selected` into (bu_entries, bl_entries, app_entries).

    `bu` and `bl` lists hold the bootloader-update entries; `app_entries`
    is everything else (regular firmware, ramapp, etc.). Order within each
    list preserves the input order. Only ecu_types in the recognised
    bootloader set (see `is_bootloader_ecu_type`) are routed to bu/bl —
    real apps ending in "bl" (e.g. `epbl`) stay in `app_entries`.
    """
    bus, bls, apps = [], [], []
    for e in selected:
        ecu_type = e.component.lower()
        if ecu_type not in _BL_PARENT_NODE:
            apps.append(e)
        elif ecu_type.endswith("bu"):
            bus.append(e)
        else:  # endswith("bl"), and known
            bls.append(e)
    return bus, bls, apps


# ---------------------------------------------------------------------------
# Subcomponents
# ---------------------------------------------------------------------------

# ECU subcomponents that are flashed alongside their parent app via the parent's
# UDS endpoint. Each subcomponent selects its own flash region with a distinct
# module byte (WDBI 0x0102) before RequestDownload — see the CP PLC entries in
# _ecu_map (cpplcfw=0x08, cpplcpib=0x06); the parent bootloader validates the
# download address against the selected module's window. Maps subcomponent
# ecu_type → parent. The CP MCU's bootloader handles cpPlcFw (PLC modem firmware,
# staged in CP flash @0x100000, loaded to the QCA7420 modem at boot) and cpPlcPib
# (PLC modem Personality Identifier Block, @0xe0000).
_SUBCOMPONENT_PARENT: dict[str, str] = {
    "cpplcfw":  "cp",
    "cpplcpib": "cp",
}


def is_subcomponent_ecu_type(ecu_type: str) -> bool:
    """True if ecu_type names a subcomponent flashed via a parent ECU."""
    return ecu_type.lower() in _SUBCOMPONENT_PARENT


def parent_node_for_subcomponent(ecu_type: str) -> str | None:
    """Return the parent ECU node name for a subcomponent ecu_type, or None."""
    return _SUBCOMPONENT_PARENT.get(ecu_type.lower())


def find_subcomponent_entries(selected: list) -> tuple[list, list]:
    """Split `selected` into (subcomponent_entries, other_entries).

    Subcomponents are entries whose ecu_type is in `_SUBCOMPONENT_PARENT` (e.g.
    `cpPlcFw`, `cpPlcPib`). Order within each list preserves input order.
    """
    subs, others = [], []
    for e in selected:
        if e.component.lower() in _SUBCOMPONENT_PARENT:
            subs.append(e)
        else:
            others.append(e)
    return subs, others
