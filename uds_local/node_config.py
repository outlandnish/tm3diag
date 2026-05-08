"""Load ECU node configuration from nodes.json, ETH.compact.json, and ODJ files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from decode_bin import load_json as _load_json  # type: ignore[import-untyped]


@dataclass
class OdjEntry:
    name: str
    hex_id: int
    read_size: int | None    # output_size bytes from read section; None if no read
    write_size: int | None   # input_size bytes from write section; None if no write
    security_level: int


@dataclass
class NodeConfig:
    name: str
    request_can_id: int
    response_can_id: int
    security_algorithm: str
    security_buffer_size: int
    security_kw: dict
    dids: dict[str, OdjEntry] = field(default_factory=dict)


def _parse_odj(odj_path: Path) -> dict[str, OdjEntry]:
    raw = _load_json(odj_path)
    entries: dict[str, OdjEntry] = {}
    for name, spec in raw.get("data", {}).items():
        hex_id_str = spec.get("hex_id", "0x0")
        hex_id = int(hex_id_str, 16)
        read_sec = spec.get("read")
        write_sec = spec.get("write")
        # security_level lives in whichever section exists (read preferred)
        sec_level = 0
        read_size = None
        write_size = None
        if read_sec:
            sec_level = read_sec.get("security_level", 0)
            read_size = read_sec.get("output_size")
            if read_size is None:
                # fall back to input_size of the read section
                read_size = read_sec.get("input_size")
        if write_sec:
            if not read_sec:
                sec_level = write_sec.get("security_level", 0)
            write_size = write_sec.get("input_size")
        entries[name] = OdjEntry(
            name=name,
            hex_id=hex_id,
            read_size=read_size,
            write_size=write_size,
            security_level=sec_level,
        )
    return entries


def load_node_config(
    node_name: str,
    nodes_json_path: Path | str,
    eth_compact_path: Path | str,
    odj_dir: Path | str,
) -> NodeConfig:
    nodes = _load_json(Path(nodes_json_path))
    eth = _load_json(Path(eth_compact_path))
    odj_dir = Path(odj_dir)

    if node_name not in nodes:
        raise KeyError(f"Node {node_name!r} not found in nodes.json")

    node_cfg = nodes[node_name]
    messages = eth["messages"]

    req_name = node_cfg["request_message_name"]
    resp_name = node_cfg["response_message_name"]

    if req_name not in messages:
        raise KeyError(f"Request message {req_name!r} not found in ETH compact JSON")
    if resp_name not in messages:
        raise KeyError(f"Response message {resp_name!r} not found in ETH compact JSON")

    request_can_id = messages[req_name]["message_id"]
    response_can_id = messages[resp_name]["message_id"]

    security = node_cfg.get("security", {})
    algorithm = security.get("algorithm", "tesla_hash")
    buffer_size = security.get("buffer_size", 16)
    security_kw = security.get("kw", {})

    # Merge DIDs from all odj_sources
    dids: dict[str, OdjEntry] = {}
    for odj_name in node_cfg.get("odj_sources", []):
        odj_path = odj_dir / odj_name
        if odj_path.exists():
            dids.update(_parse_odj(odj_path))

    return NodeConfig(
        name=node_name,
        request_can_id=request_can_id,
        response_can_id=response_can_id,
        security_algorithm=algorithm,
        security_buffer_size=buffer_size,
        security_kw=security_kw,
        dids=dids,
    )


def load_all_nodes(
    nodes_json_path: Path | str,
    eth_compact_path: Path | str,
) -> list[tuple[str, int, int]]:
    """Return (node_name, request_can_id, response_can_id) for every node in nodes.json."""
    nodes = _load_json(Path(nodes_json_path))
    eth = _load_json(Path(eth_compact_path))
    messages = eth["messages"]

    result = []
    for name, cfg in nodes.items():
        req_name = cfg.get("request_message_name")
        resp_name = cfg.get("response_message_name")
        if not req_name or not resp_name:
            continue
        if req_name not in messages or resp_name not in messages:
            continue
        tx_id = messages[req_name]["message_id"]
        rx_id = messages[resp_name]["message_id"]
        result.append((name, tx_id, rx_id))
    return result
