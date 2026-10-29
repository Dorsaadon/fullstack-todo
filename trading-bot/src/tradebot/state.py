"""Durable bot state, so a crash or restart loses nothing that matters.

Saved: the risk manager's daily-loss baseline and halt status, each
strategy's open positions and indicators, and paper accounts' balances.
Without it, a restart would reset the daily loss limit, forget positions the
strategies must still close, and, since Docker and systemd restart a crashed
bot automatically, quietly lift a halt.

Halts persist until a person clears them (`tradebot state --clear-halt`).

Writes are atomic (temp file, fsync, rename), and the previous version is
kept as `.bak`, so a crash mid-write never leaves the bot without a state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 1


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + ".bak")

    def load(self) -> dict:
        for candidate in (self.path, self.backup):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.error("state file %s unreadable (%s); trying backup", candidate, e)
                continue
            if data.get("version") != VERSION:
                raise StateError(f"{candidate}: state version {data.get('version')} unsupported")
            if candidate is self.backup:
                log.warning("restored state from backup %s", self.backup)
            return data
        if self.path.exists() or self.backup.exists():
            # Refuse to start blind: that could clear a halt or forget positions.
            raise StateError(f"state in {self.path} is corrupt; inspect it or reset with `tradebot state --reset`")
        return {}

    def save(self, data: dict) -> None:
        data = {"version": VERSION, "saved_at": time.time(), **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists():
            os.replace(self.path, self.backup)
        os.replace(tmp, self.path)

    def age(self) -> float | None:
        """Seconds since the last save, or None if never saved."""
        try:
            return time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return None

    def reset(self) -> None:
        for p in (self.path, self.backup):
            p.unlink(missing_ok=True)


class InstanceLock:
    """Exclusive lock so two copies of the bot never trade the same account.

    Held for the life of the process; the OS releases it if the process dies.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:  # Windows: no advisory locks; rely on the service manager
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = open(self.path, "w")
        except PermissionError:
            raise StateError(
                f"can't write to {self.path.parent.resolve()} as uid {os.getuid()}. "
                f"Give the bot its data directory, e.g. `sudo chown -R {os.getuid()}:{os.getgid()} data`"
            ) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            raise StateError(f"another tradebot is already running (lock {self.path} is held)") from None
        fd.write(str(os.getpid()))
        fd.flush()
        self._fd = fd
