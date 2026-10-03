"""Discord adapter for Game Presence (events in, public suggestions out).

* member_game / member_voice: read Discord Presence activities and voice state.
* render_message: deterministic templates; mentions are built ONLY here,
  from the approved user IDs of a Suggestion (players and, for Group Up, the
  rest of the target voice channel decided by the engine).
* DiscordNotifier: posts one public message with Mute pings / Allow pings
  buttons; allowed_mentions lists exactly the approved users (never
  @everyone, @here or roles), so even generated text cannot ping anyone else.
  Whole-voice pings are plain user mentions: no role is created, so the bot
  needs no Manage Roles permission and nothing is left behind after a crash.
* handle_preference_interaction: persistent buttons; the actor is always
  interaction.user and only their own opt-state changes; replies are ephemeral.
* make_ai_rewriter: optional wording layer; it may only rephrase a template
  with fixed placeholders, never choose whom or when to ping. It is asked for
  a fresh tone each time and shown its recent wordings, so messages vary.
  Any failure falls back to the deterministic template.
* After a post the bot announces it on the bot event bus (bot_events), so
  Kairo's Social Awareness knows what Group Up just did.
* GamePresenceRuntime: wires the engine to bot events and writes a status
  file for the Manager. Used by the dedicated Game Presence bot
  (GamePresence.py), which has its own Discord application and process.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

import discord

import admin_terminal
import bot_i18n
import game_presence as gp

CUSTOM_ID_MUTE = "dab:gp:mute"
CUSTOM_ID_ALLOW = "dab:gp:allow"
STATUS_FILE_NAME = "game_presence_status.json"
# Diagnoses written to the bot log when they start (problems + successful posts).
LOGGED_DIAGNOSES = frozenset(
    {
        "not_configured",
        "config_problem",
        "state_problem",
        "intent_missing",
        "guild_unavailable",
        "channel_unavailable",
        "permission_denied",
        "send_failed",
        "posted",
    }
)
AI_REWRITE_TIMEOUT_SECONDS = 15.0
MAX_MESSAGE_CHARS = 1500
MAX_REWRITE_CHARS = 320
RECENT_WORDINGS = 6
EVENT_GROUP_UP = "group_up.suggested"

# Texts are English keys; the bot instance's language picks the wording
# (bot_i18n, Russian in bot_i18n_ru_game_presence). Game Presence spoke
# Russian before languages existed, so "ru" is the default everywhere here.
DEFAULT_LANGUAGE = gp.DEFAULT_CONFIG["language"]
PREF_MUTED = "Done: I will not mention you in game suggestions any more. To turn it back on, press Allow pings."
PREF_ALREADY_MUTED = "Game pings are already off for you."
PREF_ALLOWED = "Done: game pings are on again."
PREF_ALREADY_ALLOWED = "Game pings are already on for you."
PREF_UNAVAILABLE = "Game Presence settings are unavailable right now. Try again later."
BUTTON_MUTE = "Mute pings"
BUTTON_ALLOW = "Allow pings"

_COUNT_WORDS = {"ru": {3: "трое", 4: "четверо", 5: "пятеро", 6: "шестеро", 7: "семеро", 8: "восьмеро", 9: "девятеро", 10: "десятеро"}}
TEMPLATE_PAIR = "{targets}, you have both been in {game} for a few minutes. Want to get together in voice?"
TEMPLATE_GROUP = "{targets}, {count} of you are already in {game}. Time to get together?"
TEMPLATE_SPLIT_PAIR = "{targets}, you have both been in {game} for a few minutes, but in different voice channels. Want to join up?"
TEMPLATE_SPLIT_GROUP = "{targets}, {count} of you are in {game}, but in different voice channels. Want to join up?"
# join: one/several outsiders x one/several players already in the voice channel.
TEMPLATE_JOIN_ONE = "{targets}, {others} are already playing {game} in {channel}. Jump in!"
TEMPLATE_JOIN_MANY = "{targets}, {others} are already playing {game} in {channel}. Jump in, all of you!"
TEMPLATE_JOIN_ONE_SOLO = "{targets}, {others} is already playing {game} in {channel}. Jump in!"
TEMPLATE_JOIN_MANY_SOLO = "{targets}, {others} is already playing {game} in {channel}. Jump in, all of you!"
# Added when the rest of the voice channel is told too (whole-voice ping).
TEMPLATE_CREW = "{crew}, heads up: someone may join you."
TEMPLATES = (
    TEMPLATE_PAIR,
    TEMPLATE_GROUP,
    TEMPLATE_SPLIT_PAIR,
    TEMPLATE_SPLIT_GROUP,
    TEMPLATE_JOIN_ONE,
    TEMPLATE_JOIN_MANY,
    TEMPLATE_JOIN_ONE_SOLO,
    TEMPLATE_JOIN_MANY_SOLO,
    TEMPLATE_CREW,
)
# The AI is asked for one of these tones at random, so wordings do not repeat.
AI_TONES = ("casual", "playful", "warm", "short and direct", "energetic", "laid-back", "friendly teasing")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_FORBIDDEN_AI_TEXT = re.compile(r"<[@#&!:a]|@everyone|@here|https?://|discord\.gg", re.IGNORECASE)
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


# --------------------------------------------------------------------------
# reading Discord state
# --------------------------------------------------------------------------


def member_game(member: Any) -> tuple[str, float | None] | None:
    """(game name, start timestamp) of the member's "Playing" activity, if any."""
    for activity in getattr(member, "activities", None) or ():
        if getattr(activity, "type", None) != discord.ActivityType.playing:
            continue
        name = getattr(activity, "name", None)
        if not isinstance(name, str) or not name.strip():
            continue
        start = getattr(activity, "start", None)
        return name, start.timestamp() if isinstance(start, datetime) else None
    return None


def member_voice(member: Any) -> int | None:
    channel = getattr(getattr(member, "voice", None), "channel", None)
    channel_id = getattr(channel, "id", None)
    return channel_id if isinstance(channel_id, int) else None


# --------------------------------------------------------------------------
# message text
# --------------------------------------------------------------------------


def safe_text(value: Any, limit: int = 80) -> str:
    """Names from Discord as plain text: no mentions, no markdown tricks."""
    text = " ".join(str(value or "").split())[:limit]
    text = text.replace("@", "@​").replace("<", "‹").replace(">", "›")
    return re.sub(r"([\\*_~`|])", r"\\\1", text)


def mention(user_id: int) -> str:
    return f"<@{int(user_id)}>"


def join_names(items: list[str], language: str | None = DEFAULT_LANGUAGE) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + (" и " if language == "ru" else " and ") + items[-1]


def join_ru(items: list[str]) -> str:
    return join_names(items, "ru")


def template_key(suggestion: gp.Suggestion) -> str:
    """The English template (translation key) for a suggestion (without the crew sentence)."""
    if suggestion.kind == "join":
        one = len(suggestion.outsider_user_ids) == 1
        if len(suggestion.voice_member_ids) == 1:
            return TEMPLATE_JOIN_ONE_SOLO if one else TEMPLATE_JOIN_MANY_SOLO
        return TEMPLATE_JOIN_ONE if one else TEMPLATE_JOIN_MANY
    if suggestion.kind == "split_voice":
        return TEMPLATE_SPLIT_PAIR if len(suggestion.target_user_ids) == 2 else TEMPLATE_SPLIT_GROUP
    return TEMPLATE_PAIR if len(suggestion.target_user_ids) == 2 else TEMPLATE_GROUP


def template_for(suggestion: gp.Suggestion, *, language: str | None = DEFAULT_LANGUAGE) -> str:
    """The template in the bot's language (placeholders stay as they are)."""
    text = bot_i18n.tr(language, template_key(suggestion))
    if suggestion.kind == "join" and suggestion.voice_crew_ids:
        text = f"{text} {bot_i18n.tr(language, TEMPLATE_CREW)}"
    return text


def fill(
    template: str,
    suggestion: gp.Suggestion,
    channel_name: str | None,
    *,
    language: str | None = DEFAULT_LANGUAGE,
) -> str:
    """Substitute placeholders; mentions come only from the suggestion's IDs."""
    if suggestion.kind == "join":
        targets = join_names([mention(user) for user in suggestion.outsider_user_ids], language)
        others = join_names([mention(user) for user in suggestion.voice_member_ids], language)
    else:
        targets = " ".join(mention(user) for user in suggestion.target_user_ids)
        others = ""
    count = len(suggestion.target_user_ids)
    values = {
        "targets": targets,
        "others": others,
        "crew": join_names([mention(user) for user in suggestion.voice_crew_ids], language),
        "game": f"**{safe_text(suggestion.game_display_name)}**",
        "channel": f"**{safe_text(channel_name or bot_i18n.tr(language, 'voice'))}**",
        "count": _COUNT_WORDS.get(language or "", {}).get(count) or bot_i18n.tr(language, "{count}", count=count),
    }
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), template)[:MAX_MESSAGE_CHARS]


def render_message(
    suggestion: gp.Suggestion,
    channel_name: str | None,
    rewritten_template: str | None = None,
    *,
    language: str | None = DEFAULT_LANGUAGE,
) -> str:
    template = rewritten_template if rewritten_template else template_for(suggestion, language=language)
    return fill(template, suggestion, channel_name, language=language)


def validate_rewrite(candidate: Any, original_template: str, language: str | None = None) -> str | None:
    """Accept an AI rewrite only if it keeps exactly the original placeholders.

    The rewrite cannot add or drop people: mentions are inserted afterwards
    from user IDs, and any raw mention/link syntax rejects the rewrite. With
    ``language`` the wording must also be in that language (Russian text has
    Cyrillic letters, English text has none); placeholders do not count.
    """
    if not isinstance(candidate, str):
        return None
    text = " ".join(candidate.split())
    if not text or len(text) > MAX_REWRITE_CHARS or _FORBIDDEN_AI_TEXT.search(text):
        return None
    wanted = sorted(_PLACEHOLDER_RE.findall(original_template))
    if sorted(_PLACEHOLDER_RE.findall(text)) != wanted:
        return None
    words = _PLACEHOLDER_RE.sub("", text)
    if "{" in words or "}" in words:
        return None
    # The people asked to join are addressed first-thing, not buried at the end.
    if "{targets}" not in text[:60]:
        return None
    if language == "ru" and not _CYRILLIC.search(words):
        return None
    if language == "en" and _CYRILLIC.search(words):
        return None
    return text


Rewriter = Callable[..., Awaitable[str | None]]


def rewrite_context(
    suggestion: gp.Suggestion,
    *,
    language: str,
    channel_name: str | None,
    template: str,
) -> dict[str, Any]:
    """What the wording AI may know: the situation in numbers, never who."""
    return {
        "language": language,
        "kind": suggestion.kind,
        "game": suggestion.game_display_name,
        "target_voice_channel": channel_name,
        "outsider_count": len(suggestion.outsider_user_ids),
        "voice_detected_player_count": len(suggestion.voice_member_ids),
        "total_detected_player_count": len(suggestion.target_user_ids),
        "notifies_rest_of_voice": bool(suggestion.voice_crew_ids),
        "rest_of_voice_count": len(suggestion.voice_crew_ids),
        "required_placeholders": sorted(_PLACEHOLDER_RE.findall(template)),
        "placeholder_meaning": {
            "targets": "the people invited (mentions)",
            "others": "the players already in the voice channel (mentions)",
            "crew": "everyone else in that voice channel, who may not show the game (mentions)",
            "game": "the game name",
            "channel": "the voice channel name",
            "count": "how many players",
        },
        "rules": [
            "Use placeholders exactly as provided.",
            "Do not add Discord mentions, role mentions, channel mentions, links, names, IDs, or emojis-only text.",
            "Do not decide who is pinged; the application inserts mentions after validation.",
        ],
    }


def make_ai_rewriter(get_orchestrator: Callable[[], Any], choose: Callable[[tuple[str, ...]], str] | None = None) -> Rewriter:
    """Optional: rephrase the template with the configured AI (CREATIVE route, no tools).

    Each call asks for a random tone and lists the last accepted wordings of
    this bot, so the messages stay varied; the result is validated like any
    other rewrite (placeholders, no mentions/links, language).
    """
    import random

    pick = choose or random.choice
    recent: list[str] = []

    async def rewrite(template: str, suggestion: gp.Suggestion, context: dict[str, Any] | None = None) -> str | None:
        language = DEFAULT_LANGUAGE
        try:
            import ai_orchestrator

            ai_platform = ai_orchestrator.ai_platform  # the module the orchestrator validates against
            orchestrator = get_orchestrator()
            if orchestrator is None:
                return None
            language = bot_i18n.normalize_language((context or {}).get("language"), DEFAULT_LANGUAGE)
            semantic = context or rewrite_context(suggestion, language=language, channel_name=None, template=template)
            request = ai_orchestrator.OrchestratorRequest(
                messages=(
                    ai_platform.AIMessage(
                        role="system",
                        content=(
                            f"Rewrite a Discord Group Up message in {bot_i18n.LANGUAGE_NAMES_EN[language]} so it sounds "
                            "natural and human, like a friend in the server, not like a bot notification. Keep it to one or "
                            "two short sentences. Use only the provided placeholders, each exactly once and unchanged; put "
                            "{targets} near the start. Add no names, IDs, mentions, roles, channels, links, or emojis-only text. "
                            "AI controls wording only; the application controls game, recipients, voice target, timing, "
                            "cooldowns, opt-outs, and allowed mentions. Reply with the message template only."
                        ),
                    ),
                    ai_platform.AIMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "template": template,
                                "tone": pick(AI_TONES),
                                "avoid_these_recent_wordings": list(recent[-RECENT_WORDINGS:]),
                                "semantic_context": semantic,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                ),
                task_class="CREATIVE",
                allowed_tool_names=(),
                response_language=language,
            )
            result = await asyncio.wait_for(orchestrator.orchestrate(request), AI_REWRITE_TIMEOUT_SECONDS)
        except Exception:
            return None
        if getattr(getattr(result, "status", None), "value", None) != "COMPLETED":
            return None
        accepted = validate_rewrite(getattr(result, "content", None), template, language)
        if accepted is not None:
            recent.append(accepted)
            del recent[:-RECENT_WORDINGS]
        return accepted

    return rewrite


# --------------------------------------------------------------------------
# notifier and buttons
# --------------------------------------------------------------------------


def preference_view(language: str | None = DEFAULT_LANGUAGE) -> discord.ui.View:
    """Persistent public buttons; clicks are handled by the bot's on_interaction listener."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=bot_i18n.tr(language, BUTTON_MUTE), style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_MUTE))
    view.add_item(discord.ui.Button(label=bot_i18n.tr(language, BUTTON_ALLOW), style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_ALLOW))
    return view


def allowed_mentions_for(user_ids: tuple[int, ...]) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        replied_user=False,
        users=[discord.Object(id=int(user_id)) for user_id in user_ids],
    )


class DiscordNotifier:
    """Posts suggestions. Holds no cooldown or policy logic."""

    def __init__(self, language: Callable[[], str] | None = None) -> None:
        self._language = language or (lambda: DEFAULT_LANGUAGE)

    async def send(self, channel: Any, content: str, target_user_ids: tuple[int, ...]) -> Any:
        view = preference_view(self._language())
        message = await channel.send(content, view=view, allowed_mentions=allowed_mentions_for(target_user_ids))
        view.stop()  # clicks go through the global listener (works after restarts)
        return message


async def handle_preference_interaction(interaction: Any, preferences: gp.PreferenceBook | None, language: str | None = DEFAULT_LANGUAGE) -> bool:
    """Mute/Allow pings. Returns True if the click was ours.

    Only interaction.user's own setting changes; the message text, button
    label and message author play no role in the decision.
    """
    data = getattr(interaction, "data", None) or {}
    custom_id = data.get("custom_id") if isinstance(data, dict) else None
    if custom_id not in (CUSTOM_ID_MUTE, CUSTOM_ID_ALLOW):
        return False
    guild_id = getattr(interaction, "guild_id", None) or getattr(getattr(interaction, "guild", None), "id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if preferences is None or not isinstance(guild_id, int) or not isinstance(user_id, int):
        text = PREF_UNAVAILABLE
    else:
        try:
            changed = preferences.set_muted(guild_id, user_id, custom_id == CUSTOM_ID_MUTE)
        except Exception:
            text = PREF_UNAVAILABLE
        else:
            if custom_id == CUSTOM_ID_MUTE:
                text = PREF_MUTED if changed else PREF_ALREADY_MUTED
            else:
                text = PREF_ALLOWED if changed else PREF_ALREADY_ALLOWED
    try:
        await interaction.response.send_message(bot_i18n.tr(language, text), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass
    return True


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------


class GamePresenceRuntime:
    """Feeds Discord events into the engine and publishes due suggestions."""

    def __init__(
        self,
        client: Any,
        engine: gp.GamePresenceEngine,
        *,
        notifier: DiscordNotifier | None = None,
        rewriter: Rewriter | None = None,
        runtime_dir: Any = None,
        presence_intent: bool = False,
        log: Callable[[str], None] | None = None,
        events: Any = None,
    ) -> None:
        self.client = client
        self.engine = engine
        self.notifier = notifier or DiscordNotifier(lambda: self.engine.config.language)
        self.rewriter = rewriter
        # bot_events.EventPublisher of this instance (None: nothing announced).
        self.events = events
        self.runtime_dir = runtime_dir
        self.presence_intent = presence_intent
        self.log = log or (lambda text: print(text, flush=True))
        self.diagnosis = gp.Diagnosis("starting", "Starting: the first check runs within a few seconds.")
        self.problem: str | None = None
        # Set by the bot when its config is invalid / not configured yet; it
        # survives tick() so the Manager sees why nothing is posted.
        self.config_problem: str | None = None

    # -- configuration ----------------------------------------------------------

    def apply_config(self, config: gp.GamePresenceConfig) -> None:
        was_tracking = self.engine.config.active and self.engine.config.guild_id
        self.engine.configure(config)
        if config.active and config.guild_id != was_tracking:
            self.seed()

    # -- events ------------------------------------------------------------------

    def _observe(self, member: Any) -> None:
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "bot", False):
            return
        game = member_game(member)
        self.engine.observe(
            guild.id,
            member.id,
            game[0] if game else None,
            member_voice(member),
            game[1] if game else None,
        )

    def seed(self) -> None:
        """(Re)build tracker state from the cached members of the configured server."""
        config = self.engine.config
        if not config.active:
            return
        getter = getattr(self.client, "get_guild", None)
        guild = getter(config.guild_id) if callable(getter) else None
        for member in getattr(guild, "members", None) or []:
            self._observe(member)

    def on_presence_update(self, before: Any, after: Any) -> None:
        self._observe(after)

    def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        guild = getattr(member, "guild", None)
        if guild is not None and not getattr(member, "bot", False):
            channel = getattr(getattr(after, "channel", None), "id", None)
            self.engine.set_voice(guild.id, member.id, channel if isinstance(channel, int) else None)

    def on_member_join(self, member: Any) -> None:
        self._observe(member)

    def on_member_remove(self, member: Any) -> None:
        guild = getattr(member, "guild", None)
        if guild is not None:
            self.engine.remove_member(guild.id, member.id)

    # -- publishing ----------------------------------------------------------------

    def _channel(self) -> tuple[Any, Any]:
        config = self.engine.config
        getter = getattr(self.client, "get_guild", None)
        guild = getter(config.guild_id) if callable(getter) and config.guild_id else None
        channel = guild.get_channel(config.channel_id) if guild is not None and config.channel_id else None
        return guild, channel

    def diagnose(self, code: str, text: str) -> None:
        """Current reason shown in the Manager; problems are logged once per change."""
        previous = self.diagnosis
        self.diagnosis = gp.Diagnosis(code, text)
        if code in LOGGED_DIAGNOSES and (previous.code != code or previous.text != text):
            try:
                self.log(f"Game Presence: {text}")
            except Exception:
                pass

    @staticmethod
    def missing_permissions(guild: Any, channel: Any) -> list[str]:
        """Permissions the bot lacks to post suggestions in ``channel`` (checked before posting)."""
        me = getattr(guild, "me", None)
        checker = getattr(channel, "permissions_for", None)
        if me is None or not callable(checker):
            return []
        try:
            permissions = checker(me)
        except Exception:
            return []
        missing = []
        if not getattr(permissions, "view_channel", True):
            missing.append("View Channel")
        if not getattr(permissions, "send_messages", True):
            missing.append("Send Messages")
        return missing

    async def tick(self) -> int:
        """Publish due suggestions; returns how many were posted."""
        config = self.engine.config
        self.problem = self.config_problem
        if config.enabled and not self.presence_intent:
            self.problem = "Restart the bot: Presence Intent is requested only at startup."
            self.diagnose("intent_missing", self.problem)
            self.write_status()
            return 0
        posted = 0
        if not config.active:
            if self.config_problem == gp.NOT_CONFIGURED_TEXT:
                self.diagnose("not_configured", "Not configured: choose a server and a suggestion channel on the Game Presence page and press Save. Nothing is posted until then.")
            elif self.config_problem:
                self.diagnose("config_problem", self.config_problem)
            else:
                self.diagnose("paused", "Paused: 'Post suggestions' is switched off on the Game Presence page.")
        else:
            guild, channel = self._channel()
            missing = self.missing_permissions(guild, channel) if guild is not None and channel is not None else []
            if guild is None:
                self.problem = "The bot is not in the selected server."
                self.diagnose("guild_unavailable", self.problem)
            elif channel is None or not hasattr(channel, "send"):
                self.problem = "The selected text channel was not found."
                self.diagnose("channel_unavailable", self.problem)
            elif missing:
                self.problem = f"Missing Discord permission in #{getattr(channel, 'name', channel.id)}: {', '.join(missing)}."
                self.diagnose("permission_denied", self.problem)
            else:
                posted_games = []
                for suggestion in self.engine.tick():
                    try:
                        await self._publish(channel, guild, suggestion)
                        posted += 1
                        posted_games.append(suggestion.game_display_name)
                    except discord.Forbidden:
                        self.problem = "Discord refused to post in the channel (check the bot's permissions)."
                        self.diagnose("permission_denied", self.problem)
                    except Exception as exc:
                        self.problem = f"Posting failed: {type(exc).__name__}."
                        self.diagnose("send_failed", self.problem)
                if posted_games:
                    self.diagnose("posted", f"Suggestion posted for {', '.join(posted_games)} in #{getattr(channel, 'name', channel.id)}.")
                elif self.problem is None:
                    diagnosis = self.engine.diagnosis
                    if diagnosis.code == "no_activity":
                        # Say what Discord does deliver, so "bot broken" and
                        # "nobody shares a game" look different.
                        snapshot = self.presence_snapshot()
                        diagnosis = gp.Diagnosis(
                            "no_activity",
                            f"No game activity shared: {snapshot['online']} of {snapshot['visible']} members online, "
                            "none shows 'Playing' in Discord.",
                        )
                    self.diagnose(diagnosis.code, diagnosis.text)
        self.write_status()
        return posted

    async def _publish(self, channel: Any, guild: Any, suggestion: gp.Suggestion) -> None:
        voice = guild.get_channel(suggestion.voice_channel_id) if suggestion.voice_channel_id else None
        language = self.engine.config.language
        template = template_for(suggestion, language=language)
        rewritten = None
        if self.engine.config.ai_rewrite and self.rewriter is not None:
            try:
                # Validated here as well: no rewriter is trusted to keep the rules.
                context = rewrite_context(suggestion, language=language, channel_name=getattr(voice, "name", None), template=template)
                rewritten = validate_rewrite(await self._call_rewriter(template, suggestion, context), template, language)
            except Exception:
                rewritten = None
        content = render_message(suggestion, getattr(voice, "name", None), rewritten, language=language)
        # Exactly the users the engine decided on: players, plus the rest of the
        # target voice channel for a whole-voice Group Up.
        message = await self.notifier.send(channel, content, suggestion.mentioned_user_ids)
        self.engine.mark_sent(suggestion)
        self._announce(channel, suggestion, voice, message, rewritten is not None)

    def _announce(self, channel: Any, suggestion: gp.Suggestion, voice: Any, message: Any, ai_worded: bool) -> None:
        """Tell the other DarkAbyss bots (Kairo's Social Awareness) what was just posted."""
        if self.events is None:
            return
        try:
            self.events.publish(
                EVENT_GROUP_UP,
                suggestion.guild_id,
                channel_id=getattr(channel, "id", None),
                message_id=getattr(message, "id", None),
                data={
                    "kind": suggestion.kind,
                    "game": suggestion.game_display_name,
                    "invited_user_ids": [str(user) for user in (suggestion.outsider_user_ids if suggestion.kind == "join" else suggestion.target_user_ids)],
                    "voice_player_ids": [str(user) for user in suggestion.voice_member_ids],
                    "voice_crew_ids": [str(user) for user in suggestion.voice_crew_ids],
                    "voice_channel_id": None if suggestion.voice_channel_id is None else str(suggestion.voice_channel_id),
                    "voice_channel_name": getattr(voice, "name", None),
                    "ai_worded": ai_worded,
                },
            )
        except Exception:
            pass

    async def _call_rewriter(self, template: str, suggestion: gp.Suggestion, context: dict[str, Any]) -> str | None:
        if self.rewriter is None:
            return None
        try:
            return await self.rewriter(template, suggestion, context)
        except TypeError:
            return await self.rewriter(template, suggestion)

    # -- status for the Manager -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        config = self.engine.config
        guild, channel = self._channel() if config.active else (None, None)
        problem = self.problem
        try:
            engine_status = self.engine.status()
        except Exception as exc:
            # The persisted state (cooldowns/history) could not be read: still
            # report, so the Manager shows why nothing is posted.
            engine_status = {"tracked_players": 0, "top_games": [], "pending_groups": 0, "last_suggestion": None}
            problem = problem or f"Game Presence state is unavailable ({type(exc).__name__}); posting is paused."
        snapshot = self.presence_snapshot()
        return {
            "enabled": config.enabled,
            "presence_intent": self.presence_intent,
            "guild_id": str(config.guild_id) if config.guild_id else None,
            "guild_name": getattr(guild, "name", None),
            "channel_id": str(config.channel_id) if config.channel_id else None,
            "channel_name": getattr(channel, "name", None),
            "problem": problem,
            **engine_status,
            # Runtime-level reason (config, channel, permissions) wins over the engine's.
            "diagnosis": self.diagnosis.public_dict(),
            # What Discord actually delivers: members in the cache and how many
            # of them show a Playing activity (0 visible = members/presence data missing).
            "visible_members": snapshot["visible"],
            "online_members": snapshot["online"],
            "playing_members": snapshot["playing"],
            "other_activities": snapshot["other_activities"],
        }

    def presence_counts(self) -> tuple[int, int]:
        snapshot = self.presence_snapshot()
        return snapshot["visible"], snapshot["playing"]

    def presence_snapshot(self) -> dict[str, Any]:
        """What Discord delivers for the configured server (every server when
        none is configured yet), counts only: non-bot members in the cache, how
        many are online, how many show a Playing activity, and which other
        activity types are present (custom status, listening, streaming...)."""
        getter = getattr(self.client, "get_guild", None)
        config = self.engine.config
        if config.guild_id and callable(getter):
            guild = getter(config.guild_id)
            guilds = [guild] if guild is not None else []
        else:
            guilds = list(getattr(self.client, "guilds", None) or [])
        visible = online = playing = 0
        other: dict[str, int] = {}
        for guild in guilds:
            for member in getattr(guild, "members", None) or []:
                if getattr(member, "bot", False):
                    continue
                visible += 1
                status = getattr(member, "status", None)
                # Anyone with an activity is online even if no status was reported.
                if (status is not None and str(status) != "offline") or getattr(member, "activities", None):
                    online += 1
                if member_game(member) is not None:
                    playing += 1
                for activity in getattr(member, "activities", None) or ():
                    kind = getattr(activity, "type", None)
                    if kind is None or kind == discord.ActivityType.playing:
                        continue
                    name = str(getattr(kind, "name", kind))
                    other[name] = other.get(name, 0) + 1
        return {"visible": visible, "online": online, "playing": playing, "other_activities": other}

    def write_status(self) -> None:
        if self.runtime_dir is None:
            return
        try:
            admin_terminal.write_runtime_json(self.runtime_dir, STATUS_FILE_NAME, self.status())
        except Exception:
            pass
