"""Append-only JSONL audit log of every decision, order and halt."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o):
        return asdict(o)
    return str(o)


class Journal:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **data) -> None:
        if not self.path:
            return
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "event": event, **data}, default=_default) + "\n")
