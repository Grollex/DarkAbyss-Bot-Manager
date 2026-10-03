"""Stream Director domain: every stream is one community session.

No Discord, no network here. Inputs are platform-neutral:

    StreamEvent   a Twitch EventSub notification (online, offline, update, raid,
                  sub, gift, cheer, chat) with its message id for de-duplication
    LiveStream    what Helix says about the channel right now (reconciliation
                  after a disconnect or a restart)
    Actor         who clicked / typed something (Discord member or Twitch chatter)

Outputs are ``Effect`` objects that the Discord adapter turns into posts and
message edits, and ``Outcome`` (ok + a short private reply + effects) for
member actions. All state lives in one JSON document (stream_director_store)
that is saved after every change, so a restart or a reconnect continues the
running session instead of starting a new one.

Lifecycle of a session::

    online / manual start ──► live ──offline──► ending ──grace over──► ended (recap)
                                 ▲                 │
                                 └────online───────┘   (a stream that drops and comes
                                                        back within the grace minutes
                                                        stays one session)
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import bot_i18n
import stream_director_config as sdc
import stream_director_store as sds

# Points that move the shared community level (no per-user XP).
POINTS = {
    "stream": 10,
    "stream_quarter_hour": 1,
    "challenge_completed": 15,
    "challenge_failed": 5,
    "poll": 2,
    "notable_moment": 3,
    "raid": 5,
    "follower": 1,
}
STREAM_QUARTER_HOURS_CAP = 24
NOTABLE_MOMENTS_CAP = 5
FOLLOWER_POINTS_CAP = 20
GOAL_REWARD = 25
SEASON_FORMAT = "%Y-%m"

METRICS = {
    "streams": "streams",
    "stream_minutes": "minutes live",
    "challenges_completed": "challenges completed",
    "moments": "moments marked",
    "polls": "polls and predictions",
    "raids": "raids received",
    "followers": "new followers during streams",
}
PERIODS = ("week", "season", "total")
PERIOD_LABELS = {"week": "this week", "season": "this season", "total": "long-term"}
CHALLENGE_STATUS_LABELS = {"suggested": "suggested", "accepted": "accepted", "rejected": "rejected", "completed": "completed", "failed": "failed"}
DEFAULT_GOALS = (
    ("Stream 3 times this week", "streams", 3, "week"),
    ("Complete 2 challenges this week", "challenges_completed", 2, "week"),
    ("Mark 100 community moments", "moments", 100, "total"),
)

INBOX_KINDS = ("question", "game", "clip", "topic")
INBOX_LABELS = {"question": "Question", "game": "Game suggestion", "clip": "Clip / link", "topic": "Topic", "challenge": "Challenge"}
MAX_OPEN_INBOX = 300
MAX_GOALS = 10
MAX_HISTORY = 30
MAX_MOMENTS = 1000
MAX_CHATTERS = 5000
PROCESSED_EVENT_TTL = 24 * 3600
MAX_PROCESSED_EVENTS = 2000
EVENT_MAX_AGE_SECONDS = 600  # Twitch: ignore notifications older than 10 minutes (replays)
STALE_SESSION_SECONDS = 12 * 3600
NEXT_STREAM_POLL_MINUTES = 24 * 60
POLL_OPTION_LIMIT = 5
URL_PATTERN = re.compile(r"^https?://[^\s<>]{3,300}$", re.IGNORECASE)
WEEKS_KEPT = 12


class DirectorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Actor:
    platform: str  # "discord" | "twitch" | "system"
    user_id: str
    name: str
    team: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.user_id}"


SYSTEM = Actor("system", "0", "Stream Director", team=True)


@dataclass(frozen=True)
class StreamEvent:
    kind: str  # online | offline | update | raid | sub | gift | cheer | chat
    message_id: str
    at: float
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveStream:
    stream_id: str
    title: str = ""
    category: str = ""
    started_at: float | None = None
    viewers: int | None = None


@dataclass(frozen=True)
class Effect:
    kind: str
    ref: str | None = None
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    ok: bool
    text: str
    effects: list[Effect] = field(default_factory=list)


# --------------------------------------------------------------------------
# text helpers (no mention, no markdown surprises, bounded length)
# --------------------------------------------------------------------------


def clean_text(value: Any, limit: int) -> str:
    """One line of user text: control characters removed, whitespace folded."""
    text = "" if value is None else str(value)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


MARKDOWN_CHARS = re.compile(r"([\\*_~`|>\[\]()#])")
ESCAPED_CHARS = re.compile(r"\\([\\*_~`|>\[\]()#])")
MENTION_LIKE = re.compile(r"@(everyone|here|[!&]?[0-9]{5,20})")
ZERO_WIDTH = "\u200b"


def md(value: Any) -> str:
    """User text inside a Discord post: markdown and mention syntax shown
    literally (no masked links, no headings, no fake @everyone). Posts are
    also sent with mentions disabled; this keeps them looking right."""
    text = MARKDOWN_CHARS.sub(r"\\\1", "" if value is None else str(value))
    text = text.replace("<@", f"<{ZERO_WIDTH}@").replace("<#", f"<{ZERO_WIDTH}#")
    return MENTION_LIKE.sub(lambda match: f"@{ZERO_WIDTH}{match.group(1)}", text)


def plain(value: Any) -> str:
    """Undo md() for places that show text without Discord markdown (Manager)."""
    return ESCAPED_CHARS.sub(r"\1", "" if value is None else str(value)).replace(ZERO_WIDTH, "")


def clean_link(value: Any) -> str | None:
    text = clean_text(value, 300)
    if not text:
        return None
    if not URL_PATTERN.fullmatch(text):
        raise ValueError("A link must start with http:// or https://.")
    return text


def normalized_key(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def format_offset(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_duration(seconds: float, language: str | None = None) -> str:
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return bot_i18n.tr(language, "{hours}h {minutes}m", hours=hours, minutes=f"{minutes:02d}")
    return bot_i18n.tr(language, "{minutes}m", minutes=minutes)


def vod_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s"


def level_for(points: int) -> tuple[int, int, int]:
    """(level, points at this level's start, points needed for the next level).
    Level L starts at 50·L·(L−1): 0, 100, 300, 600, 1000…"""
    level = int((1 + math.sqrt(1 + 8 * max(0, points) / 100)) / 2)
    while 50 * (level + 1) * level <= points:
        level += 1
    while level > 1 and 50 * level * (level - 1) > points:
        level -= 1
    return level, 50 * level * (level - 1), 50 * (level + 1) * level


def week_bucket(now: float) -> str:
    year, week, _day = datetime.fromtimestamp(now).isocalendar()
    return f"week:{year}-W{week:02d}"


def season_id(now: float) -> str:
    return datetime.fromtimestamp(now).strftime(SEASON_FORMAT)


def bucket_for(period: str, now: float) -> str:
    if period == "week":
        return week_bucket(now)
    if period == "season":
        return f"season:{season_id(now)}"
    return "total"


# --------------------------------------------------------------------------
# moments: clustering
# --------------------------------------------------------------------------


def moment_clusters(moments: list[dict[str, Any]], window: int, min_users: int) -> list[dict[str, Any]]:
    """Moments close to each other become one cluster (people react a few
    seconds apart). Sorted by time; ``notable`` = enough different people,
    or marked by the stream team."""
    clusters: list[dict[str, Any]] = []
    for moment in sorted(moments, key=lambda item: item["offset"]):
        current = clusters[-1] if clusters else None
        if current is not None and moment["offset"] - current["offset"] <= window:
            current["moments"].append(moment)
        else:
            clusters.append({"offset": moment["offset"], "moments": [moment]})
    result = []
    for cluster in clusters:
        users = {item["user"] for item in cluster["moments"]}
        comments: list[str] = []
        for item in cluster["moments"]:
            comment = item.get("comment") or ""
            if comment and comment.casefold() not in {c.casefold() for c in comments}:
                comments.append(comment)
        team = any(item.get("team") for item in cluster["moments"])
        result.append(
            {
                "offset": cluster["offset"],
                "users": len(users),
                "marks": len(cluster["moments"]),
                "comments": comments[:3],
                "team": team,
                "notable": len(users) >= min_users or team,
            }
        )
    return result


def top_moments(clusters: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """The clusters worth showing, in stream order: notable ones first by
    weight; a small community with single marks still gets its list."""
    ranked = sorted(clusters, key=lambda item: (-int(item["notable"]), -item["users"] - (2 if item["team"] else 0), item["offset"]))
    return sorted(ranked[:limit], key=lambda item: item["offset"])


# --------------------------------------------------------------------------
# the director
# --------------------------------------------------------------------------


class Director:
    def __init__(
        self,
        store: sds.StateStore,
        config: sdc.StreamDirectorConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.config = config or sdc.StreamDirectorConfig()
        self.clock = clock
        self.problem: str | None = None
        try:
            self.state = store.load()
        except sds.StateError as exc:
            self.problem = str(exc)
            self.state = sds.empty_state()
        if self.problem is None and not self.state["meta"].get("default_goals_created"):
            for title, metric, target, period in DEFAULT_GOALS:
                # Built-in goals keep the English title as a key and are shown in the bot's language.
                self._new_goal(title, metric, target, period)["builtin"] = True
            self.state["meta"]["default_goals_created"] = True
            self._save()

    # -- language ------------------------------------------------------------------

    def _t(self, text: str, /, **params: Any) -> str:
        """``text`` in this bot instance's language."""
        return bot_i18n.tr(self.config.language, text, **params)

    # -- persistence ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.problem is None

    def _save(self) -> None:
        if self.problem is not None:
            raise DirectorUnavailable(self.problem)
        self.store.save(self.state, now=self.clock())

    def _next_id(self) -> str:
        value = self.state["next_id"]
        self.state["next_id"] = value + 1
        return str(value)

    def _unavailable(self) -> Outcome:
        return Outcome(False, self._t("Stream Director is temporarily unavailable (its data file needs attention in the Manager)."))

    # -- sessions ----------------------------------------------------------------------

    @property
    def session(self) -> dict[str, Any] | None:
        return self.state.get("active_session")

    def _live_session(self) -> dict[str, Any] | None:
        session = self.session
        return session if session is not None and session["status"] in ("live", "ending") else None

    def _start_session(self, source: str, stream_id: str | None, title: str, category: str, started_at: float) -> list[Effect]:
        now = self.clock()
        session = {
            "id": self._next_id(),
            "source": source,
            "stream_ids": [stream_id] if stream_id else [],
            "title": clean_text(title, 140),
            "started_at": min(started_at, now),
            "ended_at": None,
            "status": "live",
            "ending_since": None,
            "last_live_seen": now,
            "categories": [{"name": clean_text(category, 80), "at": min(started_at, now)}] if category else [],
            "moments": [],
            "stats": {
                "raids": [],
                "subs": 0,
                "gift_subs": 0,
                "bits": 0,
                "chat_messages": 0,
                "chatters": [],
                "discord_messages": 0,
                "viewer_samples": [],
                "peak_viewers": 0,
                "followers_start": None,
                "followers_end": None,
            },
            "points": {"earned": 0, "breakdown": {}},
            "polls": [],
            "challenges": [],
            "discord": {},
            "vod": None,
            "recap": None,
            "card_dirty": True,
        }
        self.state["active_session"] = session
        effects = [Effect("session_started", session["id"])]
        waiting = self._waiting_challenges(limit=5)
        if waiting and self.config.feature("challenges"):
            effects.append(Effect("challenges_digest", session["id"], data={"ids": [item["id"] for item in waiting]}))
        return effects

    def _resume(self, session: dict[str, Any], stream_id: str | None) -> list[Effect]:
        session["status"] = "live"
        session["ended_at"] = None
        session["ending_since"] = None
        session["last_live_seen"] = self.clock()
        if stream_id and stream_id not in session["stream_ids"]:
            session["stream_ids"].append(stream_id)
        session["card_dirty"] = True
        return [Effect("thread_post", session["id"], self._t("▶ Back live — this is still the same session.")), Effect("card_update", session["id"])]

    def _begin_ending(self, session: dict[str, Any], ended_at: float) -> list[Effect]:
        if session["status"] != "live":
            return []
        session["status"] = "ending"
        session["ending_since"] = self.clock()
        session["ended_at"] = ended_at
        session["card_dirty"] = True
        if self.config.end_grace_minutes <= 0:
            return self._finalize(session)
        return [Effect("card_update", session["id"])]

    def _within_grace(self, session: dict[str, Any]) -> bool:
        since = session.get("ending_since") or session.get("last_live_seen") or 0
        return self.clock() - since <= self.config.end_grace_minutes * 60

    def stream_online(self, stream_id: str, title: str = "", category: str = "", started_at: float | None = None) -> list[Effect]:
        now = self.clock()
        session = self._live_session()
        if session is not None:
            if stream_id in session["stream_ids"]:
                session["last_live_seen"] = now
                if session["status"] == "ending":
                    return self._resume(session, stream_id)
                return []  # the same stream again (duplicate or reconcile)
            if session["status"] == "ending" or (session["source"] == "twitch" and self._within_grace(session)):
                return self._resume(session, stream_id)
            if session["source"] == "manual" and not session["stream_ids"]:
                session["stream_ids"].append(stream_id)
                session["source"] = "twitch"
                session["last_live_seen"] = now
                session["card_dirty"] = True
                return [Effect("card_update", session["id"])]
            effects = self._finalize(session, ended_at=session.get("last_live_seen") or now)
            return effects + self._start_session("twitch", stream_id, title, category, started_at or now)
        return self._start_session("twitch", stream_id, title, category, started_at or now)

    def stream_offline(self) -> list[Effect]:
        session = self._live_session()
        if session is None or session["source"] != "twitch":
            return []
        return self._begin_ending(session, ended_at=self.clock())

    def manual_start(self, actor: Actor, title: str = "") -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team can start a session."))
        if self._live_session() is not None:
            return Outcome(False, self._t("A stream session is already running."))
        effects = self._start_session("manual", None, title or self._t("Stream"), "", self.clock())
        self._save()
        return Outcome(True, self._t("Stream session started."), effects)

    def manual_end(self, actor: Actor) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team can end a session."))
        session = self._live_session()
        if session is None:
            return Outcome(False, self._t("No stream session is running."))
        effects = self._finalize(session, ended_at=self.clock())
        self._save()
        return Outcome(True, self._t("Stream session ended. The recap is being posted."), effects)

    def reconcile(self, live: LiveStream | None, followers_total: int | None = None) -> list[Effect]:
        """What Helix reports now (polled, and right after a (re)connect)."""
        if not self.available:
            return []
        effects: list[Effect] = []
        session = self._live_session()
        if live is not None:
            effects += self.stream_online(live.stream_id, live.title, live.category, live.started_at)
            session = self._live_session()
            if session is not None:
                session["last_live_seen"] = self.clock()
                if live.viewers is not None:
                    samples = session["stats"]["viewer_samples"]
                    samples.append(int(live.viewers))
                    del samples[:-500]
                    session["stats"]["peak_viewers"] = max(session["stats"]["peak_viewers"], int(live.viewers))
                if live.title and clean_text(live.title, 140) != session["title"]:
                    session["title"] = clean_text(live.title, 140)
                    session["card_dirty"] = True
                if live.category:
                    effects += self._category(session, live.category)
        elif session is not None and session["source"] == "twitch" and session["status"] == "live":
            effects += self._begin_ending(session, ended_at=session.get("last_live_seen") or self.clock())
        session = self._live_session()
        if session is not None and followers_total is not None:
            stats = session["stats"]
            if stats["followers_start"] is None:
                stats["followers_start"] = int(followers_total)
            stats["followers_end"] = int(followers_total)
        self._save()
        return effects

    def _category(self, session: dict[str, Any], category: str) -> list[Effect]:
        name = clean_text(category, 80)
        if not name or (session["categories"] and session["categories"][-1]["name"] == name):
            return []
        session["categories"].append({"name": name, "at": self.clock()})
        session["card_dirty"] = True
        if len(session["categories"]) == 1:
            return [Effect("card_update", session["id"])]
        return [Effect("thread_post", session["id"], self._t("🎮 Now: **{name}**", name=md(name))), Effect("card_update", session["id"])]

    # -- Twitch events -------------------------------------------------------------------

    def handle_event(self, event: StreamEvent) -> list[Effect]:
        """One EventSub notification. Repeated message ids are ignored."""
        if not self.available:
            return []
        now = self.clock()
        processed = self.state["processed_events"]
        if event.message_id in processed:
            return []
        if now - event.at > EVENT_MAX_AGE_SECONDS:
            return []
        processed[event.message_id] = now
        self._prune_processed(now)
        data = event.data
        effects: list[Effect] = []
        if event.kind == "online":
            started_at = data.get("started_at") if isinstance(data.get("started_at"), (int, float)) else event.at
            effects = self.stream_online(str(data.get("stream_id") or event.message_id), "", "", started_at)
        elif event.kind == "offline":
            effects = self.stream_offline()
        elif event.kind == "update":
            session = self._live_session()
            if session is not None:
                if data.get("title") and clean_text(data["title"], 140) != session["title"]:
                    session["title"] = clean_text(data["title"], 140)
                    session["card_dirty"] = True
                    effects.append(Effect("card_update", session["id"]))
                if data.get("category"):
                    effects += self._category(session, str(data["category"]))
        else:
            effects = self._session_event(event)
        self._save()
        return effects

    def _prune_processed(self, now: float) -> None:
        processed = self.state["processed_events"]
        for key in [key for key, at in processed.items() if now - at > PROCESSED_EVENT_TTL]:
            del processed[key]
        if len(processed) > MAX_PROCESSED_EVENTS:
            for key in sorted(processed, key=processed.get)[: len(processed) - MAX_PROCESSED_EVENTS]:
                del processed[key]

    def _session_event(self, event: StreamEvent) -> list[Effect]:
        session = self._live_session()
        data = event.data
        if event.kind == "chat":
            return self._chat(session, data)
        if session is None:
            return []
        stats = session["stats"]
        if event.kind == "raid":
            viewers = int(data.get("viewers") or 0)
            name = clean_text(data.get("from_name"), 40) or self._t("someone")
            stats["raids"].append({"from": name, "viewers": viewers, "at": self.clock()})
            effects = self._award("raid", POINTS["raid"], session)
            effects += self._count("raids", 1)
            session["card_dirty"] = True
            return [Effect("thread_post", session["id"], self._t("🚀 Raid from **{name}** with {viewers} viewers — welcome!", name=md(name), viewers=viewers))] + effects
        if event.kind == "sub":
            stats["subs"] += 1
        elif event.kind == "gift":
            stats["gift_subs"] += max(1, int(data.get("total") or 1))
        elif event.kind == "cheer":
            stats["bits"] += max(0, int(data.get("bits") or 0))
        session["card_dirty"] = True
        return []

    def _chat(self, session: dict[str, Any] | None, data: dict[str, Any]) -> list[Effect]:
        if session is not None:
            stats = session["stats"]
            stats["chat_messages"] += 1
            chatter = str(data.get("user_id") or "")
            if chatter and chatter not in stats["chatters"] and len(stats["chatters"]) < MAX_CHATTERS:
                stats["chatters"].append(chatter)
        if not self.config.feature("twitch_chat"):
            return []
        text = clean_text(data.get("text"), 300)
        if not text.startswith("!"):
            return []
        command, _space, rest = text[1:].partition(" ")
        actor = Actor("twitch", str(data.get("user_id") or "0"), clean_text(data.get("user_name"), 40) or "viewer", bool(data.get("team")))
        command = command.casefold()
        if command in ("moment", "m", "clip_this"):
            outcome = self.mark_moment(actor, rest, save=False)
        elif command in ("challenge", "ch"):
            outcome = self.suggest_challenge(actor, rest, save=False)
        elif command in ("q", "question"):
            outcome = self.suggest(actor, "question", rest, save=False)
        elif command == "game":
            outcome = self.suggest(actor, "game", rest, save=False)
        elif command in ("suggest", "idea", "topic"):
            outcome = self.suggest(actor, "topic", rest, save=False)
        elif command == "clip":
            outcome = self.suggest(actor, "clip", rest, link=rest.split(" ")[0] if rest else None, save=False)
        else:
            return []
        return outcome.effects

    # -- moments ---------------------------------------------------------------------------

    def _cooldown(self, key: str, seconds: int) -> float:
        """Seconds left; 0 = allowed (and the cooldown starts now)."""
        now = self.clock()
        cooldowns = self.state["cooldowns"]
        until = cooldowns.get(key, 0)
        if until > now:
            return until - now
        if seconds > 0:
            cooldowns[key] = now + seconds
            for stale in [item for item, value in cooldowns.items() if value <= now]:
                del cooldowns[stale]
        return 0

    def mark_moment(self, actor: Actor, comment: str = "", *, save: bool = True) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not self.config.feature("moments"):
            return Outcome(False, self._t("Moments are turned off for this community."))
        session = self._live_session()
        if session is None:
            return Outcome(False, self._t("Moments can be marked while the stream is live."))
        if len(session["moments"]) >= MAX_MOMENTS:
            return Outcome(False, self._t("This stream already has the maximum number of moments."))
        left = self._cooldown(f"moment:{actor.key}", self.config.moment_cooldown_seconds)
        if left:
            return Outcome(False, self._t("You just marked a moment — try again in {seconds} s.", seconds=math.ceil(left)))
        now = self.clock()
        offset = max(0.0, now - session["started_at"] - self.config.moment_reaction_lag_seconds)
        moment = {
            "id": self._next_id(),
            "user": actor.key,
            "name": clean_text(actor.name, 40),
            "at": now,
            "offset": offset,
            "comment": clean_text(comment, 120),
            "team": actor.team,
        }
        session["moments"].append(moment)
        session["card_dirty"] = True
        effects = self._count("moments", 1) + [Effect("card_update", session["id"])]
        nearby = {
            item["user"]
            for item in session["moments"]
            if abs(item["offset"] - offset) <= self.config.moment_cluster_seconds
        }
        if save:
            self._save()
        if len(nearby) > 1:
            text = self._t("📍 Moment saved at {time} — {count} people marked this moment.", time=format_offset(offset), count=len(nearby))
        else:
            text = self._t("📍 Moment saved at {time}.", time=format_offset(offset))
        return Outcome(True, text, effects)

    # -- challenges -------------------------------------------------------------------------

    def _open_challenges(self) -> list[dict[str, Any]]:
        return [item for item in self.state["challenges"].values() if item["status"] in ("suggested", "accepted")]

    def _waiting_challenges(self, limit: int) -> list[dict[str, Any]]:
        open_items = self._open_challenges()
        open_items.sort(key=lambda item: (item["status"] != "accepted", -len(item["supporters"]), item["created_at"]))
        return open_items[:limit]

    def suggest_challenge(self, actor: Actor, text: str, *, save: bool = True) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not self.config.feature("challenges"):
            return Outcome(False, self._t("Challenges are turned off for this community."))
        text = clean_text(text, 150)
        if len(text) < 3:
            return Outcome(False, self._t("Describe the challenge in a few words."))
        key = normalized_key(text)
        for item in self._open_challenges():
            if normalized_key(item["text"]) == key:
                return self.support_challenge(actor, item["id"], save=save, duplicate=True)
        mine = [item for item in self._open_challenges() if item["author"]["key"] == actor.key]
        if not actor.team and len(mine) >= self.config.max_open_challenges_per_user:
            return Outcome(False, self._t("You already have {count} open challenges — wait until the team decides on them.", count=len(mine)))
        # Cooldowns are per kind of suggestion: a chatter may send a question and a game in a row.
        left = 0 if actor.team else self._cooldown(f"challenge:{actor.key}", self.config.suggestion_cooldown_seconds)
        if left:
            return Outcome(False, self._t("Slow down a little — try again in {seconds} s.", seconds=math.ceil(left)))
        session = self._live_session()
        challenge = {
            "id": self._next_id(),
            "text": text,
            "author": {"key": actor.key, "name": clean_text(actor.name, 40), "platform": actor.platform},
            "created_at": self.clock(),
            "session_id": session["id"] if session else None,
            "status": "suggested",
            "supporters": [actor.key],
            "decided_by": None,
            "decided_at": None,
            "finished_session_id": None,
            "message": None,
        }
        self.state["challenges"][challenge["id"]] = challenge
        self._trim_challenges()
        effects: list[Effect] = []
        if session is not None:
            effects.append(Effect("challenge_post", challenge["id"]))
            session["card_dirty"] = True
            effects.append(Effect("card_update", session["id"]))
        if save:
            self._save()
        if session:
            text = self._t("🎯 Challenge #{id} suggested. It is in the stream thread now.", id=challenge["id"])
        else:
            text = self._t("🎯 Challenge #{id} suggested. It waits in the streamer inbox for the next stream.", id=challenge["id"])
        return Outcome(True, text, effects)

    def _trim_challenges(self) -> None:
        challenges = self.state["challenges"]
        finished = sorted((item for item in challenges.values() if item["status"] not in ("suggested", "accepted")), key=lambda item: item["created_at"])
        for item in finished[: max(0, len(finished) - 200)]:
            del challenges[item["id"]]

    def support_challenge(self, actor: Actor, challenge_id: str, *, save: bool = True, duplicate: bool = False) -> Outcome:
        if not self.available:
            return self._unavailable()
        challenge = self.state["challenges"].get(str(challenge_id))
        if challenge is None or challenge["status"] not in ("suggested", "accepted"):
            return Outcome(False, self._t("This challenge is already decided."))
        if actor.key in challenge["supporters"]:
            if duplicate:
                return Outcome(False, self._t("This challenge was already suggested — you already support challenge #{id}.", id=challenge["id"]))
            return Outcome(False, self._t("You already support challenge #{id}.", id=challenge["id"]))
        challenge["supporters"].append(actor.key)
        effects = [Effect("challenge_update", challenge["id"])]
        if save:
            self._save()
        count = len(challenge["supporters"])
        if duplicate:
            return Outcome(True, self._t("This challenge was already suggested — 👍 you support challenge #{id} ({count} supporters).", id=challenge["id"], count=count), effects)
        return Outcome(True, self._t("👍 You support challenge #{id} ({count} supporters).", id=challenge["id"], count=count), effects)

    def decide_challenge(self, actor: Actor, challenge_id: str, action: str) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team decides on challenges — 👍 Support it instead."))
        challenge = self.state["challenges"].get(str(challenge_id))
        if challenge is None:
            return Outcome(False, self._t("This challenge no longer exists."))
        transitions = {
            ("suggested", "accept"): "accepted",
            ("suggested", "reject"): "rejected",
            ("accepted", "reject"): "rejected",
            ("accepted", "complete"): "completed",
            ("accepted", "fail"): "failed",
        }
        new_status = transitions.get((challenge["status"], action))
        if new_status is None:
            status = self._t(CHALLENGE_STATUS_LABELS.get(challenge["status"], challenge["status"]))
            return Outcome(False, self._t("Challenge #{id} is {status}; that step is not possible now.", id=challenge["id"], status=status))
        challenge["status"] = new_status
        challenge["decided_by"] = clean_text(actor.name, 40)
        challenge["decided_at"] = self.clock()
        session = self._live_session()
        effects = [Effect("challenge_update", challenge["id"])]
        if session is not None and challenge["id"] not in session["challenges"]:
            session["challenges"].append(challenge["id"])
        if new_status in ("completed", "failed"):
            challenge["finished_session_id"] = session["id"] if session else None
            if new_status == "completed":
                effects += self._award("challenge_completed", POINTS["challenge_completed"], session)
                effects += self._count("challenges_completed", 1)
                text = self._t("🏆 Challenge #{id} completed: {text}", id=challenge["id"], text=md(challenge["text"]))
            else:
                effects += self._award("challenge_failed", POINTS["challenge_failed"], session)
                text = self._t("💀 Challenge #{id} failed — respect for trying: {text}", id=challenge["id"], text=md(challenge["text"]))
            if session is not None:
                effects.append(Effect("thread_post", session["id"], text))
        if session is not None:
            session["card_dirty"] = True
            effects.append(Effect("card_update", session["id"]))
        self._save()
        texts = {
            "accepted": "Challenge #{id} accepted.",
            "rejected": "Challenge #{id} rejected.",
            "completed": "Challenge #{id} marked completed.",
            "failed": "Challenge #{id} marked failed.",
        }
        return Outcome(True, self._t(texts[new_status], id=challenge["id"]), effects)

    # -- polls and predictions ----------------------------------------------------------------

    def create_poll(self, actor: Actor, kind: str, question: str, options: list[str], minutes: int | None = None) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not self.config.feature("polls"):
            return Outcome(False, self._t("Polls are turned off for this community."))
        if not actor.team:
            return Outcome(False, self._t("Only the stream team can start polls and predictions."))
        if kind not in ("poll", "prediction"):
            return Outcome(False, self._t("Unknown poll type."))
        question = clean_text(question, 150)
        options = [clean_text(item, 50) for item in options if clean_text(item, 50)]
        unique: list[str] = []
        for option in options:
            if option.casefold() not in {item.casefold() for item in unique}:
                unique.append(option)
        if len(question) < 3:
            return Outcome(False, self._t("Write a question."))
        if not 2 <= len(unique) <= POLL_OPTION_LIMIT:
            return Outcome(False, self._t("Give 2 to {limit} different options, separated by |.", limit=POLL_OPTION_LIMIT))
        minutes = self.config.poll_default_minutes if minutes is None else minutes
        if not 1 <= minutes <= 60:
            return Outcome(False, self._t("Duration must be 1 to 60 minutes."))
        poll = self._new_poll(kind, question, unique, minutes * 60)
        self._save()
        template = "Prediction #{id} is open for {minutes} min." if kind == "prediction" else "Poll #{id} is open for {minutes} min."
        return Outcome(True, self._t(template, id=poll["id"], minutes=minutes), [Effect("poll_post", poll["id"])])

    def _new_poll(self, kind: str, question: str, options: list[str], seconds: int, target: str = "session") -> dict[str, Any]:
        now = self.clock()
        session = self._live_session()
        poll = {
            "id": self._next_id(),
            "kind": kind,
            "question": question,
            "options": options,
            "votes": {},
            "created_at": now,
            "deadline": now + seconds,
            "status": "open",
            "outcome": None,
            "session_id": session["id"] if session else None,
            "target": target if session or target == "channel" else "channel",
            "message": None,
        }
        self.state["polls"][poll["id"]] = poll
        if session is not None and poll["id"] not in session["polls"]:
            session["polls"].append(poll["id"])
        self._trim_polls()
        return poll

    def _trim_polls(self) -> None:
        polls = self.state["polls"]
        closed = sorted((item for item in polls.values() if item["status"] in ("closed", "resolved")), key=lambda item: item["created_at"])
        for item in closed[: max(0, len(closed) - 100)]:
            del polls[item["id"]]

    def vote(self, actor: Actor, poll_id: str, index: int) -> Outcome:
        if not self.available:
            return self._unavailable()
        poll = self.state["polls"].get(str(poll_id))
        if poll is None:
            return Outcome(False, self._t("This poll no longer exists."))
        if poll["status"] != "open":
            return Outcome(False, self._t("Voting is over.") if poll["kind"] == "poll" else self._t("Predictions are locked."))
        if not 0 <= index < len(poll["options"]):
            return Outcome(False, self._t("Unknown option."))
        previous = poll["votes"].get(actor.key)
        poll["votes"][actor.key] = index
        self._save()
        changed = previous is not None and previous != index
        if poll["kind"] == "prediction":
            template = "Your prediction changed to “{option}”." if changed else "Your prediction: “{option}”."
        else:
            template = "Your vote changed to “{option}”." if changed else "Your vote: “{option}”."
        return Outcome(True, self._t(template, option=md(poll["options"][index])), [Effect("poll_update", poll["id"])])

    def lock_prediction(self, actor: Actor, poll_id: str) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team can lock predictions."))
        poll = self.state["polls"].get(str(poll_id))
        if poll is None or poll["kind"] != "prediction" or poll["status"] != "open":
            return Outcome(False, self._t("This prediction is not open."))
        effects = self._lock(poll)
        self._save()
        return Outcome(True, self._t("Predictions locked. Pick the outcome when it is known."), effects)

    def _lock(self, poll: dict[str, Any]) -> list[Effect]:
        poll["status"] = "locked"
        poll["deadline"] = self.clock()
        return [Effect("poll_update", poll["id"])]

    def resolve_prediction(self, actor: Actor, poll_id: str, index: int) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team picks the outcome."))
        poll = self.state["polls"].get(str(poll_id))
        if poll is None or poll["kind"] != "prediction" or poll["status"] not in ("open", "locked"):
            return Outcome(False, self._t("This prediction cannot be resolved."))
        if not 0 <= index < len(poll["options"]):
            return Outcome(False, self._t("Unknown option."))
        poll["status"] = "resolved"
        poll["outcome"] = index
        effects = [Effect("poll_update", poll["id"])] + self._poll_points(poll)
        session = self._live_session()
        result = self.poll_result(poll)
        if session is not None:
            effects.append(
                Effect("thread_post", session["id"], self._t("🔮 {question} → **{outcome}** · {summary}", question=md(poll["question"]), outcome=md(poll["options"][index]), summary=md(result["summary"])))
            )
        self._save()
        return Outcome(True, self._t("Outcome: {option}.", option=md(poll["options"][index])), effects)

    def _poll_points(self, poll: dict[str, Any]) -> list[Effect]:
        if not poll["votes"]:
            return []
        session = self.state["active_session"] if self.state["active_session"] and self.state["active_session"]["id"] == poll["session_id"] else None
        return self._award("poll", POINTS["poll"], session) + self._count("polls", 1)

    def poll_result(self, poll: dict[str, Any]) -> dict[str, Any]:
        counts = [0] * len(poll["options"])
        for index in poll["votes"].values():
            if 0 <= index < len(counts):
                counts[index] += 1
        total = sum(counts)
        best = max(counts) if counts else 0
        leaders = [poll["options"][i] for i, count in enumerate(counts) if count == best and best > 0]
        result: dict[str, Any] = {"counts": counts, "total": total, "leaders": leaders}
        if poll["kind"] == "prediction" and poll["outcome"] is not None:
            right = sum(1 for index in poll["votes"].values() if index == poll["outcome"])
            result["correct"] = right
            result["summary"] = self._t("{right} of {total} predicted it", right=right, total=total) if total else self._t("nobody predicted")
        elif total == 0:
            result["summary"] = self._t("no votes")
        elif len(leaders) == 1:
            result["summary"] = self._t("{leader} ({best} of {total})", leader=leaders[0], best=best, total=total)
        else:
            result["summary"] = self._t("tie: {leaders} ({best} each)", leaders=", ".join(leaders), best=best)
        return result

    # -- inbox ---------------------------------------------------------------------------------

    def suggest(self, actor: Actor, kind: str, text: str, link: str | None = None, *, save: bool = True) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not self.config.feature("inbox"):
            return Outcome(False, self._t("The streamer inbox is turned off for this community."))
        if kind not in INBOX_KINDS:
            return Outcome(False, self._t("Unknown suggestion type."))
        try:
            clean = clean_link(link) if link else None
        except ValueError:
            return Outcome(False, self._t("A link must start with http:// or https://."))
        text = clean_text(text, 200)
        if clean and text.startswith(clean):
            text = clean_text(text[len(clean):], 200)
        if kind == "clip" and clean is None:
            return Outcome(False, self._t("Add the link to the clip."))
        if len(text) < 2 and clean is None:
            return Outcome(False, self._t("Write a few words."))
        key = (kind, normalized_key(text) if kind != "clip" else (clean or "").casefold())
        for item in self.state["inbox"].values():
            if item["status"] == "new" and (item["kind"], normalized_key(item["text"]) if item["kind"] != "clip" else (item["link"] or "").casefold()) == key:
                if actor.key in item["supporters"]:
                    return Outcome(False, self._t("You already suggested this."))
                item["supporters"].append(actor.key)
                if save:
                    self._save()
                return Outcome(True, self._t("Someone suggested this already — your vote was added ({count}).", count=len(item["supporters"])))
        if len([item for item in self.state["inbox"].values() if item["status"] == "new"]) >= MAX_OPEN_INBOX:
            return Outcome(False, self._t("The inbox is full right now — the team will catch up soon."))
        left = 0 if actor.team else self._cooldown(f"suggest:{kind}:{actor.key}", self.config.suggestion_cooldown_seconds)
        if left:
            return Outcome(False, self._t("Slow down a little — try again in {seconds} s.", seconds=math.ceil(left)))
        session = self._live_session()
        item = {
            "id": self._next_id(),
            "kind": kind,
            "text": text,
            "link": clean,
            "author": {"key": actor.key, "name": clean_text(actor.name, 40), "platform": actor.platform},
            "created_at": self.clock(),
            "session_id": session["id"] if session else None,
            "status": "new",
            "supporters": [actor.key],
        }
        self.state["inbox"][item["id"]] = item
        self._trim_inbox()
        effects = []
        if session is not None:
            session["card_dirty"] = True
            effects.append(Effect("card_update", session["id"]))
        if save:
            self._save()
        return Outcome(True, self._t("💬 Sent to the streamer inbox ({kind} #{id}).", kind=self._t(INBOX_LABELS[kind]).lower(), id=item["id"]), effects)

    def _trim_inbox(self) -> None:
        inbox = self.state["inbox"]
        handled = sorted((item for item in inbox.values() if item["status"] != "new"), key=lambda item: item["created_at"])
        for item in handled[: max(0, len(handled) - 200)]:
            del inbox[item["id"]]

    def inbox_items(self, limit: int = 10) -> list[dict[str, Any]]:
        """Open suggestions and open challenges in one queue (most supported first, then oldest)."""
        items = [dict(item, entry="inbox") for item in self.state["inbox"].values() if item["status"] == "new"]
        items += [
            dict(item, entry="challenge", kind="challenge")
            for item in self._open_challenges()
            if item["status"] == "suggested"
        ]
        items.sort(key=lambda item: (-len(item["supporters"]), item["created_at"]))
        return items[:limit]

    def inbox_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.inbox_items(limit=10_000):
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return counts

    def set_inbox_status(self, actor: Actor, item_id: str, status: str) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team manages the inbox."))
        if status not in ("done", "dismissed"):
            return Outcome(False, self._t("Unknown status."))
        item = self.state["inbox"].get(str(item_id))
        if item is None:
            if str(item_id) in self.state["challenges"]:
                return self.decide_challenge(actor, str(item_id), "accept" if status == "done" else "reject")
            return Outcome(False, self._t("This inbox item no longer exists."))
        if item["status"] != "new":
            template = "#{id} is already done." if item["status"] == "done" else "#{id} is already dismissed."
            return Outcome(False, self._t(template, id=item["id"]))
        item["status"] = status
        item["handled_at"] = self.clock()
        effects = []
        session = self._live_session()
        if session is not None:
            session["card_dirty"] = True
            effects.append(Effect("card_update", session["id"]))
        self._save()
        return Outcome(True, self._t("#{id} marked done." if status == "done" else "#{id} dismissed.", id=item["id"]), effects)

    # -- community progression and goals ---------------------------------------------------------

    def _award(self, reason: str, points: int, session: dict[str, Any] | None) -> list[Effect]:
        if points <= 0 or not self.config.feature("progression"):
            return []
        community = self.state["community"]
        before = level_for(community["points"])[0]
        community["points"] += points
        season = season_id(self.clock())
        community["season_points"][season] = community["season_points"].get(season, 0) + points
        for old in sorted(community["season_points"])[:-12]:
            del community["season_points"][old]
        if session is not None:
            session["points"]["earned"] += points
            session["points"]["breakdown"][reason] = session["points"]["breakdown"].get(reason, 0) + points
            session["card_dirty"] = True
        after = level_for(community["points"])[0]
        if after > before:
            return [Effect("level_up", session["id"] if session else None, self._t("⭐ The community reached level {level}!", level=after), {"level": after})]
        return []

    def _count(self, metric: str, amount: int) -> list[Effect]:
        if amount <= 0:
            return []
        counters = self.state["community"]["counters"]
        now = self.clock()
        for bucket in ("total", week_bucket(now), f"season:{season_id(now)}"):
            values = counters.setdefault(bucket, {})
            values[metric] = values.get(metric, 0) + amount
        weeks = sorted(key for key in counters if key.startswith("week:"))
        for old in weeks[:-WEEKS_KEPT]:
            del counters[old]
        return self._check_goals(metric)

    def _check_goals(self, metric: str) -> list[Effect]:
        if not self.config.feature("progression"):
            return []
        effects: list[Effect] = []
        now = self.clock()
        for goal in self.state["goals"].values():
            if goal["metric"] != metric:
                continue
            bucket = bucket_for(goal["period"], now)
            progress = self.state["community"]["counters"].get(bucket, {}).get(metric, 0)
            if progress >= goal["target"] and bucket not in goal["completed"]:
                goal["completed"].append(bucket)
                del goal["completed"][:-20]
                session = self._live_session()
                effects += self._award("goal", goal["reward"], session)
                effects.append(
                    Effect("goal_completed", session["id"] if session else None, self._t("🎉 Community goal reached: **{title}** (+{reward} XP)", title=md(self.goal_title(goal)), reward=goal["reward"]))
                )
        return effects

    def _new_goal(self, title: str, metric: str, target: int, period: str) -> dict[str, Any]:
        goal = {
            "id": self._next_id(),
            "title": clean_text(title, 80),
            "metric": metric,
            "target": int(target),
            "period": period,
            "reward": GOAL_REWARD,
            "created_at": self.clock(),
            "completed": [],
        }
        self.state["goals"][goal["id"]] = goal
        return goal

    def add_goal(self, actor: Actor, title: str, metric: str, target: int, period: str) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team sets community goals."))
        if metric not in METRICS or period not in PERIODS:
            return Outcome(False, self._t("Unknown goal type."))
        if not 1 <= int(target) <= 100_000:
            return Outcome(False, self._t("The target must be between 1 and 100000."))
        if len(self.state["goals"]) >= MAX_GOALS:
            return Outcome(False, self._t("Up to {count} goals — remove one first.", count=MAX_GOALS))
        title = clean_text(title, 80) or self._t("{target} {metric} ({period})", target=target, metric=self._t(METRICS[metric]), period=self._t(PERIOD_LABELS[period]))
        goal = self._new_goal(title, metric, target, period)
        effects = self._check_goals(metric)
        self._save()
        return Outcome(True, self._t("Goal #{id} added: {title}.", id=goal["id"], title=md(goal["title"])), effects)

    def remove_goal(self, actor: Actor, goal_id: str) -> Outcome:
        if not self.available:
            return self._unavailable()
        if not actor.team:
            return Outcome(False, self._t("Only the stream team removes community goals."))
        if self.state["goals"].pop(str(goal_id), None) is None:
            return Outcome(False, self._t("No such goal."))
        self._save()
        return Outcome(True, self._t("Goal #{id} removed.", id=goal_id))

    def goal_title(self, goal: dict[str, Any]) -> str:
        return self._t(goal["title"]) if goal.get("builtin") else goal["title"]

    def goals_view(self) -> list[dict[str, Any]]:
        now = self.clock()
        view = []
        for goal in sorted(self.state["goals"].values(), key=lambda item: int(item["id"])):
            bucket = bucket_for(goal["period"], now)
            progress = self.state["community"]["counters"].get(bucket, {}).get(goal["metric"], 0)
            view.append(
                {
                    "id": goal["id"],
                    "title": self.goal_title(goal),
                    "period": goal["period"],
                    "progress": min(progress, goal["target"]),
                    "target": goal["target"],
                    "done": bucket in goal["completed"],
                }
            )
        return view

    def community_view(self) -> dict[str, Any]:
        points = self.state["community"]["points"]
        level, start, nxt = level_for(points)
        season = season_id(self.clock())
        return {
            "points": points,
            "level": level,
            "level_progress": points - start,
            "level_span": nxt - start,
            "season": season,
            "season_points": self.state["community"]["season_points"].get(season, 0),
            "goals": self.goals_view(),
        }

    # -- ticking -------------------------------------------------------------------------------

    def tick(self) -> list[Effect]:
        """Deadlines: polls closing, predictions locking, grace periods ending."""
        if not self.available:
            return []
        now = self.clock()
        effects: list[Effect] = []
        changed = False
        for poll in list(self.state["polls"].values()):
            if poll["status"] == "open" and poll["deadline"] <= now:
                changed = True
                if poll["kind"] == "prediction":
                    effects += self._lock(poll)
                    if poll["session_id"] and self._live_session():
                        effects.append(Effect("thread_post", poll["session_id"], self._t("🔒 Predictions for “{question}” are locked.", question=md(poll["question"]))))
                else:
                    poll["status"] = "closed"
                    effects.append(Effect("poll_update", poll["id"]))
                    effects += self._poll_points(poll)
                    result = self.poll_result(poll)
                    if poll["session_id"] and self._live_session() and poll["target"] == "session":
                        effects.append(Effect("thread_post", poll["session_id"], self._t("📊 {question} → {summary}", question=md(poll["question"]), summary=md(result["summary"]))))
                    if poll.get("purpose") == "next_stream":
                        self.state["meta"]["next_stream_choice"] = {"leaders": result["leaders"], "votes": result["total"], "at": now}
        session = self._live_session()
        if session is not None:
            if session["status"] == "ending" and not self._within_grace(session):
                effects += self._finalize(session)
                changed = True
            elif (
                session["status"] == "live"
                and session["source"] == "twitch"
                and now - (session.get("last_live_seen") or now) > STALE_SESSION_SECONDS
            ):
                # Twitch has not confirmed the stream for half a day (no network, no events).
                effects += self._finalize(session, ended_at=session["last_live_seen"])
                changed = True
        if changed:
            self._save()
        return effects

    def note_discord_message(self) -> None:
        session = self._live_session()
        if session is not None and self.available:
            session["stats"]["discord_messages"] += 1

    # -- ending a session: points, recap, next stream ---------------------------------------------

    def _finalize(self, session: dict[str, Any], ended_at: float | None = None) -> list[Effect]:
        ended_at = ended_at if ended_at is not None else (session.get("ended_at") or self.clock())
        session["ended_at"] = max(session["started_at"], ended_at)
        session["status"] = "ended"
        session["card_dirty"] = True
        duration = session["ended_at"] - session["started_at"]
        effects: list[Effect] = []
        effects += self._award("stream", POINTS["stream"], session)
        quarters = min(STREAM_QUARTER_HOURS_CAP, int(duration // 900))
        effects += self._award("stream_time", quarters * POINTS["stream_quarter_hour"], session)
        clusters = moment_clusters(session["moments"], self.config.moment_cluster_seconds, self.config.notable_moment_min_users)
        notable = min(NOTABLE_MOMENTS_CAP, sum(1 for item in clusters if item["notable"] and item["users"] >= 2))
        effects += self._award("moments", notable * POINTS["notable_moment"], session)
        stats = session["stats"]
        gained = 0
        if stats["followers_start"] is not None and stats["followers_end"] is not None:
            gained = max(0, stats["followers_end"] - stats["followers_start"])
        effects += self._award("followers", min(FOLLOWER_POINTS_CAP, gained) * POINTS["follower"], session)
        effects += self._count("streams", 1)
        effects += self._count("stream_minutes", int(duration // 60))
        effects += self._count("followers", gained)
        session["recap"] = self.build_recap(session, clusters, gained)
        history = self.state["sessions"]
        history.append(session)
        for old in history[:-MAX_HISTORY]:
            old["moments"] = []
            old["stats"]["chatters"] = []
        del history[: max(0, len(history) - MAX_HISTORY * 2)]
        self.state["active_session"] = None
        effects.append(Effect("session_ended", session["id"]))
        if self.config.feature("next_stream_poll") and self.config.feature("polls"):
            candidates = self.next_stream_candidates(session)
            if len(candidates) >= 2:
                poll = self._new_poll("poll", self._t("What should the next stream be?"), candidates, NEXT_STREAM_POLL_MINUTES * 60, target="channel")
                poll["purpose"] = "next_stream"
                poll["session_id"] = session["id"]
                effects.append(Effect("poll_post", poll["id"]))
        return effects

    def next_stream_candidates(self, session: dict[str, Any]) -> list[str]:
        games = [item for item in self.state["inbox"].values() if item["status"] == "new" and item["kind"] == "game"]
        games.sort(key=lambda item: (-len(item["supporters"]), item["created_at"]))
        names: list[str] = []
        for item in games:
            name = clean_text(item["text"], 50)
            if name and name.casefold() not in {value.casefold() for value in names}:
                names.append(name)
            if len(names) >= POLL_OPTION_LIMIT - 1:
                break
        if names and session["categories"]:
            last = clean_text(session["categories"][-1]["name"], 50)
            if last and last.casefold() not in {value.casefold() for value in names} and last.casefold() != "just chatting":
                names.append(self._t("More {game}", game=last)[:50])
        return names[:POLL_OPTION_LIMIT]

    def build_recap(self, session: dict[str, Any], clusters: list[dict[str, Any]], followers_gained: int) -> dict[str, Any]:
        stats = session["stats"]
        categories = []
        marks = session["categories"]
        for index, item in enumerate(marks):
            end = marks[index + 1]["at"] if index + 1 < len(marks) else session["ended_at"]
            categories.append({"name": item["name"], "minutes": max(0, int((end - max(item["at"], session["started_at"])) // 60))})
        challenges = [self.state["challenges"][cid] for cid in session["challenges"] if cid in self.state["challenges"]]
        polls = [self.state["polls"][pid] for pid in session["polls"] if pid in self.state["polls"]]
        samples = stats["viewer_samples"]
        return {
            "title": session["title"],
            "started_at": session["started_at"],
            "duration_seconds": int(session["ended_at"] - session["started_at"]),
            "categories": categories,
            "viewers": {"peak": stats["peak_viewers"], "average": round(sum(samples) / len(samples)) if samples else None},
            "followers_gained": followers_gained if stats["followers_start"] is not None else None,
            "subs": stats["subs"],
            "gift_subs": stats["gift_subs"],
            "bits": stats["bits"],
            "raids": list(stats["raids"]),
            "chat": {"messages": stats["chat_messages"], "chatters": len(stats["chatters"])},
            "discord_messages": stats["discord_messages"],
            "moments_total": len(session["moments"]),
            "moments": top_moments(clusters),
            "challenges": {
                "completed": [item["text"] for item in challenges if item["status"] == "completed"],
                "failed": [item["text"] for item in challenges if item["status"] == "failed"],
                "accepted_open": [item["text"] for item in self._open_challenges() if item["status"] == "accepted"],
                "suggested": sum(1 for item in self.state["challenges"].values() if item.get("session_id") == session["id"]),
            },
            "polls": [
                {"kind": poll["kind"], "question": poll["question"], "summary": self.poll_result(poll)["summary"], "status": poll["status"]}
                for poll in polls
            ],
            "points": dict(session["points"]),
            "community": self.community_view(),
            "inbox": self.inbox_counts(),
        }

    def attach_vod(self, session_id: str, video_id: str, url: str) -> None:
        for session in self.state["sessions"]:
            if session["id"] == session_id:
                session["vod"] = {"id": clean_text(video_id, 30), "url": clean_text(url, 200)}
                if session.get("recap") is not None:
                    session["recap"]["vod"] = session["vod"]
                self._save()
                return

    def find_session(self, session_id: str | None) -> dict[str, Any] | None:
        active = self.state.get("active_session")
        if active is not None and active["id"] == session_id:
            return active
        return next((item for item in reversed(self.state["sessions"]) if item["id"] == session_id), None)

    # -- message references (set by the Discord adapter) -----------------------------------------

    def set_session_discord(self, session_id: str, **values: Any) -> None:
        session = self.find_session(session_id)
        if session is not None and self.available:
            session["discord"].update({key: str(value) for key, value in values.items() if value is not None})
            self._save()

    def set_message(self, collection: str, item_id: str, channel_id: int, message_id: int) -> None:
        item = self.state[collection].get(str(item_id))
        if item is not None and self.available:
            item["message"] = {"channel_id": str(channel_id), "message_id": str(message_id)}
            self._save()

    def card_done(self, session_id: str) -> None:
        session = self.find_session(session_id)
        if session is not None and self.available and session.get("card_dirty"):
            session["card_dirty"] = False
            self._save()

    # -- views ----------------------------------------------------------------------------------

    def card_view(self, session: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        end = session["ended_at"] if session["status"] != "live" else now
        clusters = moment_clusters(session["moments"], self.config.moment_cluster_seconds, self.config.notable_moment_min_users)
        accepted = [item for item in self._open_challenges() if item["status"] == "accepted"]
        return {
            "status": session["status"],
            "title": session["title"] or self._t("Stream"),
            "category": session["categories"][-1]["name"] if session["categories"] else "",
            "uptime": format_duration((end or now) - session["started_at"]),
            "moments": len(session["moments"]),
            "notable_moments": sum(1 for item in clusters if item["notable"]),
            "challenges_accepted": len(accepted),
            "challenges_waiting": sum(1 for item in self._open_challenges() if item["status"] == "suggested"),
            "polls_open": sum(1 for pid in session["polls"] if self.state["polls"].get(pid, {}).get("status") == "open"),
            "inbox_new": sum(self.inbox_counts().values()),
            "points": session["points"]["earned"],
            "community": self.community_view(),
            "grace_minutes": self.config.end_grace_minutes,
        }

    def status_summary(self) -> dict[str, Any]:
        """Facts for the Manager page (no tokens, no secrets)."""
        if not self.available:
            return {"problem": self.problem}
        session = self._live_session()
        last = self.state["sessions"][-1] if self.state["sessions"] else None
        return {
            "session": None
            if session is None
            else {
                "id": session["id"],
                "status": session["status"],
                "source": session["source"],
                "title": session["title"],
                "started_at": session["started_at"],
                "moments": len(session["moments"]),
                "thread_id": session["discord"].get("thread_id"),
            },
            "community": self.community_view(),
            "inbox": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "text": item["text"],
                    "link": item.get("link"),
                    "author": item["author"]["name"],
                    "platform": item["author"]["platform"],
                    "supporters": len(item["supporters"]),
                    "created_at": item["created_at"],
                }
                for item in self.inbox_items(limit=15)
            ],
            "inbox_counts": self.inbox_counts(),
            "accepted_challenges": [
                {"id": item["id"], "text": item["text"]} for item in self._open_challenges() if item["status"] == "accepted"
            ][:10],
            "last_recap": None if last is None else recap_lines(last.get("recap") or {}, last.get("vod"), self.config.language),
            "sessions_total": len(self.state["sessions"]),
        }


# --------------------------------------------------------------------------
# recap text (Discord embed and Manager use the same lines)
# --------------------------------------------------------------------------


def moment_link(vod: dict[str, Any] | None, offset: float) -> str | None:
    if not vod or not vod.get("url"):
        return None
    return f"{vod['url']}?t={vod_timestamp(offset)}"


def recap_sections(recap: dict[str, Any], vod: dict[str, Any] | None = None, language: str | None = None) -> list[tuple[str, str, list[str]]]:
    """(section id, title, lines) of the post-stream recap in ``language``.
    User content was cleaned on input and is markdown-escaped here; posts are
    sent with mentions disabled."""
    if not recap:
        return []

    def t(text: str, /, **params: Any) -> str:
        return bot_i18n.tr(language, text, **params)

    vod = vod or recap.get("vod")
    sections: list[tuple[str, str, list[str]]] = []
    head = [t("⏱ {duration}", duration=format_duration(recap["duration_seconds"], language))]
    if recap.get("categories"):
        head.append("🎮 " + " → ".join(f"{md(item['name'])} ({format_duration(item['minutes'] * 60, language)})" for item in recap["categories"]))
    viewers = recap.get("viewers") or {}
    if viewers.get("peak"):
        if viewers.get("average") is not None:
            head.append(t("👀 peak {peak} · average {average}", peak=viewers["peak"], average=viewers["average"]))
        else:
            head.append(t("👀 peak {peak}", peak=viewers["peak"]))
    extras = []
    if recap.get("followers_gained"):
        extras.append(t("+{count} followers", count=recap["followers_gained"]))
    if recap.get("subs") or recap.get("gift_subs"):
        extras.append(t("{count} subs", count=recap.get("subs", 0) + recap.get("gift_subs", 0)))
    if recap.get("bits"):
        extras.append(t("{count} bits", count=recap["bits"]))
    for raid in recap.get("raids") or []:
        extras.append(t("raid from {name} ({viewers})", name=md(raid["from"]), viewers=raid["viewers"]))
    if extras:
        head.append("💜 " + " · ".join(extras))
    chat = recap.get("chat") or {}
    if chat.get("messages") or recap.get("discord_messages"):
        head.append(
            t(
                "💬 Twitch chat {messages} messages from {chatters} people · Discord thread {discord} messages",
                messages=chat.get("messages", 0),
                chatters=chat.get("chatters", 0),
                discord=recap.get("discord_messages", 0),
            )
        )
    sections.append(("stream", t("Stream"), head))
    moments = []
    for item in recap.get("moments") or []:
        if item["users"] == 1:
            label = t("{time} · 1 person", time=format_offset(item["offset"]))
        else:
            label = t("{time} · {count} people", time=format_offset(item["offset"]), count=item["users"])
        if item.get("comments"):
            label += " · " + " / ".join(f"“{md(comment)}”" for comment in item["comments"])
        link = moment_link(vod, item["offset"])
        moments.append(f"{label} · <{link}>" if link else label)
    if moments:
        sections.append(("moments", t("Moments ({count} marks)", count=recap.get("moments_total", 0)), moments))
    challenges = recap.get("challenges") or {}
    lines = [f"🏆 {md(text)}" for text in challenges.get("completed", [])] + [f"💀 {md(text)}" for text in challenges.get("failed", [])]
    if challenges.get("accepted_open"):
        lines.append(t("⏳ still accepted for next time: {count}", count=len(challenges["accepted_open"])))
    if challenges.get("suggested"):
        lines.append(t("🎯 new suggestions: {count}", count=challenges["suggested"]))
    if lines:
        sections.append(("challenges", t("Challenges"), lines))
    polls = [f"{'🔮' if item['kind'] == 'prediction' else '📊'} {md(item['question'])} → {md(item['summary'])}" for item in recap.get("polls") or []]
    if polls:
        sections.append(("polls", t("Polls & predictions"), polls))
    community = recap.get("community") or {}
    points = recap.get("points") or {}
    if community:
        lines = [
            t(
                "+{earned} XP this stream · Level {level} ({progress}/{span}) · Season {season}: {season_points} XP",
                earned=points.get("earned", 0),
                level=community["level"],
                progress=community["level_progress"],
                span=community["level_span"],
                season=community["season"],
                season_points=community["season_points"],
            )
        ]
        for goal in community.get("goals") or []:
            mark = "✅" if goal["done"] else "▫"
            lines.append(f"{mark} {md(goal['title'])}: {goal['progress']}/{goal['target']}")
        sections.append(("community", t("Community"), lines))
    inbox = recap.get("inbox") or {}
    if inbox:
        parts = ", ".join(f"{count} {t(INBOX_LABELS.get(kind, kind)).lower()}" for kind, count in sorted(inbox.items()))
        sections.append(("inbox", t("Inbox"), [t("Waiting for the streamer: {parts}", parts=parts)]))
    return sections


def recap_lines(recap: dict[str, Any], vod: dict[str, Any] | None = None, language: str | None = None) -> dict[str, list[str]]:
    """{title: lines} of the recap (Manager page, tests)."""
    return {title: lines for _key, title, lines in recap_sections(recap, vod, language)}
