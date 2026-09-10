"""Minimal ANSI styling for the interactive terminal.

Errors are red, warnings yellow; structure uses bold/dim only. Auto-disabled
when stdout is not a TTY or when NO_COLOR is set (https://no-color.org).
"""

from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


def _enabled() -> bool:
    """True if ANSI codes should be emitted (respects NO_COLOR, TM3_NO_COLOR, TTY)."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TM3_NO_COLOR") is not None:
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


_ON = _enabled()


def _wrap(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _ON else text


def bold(text: str) -> str:
    return _wrap(_BOLD, text)


def dim(text: str) -> str:
    return _wrap(_DIM, text)


def error(text: str) -> str:
    return _wrap(_RED, text)


def warning(text: str) -> str:
    return _wrap(_YELLOW, text)
