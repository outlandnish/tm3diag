#!/usr/bin/env python3
"""PMF node — the FRONT power module: CPU1 of the front drive unit, as DIF is its CPU2.

Front and rear are each one physical unit running two cores that both talk on the bus, so a
simulated front needs both halves: `pmf:` owns `pm/<build>/PMF_*` (CPU1)
and `di/<build>/DIF_*` (CPU2) at one build number, exactly as `pmr:` owns PMR + DIR.

One frame is all the evidence supports: **0x1D5 PMF_state4**. It is the only PMF-sourced ID any
imported image subscribes to -- but three separate images do, and one of them makes this node
mandatory rather than forward-looking:

* the front's own DIF (CPU2 of the same unit), 2022;
* **the REAR's PMR (CPU1), 2022** -- and on a drive bench the rear PMR is real hardware, so a
  simulated front that omits 0x1D5 leaves it in pmfMIA (DI_a042) *today*, not at some later
  revision. The 2022 rear DIR (CPU2) does NOT subscribe, and reading only the DIR is what first
  made this look like a 2026-only concern;
* the rear DIR, from 2026.

Field placement is firmware-read and **identical in all three** (2022 DIF, 2022 AWD PMR, 2026 DIR):
seed 0xD6 (= id_lo + id_hi, the plain rule), checksum in byte 7, and a **3-bit** rolling counter at
bit 53 -- not the 4-bit counter most validated frames carry. Same shape as DAS_control 0x2B9. A
4-bit counter here would overflow into bit 56 and corrupt the checksum byte.

Payload is zeros per the ``zeros()`` contract -- MIA gates on DLC + checksum/counter, not values.

Cycle time is the one number NOT read out of firmware: the 2022 DBC does not define 0x1D5 at all,
so 10 ms comes from the 2024/2026 DBCs, where its twin 0x1D8 PMR_state4 is also 10 ms. The PMF
firmware has no rx DLC check to read a rate from. The error is one-sided -- transmitting
faster than required cannot cause an MIA -- so 10 ms is safe unless the true rate is FASTER, which
no state frame in this set is.

Baseline-only: the placement does not move across the revisions that can be read, and no 2020 AWD
image is imported to say whether it existed then.
"""
from __future__ import annotations

from sim_core import Node, SimFrame, zeros


class Pmf(Node):
    name = "PMF"

    def frames(self) -> list[SimFrame]:
        return [
            SimFrame("PMF_state4", 0x1D5, 0.010, zeros(8), 53, 56, counter_width=3, bus="vehicle"),
        ]


NODE = Pmf
