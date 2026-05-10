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

# Suffix → parent ECU node name for bootloader nodes. Used by phase 2 to
# verify that the user's --node argument can drive the bootloader flash, and
# by display logic to group bu+bl with the parent app entry.
_BL_PARENT_NODE: dict[str, str] = {
    "parkbu":     "park",
    "parkbl":     "park",
    "hvbmsbu":    "hvbms",
    "hvbmsbl":    "hvbms",
    "hvpbu":      "hvp",
    "hvpbl":      "hvp",
    "vcfrontbu":  "vcfront",
    "vcfrontbl":  "vcfront",
    "pcsbu":      "pcs",
    "pcsbl":      "pcs",
    "pcscpu2bu":  "pcs",
    "pcscpu2bl":  "pcs",
}


def is_bootloader_ecu_type(ecu_type: str) -> bool:
    """True if ecu_type names a bootloader updater/image (bu/bl)."""
    t = ecu_type.lower()
    return t.endswith("bu") or t.endswith("bl")


def parent_node_for_bootloader(ecu_type: str) -> str | None:
    """Return the parent ECU node name for a bootloader ecu_type, or None."""
    return _BL_PARENT_NODE.get(ecu_type.lower())


def find_bootloader_entries(selected: list) -> tuple[list, list, list]:
    """Split `selected` into (bu_entries, bl_entries, app_entries).

    `bu` and `bl` lists hold the bootloader-update entries; `app_entries`
    is everything else (regular firmware, ramapp, etc.). Order within each
    list preserves the input order.
    """
    bus, bls, apps = [], [], []
    for e in selected:
        ecu_type = e.component.lower()
        if ecu_type.endswith("bu"):
            bus.append(e)
        elif ecu_type.endswith("bl"):
            bls.append(e)
        else:
            apps.append(e)
    return bus, bls, apps


# ---------------------------------------------------------------------------
# Subcomponents
# ---------------------------------------------------------------------------

# ECU subcomponents that are flashed alongside their parent app via the parent's
# UDS endpoint (parent MCU bootloader routes the file contents to the
# subcomponent based on address ranges). Maps subcomponent ecu_type → parent.
# The CP MCU's bootloader handles cpPlcFw (PLC modem firmware) and cpPlcPib
# (PLC modem Personality Identifier Block) over its internal interconnect.
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
