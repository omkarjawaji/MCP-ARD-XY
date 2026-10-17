"""Append-only JSONL logs: `audit.jsonl` (every tool call) and `discovery.jsonl` (searches)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **fields}
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def preview(value: Any, limit: int = 300) -> str:
    """Short, single-line rendering of arguments/results for log files."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"
