"""Shared pytest setup: make the top-level scripts/ dir importable."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
