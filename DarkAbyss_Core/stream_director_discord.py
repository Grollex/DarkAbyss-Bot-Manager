"""Discord side of Stream Director: the live card, the session thread,
buttons, modals and slash commands.

Where things appear:

* **Stream channel** (configured): one *card* per stream session. It is
  edited while the stream runs (title, category, uptime, moments, challenges,
  inbox, community level) and becomes the short recap when it ends.
* **Session thread** (a public thread on the card): the useful events of the
  session only — category changes, raids, challenge suggestions and
  decisions, poll results, goals reached — and the full recap at the end.
  Follows, subs and cheers are counted for the recap, never posted one by one.
* **Ephemeral replies** for everything a member does (moment saved, vote
  counted, …), and the streamer inbox (``/inbox``, stream team only).

Everything is written in the language of this bot instance (config
``language``): render functions take it as a parameter, the adapter passes
its director's language, so a language change applies with the next post.
Slash command descriptions are registered at start (restart to change them).

Buttons are persistent: their custom ids carry the entity id
(``sd:ch:12:support``) and are routed in ``on_interaction``, so they keep
working after a restart without any in-memory view. Every message is sent
with mentions disabled; the only exception is the optional go-live role ping,
which allows exactly that role.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import discord
from discord import app_commands

import bot_i18n
import stream_director as sd
import stream_director_config as sdc

PREFIX = "sd"
CARD_MIN_INTERVAL = 20.0  # seconds between edits of the live card
POLL_MIN_INTERVAL = 4.0
THREAD_ARCHIVE_MINUTES = 1440
NO_MENTIONS = discord.AllowedMentions.none()
UNAVAILABLE = "Stream Director is not ready yet (the bot reports why in the Manager)."
NOT_YOURS = "Only the stream team can do that."

# Permissions the bot needs in the stream channel (name -> label shown in the Manager)
CHANNEL_PERMISSIONS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "read_message_history": "Read Message History",
    "create_public_threads": "Create Public Threads",
    "send_messages_in_threads": "Send Messages in Threads",
}
KINDS = (("Question", "question"), ("Game for a stream", "game"), ("Clip / link", "clip"), ("Topic / idea", "topic"))
PERIODS = (("This week (resets every Monday)", "week"), ("This season (calendar month)", "season"), ("Long-term", "total"))


def _(language: str | None, text: str, /, **params: Any) -> str:
    return bot_i18n.tr(language, text, **params)


def kind_choices(language: str | None = None) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=_(language, label), value=value) for label, value in KINDS]


def metric_choices(language: str | None = None) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=_(language, label), value=key) for key, label in sd.METRICS.items()]


def period_choices(language: str | None = None) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=_(language, label), value=value) for label, value in PERIODS]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def custom_id(*parts: Any) -> str:
    return ":".join([PREFIX, *(str(part) for part in parts)])


def parse_custom_id(value: Any) -> list[str] | None:
    if not isinstance(value, str) or not value.startswith(f"{PREFIX}:") or len(value) > 100:
        return None
    return value.split(":")[1:]


def actor_for(member: Any, config: sdc.StreamDirectorConfig) -> sd.Actor:
    """A Discord member; ``team`` = owner, Manage Server, Administrator or a stream team role."""
    user_id = str(getattr(member, "id", "0"))
    name = getattr(member, "display_name", None) or getattr(member, "name", None) or "member"
    team = False
    guild = getattr(member, "guild", None)
    if guild is not None and getattr(guild, "owner_id", None) == getattr(member, "id", None):
        team = True
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and (getattr(permissions, "manage_guild", False) or getattr(permissions, "administrator", False)):
        team = True
    role_ids = {getattr(role, "id", None) for role in getattr(member, "roles", None) or []}
    if role_ids & set(config.team_role_ids):
        team = True
    return sd.Actor("discord", user_id, str(name), team)


def modal_values(data: Any) -> dict[str, str]:
    """Values of a submitted modal (classic action rows and newer label components)."""
    values: dict[str, str] = {}

    def walk(component: Any) -> None:
        if not isinstance(component, dict):
            return
        if "custom_id" in component and "value" in component:
            values[str(component["custom_id"])] = str(component.get("value") or "")
        for child in component.get("components") or []:
            walk(child)
        if isinstance(component.get("component"), dict):
            walk(component["component"])

    for row in (data or {}).get("components") or []:
        walk(row)
    return values


def bar(count: int, total: int, width: int = 10) -> str:
    filled = round(width * count / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def twitch_url(login: str | None) -> str | None:
    return f"https://twitch.tv/{login}" if login else None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

STATUS_HEADS = {"live": ("🔴 LIVE", 0xE91E63), "ending": ("⏸ Stream paused", 0xF39C12), "ended": ("📼 Stream ended", 0x7F8C8D)}


def card_embed(view: dict[str, Any], session: dict[str, Any], login: str | None = None, language: str | None = None) -> discord.Embed:
    head, color = STATUS_HEADS.get(view["status"], STATUS_HEADS["live"])
    embed = discord.Embed(title=f"{_(language, head)} · {sd.md(view['title'])}"[:256], color=color, url=twitch_url(login))
    if view["status"] == "ending":
        embed.description = _(language, "The stream went offline. If it comes back within {minutes} min this stays the same session.", minutes=view["grace_minutes"])
    elif view["status"] == "ended":
        sections = sd.recap_sections(session.get("recap") or {}, session.get("vod"), language)
        stream_lines = next((lines for key, _title, lines in sections if key == "stream"), [])
        embed.description = "\n".join(stream_lines[:3] + [_(language, "Full recap in the thread below.")])
    if view.get("category"):
        embed.add_field(name=_(language, "Category"), value=sd.md(view["category"])[:1024], inline=True)
    embed.add_field(name=_(language, "Uptime"), value=view["uptime"], inline=True)
    embed.add_field(name=_(language, "Moments"), value=_(language, "{count} marked · {notable} notable", count=view["moments"], notable=view["notable_moments"]), inline=True)
    embed.add_field(
        name=_(language, "Challenges"),
        value=_(language, "{accepted} accepted · {waiting} waiting", accepted=view["challenges_accepted"], waiting=view["challenges_waiting"]),
        inline=True,
    )
    if view["inbox_new"]:
        embed.add_field(name=_(language, "Inbox"), value=_(language, "{count} new for the streamer", count=view["inbox_new"]), inline=True)
    community = view["community"]
    embed.add_field(
        name=_(language, "Community"),
        value=_(
            language,
            "Level {level} · {progress}/{span} XP · +{points} this stream",
            level=community["level"],
            progress=community["level_progress"],
            span=community["level_span"],
            points=view["points"],
        ),
        inline=False,
    )
    if view["status"] != "ended":
        embed.set_footer(text=_(language, "📍 mark a moment · 🎯 suggest a challenge · 💬 send something to the streamer — or use /moment, /challenge, /suggest"))
    return embed


def card_components(status: str, language: str | None = None) -> discord.ui.View | None:
    if status == "ended":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=_(language, "Moment"), emoji="📍", style=discord.ButtonStyle.primary, custom_id=custom_id("card", "moment")))
    view.add_item(discord.ui.Button(label=_(language, "Challenge"), emoji="🎯", style=discord.ButtonStyle.secondary, custom_id=custom_id("card", "challenge")))
    view.add_item(discord.ui.Button(label=_(language, "Suggest"), emoji="💬", style=discord.ButtonStyle.secondary, custom_id=custom_id("card", "suggest")))
    return view


CHALLENGE_HEADS = {
    "suggested": "🎯 Challenge",
    "accepted": "✅ Accepted challenge",
    "rejected": "✖ Not this time",
    "completed": "🏆 Challenge completed",
    "failed": "💀 Challenge failed",
}


def challenge_text(challenge: dict[str, Any], language: str | None = None) -> str:
    supporters = len(challenge["supporters"])
    head = _(language, CHALLENGE_HEADS.get(challenge["status"], "🎯 Challenge"))
    byline = _(language, "by {name} · 👍 {count}", name=sd.md(challenge["author"]["name"]), count=supporters)
    if challenge.get("decided_by") and challenge["status"] != "suggested":
        status = _(language, sd.CHALLENGE_STATUS_LABELS.get(challenge["status"], challenge["status"]))
        byline += " · " + _(language, "{status} by {name}", status=status, name=sd.md(challenge["decided_by"]))
    return f"**{head} #{challenge['id']}** — {sd.md(challenge['text'])}\n{byline}"


def challenge_components(challenge: dict[str, Any], language: str | None = None) -> discord.ui.View | None:
    status = challenge["status"]
    if status not in ("suggested", "accepted"):
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=_(language, "Support"), emoji="👍", style=discord.ButtonStyle.secondary, custom_id=custom_id("ch", challenge["id"], "support")))
    if status == "suggested":
        view.add_item(discord.ui.Button(label=_(language, "Accept"), style=discord.ButtonStyle.success, custom_id=custom_id("ch", challenge["id"], "accept")))
        view.add_item(discord.ui.Button(label=_(language, "Reject"), style=discord.ButtonStyle.danger, custom_id=custom_id("ch", challenge["id"], "reject")))
    else:
        view.add_item(discord.ui.Button(label=_(language, "Completed"), emoji="🏆", style=discord.ButtonStyle.success, custom_id=custom_id("ch", challenge["id"], "complete")))
        view.add_item(discord.ui.Button(label=_(language, "Failed"), emoji="💀", style=discord.ButtonStyle.danger, custom_id=custom_id("ch", challenge["id"], "fail")))
    return view


def poll_embed(poll: dict[str, Any], result: dict[str, Any], language: str | None = None) -> discord.Embed:
    prediction = poll["kind"] == "prediction"
    status = poll["status"]
    if prediction:
        head = {"open": "🔮 Prediction", "locked": "🔒 Prediction locked", "resolved": "🔮 Prediction resolved"}.get(status, "🔮 Prediction")
    else:
        head = "📊 Poll" if status == "open" else "📊 Poll closed"
    embed = discord.Embed(title=f"{_(language, head)} · {sd.md(poll['question'])}"[:256], color=0x9B59B6 if prediction else 0x3498DB)
    lines = []
    for index, option in enumerate(poll["options"]):
        count = result["counts"][index]
        mark = " ✅" if prediction and poll.get("outcome") == index else ""
        lines.append(f"**{index + 1}. {sd.md(option)}**{mark}\n{bar(count, result['total'])} {count}")
    embed.description = "\n".join(lines)
    if status == "open":
        embed.set_footer(text=_(language, "{count} votes · one vote per person, you can change it", count=result["total"]))
        embed.add_field(name=_(language, "Ends"), value=f"<t:{int(poll['deadline'])}:R>", inline=True)
    else:
        embed.add_field(name=_(language, "Result"), value=sd.md(result["summary"])[:1024], inline=False)
    if prediction:
        embed.add_field(name=_(language, "No stakes"), value=_(language, "Just bragging rights — no points, no currency."), inline=False)
    return embed


def poll_components(poll: dict[str, Any], language: str | None = None) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    if poll["status"] == "open":
        for index, option in enumerate(poll["options"]):
            view.add_item(
                discord.ui.Button(label=f"{index + 1}. {option}"[:80], style=discord.ButtonStyle.secondary, custom_id=custom_id("poll", poll["id"], "vote", index), row=0)
            )
        if poll["kind"] == "prediction":
            view.add_item(discord.ui.Button(label=_(language, "Lock (team)"), emoji="🔒", style=discord.ButtonStyle.secondary, custom_id=custom_id("poll", poll["id"], "lock"), row=1))
    if poll["kind"] == "prediction" and poll["status"] in ("open", "locked"):
        for index, option in enumerate(poll["options"]):
            view.add_item(
                discord.ui.Button(label=f"✔ {option}"[:80], style=discord.ButtonStyle.success, custom_id=custom_id("poll", poll["id"], "resolve", index), row=2)
            )
    return view if view.children else None


def recap_embed(session: dict[str, Any], language: str | None = None) -> discord.Embed:
    recap = session.get("recap") or {}
    embed = discord.Embed(title=_(language, "📼 Recap · {title}", title=sd.md(recap.get("title") or _(language, "Stream")))[:256], color=0x7F8C8D)
    for _key, name, lines in sd.recap_sections(recap, session.get("vod"), language):
        text = ""
        for line in lines:
            if len(text) + len(line) + 1 > 1000:
                text += "\n…"
                break
            text += ("\n" if text else "") + line
        embed.add_field(name=name, value=text or "—", inline=False)
    return embed


def inbox_embed(items: list[dict[str, Any]], counts: dict[str, int], language: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=_(language, "📥 Streamer inbox"), color=0x2ECC71)
    if not items:
        embed.description = _(language, "Nothing waiting. 🎉")
        return embed
    lines = []
    for item in items:
        label = _(language, sd.INBOX_LABELS.get(item["kind"], item["kind"]))
        line = f"**#{item['id']} {label}** · {sd.md(item['text'])}"
        if item.get("link"):
            line += f" <{item['link']}>"
        line += "\n  " + _(language, "by {name} ({platform}) · 👍 {count}", name=sd.md(item["author"]["name"]), platform=item["author"]["platform"], count=len(item["supporters"]))
        lines.append(line)
    embed.description = "\n".join(lines)[:4000]
    embed.set_footer(text=" · ".join(f"{count} {_(language, sd.INBOX_LABELS.get(kind, kind)).lower()}" for kind, count in sorted(counts.items())))
    return embed


def inbox_components(items: list[dict[str, Any]], language: str | None = None) -> discord.ui.View | None:
    view = discord.ui.View(timeout=600)
    for item in items[:10]:
        done = "Accept #{id}" if item["entry"] == "challenge" else "Done #{id}"
        view.add_item(discord.ui.Button(label=_(language, done, id=item["id"]), style=discord.ButtonStyle.success, custom_id=custom_id("inbox", item["id"], "done")))
        view.add_item(discord.ui.Button(label=_(language, "Dismiss #{id}", id=item["id"]), style=discord.ButtonStyle.secondary, custom_id=custom_id("inbox", item["id"], "dismissed")))
    return view if view.children else None


def community_embed(view: dict[str, Any], language: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=_(language, "⭐ Community level {level}", level=view["level"]), color=0xF1C40F)
    embed.description = "\n".join(
        [
            f"{bar(view['level_progress'], view['level_span'])} "
            + _(language, "{progress}/{span} XP to level {next}", progress=view["level_progress"], span=view["level_span"], next=view["level"] + 1),
            _(language, "Season {season}: {season_points} XP · all time {points} XP", season=view["season"], season_points=view["season_points"], points=view["points"]),
        ]
    )
    for goal in view["goals"]:
        period = _(language, sd.PERIOD_LABELS[goal["period"]])
        mark = "✅" if goal["done"] else "▫"
        embed.add_field(name=f"{mark} #{goal['id']} {sd.md(goal['title'])}"[:256], value=f"{bar(goal['progress'], goal['target'])} {goal['progress']}/{goal['target']} · {period}", inline=False)
    embed.set_footer(text=_(language, "Streams, completed challenges, notable moments, raids and goals move the whole community forward."))
    return embed


# --------------------------------------------------------------------------
# modals (submissions are routed in on_interaction like the buttons)
# --------------------------------------------------------------------------


class _Modal(discord.ui.Modal):
    async def on_submit(self, interaction: discord.Interaction) -> None:  # handled by StreamDirectorDiscord
        return None


def moment_modal(language: str | None = None) -> discord.ui.Modal:
    modal = _Modal(title=_(language, "Mark this moment"), custom_id=custom_id("modal", "moment"), timeout=600)
    modal.add_item(discord.ui.TextInput(label=_(language, "What happened? (optional)"), custom_id="comment", required=False, max_length=120))
    return modal


def challenge_modal(language: str | None = None) -> discord.ui.Modal:
    modal = _Modal(title=_(language, "Suggest a challenge"), custom_id=custom_id("modal", "challenge"), timeout=600)
    modal.add_item(discord.ui.TextInput(label=_(language, "Challenge for the streamer"), custom_id="text", min_length=3, max_length=150))
    return modal


def suggest_modal(language: str | None = None) -> discord.ui.Modal:
    modal = _Modal(title=_(language, "Send to the streamer"), custom_id=custom_id("modal", "suggest"), timeout=600)
    modal.add_item(discord.ui.TextInput(label=_(language, "Question, game, topic or clip"), custom_id="text", max_length=200))
    modal.add_item(discord.ui.TextInput(label=_(language, "Link (optional, for clips)"), custom_id="link", required=False, max_length=300))
    return modal


def guess_kind(text: str, link: str | None) -> str:
    if link:
        return "clip"
    return "question" if text.rstrip().endswith("?") else "topic"


# --------------------------------------------------------------------------
# when: "in 2h", "tomorrow 20:00", "2026-10-05 19:30", "20:00" (also Russian words)
# --------------------------------------------------------------------------


def parse_when(text: str, now: float) -> float | None:
    import re

    value = (text or "").strip().lower()
    for russian, english in (("через", "in"), ("завтра", "tomorrow"), ("д", "d"), ("ч", "h"), ("мин", "m"), ("м", "m")):
        value = re.sub(rf"(?<![a-zа-яё]){russian}(?![a-zа-яё])", english, value) if len(russian) > 1 else re.sub(rf"(?<=\d)\s*{russian}(?![a-zа-яё])", english, value)
    base = datetime.fromtimestamp(now)
    match = re.fullmatch(r"in\s+(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?", value)
    if match and any(match.groups()):
        days, hours, minutes = (int(part or 0) for part in match.groups())
        return now + days * 86400 + hours * 3600 + minutes * 60
    day_offset = 0
    if value.startswith("tomorrow"):
        day_offset, value = 1, value[len("tomorrow"):].strip()
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp() + day_offset * 86400
        return candidate if candidate > now else (candidate + 86400 if not day_offset else None)
    for pattern in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        if pattern == "%d.%m %H:%M":
            parsed = parsed.replace(year=base.year)
        return parsed.timestamp()
    return None


# --------------------------------------------------------------------------
# effects -> Discord
# --------------------------------------------------------------------------


class StreamDirectorDiscord:
    """Applies domain effects to Discord and routes interactions back."""

    def __init__(
        self,
        client: Any,
        director: sd.Director,
        *,
        twitch_login: Callable[[], str | None] = lambda: None,
        vod_lookup: Callable[[list[str]], Any] | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
        events: Any = None,
    ) -> None:
        self.client = client
        self.director = director
        self.twitch_login = twitch_login
        self.vod_lookup = vod_lookup
        self.clock = clock
        self.log = log
        # bot_events.EventPublisher of this instance: Kairo's Social Awareness
        # learns that a stream started/ended (None: nothing announced).
        self.events = events
        self.problem: str | None = None
        self.missing_permissions: list[str] = []
        self._card_edited_at: dict[str, float] = {}
        self._dirty_polls: dict[str, float] = {}

    @property
    def config(self) -> sdc.StreamDirectorConfig:
        return self.director.config

    @property
    def language(self) -> str:
        return self.director.config.language

    def t(self, text: str, /, **params: Any) -> str:
        return bot_i18n.tr(self.language, text, **params)

    # -- where to post ---------------------------------------------------------------------

    def channel(self) -> Any:
        if not self.config.configured:
            return None
        return self.client.get_channel(self.config.channel_id)

    def check_channel(self) -> str | None:
        """Problem text about the stream channel for the Manager (English), or None."""
        channel = self.channel()
        if channel is None:
            self.missing_permissions = []
            return "The stream channel is not visible to the bot (deleted, or the bot is not in that server)."
        me = getattr(getattr(channel, "guild", None), "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        missing = []
        if me is not None and callable(permissions_for):
            permissions = permissions_for(me)
            missing = [label for name, label in CHANNEL_PERMISSIONS.items() if not getattr(permissions, name, False)]
        self.missing_permissions = missing
        if missing:
            return f"The bot is missing permissions in the stream channel: {', '.join(missing)}."
        return None

    async def _resolve(self, channel_id: Any) -> Any:
        if not channel_id:
            return None
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.client.fetch_channel(int(channel_id))
            except Exception:
                return None
        archived = getattr(channel, "archived", False)
        if archived:
            try:
                await channel.edit(archived=False)
            except Exception:
                pass
        return channel

    async def session_target(self, session: dict[str, Any]) -> Any:
        """The session thread (created with the card when missing), or the channel."""
        discord_refs = session.get("discord") or {}
        if not discord_refs.get("card_message_id"):
            await self._post_card(session)
            discord_refs = session.get("discord") or {}
        target = await self._resolve(discord_refs.get("thread_id"))
        return target or self.channel()

    async def _thread_or_channel(self, session: dict[str, Any] | None) -> Any:
        """The session's thread (an ended session too, never re-created), else the channel."""
        if session is None:
            return self.channel()
        if session["status"] != "ended":
            return await self.session_target(session)
        return await self._resolve((session.get("discord") or {}).get("thread_id")) or self.channel()

    async def _send(self, target: Any, content: str | None = None, **kwargs: Any) -> Any:
        if target is None:
            return None
        try:
            return await target.send(content=content, allowed_mentions=kwargs.pop("allowed_mentions", NO_MENTIONS), **kwargs)
        except discord.Forbidden:
            self.problem = "Discord refused a post (missing permissions in the stream channel or thread)."
        except discord.HTTPException as exc:
            self.problem = f"Discord rejected a post (HTTP {exc.status})."
        return None

    async def _edit(self, channel_id: Any, message_id: Any, **kwargs: Any) -> bool:
        channel = await self._resolve(channel_id)
        if channel is None or not message_id:
            return False
        try:
            await channel.get_partial_message(int(message_id)).edit(**kwargs)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as exc:
            self.problem = f"Discord rejected an edit (HTTP {exc.status})."
            return False

    # -- the card and the thread -------------------------------------------------------------

    async def _post_card(self, session: dict[str, Any]) -> None:
        channel = self.channel()
        if channel is None:
            self.problem = "The stream channel is not available; the session continues without Discord posts."
            return
        login = self.twitch_login()
        view = self.director.card_view(session)
        content = None
        mentions = NO_MENTIONS
        if self.config.go_live_role_id and session["status"] == "live":
            content = f"<@&{self.config.go_live_role_id}> " + (f"{twitch_url(login)}" if login else self.t("we are live!"))
            mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(self.config.go_live_role_id)])
        message = await self._send(
            channel,
            content,
            embed=card_embed(view, session, login, self.language),
            view=card_components(session["status"], self.language),
            allowed_mentions=mentions,
        )
        if message is None:
            return
        self._card_edited_at[session["id"]] = self.clock()
        thread = None
        name = f"🔴 {datetime.fromtimestamp(session['started_at']).strftime('%d.%m')} · {session['title'] or self.t('Stream')}"[:100]
        try:
            thread = await message.create_thread(name=name, auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        except discord.Forbidden:
            self.problem = "The bot cannot create threads in the stream channel (Create Public Threads); posts go to the channel."
        except discord.HTTPException as exc:
            self.problem = f"Discord did not create the session thread (HTTP {exc.status}); posts go to the channel."
        self.director.set_session_discord(
            session["id"],
            channel_id=channel.id,
            card_message_id=message.id,
            thread_id=getattr(thread, "id", None),
        )
        if thread is not None:
            await self._send(
                thread,
                self.t(
                    "This thread is the stream session: 📍 marks a moment (it is saved with the stream time), 🎯 suggests a challenge, "
                    "💬 sends a question, game, clip or topic to the streamer. Commands: /moment, /challenge, /suggest, /community."
                ),
            )

    async def update_card(self, session: dict[str, Any], *, force: bool = False) -> None:
        refs = session.get("discord") or {}
        if not refs.get("card_message_id"):
            return
        last = self._card_edited_at.get(session["id"], 0)
        if not force and self.clock() - last < CARD_MIN_INTERVAL:
            return
        self._card_edited_at[session["id"]] = self.clock()
        view = self.director.card_view(session)
        ok = await self._edit(
            refs.get("channel_id"),
            refs.get("card_message_id"),
            embed=card_embed(view, session, self.twitch_login(), self.language),
            view=card_components(session["status"], self.language),
            allowed_mentions=NO_MENTIONS,
        )
        if ok:
            self.director.card_done(session["id"])

    async def flush(self) -> None:
        """Debounced edits: the live card and polls with new votes."""
        session = self.director.session
        if session is not None and session.get("card_dirty"):
            await self.update_card(session)
        now = self.clock()
        for poll_id, since in list(self._dirty_polls.items()):
            if now - since >= POLL_MIN_INTERVAL:
                del self._dirty_polls[poll_id]
                await self._update_poll(poll_id)

    # -- effects ------------------------------------------------------------------------------

    async def apply(self, effects: list[sd.Effect]) -> None:
        for effect in effects:
            try:
                await self._apply(effect)
            except Exception as exc:  # one failed post never stops the others
                self.log(f"Stream Director post failed ({effect.kind}): {type(exc).__name__}")
            self._announce(effect)

    ANNOUNCED = {"session_started": "stream.started", "session_ended": "stream.ended", "level_up": "stream.level_up", "goal_completed": "stream.goal_completed"}

    def _announce(self, effect: sd.Effect) -> None:
        """Facts for the other DarkAbyss bots (bot_events); never raises."""
        kind = self.ANNOUNCED.get(effect.kind)
        if self.events is None or kind is None or not self.config.configured:
            return
        try:
            session = self.director.find_session(effect.ref) if effect.ref else None
            discord_ids = (session or {}).get("discord") or {}
            self.events.publish(
                kind,
                self.config.guild_id,
                channel_id=discord_ids.get("thread_id") or self.config.channel_id,
                message_id=discord_ids.get("card_message_id"),
                data={
                    "title": (session or {}).get("title"),
                    "category": ((session or {}).get("categories") or [{}])[-1].get("name") if session else None,
                    "text": effect.text or None,
                },
            )
        except Exception:
            pass

    async def _apply(self, effect: sd.Effect) -> None:
        director = self.director
        if not self.config.configured or not self.config.enabled:
            return
        if effect.kind == "session_started":
            session = director.find_session(effect.ref)
            if session is not None and not (session.get("discord") or {}).get("card_message_id"):
                await self._post_card(session)
        elif effect.kind == "card_update":
            session = director.find_session(effect.ref)
            if session is not None:
                await self.update_card(session)
        elif effect.kind in ("thread_post", "level_up", "goal_completed"):
            session = director.find_session(effect.ref) if effect.ref else None
            await self._send(await self._thread_or_channel(session), effect.text)
        elif effect.kind == "challenges_digest":
            session = director.find_session(effect.ref)
            if session is None:
                return
            target = await self.session_target(session)
            await self._send(target, self.t("🎯 Challenges waiting for the streamer (👍 to support):"))
            for challenge_id in effect.data.get("ids", []):
                await self._post_challenge(challenge_id, target)
        elif effect.kind == "challenge_post":
            session = director.session
            target = await self.session_target(session) if session is not None else None
            await self._post_challenge(effect.ref, target)
        elif effect.kind == "challenge_update":
            challenge = director.state["challenges"].get(str(effect.ref))
            if challenge and challenge.get("message"):
                await self._edit(
                    challenge["message"]["channel_id"],
                    challenge["message"]["message_id"],
                    content=challenge_text(challenge, self.language),
                    view=challenge_components(challenge, self.language),
                    allowed_mentions=NO_MENTIONS,
                )
        elif effect.kind == "poll_post":
            await self._post_poll(effect.ref)
        elif effect.kind == "poll_update":
            poll = director.state["polls"].get(str(effect.ref))
            if poll is not None and poll["status"] != "open":
                self._dirty_polls.pop(str(effect.ref), None)
                await self._update_poll(str(effect.ref))  # closing/locking shows at once
            else:
                self._dirty_polls.setdefault(str(effect.ref), self.clock())
        elif effect.kind == "session_ended":
            await self._session_ended(effect.ref)

    async def _post_challenge(self, challenge_id: Any, target: Any) -> None:
        challenge = self.director.state["challenges"].get(str(challenge_id))
        if challenge is None or target is None:
            return
        message = await self._send(target, challenge_text(challenge, self.language), view=challenge_components(challenge, self.language))
        if message is not None:
            self.director.set_message("challenges", challenge["id"], target.id, message.id)

    async def _post_poll(self, poll_id: Any) -> None:
        poll = self.director.state["polls"].get(str(poll_id))
        if poll is None:
            return
        session = self.director.session
        if poll["target"] == "session" and session is not None and session["id"] == poll["session_id"]:
            target = await self.session_target(session)
        else:
            target = self.channel()
        message = await self._send(target, embed=poll_embed(poll, self.director.poll_result(poll), self.language), view=poll_components(poll, self.language))
        if message is not None:
            self.director.set_message("polls", poll["id"], target.id, message.id)

    async def _update_poll(self, poll_id: str) -> None:
        poll = self.director.state["polls"].get(poll_id)
        if poll is None or not poll.get("message"):
            return
        await self._edit(
            poll["message"]["channel_id"],
            poll["message"]["message_id"],
            embed=poll_embed(poll, self.director.poll_result(poll), self.language),
            view=poll_components(poll, self.language),
            allowed_mentions=NO_MENTIONS,
        )

    async def _session_ended(self, session_id: Any) -> None:
        session = self.director.find_session(session_id)
        if session is None:
            return
        if self.vod_lookup is not None and session.get("stream_ids") and not session.get("vod"):
            try:
                vod = await self.vod_lookup(list(session["stream_ids"]))
            except Exception:
                vod = None
            if vod:
                self.director.attach_vod(session["id"], vod[0], vod[1])
                session = self.director.find_session(session_id) or session
        refs = session.get("discord") or {}
        if refs.get("card_message_id"):
            await self.update_card(session, force=True)
        target = await self._resolve(refs.get("thread_id")) or self.channel()
        await self._send(target, embed=recap_embed(session, self.language))
        thread = await self._resolve(refs.get("thread_id"))
        if thread is not None:
            try:
                await thread.edit(name=f"📼 {thread.name.lstrip('🔴 ')}"[:100])
            except Exception:
                pass

    # -- interactions -------------------------------------------------------------------------

    async def _reply(self, interaction: Any, text: str, **kwargs: Any) -> None:
        content = text or None
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)
            else:
                await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)
        except Exception:
            pass

    async def _outcome(self, interaction: Any, outcome: sd.Outcome) -> None:
        await self._reply(interaction, outcome.text)
        await self.apply(outcome.effects)

    def _in_guild(self, interaction: Any) -> bool:
        guild_id = getattr(interaction, "guild_id", None) or getattr(getattr(interaction, "guild", None), "id", None)
        return self.config.configured and guild_id == self.config.guild_id

    async def handle_interaction(self, interaction: Any) -> bool:
        """Buttons and modals with an ``sd:`` custom id. True if it was ours."""
        data = getattr(interaction, "data", None) or {}
        parts = parse_custom_id(data.get("custom_id") if isinstance(data, dict) else None)
        if parts is None:
            return False
        if not self.director.available or not self._in_guild(interaction) or not self.config.enabled:
            await self._reply(interaction, self.t(UNAVAILABLE))
            return True
        actor = actor_for(getattr(interaction, "user", None), self.config)
        director = self.director
        head = parts[0]
        try:
            if head == "card":
                modal = {"moment": moment_modal, "challenge": challenge_modal, "suggest": suggest_modal}.get(parts[1])
                if modal is None:
                    return True
                if parts[1] == "moment" and director.session is None:
                    await self._reply(interaction, self.t("Moments can be marked while the stream is live."))
                    return True
                await interaction.response.send_modal(modal(self.language))
            elif head == "modal":
                values = modal_values(data)
                if parts[1] == "moment":
                    await self._outcome(interaction, director.mark_moment(actor, values.get("comment", "")))
                elif parts[1] == "challenge":
                    await self._outcome(interaction, director.suggest_challenge(actor, values.get("text", "")))
                elif parts[1] == "suggest":
                    link = values.get("link") or None
                    await self._outcome(interaction, director.suggest(actor, guess_kind(values.get("text", ""), link), values.get("text", ""), link))
            elif head == "ch" and len(parts) == 3:
                if parts[2] == "support":
                    await self._outcome(interaction, director.support_challenge(actor, parts[1]))
                else:
                    await self._outcome(interaction, director.decide_challenge(actor, parts[1], parts[2]))
            elif head == "poll" and len(parts) >= 3:
                if parts[2] == "vote" and len(parts) == 4:
                    await self._outcome(interaction, director.vote(actor, parts[1], int(parts[3])))
                elif parts[2] == "lock":
                    await self._outcome(interaction, director.lock_prediction(actor, parts[1]))
                elif parts[2] == "resolve" and len(parts) == 4:
                    await self._outcome(interaction, director.resolve_prediction(actor, parts[1], int(parts[3])))
            elif head == "inbox" and len(parts) == 3:
                await self._outcome(interaction, director.set_inbox_status(actor, parts[1], parts[2]))
        except ValueError:
            await self._reply(interaction, self.t("That button is outdated."))
        except sd.DirectorUnavailable:
            await self._reply(interaction, self.t(UNAVAILABLE))
        return True

    # -- slash commands -------------------------------------------------------------------------

    def build_commands(self, tree: app_commands.CommandTree) -> None:
        """Slash commands with descriptions in this bot's language (registered at start)."""
        front = self
        language = self.language

        def d(text: str) -> str:
            return bot_i18n.tr(language, text)[:100]

        async def guard(interaction: discord.Interaction) -> sd.Actor | None:
            if not front.director.available or not front._in_guild(interaction) or not front.config.enabled:
                await front._reply(interaction, front.t(UNAVAILABLE))
                return None
            return actor_for(interaction.user, front.config)

        @tree.command(name="moment", description=d("Mark this moment of the live stream"))
        @app_commands.describe(comment=d("What happened (optional)"))
        async def moment(interaction: discord.Interaction, comment: str | None = None) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.mark_moment(actor, comment or ""))

        @tree.command(name="challenge", description=d("Suggest a challenge for the streamer"))
        @app_commands.describe(text=d("The challenge"))
        async def challenge(interaction: discord.Interaction, text: app_commands.Range[str, 3, 150]) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.suggest_challenge(actor, text))

        @tree.command(name="suggest", description=d("Send a question, game, clip or topic to the streamer inbox"))
        @app_commands.describe(kind=d("What it is"), text=d("Your suggestion"), link=d("Link (for clips)"))
        @app_commands.choices(kind=kind_choices(language))
        async def suggest(interaction: discord.Interaction, kind: app_commands.Choice[str], text: app_commands.Range[str, 1, 200], link: str | None = None) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.suggest(actor, kind.value, text, link))

        @tree.command(name="poll", description=d("Quick poll for the stream (stream team)"))
        @app_commands.describe(question=d("The question"), options=d("2 to 5 options separated by |"), minutes=d("1 to 60 (default from the Manager)"))
        async def poll(interaction: discord.Interaction, question: app_commands.Range[str, 3, 150], options: str, minutes: app_commands.Range[int, 1, 60] | None = None) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.create_poll(actor, "poll", question, options.split("|"), minutes))

        @tree.command(name="prediction", description=d("Prediction without stakes (stream team)"))
        @app_commands.describe(question=d("The question"), options=d("2 to 5 outcomes separated by |"), minutes=d("Minutes until predictions lock"))
        async def prediction(interaction: discord.Interaction, question: app_commands.Range[str, 3, 150], options: str, minutes: app_commands.Range[int, 1, 60] | None = None) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.create_poll(actor, "prediction", question, options.split("|"), minutes))

        @tree.command(name="inbox", description=d("Streamer inbox: questions, games, clips, topics, challenges (stream team)"))
        async def inbox(interaction: discord.Interaction) -> None:
            actor = await guard(interaction)
            if not actor:
                return
            if not actor.team:
                await front._reply(interaction, front.t(NOT_YOURS))
                return
            items = front.director.inbox_items(limit=10)
            view = inbox_components(items, front.language)
            kwargs: dict[str, Any] = {"embed": inbox_embed(items, front.director.inbox_counts(), front.language)}
            if view is not None:
                kwargs["view"] = view
            await front._reply(interaction, "", **kwargs)

        @tree.command(name="community", description=d("Community level, season and goals"))
        async def community(interaction: discord.Interaction) -> None:
            if await guard(interaction):
                await front._reply(interaction, "", embed=community_embed(front.director.community_view(), front.language))

        stream = app_commands.Group(name="stream", description=d("Stream session (stream team)"))

        @stream.command(name="start", description=d("Start a session by hand (when Twitch is not connected)"))
        @app_commands.describe(title=d("Title of the stream"))
        async def stream_start(interaction: discord.Interaction, title: app_commands.Range[str, 1, 140] | None = None) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.manual_start(actor, title or ""))

        @stream.command(name="end", description=d("End the current session now and post the recap"))
        async def stream_end(interaction: discord.Interaction) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.manual_end(actor))

        goal = app_commands.Group(name="goal", description=d("Community goals (stream team)"))

        @goal.command(name="add", description=d("Add a community goal"))
        @app_commands.describe(metric=d("What counts"), target=d("How many"), period=d("For which period"), title=d("Name of the goal (optional)"))
        @app_commands.choices(metric=metric_choices(language), period=period_choices(language))
        async def goal_add(
            interaction: discord.Interaction,
            metric: app_commands.Choice[str],
            target: app_commands.Range[int, 1, 100000],
            period: app_commands.Choice[str],
            title: app_commands.Range[str, 1, 80] | None = None,
        ) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.add_goal(actor, title or "", metric.value, target, period.value))

        @goal.command(name="remove", description=d("Remove a community goal"))
        @app_commands.describe(goal_id=d("Number of the goal"))
        async def goal_remove(interaction: discord.Interaction, goal_id: app_commands.Range[int, 1, 10_000_000]) -> None:
            actor = await guard(interaction)
            if actor:
                await front._outcome(interaction, front.director.remove_goal(actor, str(goal_id)))

        @tree.command(name="nextstream", description=d("Announce the next stream as a Discord event (stream team)"))
        @app_commands.describe(when=d("e.g. 'tomorrow 20:00', 'in 2d 3h', '2026-10-05 19:30' (this PC's time zone)"), title=d("Title of the event (optional)"))
        async def nextstream(interaction: discord.Interaction, when: str, title: app_commands.Range[str, 1, 100] | None = None) -> None:
            actor = await guard(interaction)
            if not actor:
                return
            if not actor.team:
                await front._reply(interaction, front.t(NOT_YOURS))
                return
            await front._reply(interaction, await front.schedule_next_stream(interaction.guild, when, title))

        tree.add_command(stream)
        tree.add_command(goal)

    async def schedule_next_stream(self, guild: Any, when: str, title: str | None) -> str:
        start = parse_when(when, self.clock())
        if start is None or start <= self.clock() + 60:
            return self.t("I could not read that time. Try 'tomorrow 20:00', 'in 2h' or '2026-10-05 19:30'.")
        choice = self.director.state["meta"].get("next_stream_choice") or {}
        leaders = choice.get("leaders") or []
        description = self.t("Next stream")
        if leaders:
            description = self.t("Community pick from the last vote: {games}", games=", ".join(leaders))
        login = self.twitch_login()
        try:
            event = await guild.create_scheduled_event(
                name=(title or (self.t("Stream: {game}", game=leaders[0]) if leaders else self.t("Next stream")))[:100],
                start_time=datetime.fromtimestamp(start).astimezone(),
                end_time=datetime.fromtimestamp(start + 3 * 3600).astimezone(),
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=twitch_url(login) or "Twitch",
                description=sd.plain(description)[:1000],
            )
        except discord.Forbidden:
            return self.t("The bot needs the Manage Events permission to create the event.")
        except discord.HTTPException as exc:
            return self.t("Discord did not create the event (HTTP {status}).", status=exc.status)
        return self.t("📅 Event created for {time}: {url}", time=f"<t:{int(start)}:F>", url=getattr(event, "url", ""))
