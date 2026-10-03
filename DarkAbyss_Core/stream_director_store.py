"""Persistent state of one Stream Director instance.

    instances/<id>/data/stream_director_state.json       the state (atomic writes)
    instances/<id>/data/stream_director_state.backup.json last state that loaded fine

The state holds community progress, the active stream session, challenges,
polls, the inbox and the ids of processed Twitch events. It is never reset
silently: an unreadable or malformed file raises StateError and the bot stops
changing anything (fail closed) until the file is restored or removed; the
Manager shows the problem. The backup is refreshed from a state that loaded
fine, so a broken write never overwrites the last good copy.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

STATE_FILE_NAME = "stream_director_state.json"
BACKUP_FILE_NAME = "stream_director_state.backup.json"
STATE_VERSION = 1
BACKUP_EVERY_SECONDS = 300


class StateError(RuntimeError):
    """The persisted Stream Director state cannot be used (fail closed)."""


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "next_id": 1,
        "processed_events": {},
        "active_session": None,
        "sessions": [],
        "challenges": {},
        "polls": {},
        "inbox": {},
        "goals": {},
        "community": {"points": 0, "season_points": {}, "counters": {}},
        "cooldowns": {},
        "meta": {"default_goals_created": False},
    }


REQUIRED_SHAPE: dict[str, type] = {
    "next_id": int,
    "processed_events": dict,
    "sessions": list,
    "challenges": dict,
    "polls": dict,
    "inbox": dict,
    "goals": dict,
    "community": dict,
    "cooldowns": dict,
    "meta": dict,
}


def state_problem_text(file_name: str, reason: str) -> str:
    return (
        f"Stream Director state file {file_name} {reason}. The bot changes nothing (sessions, community progress, "
        f"challenges and the inbox stay as they are) until it is fixed: restore {BACKUP_FILE_NAME} over it, or remove "
        "both files to start from zero, then restart the bot."
    )


def validate_state(raw: Any, file_name: str = STATE_FILE_NAME) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StateError(state_problem_text(file_name, "has an invalid shape"))
    version = raw.get("version")
    if version != STATE_VERSION:
        raise StateError(state_problem_text(file_name, f"has an unsupported version {version!r}"))
    for key, expected in REQUIRED_SHAPE.items():
        if not isinstance(raw.get(key), expected) or isinstance(raw.get(key), bool):
            raise StateError(state_problem_text(file_name, "has an invalid shape"))
    if raw.get("active_session") is not None and not isinstance(raw.get("active_session"), dict):
        raise StateError(state_problem_text(file_name, "has an invalid shape"))
    base = empty_state()
    base.update(raw)
    return base


class StateStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir) / STATE_FILE_NAME
        self.backup_path = Path(data_dir) / BACKUP_FILE_NAME
        self._lock = threading.RLock()
        self._backup_at = 0.0

    def load(self) -> dict[str, Any]:
        """The state (a fresh one when no file exists yet). Raises StateError."""
        with self._lock:
            if not self.path.exists():
                return empty_state()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StateError(state_problem_text(self.path.name, "is unreadable")) from exc
            state = validate_state(raw, self.path.name)
            self._write(self.backup_path, state)
            self._backup_at = time.time()
            return state

    def save(self, state: dict[str, Any], now: float | None = None) -> None:
        with self._lock:
            validate_state(state)
            self._write(self.path, state)
            now = time.time() if now is None else now
            if now - self._backup_at >= BACKUP_EVERY_SECONDS:
                self._write(self.backup_path, state)
                self._backup_at = now

    def verify(self) -> str | None:
        """Problem text when the file is unusable, else None (read only)."""
        if not self.path.exists():
            return None
        try:
            validate_state(json.loads(self.path.read_text(encoding="utf-8")), self.path.name)
        except (OSError, ValueError):
            return state_problem_text(self.path.name, "is unreadable")
        except StateError as exc:
            return str(exc)
        return None

    @staticmethod
    def _write(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def read_only_snapshot(data_dir: Path | str) -> tuple[dict[str, Any] | None, str | None]:
    """(state, problem) for the Manager: never writes, never repairs."""
    store = StateStore(data_dir)
    if not store.path.exists():
        return None, None
    problem = store.verify()
    if problem:
        return None, problem
    try:
        return copy.deepcopy(json.loads(store.path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError):
        return None, state_problem_text(store.path.name, "is unreadable")
