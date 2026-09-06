"""Persistent, board-keyed interop data store (odin_data.json).

One gitignored JSON, ``{board_id: {namespace: {key: value}}}``. The board id is the
board serial number (DID 0xF013) -- the same key the immobilizer already uses.
Namespaces group data by producer:

  * ``immo``    -- optional per-board key material written by a user-supplied
                   immobilizer provider (see docs/SECURITY_PROVIDER.md); absent
                   unless such a provider is configured
  * ``outputs`` -- outputs captured from an ODIN procedure run (scripts/odin_runner)

This is the single place bench interop persists per-board state, so anything that
needs a board's key / prior outputs reads it from here.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

# Lives at the project root; gitignored (see .gitignore).
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "odin_data.json"


def json_safe(obj):
    """Recursively coerce a value to something json.dumps accepts: bytes -> hex
    string, tuples -> lists, dict keys -> str. ODIN procedure outputs carry raw
    bytes (odometer / resolver-calibration blobs), so sanitize before storing."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


class DataStore:
    """``{board_id: {namespace: {key: value}}}`` persisted to one JSON file.

    Board ids and namespaces are strings; values are JSON-serializable. Writes save
    immediately (the file is small and written rarely). Missing lookups return empty
    -- callers treat "no data for this board/namespace" as the common case.
    """

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        with contextlib.suppress(json.JSONDecodeError, OSError):
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self._data = loaded

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")

    def _board(self, board_id) -> dict:
        return self._data.setdefault(str(board_id), {})

    def get(self, board_id, namespace: str) -> dict:
        """A copy of one namespace's ``{key: value}`` for a board (``{}`` if absent)."""
        return dict(self._data.get(str(board_id), {}).get(namespace, {}))

    def has(self, board_id, namespace: str) -> bool:
        return bool(self._data.get(str(board_id), {}).get(namespace))

    def put(self, board_id, namespace: str, key: str, value) -> None:
        """Set one key in a namespace and persist."""
        self._board(board_id).setdefault(namespace, {})[key] = value
        self.save()

    def update(self, board_id, namespace: str, mapping: dict,
               *, replace: bool = False) -> None:
        """Merge (default) or replace a namespace's mapping and persist."""
        board = self._board(board_id)
        if replace or not isinstance(board.get(namespace), dict):
            board[namespace] = dict(mapping)
        else:
            board[namespace].update(mapping)
        self.save()

    def boards(self) -> list[str]:
        return list(self._data)
