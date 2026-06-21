"""Parse signed_metadata_map.tsv and select firmware entries for an ECU."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class FirmwareEntry:
    lookup_key: str           # e.g. "pcs:84082689"
    src_path: str             # e.g. "pcs/531/pcs_531.bhx"
    dest_name: str            # e.g. "pcs.bhx"
    component: str            # e.g. "pcs"
    crc: str                  # hex string, e.g. "aa813ff8"
    conditions: dict[str, str]  # e.g. {"drivetrainType": "0"} or {} for wildcard "*"
    signature: str            # base64 signature


def load_metadata(tsv_path: Path | str) -> list[FirmwareEntry]:
    entries: list[FirmwareEntry] = []
    with open(tsv_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            # First line is a git SHA header: "<sha>\t<version>"
            if len(parts) == 2 and len(parts[0]) == 40 and parts[0].isalnum():
                continue
            if len(parts) < 7:
                continue
            lookup_key, src_path, dest_name, component, crc, conditions_str, signature = parts[:7]
            if conditions_str.strip() == "*":
                conditions: dict[str, str] = {}
            else:
                conditions = {}
                for pair in conditions_str.split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        conditions[k.strip()] = v.strip()
            entries.append(FirmwareEntry(
                lookup_key=lookup_key,
                src_path=src_path,
                dest_name=dest_name,
                component=component,
                crc=crc,
                conditions=conditions,
                signature=signature,
            ))
    return entries


def find_firmware(
    entries: list[FirmwareEntry],
    ecu_name: str,
    packed_key: int,
    conditions: dict[str, str] | None = None,
) -> list[FirmwareEntry]:
    """Filter entries by lookup_key (ecu:packed_key) and optional conditions."""
    lookup_key = f"{ecu_name.lower()}:{packed_key}"
    matches = [e for e in entries if e.lookup_key == lookup_key]
    if conditions:
        # Prefer entries whose conditions are a subset of the supplied conditions
        filtered = [
            e for e in matches
            if not e.conditions or all(
                conditions.get(k) == v for k, v in e.conditions.items()
            )
        ]
        if filtered:
            return filtered
    return matches


def varying_condition_keys(matches: list[FirmwareEntry]) -> list[str]:
    """Return sorted condition key names whose values differ across matches."""
    typed = [e for e in matches if e.conditions]
    if len(typed) <= 1:
        return []
    all_keys: set[str] = set()
    for e in typed:
        all_keys.update(e.conditions)
    return sorted(
        k for k in all_keys
        if len({e.conditions[k] for e in typed if k in e.conditions}) > 1
    )


def narrow_by_conditions(
    matches: list[FirmwareEntry],
    key: str,
    value: str,
) -> list[FirmwareEntry]:
    """Return entries where conditions[key] == value; falls back to matches if empty."""
    filtered = [e for e in matches if e.conditions.get(key) == value]
    return filtered if filtered else matches


def packed_key_from_f180(f180: bytes) -> int:
    """
    Derive the version_map packed key from the 19-byte BOOTLOADER_VERSION DID (0xF180).

    DID layout (per odin-architecture.md):
      byte 0:  MODULES
      byte 1-2: COMPONENT_ID (big-endian 16-bit)
      byte 3:  PCBA_ID
      byte 4:  ASSEMBLY_ID
      byte 5:  USAGE_ID (lower byte; some ECUs only)
      ...

    version_map2.tsv key encoding: PPAA00UU (32-bit big-endian)
      PP = PCBA_ID, AA = ASSEMBLY_ID, 00 = padding, UU = USAGE_ID
    """
    pcba_id     = f180[3]
    assembly_id = f180[4]
    usage_id    = f180[5] if len(f180) > 5 else 0
    packed = (pcba_id << 24) | (assembly_id << 16) | (0 << 8) | usage_id
    return packed
