"""Shared JSON-file reader for the catalog scanners."""
from __future__ import annotations

import json
from pathlib import Path


def read_json_dict(path: Path) -> dict:
    """Read a JSON object from ``path``; return ``{}`` on any error or non-object.

    A missing/corrupt file, or a top-level value that isn't a JSON object
    (array, string, number), all yield ``{}`` so callers can safely ``.get()``
    without a type check of their own.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
