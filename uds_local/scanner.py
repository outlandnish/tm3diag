"""Network scanner: probe all nodes via raw CAN TesterPresent frames."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import can

from .node_config import load_all_nodes

# ISO-TP single frame: PCI=0x02 (SF, length 2), UDS=3E 00, padded to 8 bytes
_TESTER_PRESENT_PROBE = bytes([0x02, 0x3E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
_POSITIVE_RESPONSE_SID = 0x7E  # data[1] for positive TesterPresent response


@dataclass
class ScanResult:
    node: str
    tx_id: int
    rx_id: int
    responded: bool
    response_data: bytes | None
    is_positive: bool


def scan_network(
    channel: str,
    nodes_json_path: Path | str,
    eth_compact_path: Path | str,
    timeout_per_node: float = 0.1,
    interface: str = "socketcan",
) -> list[ScanResult]:
    nodes = load_all_nodes(nodes_json_path, eth_compact_path)
    bus = can.Bus(interface=interface, channel=channel)
    results: list[ScanResult] = []

    try:
        for name, tx_id, rx_id in nodes:
            msg = can.Message(
                arbitration_id=tx_id,
                data=_TESTER_PRESENT_PROBE,
                is_extended_id=False,
            )
            try:
                bus.send(msg)
            except can.CanError as e:
                # Abort the whole scan on a transmit failure. ENOBUFS (105)
                # means the tx queue can't drain because nothing on the bus is
                # ACKing — every subsequent send would fail too, so a full table
                # of "no" responses would be misleading. Surface the real cause.
                raise RuntimeError(
                    f"CAN transmit failed at {name} (tx 0x{tx_id:X}): {e}. "
                    "No frames are being ACKed — check that the bus is wired, "
                    "terminated, and another node is present."
                ) from e

            deadline = time.monotonic() + timeout_per_node
            response: can.Message | None = None
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                frame = bus.recv(timeout=remaining)
                if frame and frame.arbitration_id == rx_id:
                    response = frame
                    break

            is_positive = bool(
                response
                and len(response.data) >= 2
                and response.data[1] == _POSITIVE_RESPONSE_SID
            )
            results.append(ScanResult(
                node=name,
                tx_id=tx_id,
                rx_id=rx_id,
                responded=response is not None,
                response_data=bytes(response.data) if response else None,
                is_positive=is_positive,
            ))
    finally:
        bus.shutdown()

    return results


def print_scan_table(results: list[ScanResult]) -> None:
    header = f"{'Node':<16} {'TX ID':>8} {'RX ID':>8}  {'Responded':<12} {'Positive':<10} Response"
    print(header)
    print("-" * len(header))
    for r in results:
        resp_hex = r.response_data.hex() if r.response_data else "-"
        print(
            f"{r.node:<16} {r.tx_id:#010x} {r.rx_id:#010x}"
            f"  {'yes' if r.responded else 'no':<12}"
            f" {'yes' if r.is_positive else ('no' if r.responded else '-'):<10}"
            f" {resp_hex}"
        )
