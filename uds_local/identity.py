"""Shared ECU identity decode (DID 0xF180).

The bootloader-version DID 0xF180 carries the board's identity tuple. Several
tools need the same parse + lookup-key derivation:

  * dfu.py — to pick the matching firmware row in signed_metadata_map.tsv
  * tm3cli.py — to show the lookup key in the connection banner
  * tm3uds.py — the `identity` subcommand

This module is the single source of truth for that decode so the lookup key is
computed identically everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

DID_BOOTLOADER_VERSION = 0xF180


@dataclass(frozen=True)
class Identity:
    """Decoded identity of a connected node (from DID 0xF180)."""

    node: str
    f180_raw: str
    component_id: int
    pcba_id: int
    assembly_id: int
    usage_id: int
    packed_key: int
    lookup_key: str

    def as_dict(self) -> dict:
        """Return the legacy dict shape used by dfu.py's flash phases."""
        return {
            "f180_raw": self.f180_raw,
            "component_id": self.component_id,
            "pcba_id": self.pcba_id,
            "assembly_id": self.assembly_id,
            "usage_id": self.usage_id,
            "packed_key": self.packed_key,
            "lookup_key": self.lookup_key,
        }


def pack_key(pcba_id: int, assembly_id: int, usage_id: int) -> int:
    """Pack the identity tuple into the version_map key (PPAA00UU)."""
    return (pcba_id << 24) | (assembly_id << 16) | (usage_id & 0xFF)


def lookup_key_for(node_name: str, packed_key: int) -> str:
    """Build the version_map lookup key string: '<node>:<packed_key>'."""
    return f"{node_name.lower()}:{packed_key}"


def parse_f180(f180: bytes, node_name: str) -> Identity:
    """Decode a DID 0xF180 response into an :class:`Identity`.

    Layout: [MODULES:1][COMPONENT_ID:2][PCBA_ID:1][ASSEMBLY_ID:1][USAGE_ID:2]

    Raises ValueError if the response is too short to contain the identity.
    """
    if len(f180) < 7:
        raise ValueError(
            f"DID 0xF180 response too short ({len(f180)} bytes, expected >=7)"
        )

    component_id = (f180[1] << 8) | f180[2]
    pcba_id = f180[3]
    assembly_id = f180[4]
    usage_id = (f180[5] << 8) | f180[6]

    packed = pack_key(pcba_id, assembly_id, usage_id)
    return Identity(
        node=node_name,
        f180_raw=f180.hex(),
        component_id=component_id,
        pcba_id=pcba_id,
        assembly_id=assembly_id,
        usage_id=usage_id,
        packed_key=packed,
        lookup_key=lookup_key_for(node_name, packed),
    )


def read_identity(sess, node_name: str) -> Identity:
    """Read DID 0xF180 from an open UdsSession and decode it."""
    f180 = sess.read_did(DID_BOOTLOADER_VERSION)
    return parse_f180(f180, node_name)
