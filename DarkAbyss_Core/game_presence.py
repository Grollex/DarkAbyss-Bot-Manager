"""Game Presence: suggest that members playing the same game get together.

Pure domain logic (no discord.py import) so it is fully testable with a fake
clock; the Discord adapter lives in ``game_presence_discord``.

Pipeline (each part replaceable):

    Discord presence / voice events
        -> ActivityTracker   who plays what right now, who sits in which voice
        -> SessionTracker    when each (member, game) session started (restart grace)
        -> CandidateSelector groups of >= 2 players per guild + game (index, no O(n^2))
        -> pending candidate (delay, aggregation, re-check)
        -> SuggestionPolicy  decides whether/whom to suggest (replaceable)
             uses PreferenceBook (opt-out) and CooldownLedger (anti-spam)
        -> Suggestion        handed to the notifier; mentions are user IDs only

Persistence (opt-out, suggestion history, cooldowns) goes through a small
``PresenceStore`` interface; the bot backs it with the existing per-instance
FeatureStore. Session state is rebuilt from Discord presence (which carries
activity start times) after a restart, and the pending delay always starts
fresh, so a restart never causes an immediate or repeated suggestion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Protocol

MIN_GROUP_SIZE = 2
MAX_MENTIONS = 10
RESTART_GRACE_SECONDS = 120.0
MAX_START_HINT_AGE_SECONDS = 24 * 3600.0
MAX_HISTORY = 300
MAX_GAME_NAME = 100
MAX_LIST_ITEMS = 100
STORE_KEY = "game_presence"

# Config defaults (minutes in the config file; seconds internally).
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "guild_id": None,
    "channel_id": None,
    "delay_minutes": 3,
    "user_cooldown_minutes": 60,
    "group_cooldown_minutes": 180,
    "guild_cooldown_minutes": 15,
    "voice_aware": True,
    "allowlist": [],
    "ignore_list": [],
    "ai_rewrite": False,
}
LIMITS = {
    "delay_minutes": (1, 120),
    "user_cooldown_minutes": (0, 10080),
    "group_cooldown_minutes": (0, 43200),
    "guild_cooldown_minutes": (0, 1440),
}


# --------------------------------------------------------------------------
# game normalization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GameIdentity:
    key: str
    display_name: str


_DECORATIONS = str.maketrans({"™": " ", "®": " ", "©": " "})


class GameNormalizer:
    """Trim/case-normalize names into a stable ``game_key``.

    ``aliases`` maps a normalized variant to a canonical key (e.g.
    {"overwatch": "overwatch 2"}); it is empty for now but lets a future
    alias list merge name variants without touching the trackers.
    """

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        self._aliases = {self.key_of(source): self.key_of(target) for source, target in (aliases or {}).items()}

    @staticmethod
    def key_of(name: str) -> str:
        return " ".join(str(name).translate(_DECORATIONS).split()).casefold()

    def normalize(self, name: Any) -> GameIdentity | None:
        if not isinstance(name, str):
            return None
        display = " ".join(name.split())[:MAX_GAME_NAME]
        key = self.key_of(display)
        if not key:
            return None
        return GameIdentity(self._aliases.get(key, key), display)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GamePresenceConfig:
    enabled: bool = False
    guild_id: int | None = None
    channel_id: int | None = None
    delay_seconds: float = 180.0
    user_cooldown_seconds: float = 3600.0
    group_cooldown_seconds: float = 10800.0
    guild_cooldown_seconds: float = 900.0
    voice_aware: bool = True
    allowlist: frozenset[str] = frozenset()
    ignore_list: frozenset[str] = frozenset()
    ai_rewrite: bool = False

    @property
    def active(self) -> bool:
        return self.enabled and self.guild_id is not None and self.channel_id is not None

    def game_allowed(self, game_key: str) -> bool:
        if game_key in self.ignore_list:
            return False
        return not self.allowlist or game_key in self.allowlist


def _snowflake(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f'"game_presence.{field_name}" must be a Discord ID.')
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    raise ValueError(f'"game_presence.{field_name}" must be a Discord ID.')


def normalize_config_dict(raw: Any) -> dict[str, Any]:
    """Validate the ``game_presence`` config section; fail closed with ValueError.

    Missing keys get defaults, so older configs work without migration.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('"game_presence" must be an object.')
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(f'Unknown "game_presence" setting: {unknown[0]}.')
    merged = {**DEFAULT_CONFIG, **raw}
    out: dict[str, Any] = {}
    for flag in ("enabled", "voice_aware", "ai_rewrite"):
        if not isinstance(merged[flag], bool):
            raise ValueError(f'"game_presence.{flag}" must be true or false.')
        out[flag] = merged[flag]
    guild_id = _snowflake(merged["guild_id"], "guild_id")
    channel_id = _snowflake(merged["channel_id"], "channel_id")
    out["guild_id"] = None if guild_id is None else str(guild_id)
    out["channel_id"] = None if channel_id is None else str(channel_id)
    for name, (low, high) in LIMITS.items():
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'"game_presence.{name}" must be a whole number from {low} to {high}.')
        out[name] = value
    for name in ("allowlist", "ignore_list"):
        items = merged[name]
        if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
            raise ValueError(f'"game_presence.{name}" must be a list of up to {MAX_LIST_ITEMS} game names.')
        cleaned = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or len(item) > MAX_GAME_NAME:
                raise ValueError(f'"game_presence.{name}" must contain non-empty game names.')
            text = " ".join(item.split())
            if text not in cleaned:
                cleaned.append(text)
        out[name] = cleaned
    if out["enabled"] and (out["guild_id"] is None or out["channel_id"] is None):
        raise ValueError('"game_presence" is enabled but has no server/channel; choose them in Manager -> Game Presence.')
    return out


NOT_CONFIGURED_TEXT = "Choose a server and a suggestion channel in Manager -> Game Presence."


def normalize_bot_config(raw: Any) -> dict[str, Any]:
    """Config of a dedicated Game Presence bot instance (top-level keys).

    Same validation as ``normalize_config_dict``; the only difference is that a
    bot which is switched on but has no server/channel yet is valid and simply
    "not configured" (nothing is posted until both are chosen).
    """
    if isinstance(raw, dict) and raw.get("enabled") is True and (raw.get("guild_id") is None or raw.get("channel_id") is None):
        data = normalize_config_dict({**raw, "enabled": False})
        data["enabled"] = True
        return data
    return normalize_config_dict(raw)


def is_configured(data: Mapping[str, Any]) -> bool:
    return data.get("guild_id") is not None and data.get("channel_id") is not None


def parse_bot_config(raw: Any, normalizer: GameNormalizer | None = None) -> GamePresenceConfig:
    """GamePresenceConfig for a dedicated bot; "not configured" = inactive (no posting)."""
    data = normalize_bot_config(raw)
    return parse_config({**data, "enabled": data["enabled"] and is_configured(data)}, normalizer)


def parse_config(raw: Any, normalizer: GameNormalizer | None = None) -> GamePresenceConfig:
    data = normalize_config_dict(raw)
    normalizer = normalizer or GameNormalizer()

    def keys(names: list[str]) -> frozenset[str]:
        return frozenset(identity.key for identity in map(normalizer.normalize, names) if identity is not None)

    return GamePresenceConfig(
        enabled=data["enabled"],
        guild_id=None if data["guild_id"] is None else int(data["guild_id"]),
        channel_id=None if data["channel_id"] is None else int(data["channel_id"]),
        delay_seconds=data["delay_minutes"] * 60.0,
        user_cooldown_seconds=data["user_cooldown_minutes"] * 60.0,
        group_cooldown_seconds=data["group_cooldown_minutes"] * 60.0,
        guild_cooldown_seconds=data["guild_cooldown_minutes"] * 60.0,
        voice_aware=data["voice_aware"],
        allowlist=keys(data["allowlist"]),
        ignore_list=keys(data["ignore_list"]),
        ai_rewrite=data["ai_rewrite"],
    )


# --------------------------------------------------------------------------
# trackers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityRecord:
    guild_id: int
    user_id: int
    game_key: str
    game_display_name: str
    started_at: float
    voice_channel_id: int | None


class ActivityTracker:
    """Who plays what right now and who sits in which voice channel.

    Keeps an index (guild, game_key) -> user IDs so candidate selection
    never compares every player with every other player.
    """

    def __init__(self) -> None:
        self._games: dict[tuple[int, int], GameIdentity] = {}
        self._voice: dict[tuple[int, int], int] = {}
        self._index: dict[tuple[int, str], set[int]] = {}

    def set_game(self, guild_id: int, user_id: int, game: GameIdentity | None) -> tuple[GameIdentity | None, GameIdentity | None]:
        key = (guild_id, user_id)
        old = self._games.get(key)
        if old == game:
            return old, game
        if old is not None:
            players = self._index.get((guild_id, old.key))
            if players is not None:
                players.discard(user_id)
                if not players:
                    self._index.pop((guild_id, old.key), None)
        if game is None:
            self._games.pop(key, None)
        else:
            self._games[key] = game
            self._index.setdefault((guild_id, game.key), set()).add(user_id)
        return old, game

    def set_voice(self, guild_id: int, user_id: int, channel_id: int | None) -> None:
        if channel_id is None:
            self._voice.pop((guild_id, user_id), None)
        else:
            self._voice[(guild_id, user_id)] = channel_id

    def remove(self, guild_id: int, user_id: int) -> GameIdentity | None:
        old, _ = self.set_game(guild_id, user_id, None)
        self._voice.pop((guild_id, user_id), None)
        return old

    def game_of(self, guild_id: int, user_id: int) -> GameIdentity | None:
        return self._games.get((guild_id, user_id))

    def voice_of(self, guild_id: int, user_id: int) -> int | None:
        return self._voice.get((guild_id, user_id))

    def players(self, guild_id: int, game_key: str) -> set[int]:
        return set(self._index.get((guild_id, game_key), ()))

    def game_keys(self, guild_id: int) -> list[str]:
        return [key for (guild, key) in self._index if guild == guild_id]

    def player_count(self, guild_id: int) -> int:
        return sum(1 for (guild, _user) in self._games if guild == guild_id)

    def drop_guild(self, guild_id: int) -> None:
        for key in [key for key in self._games if key[0] == guild_id]:
            self.remove(*key)
        for key in [key for key in self._voice if key[0] == guild_id]:
            self._voice.pop(key, None)


class SessionTracker:
    """Session start per (guild, user, game) with a grace period for restarts.

    A game closed and reopened within ``restart_grace`` keeps its original
    start, so a quick restart does not look like a brand-new session.
    """

    def __init__(self, restart_grace: float = RESTART_GRACE_SECONDS) -> None:
        self.restart_grace = restart_grace
        self._active: dict[tuple[int, int, str], float] = {}
        self._ended: dict[tuple[int, int, str], tuple[float, float]] = {}

    def start(self, guild_id: int, user_id: int, game_key: str, now: float, start_hint: float | None = None) -> float:
        key = (guild_id, user_id, game_key)
        if key in self._active:
            return self._active[key]
        ended = self._ended.pop(key, None)
        if ended is not None and now - ended[1] <= self.restart_grace:
            started = ended[0]
        elif start_hint is not None and now - MAX_START_HINT_AGE_SECONDS <= start_hint <= now:
            started = start_hint
        else:
            started = now
        self._active[key] = started
        return started

    def end(self, guild_id: int, user_id: int, game_key: str, now: float) -> None:
        key = (guild_id, user_id, game_key)
        started = self._active.pop(key, None)
        if started is not None:
            self._ended[key] = (started, now)

    def started_at(self, guild_id: int, user_id: int, game_key: str) -> float | None:
        return self._active.get((guild_id, user_id, game_key))

    def prune(self, now: float) -> None:
        for key in [key for key, (_start, ended) in self._ended.items() if now - ended > self.restart_grace]:
            self._ended.pop(key, None)

    def __len__(self) -> int:
        return len(self._active)


# --------------------------------------------------------------------------
# candidates and policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateGroup:
    guild_id: int
    game_key: str
    game_display_name: str
    members: tuple[ActivityRecord, ...]


class CandidateSelector:
    """Groups of >= MIN_GROUP_SIZE players per guild + game (via the tracker index)."""

    def groups(
        self,
        tracker: ActivityTracker,
        sessions: SessionTracker,
        guild_id: int,
        config: GamePresenceConfig,
    ) -> list[CandidateGroup]:
        groups = []
        for game_key in tracker.game_keys(guild_id):
            if not config.game_allowed(game_key):
                continue
            members = []
            display = game_key
            for user_id in sorted(tracker.players(guild_id, game_key)):
                game = tracker.game_of(guild_id, user_id)
                started = sessions.started_at(guild_id, user_id, game_key)
                if game is None or started is None:
                    continue
                display = game.display_name
                members.append(
                    ActivityRecord(guild_id, user_id, game_key, game.display_name, started, tracker.voice_of(guild_id, user_id))
                )
            if len(members) >= MIN_GROUP_SIZE:
                groups.append(CandidateGroup(guild_id, game_key, display, tuple(members)))
        return groups


@dataclass(frozen=True)
class Suggestion:
    guild_id: int
    game_key: str
    game_display_name: str
    kind: str  # "gather" (nobody together) or "join" (some already share a voice channel)
    target_user_ids: tuple[int, ...]  # everyone mentioned, in message order
    outsider_user_ids: tuple[int, ...] = ()  # join: players not in the voice channel
    voice_member_ids: tuple[int, ...] = ()  # join: players already in the voice channel
    voice_channel_id: int | None = None


@dataclass(frozen=True)
class PolicyContext:
    now: float
    config: GamePresenceConfig
    is_muted: Callable[[int, int], bool]
    ledger: "CooldownLedger"


class SuggestionPolicy(Protocol):
    """Replaceable decision step (role-based, affinity, large-guild policies...)."""

    def evaluate(self, group: CandidateGroup, context: PolicyContext) -> Suggestion | None:
        ...


class DefaultSuggestionPolicy:
    """MVP policy: >= 2 settled players, not all in one voice, opt-out and cooldowns respected."""

    def evaluate(self, group: CandidateGroup, context: PolicyContext) -> Suggestion | None:
        config, now, ledger = context.config, context.now, context.ledger
        settled = [record for record in group.members if now - record.started_at >= config.delay_seconds]
        if len(settled) < MIN_GROUP_SIZE:
            return None
        if not ledger.guild_available(group.guild_id, now, config):
            return None
        eligible = [
            record
            for record in group.members
            if not context.is_muted(group.guild_id, record.user_id)
            and ledger.user_available(group.guild_id, record.user_id, now, config)
        ]
        if len(eligible) < MIN_GROUP_SIZE:
            return None
        # Everyone eligible already together in one voice channel: nothing to suggest.
        channels = {record.voice_channel_id for record in eligible}
        if len(channels) == 1 and None not in channels:
            return None
        eligible = eligible[:MAX_MENTIONS]
        target_ids = tuple(record.user_id for record in eligible)
        if ledger.group_recently_suggested(group.guild_id, group.game_key, target_ids, now, config):
            return None
        if config.voice_aware:
            cluster_channel, cluster = _largest_voice_cluster(eligible)
            if cluster_channel is not None and len(cluster) >= MIN_GROUP_SIZE:
                outsiders = tuple(record.user_id for record in eligible if record.voice_channel_id != cluster_channel)
                insiders = tuple(record.user_id for record in cluster)
                return Suggestion(
                    group.guild_id,
                    group.game_key,
                    group.game_display_name,
                    "join",
                    outsiders + insiders,
                    outsiders,
                    insiders,
                    cluster_channel,
                )
        return Suggestion(group.guild_id, group.game_key, group.game_display_name, "gather", target_ids)


def _largest_voice_cluster(records: Iterable[ActivityRecord]) -> tuple[int | None, list[ActivityRecord]]:
    clusters: dict[int, list[ActivityRecord]] = {}
    for record in records:
        if record.voice_channel_id is not None:
            clusters.setdefault(record.voice_channel_id, []).append(record)
    if not clusters:
        return None, []
    channel_id, members = max(clusters.items(), key=lambda item: (len(item[1]), -item[0]))
    return channel_id, members


# --------------------------------------------------------------------------
# persistence: preferences and cooldowns
# --------------------------------------------------------------------------


class PresenceStore(Protocol):
    def load(self, guild_id: int) -> dict[str, Any]:
        ...

    def save(self, guild_id: int, state: dict[str, Any]) -> None:
        ...


class MemoryPresenceStore:
    def __init__(self) -> None:
        self._data: dict[int, dict[str, Any]] = {}

    def load(self, guild_id: int) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self._data.get(guild_id, {}))

    def save(self, guild_id: int, state: dict[str, Any]) -> None:
        import copy

        self._data[guild_id] = copy.deepcopy(state)


class FeatureStorePresenceStore:
    """Game Presence state inside the existing per-instance FeatureStore (key STORE_KEY)."""

    def __init__(self, feature_store: Any) -> None:
        self._store = feature_store

    def load(self, guild_id: int) -> dict[str, Any]:
        value = self._store.get(guild_id, STORE_KEY, {})
        return value if isinstance(value, dict) else {}

    def save(self, guild_id: int, state: dict[str, Any]) -> None:
        self._store.set(guild_id, STORE_KEY, state or None)


class PreferenceBook:
    """Per guild + user opt-out of Game Presence mentions (default: allowed)."""

    def __init__(self, store: PresenceStore, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock

    def is_muted(self, guild_id: int, user_id: int) -> bool:
        return str(user_id) in (self._store.load(guild_id).get("muted") or {})

    def set_muted(self, guild_id: int, user_id: int, muted: bool) -> bool:
        """Returns True if the state changed (False = it already was so)."""
        state = self._store.load(guild_id)
        muted_users = dict(state.get("muted") or {})
        key = str(user_id)
        if (key in muted_users) == muted:
            return False
        if muted:
            muted_users[key] = self._clock()
        else:
            muted_users.pop(key, None)
        state["muted"] = muted_users
        self._store.save(guild_id, state)
        return True


class CooldownLedger:
    """Suggestion history and cooldowns (persistent, survives restarts)."""

    def __init__(self, store: PresenceStore) -> None:
        self._store = store

    def guild_available(self, guild_id: int, now: float, config: GamePresenceConfig) -> bool:
        last = self._store.load(guild_id).get("guild_at")
        return not isinstance(last, (int, float)) or now - last >= config.guild_cooldown_seconds

    def user_available(self, guild_id: int, user_id: int, now: float, config: GamePresenceConfig) -> bool:
        last = (self._store.load(guild_id).get("users") or {}).get(str(user_id))
        return not isinstance(last, (int, float)) or now - last >= config.user_cooldown_seconds

    def group_recently_suggested(
        self, guild_id: int, game_key: str, user_ids: Iterable[int], now: float, config: GamePresenceConfig
    ) -> bool:
        """Same game and >= 2 of the same people within the group cooldown.

        Overlap instead of exact equality: one person joining or leaving a
        group that was already suggested does not produce a new message.
        """
        wanted = {str(user_id) for user_id in user_ids}
        for entry in self._store.load(guild_id).get("history") or []:
            if not isinstance(entry, dict) or entry.get("game") != game_key:
                continue
            at = entry.get("at")
            if not isinstance(at, (int, float)) or now - at >= config.group_cooldown_seconds:
                continue
            if len(wanted & {str(user) for user in entry.get("users") or []}) >= MIN_GROUP_SIZE:
                return True
        return False

    def record(self, suggestion: Suggestion, now: float, config: GamePresenceConfig) -> None:
        state = self._store.load(suggestion.guild_id)
        horizon = max(config.group_cooldown_seconds, config.user_cooldown_seconds, config.guild_cooldown_seconds)
        history = [
            entry
            for entry in state.get("history") or []
            if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) and now - entry["at"] < horizon
        ]
        history.append(
            {
                "game": suggestion.game_key,
                "name": suggestion.game_display_name,
                "users": [str(user) for user in suggestion.target_user_ids],
                "at": now,
            }
        )
        users = {
            key: value
            for key, value in (state.get("users") or {}).items()
            if isinstance(value, (int, float)) and now - value < config.user_cooldown_seconds
        }
        for user_id in suggestion.target_user_ids:
            users[str(user_id)] = now
        state["history"] = history[-MAX_HISTORY:]
        state["users"] = users
        state["guild_at"] = now
        self._store.save(suggestion.guild_id, state)

    def last_suggestion(self, guild_id: int) -> dict[str, Any] | None:
        history = self._store.load(guild_id).get("history") or []
        return history[-1] if history else None


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------


@dataclass
class _Pending:
    created_at: float


@dataclass
class GamePresenceEngine:
    """Ties the parts together; the Discord runtime feeds events and calls tick()."""

    store: PresenceStore
    clock: Callable[[], float] = time.time
    policy: SuggestionPolicy = field(default_factory=DefaultSuggestionPolicy)
    normalizer: GameNormalizer = field(default_factory=GameNormalizer)
    selector: CandidateSelector = field(default_factory=CandidateSelector)
    config: GamePresenceConfig = field(default_factory=GamePresenceConfig)

    def __post_init__(self) -> None:
        self.tracker = ActivityTracker()
        self.sessions = SessionTracker()
        self.preferences = PreferenceBook(self.store, self.clock)
        self.ledger = CooldownLedger(self.store)
        self._pending: dict[tuple[int, str], _Pending] = {}

    # -- configuration ------------------------------------------------------

    def configure(self, config: GamePresenceConfig) -> None:
        if config.guild_id != self.config.guild_id:
            # Another server: forget everything tracked for the old one.
            if self.config.guild_id is not None:
                self.tracker.drop_guild(self.config.guild_id)
            self._pending.clear()
        self.config = config

    def tracks(self, guild_id: int) -> bool:
        return self.config.active and guild_id == self.config.guild_id

    # -- events ---------------------------------------------------------------

    def observe(
        self,
        guild_id: int,
        user_id: int,
        game_name: str | None,
        voice_channel_id: int | None,
        start_hint: float | None = None,
    ) -> None:
        """Full current state of one member (presence update, join, or seeding)."""
        if not self.tracks(guild_id):
            return
        now = self.clock()
        game = self.normalizer.normalize(game_name) if game_name else None
        old, new = self.tracker.set_game(guild_id, user_id, game)
        if old is not None and (new is None or new.key != old.key):
            self.sessions.end(guild_id, user_id, old.key, now)
        if new is not None:
            self.sessions.start(guild_id, user_id, new.key, now, start_hint)
        self.tracker.set_voice(guild_id, user_id, voice_channel_id)

    def set_voice(self, guild_id: int, user_id: int, voice_channel_id: int | None) -> None:
        if self.tracks(guild_id):
            self.tracker.set_voice(guild_id, user_id, voice_channel_id)

    def remove_member(self, guild_id: int, user_id: int) -> None:
        old = self.tracker.remove(guild_id, user_id)
        if old is not None:
            self.sessions.end(guild_id, user_id, old.key, self.clock())

    # -- decisions -------------------------------------------------------------

    def tick(self) -> list[Suggestion]:
        """Create/cancel pending candidates and re-check the due ones.

        A suggestion is never produced on first sight: a candidate group must
        exist for ``delay`` before the policy re-checks the CURRENT state.
        """
        now = self.clock()
        self.sessions.prune(now)
        config = self.config
        if not config.active:
            self._pending.clear()
            return []
        guild_id = config.guild_id
        groups = {group.game_key: group for group in self.selector.groups(self.tracker, self.sessions, guild_id, config)}
        for key in [key for key in self._pending if key[1] not in groups]:
            self._pending.pop(key, None)  # group fell apart: cancel silently
        suggestions = []
        for game_key, group in groups.items():
            pending = self._pending.setdefault((guild_id, game_key), _Pending(now))
            if now - pending.created_at < config.delay_seconds:
                continue
            context = PolicyContext(now, config, self.preferences.is_muted, self.ledger)
            suggestion = self.policy.evaluate(group, context)
            # Re-arm: the next re-check happens one delay later at the earliest.
            self._pending[(guild_id, game_key)] = _Pending(now)
            if suggestion is not None:
                suggestions.append(suggestion)
        return suggestions

    def mark_sent(self, suggestion: Suggestion) -> None:
        self.ledger.record(suggestion, self.clock(), self.config)

    # -- status ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        guild_id = self.config.guild_id
        games = []
        if guild_id is not None:
            for key in self.tracker.game_keys(guild_id):
                players = self.tracker.players(guild_id, key)
                identity = self.tracker.game_of(guild_id, next(iter(players))) if players else None
                games.append({"game": identity.display_name if identity else key, "players": len(players)})
        games.sort(key=lambda item: (-item["players"], item["game"]))
        last = self.ledger.last_suggestion(guild_id) if guild_id is not None else None
        return {
            "tracked_players": self.tracker.player_count(guild_id) if guild_id is not None else 0,
            "top_games": games[:5],
            "pending_groups": len(self._pending),
            "last_suggestion": last,
        }


def with_config(engine: GamePresenceEngine, **changes: Any) -> None:
    """Test/helper convenience: update selected config fields."""
    engine.configure(replace(engine.config, **changes))
