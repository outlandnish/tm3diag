"""Shared pytest setup: make the top-level scripts/ dir importable.

odin_runner.py (and its siblings) live in scripts/, which is not on sys.path by
default when pytest runs from the repo root. Prepend it so tests can
`import odin_runner` the same way the CLI does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
