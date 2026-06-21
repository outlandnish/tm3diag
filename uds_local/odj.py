"""ODJ file types and parser — single source of truth for all ODJ parsing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from decode_bin import load_json as _load_json  # type: ignore[import-untyped]


@dataclass
class FieldSpec:
    bit_length: int
    byte_position: int
    bit_position: int
    data_type: str            # "uint" | "int" | "ascii" | "bytes"
    enum_map: dict[str, int]  # empty dict if no enum


@dataclass
class SubSpec:
    security_level: int
    input: dict[str, FieldSpec]
    output: dict[str, FieldSpec]
    input_size: int | None
    output_size: int | None


@dataclass
class OdjEntry:
    name: str
    hex_id: int
    read: SubSpec | None
    write: SubSpec | None


@dataclass
class RoutineEntry:
    name: str
    hex_id: int
    start: SubSpec | None
    stop: SubSpec | None
    results: SubSpec | None


@dataclass
class IoControlEntry:
    name: str
    hex_id: int
    security_level: int
    input: dict[str, FieldSpec]
    output: dict[str, FieldSpec]
    input_size: int
    output_size: int


def _parse_field_spec(raw: dict) -> FieldSpec:
    map_block = raw.get("map") or {}
    enum_map: dict[str, int] = {}
    if map_block.get("calculator") == "enum":
        enum_map = {str(k): int(v) for k, v in map_block.get("enum", {}).items()}
    return FieldSpec(
        bit_length=raw["bit_length"],
        byte_position=raw["byte_position"],
        bit_position=raw["bit_position"],
        data_type=raw.get("data_type", "uint"),
        enum_map=enum_map,
    )


def _parse_fields(raw: dict) -> dict[str, FieldSpec]:
    return {name: _parse_field_spec(spec) for name, spec in raw.items()}


def _parse_subspec(raw: dict | None) -> SubSpec | None:
    if raw is None:
        return None
    return SubSpec(
        security_level=raw.get("security_level", 0),
        input=_parse_fields(raw.get("input") or {}),
        output=_parse_fields(raw.get("output") or {}),
        input_size=raw.get("input_size"),
        output_size=raw.get("output_size"),
    )


def _parse_odj_section(data: dict) -> dict[str, OdjEntry]:
    entries: dict[str, OdjEntry] = {}
    for name, spec in data.get("data", {}).items():
        entries[name] = OdjEntry(
            name=name,
            hex_id=int(spec.get("hex_id", "0x0"), 16),
            read=_parse_subspec(spec.get("read")),
            write=_parse_subspec(spec.get("write")),
        )
    return entries


def _parse_routines_section(data: dict) -> dict[str, RoutineEntry]:
    entries: dict[str, RoutineEntry] = {}
    for name, spec in data.get("routines", {}).items():
        entries[name] = RoutineEntry(
            name=name,
            hex_id=int(spec.get("hex_id", "0x0"), 16),
            start=_parse_subspec(spec.get("start")),
            stop=_parse_subspec(spec.get("stop")),
            results=_parse_subspec(spec.get("results")),
        )
    return entries


def _parse_io_controls_section(data: dict) -> dict[str, IoControlEntry]:
    entries: dict[str, IoControlEntry] = {}
    for name, spec in data.get("io_controls", {}).items():
        entries[name] = IoControlEntry(
            name=name,
            hex_id=int(spec.get("hex_id", "0x0"), 16),
            security_level=spec.get("security_level", 0),
            input=_parse_fields(spec.get("input") or {}),
            output=_parse_fields(spec.get("output") or {}),
            input_size=spec.get("input_size", 0),
            output_size=spec.get("output_size", 0),
        )
    return entries


def load_odj(path: Path) -> tuple[
    dict[str, OdjEntry],
    dict[str, RoutineEntry],
    dict[str, IoControlEntry],
]:
    """Load and parse an ODJ file, falling back to .bin twin if plain file absent."""
    if not path.exists():
        bin_twin = path.with_name(path.name + ".bin")
        if bin_twin.exists():
            path = bin_twin
    if not path.exists():
        return {}, {}, {}
    data = _load_json(path)
    return (
        _parse_odj_section(data),
        _parse_routines_section(data),
        _parse_io_controls_section(data),
    )
