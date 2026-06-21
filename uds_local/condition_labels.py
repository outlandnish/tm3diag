"""Load human-readable value labels for condition keys from a compact.json."""

from __future__ import annotations

from pathlib import Path

from decode_bin import load_json as _load_json


def load_condition_labels(compact_path: Path | str | None) -> dict[str, dict[str, str]]:
    """Return {value_table_name: {int_str: label}} from a compact.json file.

    Returns {} when compact_path is None, the file is absent, or JSON is invalid.
    Uses decode_bin.load_json so an encrypted .bin twin is auto-decrypted rather
    than silently read as text (which yields no labels).
    """
    if compact_path is None:
        return {}
    path = Path(compact_path)
    try:
        data = _load_json(path)
    except Exception:
        # load_json may raise OSError (missing file), ValueError (bad JSON), or
        # decrypt/key errors (RuntimeError, InvalidToken) for an encrypted .bin.
        # The contract is best-effort: any failure yields no labels.
        return {}
    result: dict[str, dict[str, str]] = {}
    try:
        for msg in data.get("messages", {}).values():
            for sig in msg.get("signals", {}).values():
                table_name = sig.get("value_table_name")
                value_desc = sig.get("value_description")
                if table_name and value_desc:
                    result[table_name] = {str(v): k for k, v in value_desc.items()}
    except (AttributeError, TypeError):
        return {}
    return result
