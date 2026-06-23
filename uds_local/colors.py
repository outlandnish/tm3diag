"""Minimal ANSI styling for the interactive terminal.

Muted / low-color scheme: only errors (red) and warnings (yellow) carry a
hue; structure (headers, menu commands, prompt) uses bold/dim only. Styling
auto-disables when stdout is not a TTY or when NO_COLOR is set (see
https://no-color.org), so piped/redirected output stays clean.
"""

from __future__ import annotations

import os
import sys

# --- raw SGR codes ---------------------------------------------------------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


def _enabled() -> bool:
    """True if we should emit ANSI codes.

    Disabled when NO_COLOR is set (any value), when TM3_NO_COLOR is set, or
    when stdout isn't a terminal (pipe, file, CI log).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TM3_NO_COLOR") is not None:
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# Resolved once at import time. The CLI is short-lived and its stdout doesn't
# change underneath it, so there's no need to re-check per call.
_ON = _enabled()


def _wrap(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _ON else text


def bold(text: str) -> str:
    """Structural emphasis — headers, prompt, menu command names."""
    return _wrap(_BOLD, text)


def dim(text: str) -> str:
    """De-emphasis — menu descriptions, secondary detail."""
    return _wrap(_DIM, text)


def error(text: str) -> str:
    """Errors — negative responses, failures, unknown commands."""
    return _wrap(_RED, text)


def warning(text: str) -> str:
    """Warnings / actionable hints — bus down, no response, recoverable issues."""
    return _wrap(_YELLOW, text)
