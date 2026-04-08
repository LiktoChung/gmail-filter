"""Load/save OAuth tokens on disk."""

from __future__ import annotations

import json
from typing import Any

from .config import settings


def load_tokens() -> dict[str, Any] | None:
    path = settings.token_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_tokens(data: dict[str, Any]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_tokens() -> None:
    if settings.token_path.exists():
        settings.token_path.unlink()
