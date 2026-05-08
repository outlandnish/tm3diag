"""Two-line live status display with ANSI cursor control."""

from __future__ import annotations

import sys


def _bar(current: int, total: int, width: int = 28) -> str:
    """[████████░░░░░░░░]  60%   45.6 / 76.0 KB"""
    if total <= 0:
        return ""
    frac = min(current / total, 1.0)
    filled = int(frac * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(frac * 100)
    return f"[{bar}] {pct:3d}%   {current / 1024:.1f} / {total / 1024:.1f} KB"


class StatusDisplay:
    """Maintains a two-line block (header + detail) that updates in place.

    Call set_header() once per phase/section, then set_detail() for each
    sub-step — only the detail line is overwritten. Call finalize() before
    any interactive prompt or multi-line static output so subsequent prints
    don't clobber the status block.
    """

    def __init__(self) -> None:
        self._lines = 0  # 0 = nothing printed, 1 = header only, 2 = header+detail

    def set_header(self, header: str) -> None:
        self._erase(self._lines)
        print(header)
        sys.stdout.flush()
        self._lines = 1

    def set_detail(self, detail: str) -> None:
        if self._lines >= 2:
            self._erase(1)  # erase only the detail line
        print(f"  {detail}")
        sys.stdout.flush()
        if self._lines < 2:
            self._lines = 2

    def finalize(self) -> None:
        """Stop tracking — subsequent output starts on new lines below."""
        self._lines = 0

    @staticmethod
    def _erase(n: int) -> None:
        for _ in range(n):
            sys.stdout.write("\033[F\033[2K")
        if n:
            sys.stdout.flush()
