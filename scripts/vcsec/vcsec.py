#!/usr/bin/env python3
"""VCSEC node — vehicle security controller: the runtime immobilizer handshake.

The 0x3D9 immobilizer RESPONSE is VCSEC's message: the DIR sends the parked 0x276
challenge and verifies the reply locally.

VCSEC sources no PERIODIC frames; it owns a reactive state machine: on the 0x276
challenge it answers 0x3D9 once per counter. The response is computed by a
key-derivation provider you configure (see docs/SECURITY_PROVIDER.md) — the
framework ships no immobilizer algorithm. The driver hands it the resolved key
via ``immo_key``; with no key/provider (or --real VCSEC / --no-immo) it stays
silent.
"""
from __future__ import annotations

from sim_core import Node, SimFrame

IMMO_CHALLENGE_ID = 0x276  # DIR -> us: parked challenge (full counter+nonce on the wire)
IMMO_RESPONSE_ID = 0x3D9  # us -> DIR: answer (DLC 8) -> 0x118 immo = 3 DISARMED


class Vcsec(Node):
    name = "VCSEC"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.immo_key: bytes | None = None  # driver sets the resolved key
        self._last_ctr: int | None = None
        self._warned = False

    def frames(self) -> list[SimFrame]:
        return []

    def rx_handlers(self):
        return {IMMO_CHALLENGE_ID: self._on_challenge}  # 0x276 parked challenge

    def _on_challenge(self, data, send) -> None:
        if self.immo_key is None or len(data) < 8:
            return
        # Response is derived by the configured key-derivation provider; the
        # framework ships no immobilizer algorithm. Fail closed (silently) if
        # none is configured.
        from uds_local.security_provider import challenge_counter, challenge_response_l04

        try:
            ctr = challenge_counter(bytes(data))
            resp = challenge_response_l04(self.immo_key, bytes(data))
        except NotImplementedError as e:
            if not self._warned:
                print(f"  immo  no key-derivation provider configured: {e}")
                self._warned = True
            return
        if ctr == self._last_ctr:  # answer once per counter
            return
        self._last_ctr = ctr
        send(IMMO_RESPONSE_ID, bytes(resp))
        print(f"  immo  0x276 ctr={ctr:#06x} -> 0x3D9 {resp.hex()}")


NODE = Vcsec
