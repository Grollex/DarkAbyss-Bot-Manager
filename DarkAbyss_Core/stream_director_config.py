"""Stream Director settings: defaults, limits and normalization.

The whole instance config of a Stream Director bot is this object (top-level
keys, validated by bots/stream_director/config.schema.json). Secrets are never
part of it: the Twitch OAuth tokens live in the instance's secrets folder
(stream_director_twitch.TokenStore); the Twitch Client ID of a public
application is not a secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import bot_i18n

FEATURES = ("moments", "challenges", "polls", "inbox", "progression", "twitch_chat", "next_stream_poll")
FEATURE_LABELS = {
    "moments": "Community moments (viewers mark moments of the stream)",
    "challenges": "Challenges (viewers suggest and support, the team accepts)",
    "polls": "Polls and predictions (no stakes, no currency)",
    "inbox": "Streamer inbox (questions, games, clips, topics)",
    "progression": "Community level, season and goals",
    "twitch_chat": "Twitch chat commands (!moment, !challenge, !q, !game, !suggest)",
    "next_stream_poll": "After the stream: vote on the next stream from game suggestions",
}

# key: (low, high) for the integer settings shown in the Manager.
LIMITS: dict[str, tuple[int, int]] = {
    "moment_cooldown_seconds": (0, 3600),
    "suggestion_cooldown_seconds": (0, 3600),
    "max_open_challenges_per_user": (1, 20),
    "moment_cluster_seconds": (15, 600),
    "moment_reaction_lag_seconds": (0, 120),
    "notable_moment_min_users": (1, 20),
    "poll_default_minutes": (1, 60),
    "end_grace_minutes": (0, 30),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "language": "en",
    "guild_id": None,
    "channel_id": None,
    "twitch_client_id": "",
    "team_role_ids": [],
    "go_live_role_id": None,
    "features": {name: True for name in FEATURES},
    "moment_cooldown_seconds": 30,
    "suggestion_cooldown_seconds": 60,
    "max_open_challenges_per_user": 3,
    "moment_cluster_seconds": 90,
    "moment_reaction_lag_seconds": 15,
    "notable_moment_min_users": 2,
    "poll_default_minutes": 3,
    "end_grace_minutes": 5,
}

NOT_CONFIGURED_TEXT = (
    "Stream Director has no server and stream channel yet. Choose both on the Stream Director page in the "
    "Manager and press Save; nothing is posted until then."
)
CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9]{20,40}$")
SNOWFLAKE_PATTERN = re.compile(r"^[0-9]{5,25}$")


class ConfigError(ValueError):
    """The Stream Director config is invalid (the bot then fails closed)."""


@dataclass(frozen=True)
class StreamDirectorConfig:
    enabled: bool = False
    language: str = "en"
    guild_id: int | None = None
    channel_id: int | None = None
    twitch_client_id: str = ""
    team_role_ids: tuple[int, ...] = ()
    go_live_role_id: int | None = None
    features: dict[str, bool] = field(default_factory=lambda: {name: True for name in FEATURES})
    moment_cooldown_seconds: int = 30
    suggestion_cooldown_seconds: int = 60
    max_open_challenges_per_user: int = 3
    moment_cluster_seconds: int = 90
    moment_reaction_lag_seconds: int = 15
    notable_moment_min_users: int = 2
    poll_default_minutes: int = 3
    end_grace_minutes: int = 5

    def feature(self, name: str) -> bool:
        return bool(self.features.get(name, False))

    @property
    def configured(self) -> bool:
        return self.guild_id is not None and self.channel_id is not None


def _snowflake(value: Any, label: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a Discord ID.")
    text = str(value).strip()
    if not SNOWFLAKE_PATTERN.fullmatch(text):
        raise ConfigError(f"{label} must be a Discord ID (digits only).")
    return text


def normalize_config(raw: Any) -> dict[str, Any]:
    """Defaults + validation; returns the JSON form that is saved (IDs as strings)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Stream Director config must be an object.")
    data: dict[str, Any] = {}
    enabled = raw.get("enabled", DEFAULT_CONFIG["enabled"])
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be true or false.")
    data["enabled"] = enabled
    try:
        data["language"] = bot_i18n.normalize_language(raw.get("language"), DEFAULT_CONFIG["language"])
    except bot_i18n.LanguageError as exc:
        raise ConfigError(str(exc)) from exc
    data["guild_id"] = _snowflake(raw.get("guild_id"), "Server ID")
    data["channel_id"] = _snowflake(raw.get("channel_id"), "Stream channel ID")
    data["go_live_role_id"] = _snowflake(raw.get("go_live_role_id"), "Go-live role ID")
    client_id = raw.get("twitch_client_id", "") or ""
    if not isinstance(client_id, str):
        raise ConfigError("Twitch Client ID must be text.")
    client_id = client_id.strip().lower()
    if client_id and not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ConfigError("Twitch Client ID looks wrong: copy it from dev.twitch.tv/console (letters and digits).")
    data["twitch_client_id"] = client_id
    roles = raw.get("team_role_ids", [])
    if not isinstance(roles, list):
        raise ConfigError("team_role_ids must be a list of role IDs.")
    data["team_role_ids"] = sorted({_snowflake(item, "Stream team role ID") for item in roles} - {None})
    features = raw.get("features", {})
    if not isinstance(features, dict):
        raise ConfigError("features must be an object.")
    # Features unknown to this version (saved by a newer one) are ignored, so a
    # rollback keeps working; they are not written back.
    data["features"] = {}
    for name in FEATURES:
        value = features.get(name, True)
        if not isinstance(value, bool):
            raise ConfigError(f"features.{name} must be true or false.")
        data["features"][name] = value
    for key, (low, high) in LIMITS.items():
        value = raw.get(key, DEFAULT_CONFIG[key])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"{key} must be a whole number from {low} to {high}.")
        data[key] = value
    return data


def parse_config(data: dict[str, Any]) -> StreamDirectorConfig:
    """Normalized JSON form -> typed config."""
    return StreamDirectorConfig(
        enabled=data["enabled"],
        language=data.get("language", DEFAULT_CONFIG["language"]),
        guild_id=int(data["guild_id"]) if data["guild_id"] else None,
        channel_id=int(data["channel_id"]) if data["channel_id"] else None,
        twitch_client_id=data["twitch_client_id"],
        team_role_ids=tuple(int(item) for item in data["team_role_ids"]),
        go_live_role_id=int(data["go_live_role_id"]) if data["go_live_role_id"] else None,
        features=dict(data["features"]),
        **{key: data[key] for key in LIMITS},
    )


def load_config(raw: Any) -> StreamDirectorConfig:
    return parse_config(normalize_config(raw))


def is_configured(data: dict[str, Any]) -> bool:
    return bool(data.get("guild_id")) and bool(data.get("channel_id"))
