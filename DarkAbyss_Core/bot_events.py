"""A light event bus between DarkAbyss bots (separate processes, one machine).

A bot announces what it just did in Discord ("Group Up suggested Overwatch 2
to these members, message 123 in #games") so another bot can understand it as
part of what happens on the server - today Kairo's Social Awareness reads the
Game Presence and Stream Director events.

* One file per producing instance: ``<data root>/runtime/bot_events/<instance>.json``
  holds that bot's latest events (bounded count and age) and is replaced
  atomically on every publish, so a reader never sees a half-written file and
  no two processes ever write the same file.
* Readers poll the folder (file signature check, cheap), skip their own file
  and keep the IDs they have seen.
* Events carry Discord IDs, names and facts only - no tokens, keys, paths or
  message text. Readers validate every field and treat names (game, channel)
  as untrusted data.
* Best effort by design: publishing never raises, a missing/broken file is
  skipped; nothing in a bot depends on another bot being up.
* Heartbeat: the same file carries a small "bot" section (type, Manager name,
  Discord account, servers, last sign of life). ``EventReader.bots()`` turns it
  into "which DarkAbyss bots exist and which run right now" - awareness only,
  no bot can control another through it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

EVENTS_DIR_NAME = "bot_events"
HEARTBEAT_SECONDS = 60.0
HEARTBEAT_STALE_SECONDS = 180.0  # no sign of life for 3 minutes: not running
MAX_GUILDS = 50
FILE_VERSION = 1
MAX_EVENTS_PER_PRODUCER = 50
EVENT_TTL_SECONDS = 6 * 3600.0
MAX_FILE_BYTES = 256 * 1024
MAX_DATA_ITEMS = 24
MAX_TEXT = 120
MAX_LIST = 25
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_KIND = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


def default_events_dir() -> Path:
    import app_paths

    return app_paths.RUNTIME_DIR / EVENTS_DIR_NAME


@dataclass(frozen=True)
class BotEvent:
    event_id: str
    at: float
    source_type: str
    source_instance: str
    kind: str
    guild_id: int
    channel_id: int | None = None
    message_id: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "at": self.at,
            "kind": self.kind,
            "guild_id": str(self.guild_id),
            "channel_id": None if self.channel_id is None else str(self.channel_id),
            "message_id": None if self.message_id is None else str(self.message_id),
            "data": dict(self.data),
        }


def _snowflake(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and 0 < int(value) < 2**64:
        return int(value)
    return None


def _clean_value(value: Any) -> Any:
    """Plain JSON data only: short strings, numbers, booleans, short lists of those."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if abs(value) < 2**64 else None
    if isinstance(value, str):
        return " ".join(value.split())[:MAX_TEXT]
    if isinstance(value, (list, tuple)):
        return [item for item in (_clean_value(item) for item in list(value)[:MAX_LIST]) if not isinstance(item, (list, dict))]
    return None


def clean_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list((data or {}).items())[:MAX_DATA_ITEMS]:
        if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
            out[key] = _clean_value(value)
    return out


@dataclass(frozen=True)
class BotInfo:
    """One DarkAbyss bot instance as it describes itself (heartbeat)."""

    instance_id: str
    bot_type: str
    display_name: str
    discord_user_id: int | None
    discord_name: str
    guild_ids: frozenset[int]
    started_at: float
    heartbeat_at: float
    stopped: bool

    def running(self, now: float) -> bool:
        return not self.stopped and now - self.heartbeat_at <= HEARTBEAT_STALE_SECONDS


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


class EventPublisher:
    """Writes this bot instance's own event file."""

    def __init__(
        self,
        instance_id: str,
        bot_type: str,
        directory: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(instance_id, str) or not _SAFE_ID.fullmatch(instance_id):
            raise ValueError("instance_id is not a safe instance ID.")
        if not isinstance(bot_type, str) or not _SAFE_ID.fullmatch(bot_type):
            raise ValueError("bot_type is not a safe bot type ID.")
        self.instance_id = instance_id
        self.bot_type = bot_type
        self.directory = Path(directory) if directory is not None else default_events_dir()
        self.path = self.directory / f"{instance_id}.json"
        self._clock = clock
        self._log = log
        self._events: list[dict[str, Any]] | None = None
        self._counter = 0
        self._bot: dict[str, Any] | None = None
        self._last_heartbeat = 0.0

    def _load(self) -> list[dict[str, Any]]:
        if self._events is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                events = raw.get("events") if isinstance(raw, dict) else None
                self._events = [item for item in events if isinstance(item, dict)] if isinstance(events, list) else []
            except (OSError, ValueError):
                self._events = []
        return self._events

    def _write(self) -> None:
        payload: dict[str, Any] = {
            "version": FILE_VERSION,
            "source_type": self.bot_type,
            "source_instance": self.instance_id,
            "events": self._load(),
        }
        if self._bot is not None:
            payload["bot"] = self._bot
        _atomic_write(self.path, payload)

    def heartbeat(
        self,
        *,
        display_name: Any = "",
        discord_user_id: Any = None,
        discord_name: Any = "",
        guild_ids: Iterable[Any] = (),
        force: bool = False,
    ) -> bool:
        """"I am running" (at most every HEARTBEAT_SECONDS unless ``force``); never raises."""
        try:
            now = float(self._clock())
            if not force and self._bot is not None and now - self._last_heartbeat < HEARTBEAT_SECONDS:
                return False
            started = (self._bot or {}).get("started_at") if not (self._bot or {}).get("stopped") else None
            guilds = [str(guild) for guild in (_snowflake(item) for item in list(guild_ids)[:MAX_GUILDS]) if guild is not None]
            self._bot = {
                "display_name": _clean_value(str(display_name or ""))[:60],
                "discord_user_id": None if _snowflake(discord_user_id) is None else str(_snowflake(discord_user_id)),
                "discord_name": _clean_value(str(discord_name or ""))[:60],
                "guild_ids": guilds,
                "started_at": started if isinstance(started, (int, float)) else now,
                "heartbeat_at": now,
                "stopped": False,
            }
            self._last_heartbeat = now
            self._write()
            return True
        except Exception:
            return False

    def stopped(self) -> None:
        """Clean shutdown: the others see "not running" at once."""
        try:
            if self._bot is None:
                return
            self._bot = {**self._bot, "stopped": True, "heartbeat_at": float(self._clock())}
            self._write()
        except Exception:
            pass

    def publish(
        self,
        kind: str,
        guild_id: Any,
        *,
        channel_id: Any = None,
        message_id: Any = None,
        data: Mapping[str, Any] | None = None,
    ) -> BotEvent | None:
        """Announce one event; returns it, or None when it could not be written (never raises)."""
        try:
            guild = _snowflake(guild_id)
            if guild is None or not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind):
                return None
            now = float(self._clock())
            self._counter += 1
            event = BotEvent(
                event_id=f"{self.instance_id}:{int(now * 1000)}:{self._counter}",
                at=now,
                source_type=self.bot_type,
                source_instance=self.instance_id,
                kind=kind,
                guild_id=guild,
                channel_id=_snowflake(channel_id),
                message_id=_snowflake(message_id),
                data=clean_data(data),
            )
            events = [item for item in self._load() if isinstance(item.get("at"), (int, float)) and now - item["at"] < EVENT_TTL_SECONDS]
            events.append(event.to_json())
            self._events = events[-MAX_EVENTS_PER_PRODUCER:]
            self._write()
            return event
        except Exception as exc:
            if self._log is not None:
                try:
                    self._log(f"Bot event not published: {type(exc).__name__}")
                except Exception:
                    pass
            return None


def _parse_event(item: Any, source_type: str, source_instance: str) -> BotEvent | None:
    if not isinstance(item, dict):
        return None
    event_id, at, kind = item.get("id"), item.get("at"), item.get("kind")
    guild = _snowflake(item.get("guild_id"))
    if not isinstance(event_id, str) or len(event_id) > 120 or not event_id.startswith(f"{source_instance}:"):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind):
        return None
    if guild is None:
        return None
    data = item.get("data")
    return BotEvent(
        event_id=event_id,
        at=float(at),
        source_type=source_type,
        source_instance=source_instance,
        kind=kind,
        guild_id=guild,
        channel_id=_snowflake(item.get("channel_id")),
        message_id=_snowflake(item.get("message_id")),
        data=clean_data(data if isinstance(data, dict) else {}),
    )


def _read_producer(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != FILE_VERSION:
        return None
    source_type, source_instance = raw.get("source_type"), raw.get("source_instance")
    if not isinstance(source_type, str) or not _SAFE_ID.fullmatch(source_type):
        return None
    # The file name is the producer: a file cannot speak for another instance.
    if source_instance != path.stem or not _SAFE_ID.fullmatch(path.stem):
        return None
    return raw


def read_bot(path: Path) -> BotInfo | None:
    """The heartbeat section of one producer file (None: never sent / invalid)."""
    raw = _read_producer(path)
    bot = raw.get("bot") if raw is not None else None
    if not isinstance(bot, dict):
        return None
    heartbeat, started = bot.get("heartbeat_at"), bot.get("started_at")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return None
    guilds = frozenset(guild for guild in (_snowflake(item) for item in (bot.get("guild_ids") or [])[:MAX_GUILDS]) if guild is not None)
    return BotInfo(
        instance_id=raw["source_instance"],
        bot_type=raw["source_type"],
        display_name=str(_clean_value(bot.get("display_name")) or "")[:60] if isinstance(bot.get("display_name"), str) else "",
        discord_user_id=_snowflake(bot.get("discord_user_id")),
        discord_name=str(_clean_value(bot.get("discord_name")) or "")[:60] if isinstance(bot.get("discord_name"), str) else "",
        guild_ids=guilds,
        started_at=float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else float(heartbeat),
        heartbeat_at=float(heartbeat),
        stopped=bot.get("stopped") is True,
    )


def read_file(path: Path) -> list[BotEvent]:
    """Events of one producer file; anything invalid is skipped."""
    raw = _read_producer(path)
    if raw is None:
        return []
    source_type, source_instance = raw["source_type"], raw["source_instance"]
    events = raw.get("events")
    if not isinstance(events, list):
        return []
    parsed = (_parse_event(item, source_type, source_instance) for item in events[-MAX_EVENTS_PER_PRODUCER:])
    return [event for event in parsed if event is not None]


class EventReader:
    """New events of the OTHER bots, oldest first (polling, no background thread)."""

    def __init__(
        self,
        own_instance_id: str | None = None,
        directory: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        backlog_seconds: float = 1800.0,
    ) -> None:
        self.own_instance_id = own_instance_id
        self.directory = Path(directory) if directory is not None else default_events_dir()
        self._clock = clock
        # (mtime_ns, size, file id) per file: every publish replaces the file, so
        # even two writes within one clock tick look different.
        self._signatures: dict[str, tuple[int, int, int]] = {}
        self._seen: dict[str, float] = {}
        # On start, recent events (the last half hour) still count as context.
        self._not_before = float(clock()) - backlog_seconds

    def _files(self) -> Iterable[Path]:
        try:
            return sorted(self.directory.glob("*.json"))
        except OSError:
            return ()

    def poll(self) -> list[BotEvent]:
        now = float(self._clock())
        fresh: list[BotEvent] = []
        for path in self._files():
            if path.stem == self.own_instance_id:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if self._signatures.get(path.name) == signature:
                continue
            self._signatures[path.name] = signature
            for event in read_file(path):
                if event.event_id in self._seen or event.at < self._not_before or now - event.at > EVENT_TTL_SECONDS:
                    continue
                self._seen[event.event_id] = event.at
                fresh.append(event)
        for key in [key for key, at in self._seen.items() if now - at > EVENT_TTL_SECONDS]:
            self._seen.pop(key, None)
        fresh.sort(key=lambda event: event.at)
        return fresh

    def bots(self) -> list[BotInfo]:
        """Heartbeats of the OTHER DarkAbyss bots (running or not), by instance ID."""
        found = []
        for path in self._files():
            if path.stem == self.own_instance_id:
                continue
            info = read_bot(path)
            if info is not None:
                found.append(info)
        return sorted(found, key=lambda info: info.instance_id)
