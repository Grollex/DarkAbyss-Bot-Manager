"""Kairo's long-term social memory of each Discord server (one Admin instance).

``instances/<id>/data/social_memory.json``::

    {"version": 1, "guilds": {"<guild id>": {
        "lore":     [entry...],     Server Lore: durable things worth knowing
        "outcomes": [outcome...],   how people took Kairo's autonomous actions
        "mutes":    [mute...]}}}    "be quiet" wishes (explicit or automatic)

Server Lore is NOT a chat archive. The AI analysis may propose at most a few
items (a local meme, a nickname people use, a running joke, a social pattern,
a notable server event); a new item is only a *candidate* until it is
proposed again in a later analysis (at least CONFIRM_GAP later), so one-off
remarks never become lore. Text is validated (length, no mentions, links, IDs,
e-mail addresses or phone numbers), counts are capped per server, candidates
expire, and stale low-confidence lore fades out. The Manager can list, forget
and clear it.

The file is shared by the bot and the Manager: every change is
read-modify-write under a lock file and an atomic replace. An unreadable file
raises ``SocialMemoryError`` and is never overwritten (Social Awareness then
stays silent: it cannot know the quiet wishes); ``reset`` keeps a copy.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import locked_json

FILE_NAME = "social_memory.json"
VERSION = 1
LORE_KINDS = ("meme", "nickname", "joke", "relation", "event", "norm", "fact")
LORE_TEXT_RANGE = (12, 180)
MAX_ACTIVE_LORE = 30
MAX_CANDIDATES = 20
CONFIRM_GAP = 600.0  # a candidate needs a second proposal at least 10 minutes later
CANDIDATE_TTL = 7 * 86400.0
STALE_LORE_SECONDS = 120 * 86400.0
STALE_LORE_CONFIRMATIONS = 3
MAX_OUTCOMES = 40
MAX_MUTES = 50
MAX_MUTE_SECONDS = 30 * 86400
OUTCOME_RESULTS = ("engaged", "positive", "neutral", "ignored", "negative")
MUTE_SCOPES = ("guild", "channel", "user")


class SocialMemoryError(RuntimeError):
    """The memory file cannot be read; nothing is written over it."""


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class LoreEntry:
    id: str
    kind: str
    text: str
    status: str  # "candidate" | "active"
    confirmations: int
    created_at: float
    updated_at: float


@dataclass
class Outcome:
    at: float
    channel_id: int
    action: str  # "reply" | "react"
    reason: str  # what triggered the analysis
    result: str  # OUTCOME_RESULTS


@dataclass
class Mute:
    id: str
    scope: str  # "guild" | "channel" | "user"
    until: float
    reason: str  # "asked" | "auto"
    at: float
    channel_id: int | None = None
    user_id: int | None = None
    by_user_id: int | None = None

    def covers(self, channel_id: int | None, parent_id: int | None, user_ids: tuple[int, ...]) -> bool:
        if self.scope == "guild":
            return True
        if self.scope == "channel":
            return self.channel_id is not None and self.channel_id in (channel_id, parent_id)
        return self.user_id is not None and self.user_id in user_ids


@dataclass
class GuildMemory:
    lore: list[LoreEntry] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    mutes: list[Mute] = field(default_factory=list)

    def active_lore(self) -> list[LoreEntry]:
        return [entry for entry in self.lore if entry.status == "active"]


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_BANNED = re.compile(
    r"<[@#&!:a]|@everyone|@here|https?://|www\.|discord\.gg|\d{15,}|[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d",
    re.IGNORECASE,
)


def clean_lore_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip(" .\"'«»")
    if not LORE_TEXT_RANGE[0] <= len(text) <= LORE_TEXT_RANGE[1] or _BANNED.search(text):
        return None
    return text


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"\w+", text.casefold()) if len(token) > 2}


def similar(left: str, right: str) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return left.casefold() == right.casefold()
    return len(a & b) / len(a | b) >= 0.6 or a <= b or b <= a


def _snowflake(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _parse_guild(raw: Any) -> GuildMemory:
    if not isinstance(raw, dict):
        raise ValueError("guild entry must be an object")
    memory = GuildMemory()
    for item in raw.get("lore") or []:
        if not isinstance(item, dict):
            continue
        text = clean_lore_text(item.get("text"))
        if text is None or item.get("kind") not in LORE_KINDS or item.get("status") not in ("candidate", "active"):
            continue
        memory.lore.append(
            LoreEntry(
                id=str(item.get("id") or f"l{secrets.token_hex(3)}")[:16],
                kind=item["kind"],
                text=text,
                status=item["status"],
                confirmations=max(1, int(_number(item.get("confirmations"), 1))),
                created_at=_number(item.get("created_at")),
                updated_at=_number(item.get("updated_at")),
            )
        )
    for item in raw.get("outcomes") or []:
        if isinstance(item, dict) and item.get("result") in OUTCOME_RESULTS and _snowflake(item.get("channel_id")):
            memory.outcomes.append(
                Outcome(_number(item.get("at")), _snowflake(item["channel_id"]), str(item.get("action") or "reply")[:10], str(item.get("reason") or "")[:20], item["result"])
            )
    for item in raw.get("mutes") or []:
        if isinstance(item, dict) and item.get("scope") in MUTE_SCOPES:
            memory.mutes.append(
                Mute(
                    id=str(item.get("id") or f"q{secrets.token_hex(3)}")[:16],
                    scope=item["scope"],
                    until=_number(item.get("until")),
                    reason="auto" if item.get("reason") == "auto" else "asked",
                    at=_number(item.get("at")),
                    channel_id=_snowflake(item.get("channel_id")),
                    user_id=_snowflake(item.get("user_id")),
                    by_user_id=_snowflake(item.get("by_user_id")),
                )
            )
    return memory


def _dump_guild(memory: GuildMemory) -> dict[str, Any]:
    def plain(record: Any) -> dict[str, Any]:
        data = asdict(record)
        for key in ("channel_id", "user_id", "by_user_id"):
            if key in data and data[key] is not None:
                data[key] = str(data[key])
        return data

    return {
        "lore": [plain(entry) for entry in memory.lore],
        "outcomes": [plain(outcome) for outcome in memory.outcomes],
        "mutes": [plain(mute) for mute in memory.mutes],
    }


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class SocialMemory:
    """Server Lore, autonomy outcomes and quiet wishes of one Kairo instance."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._cache: tuple[tuple[int, int] | None, dict[str, GuildMemory]] | None = None

    # -- file ------------------------------------------------------------------------

    def _locked(self):
        return locked_json.locked(self.path, error=SocialMemoryError)

    def _signature(self) -> tuple[int, int] | None:
        return locked_json.signature(self.path)

    def _read(self) -> dict[str, GuildMemory]:
        signature = self._signature()
        if self._cache is not None and self._cache[0] == signature:
            return self._cache[1]
        guilds: dict[str, GuildMemory] = {}
        if signature is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("guilds"), dict):
                    raise ValueError("unexpected shape")
                guilds = {str(key): _parse_guild(value) for key, value in raw["guilds"].items() if str(key).isdigit()}
            except (OSError, ValueError) as exc:
                raise SocialMemoryError(f"Kairo's social memory file {self.path.name} is unreadable ({type(exc).__name__}).") from exc
        self._cache = (signature, guilds)
        return guilds

    def _write(self, guilds: dict[str, GuildMemory]) -> None:
        payload = {"version": VERSION, "guilds": {key: _dump_guild(value) for key, value in sorted(guilds.items())}}
        locked_json.atomic_write_json(self.path, payload)
        self._cache = (self._signature(), guilds)

    def _change(self, guild_id: Any, change: Callable[[GuildMemory, float], Any]) -> Any:
        with self._locked():
            self._cache = None  # always the latest file inside the lock
            guilds = {key: value for key, value in self._read().items()}
            key = str(int(guild_id))
            memory = guilds.setdefault(key, GuildMemory())
            now = float(self._clock())
            result = change(memory, now)
            self._prune(memory, now)
            if not memory.lore and not memory.outcomes and not memory.mutes:
                guilds.pop(key, None)
            self._write(guilds)
            return result

    @staticmethod
    def _prune(memory: GuildMemory, now: float) -> None:
        memory.mutes = [mute for mute in memory.mutes if mute.until > now][-MAX_MUTES:]
        memory.outcomes = memory.outcomes[-MAX_OUTCOMES:]
        kept = []
        for entry in memory.lore:
            if entry.status == "candidate" and now - entry.updated_at > CANDIDATE_TTL:
                continue
            if entry.status == "active" and now - entry.updated_at > STALE_LORE_SECONDS and entry.confirmations < STALE_LORE_CONFIRMATIONS:
                continue
            kept.append(entry)
        candidates = [entry for entry in kept if entry.status == "candidate"]
        while len(candidates) > MAX_CANDIDATES:
            oldest = min(candidates, key=lambda entry: entry.updated_at)
            candidates.remove(oldest)
            kept.remove(oldest)
        memory.lore = kept

    # -- reading -----------------------------------------------------------------------

    def guild(self, guild_id: Any) -> GuildMemory:
        """Raises SocialMemoryError when the file is unreadable."""
        return self._read().get(str(int(guild_id)), GuildMemory())

    def guild_ids(self) -> list[int]:
        return sorted(int(key) for key in self._read())

    def check(self) -> str | None:
        try:
            self._read()
        except SocialMemoryError as exc:
            return str(exc)
        return None

    # -- Server Lore -------------------------------------------------------------------

    def propose(self, guild_id: Any, op: str, *, kind: Any = None, text: Any = None, entry_id: Any = None) -> str | None:
        """Apply one AI proposal; returns what happened ("candidate", "confirmed",
        "activated", "updated", "forgot") or None when it was refused."""
        cleaned = clean_lore_text(text) if text is not None else None
        if op == "remember" and (cleaned is None or kind not in LORE_KINDS):
            return None
        if op == "update" and (cleaned is None or not isinstance(entry_id, str)):
            return None
        if op == "forget" and not isinstance(entry_id, str):
            return None
        if op not in ("remember", "update", "forget"):
            return None

        def change(memory: GuildMemory, now: float) -> str | None:
            if op == "forget":
                before = len(memory.lore)
                memory.lore = [entry for entry in memory.lore if not (entry.id == entry_id and entry.status == "active")]
                return "forgot" if len(memory.lore) < before else None
            if op == "update":
                for entry in memory.lore:
                    if entry.id == entry_id and entry.status == "active":
                        entry.text, entry.updated_at = cleaned, now
                        return "updated"
                return None
            for entry in memory.active_lore():
                if similar(entry.text, cleaned):
                    if now - entry.updated_at >= CONFIRM_GAP:
                        entry.confirmations += 1
                        entry.updated_at = now
                        return "confirmed"
                    return None
            for entry in memory.lore:
                if entry.status == "candidate" and similar(entry.text, cleaned):
                    if now - entry.updated_at < CONFIRM_GAP:
                        return None  # the same moment again: not a second sighting
                    entry.status, entry.confirmations, entry.updated_at, entry.text = "active", entry.confirmations + 1, now, cleaned
                    active = memory.active_lore()
                    while len(active) > MAX_ACTIVE_LORE:
                        weakest = min((item for item in active if item is not entry), key=lambda item: (item.confirmations, item.updated_at))
                        memory.lore.remove(weakest)
                        active.remove(weakest)
                    return "activated"
            memory.lore.append(LoreEntry(f"l{secrets.token_hex(3)}", kind, cleaned, "candidate", 1, now, now))
            return "candidate"

        return self._change(guild_id, change)

    def forget(self, guild_id: Any, entry_id: str) -> bool:
        def change(memory: GuildMemory, now: float) -> bool:
            before = len(memory.lore)
            memory.lore = [entry for entry in memory.lore if entry.id != entry_id]
            return len(memory.lore) < before

        return bool(self._change(guild_id, change))

    def clear_lore(self, guild_id: Any) -> int:
        def change(memory: GuildMemory, now: float) -> int:
            count = len(memory.lore)
            memory.lore = []
            return count

        return int(self._change(guild_id, change))

    # -- feedback ------------------------------------------------------------------------

    def record_outcome(self, guild_id: Any, outcome: Outcome) -> None:
        if outcome.result not in OUTCOME_RESULTS:
            return
        self._change(guild_id, lambda memory, now: memory.outcomes.append(outcome))

    def clear_outcomes(self, guild_id: Any) -> None:
        self._change(guild_id, lambda memory, now: setattr(memory, "outcomes", []))

    # -- quiet ------------------------------------------------------------------------------

    def add_mute(
        self,
        guild_id: Any,
        scope: str,
        seconds: float,
        *,
        reason: str = "asked",
        channel_id: int | None = None,
        user_id: int | None = None,
        by_user_id: int | None = None,
    ) -> Mute:
        if scope not in MUTE_SCOPES:
            raise ValueError("unknown quiet scope")

        def change(memory: GuildMemory, now: float) -> Mute:
            until = now + max(60.0, min(float(seconds), MAX_MUTE_SECONDS))
            # One wish per place: a new one replaces the old one there.
            memory.mutes = [
                mute
                for mute in memory.mutes
                if not (mute.scope == scope and mute.channel_id == (channel_id if scope == "channel" else None) and mute.user_id == (user_id if scope == "user" else None))
            ]
            mute = Mute(
                id=f"q{secrets.token_hex(3)}",
                scope=scope,
                until=until,
                reason=reason,
                at=now,
                channel_id=channel_id if scope == "channel" else None,
                user_id=user_id if scope == "user" else None,
                by_user_id=by_user_id,
            )
            memory.mutes.append(mute)
            return mute

        return self._change(guild_id, change)

    def lift(self, guild_id: Any, predicate: Callable[[Mute], bool]) -> int:
        def change(memory: GuildMemory, now: float) -> int:
            before = len(memory.mutes)
            memory.mutes = [mute for mute in memory.mutes if not predicate(mute)]
            return before - len(memory.mutes)

        return int(self._change(guild_id, change))

    def active_mutes(self, guild_id: Any, now: float | None = None) -> list[Mute]:
        moment = float(self._clock()) if now is None else now
        return [mute for mute in self.guild(guild_id).mutes if mute.until > moment]

    def quiet_for(self, guild_id: Any, channel_id: int | None, parent_id: int | None = None, user_ids: tuple[int, ...] = (), now: float | None = None) -> Mute | None:
        for mute in self.active_mutes(guild_id, now):
            if mute.covers(channel_id, parent_id, tuple(user_ids)):
                return mute
        return None

    # -- whole file ------------------------------------------------------------------------

    def clear_guild(self, guild_id: Any) -> None:
        def change(memory: GuildMemory, now: float) -> None:
            memory.lore, memory.outcomes, memory.mutes = [], [], []

        self._change(guild_id, change)

    def reset(self) -> Path | None:
        """Start over after an unreadable file; the old file is kept next to it."""
        with self._locked():
            backup = None
            if self.path.exists():
                backup = self.path.with_name(f"{self.path.stem}.corrupt-{int(time.time())}.json")
                os.replace(self.path, backup)
            self._cache = None
            return backup


def feedback_counts(outcomes: list[Outcome], channel_id: int | None = None, last: int = 10) -> dict[str, int]:
    picked = [item for item in outcomes if channel_id is None or item.channel_id == channel_id][-last:]
    counts = {result: 0 for result in OUTCOME_RESULTS}
    for item in picked:
        counts[item.result] += 1
    return counts


def feedback_score(counts: dict[str, int]) -> float:
    """> 0: people welcomed Kairo's interventions; < 0: they ignored or disliked them."""
    return counts["engaged"] + counts["positive"] - 2.0 * counts["negative"] - 0.7 * counts["ignored"]
