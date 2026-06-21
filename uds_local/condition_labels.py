"""Load human-readable value labels for condition keys from a compact.json."""

from __future__ import annotations

import json
from pathlib import Path


def load_condition_labels(compact_path: Path | str | None) -> dict[str, dict[str, str]]:
    """Return {value_table_name: {int_str: label}} from a compact.json file.

    Returns {} when compact_path is None, the file is absent, or JSON is invalid.
    """
    if compact_path is None:
        return {}
    path = Path(compact_path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
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
