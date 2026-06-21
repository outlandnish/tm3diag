"""Load ECU node configuration from nodes.json, ETH.compact.json, and ODJ files."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from decode_bin import load_json as _load_json  # type: ignore[import-untyped]
from .odj import OdjEntry, RoutineEntry, IoControlEntry, load_odj


@dataclass
class NodeConfig:
    name: str
    request_can_id: int
    response_can_id: int
    security_algorithm: str
    security_buffer_size: int
    security_kw: dict
    dids: dict[str, OdjEntry] = field(default_factory=dict)
    routines: dict[str, RoutineEntry] = field(default_factory=dict)
    io_controls: dict[str, IoControlEntry] = field(default_factory=dict)


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

    dids: dict[str, OdjEntry] = {}
    routines: dict[str, RoutineEntry] = {}
    io_controls: dict[str, IoControlEntry] = {}
    for odj_name in node_cfg.get("odj_sources", []):
        d, r, io = load_odj(odj_dir / odj_name)
        dids.update(d)
        routines.update(r)
        io_controls.update(io)

    return NodeConfig(
        name=node_name,
        request_can_id=request_can_id,
        response_can_id=response_can_id,
        security_algorithm=algorithm,
        security_buffer_size=buffer_size,
        security_kw=security_kw,
        dids=dids,
        routines=routines,
        io_controls=io_controls,
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
