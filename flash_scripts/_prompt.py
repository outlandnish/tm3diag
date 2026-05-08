"""Arrow-key interactive prompts that integrate with StatusDisplay's two-line model."""

from __future__ import annotations

import sys
import termios
import tty

from ._display import StatusDisplay

_KEY_UP    = b'\x1b[A'
_KEY_DOWN  = b'\x1b[B'
_KEY_LEFT  = b'\x1b[D'
_KEY_RIGHT = b'\x1b[C'
_KEY_ENTER = frozenset([b'\r', b'\n'])
_KEY_CTRL_C = b'\x03'


def _read_key() -> bytes:
    """Read one logical keypress, collapsing arrow escape sequences."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.buffer.read(1)
        if ch == b'\x1b':
            nxt = sys.stdin.buffer.read(1)
            if nxt == b'[':
                return b'\x1b[' + sys.stdin.buffer.read(1)
            return ch
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def prompt_confirm(question: str, default: bool, display: StatusDisplay) -> bool:
    """Yes/No prompt — ↑↓ or ←→ to toggle, Enter to confirm.

    Line 1: question
    Line 2: [Yes]  No   or   Yes  [No]   (brackets = current selection)
    """
    idx = 0 if default else 1  # 0=Yes 1=No

    while True:
        labels = ["Yes", "No"]
        parts = [f"[{lbl}]" if i == idx else f" {lbl} " for i, lbl in enumerate(labels)]
        display.set_header(question)
        display.set_detail("  ".join(parts) + "    ↑↓ · Enter")

        key = _read_key()
        if key in (_KEY_UP, _KEY_DOWN, _KEY_LEFT, _KEY_RIGHT):
            idx = 1 - idx
        elif key in _KEY_ENTER:
            chosen = idx == 0
            display.set_detail(f"→ {'Yes' if chosen else 'No'}")
            display.finalize()
            return chosen
        elif key == _KEY_CTRL_C:
            raise KeyboardInterrupt


def prompt_select(
    question: str,
    labels: list[str],
    default: int = 0,
    display: StatusDisplay | None = None,
) -> int:
    """Cycle-select from a list — ↑↓ to move, Enter to confirm.

    Line 1: question
    Line 2: [n/total]  current label    ↑↓ · Enter
    """
    if display is None:
        display = StatusDisplay()
    idx = default
    n = len(labels)

    while True:
        display.set_header(question)
        display.set_detail(f"[{idx + 1}/{n}]  {labels[idx]}    ↑↓ · Enter")

        key = _read_key()
        if key in (_KEY_UP, _KEY_LEFT):
            idx = (idx - 1) % n
        elif key in (_KEY_DOWN, _KEY_RIGHT):
            idx = (idx + 1) % n
        elif key in _KEY_ENTER:
            display.set_detail(f"→ {labels[idx]}")
            display.finalize()
            return idx
        elif key == _KEY_CTRL_C:
            raise KeyboardInterrupt
