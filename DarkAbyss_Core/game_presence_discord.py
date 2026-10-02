"""Discord adapter for Game Presence (events in, public suggestions out).

* member_game / member_voice: read Discord Presence activities and voice state.
* render_message: deterministic templates; mentions are built ONLY here,
  from the approved user IDs of a Suggestion.
* DiscordNotifier: posts one public message with Mute pings / Allow pings
  buttons; allowed_mentions lists exactly the approved users (never
  @everyone, @here or roles), so even generated text cannot ping anyone else.
* handle_preference_interaction: persistent buttons; the actor is always
  interaction.user and only their own opt-state changes; replies are ephemeral.
* make_ai_rewriter: optional wording layer; it may only rephrase a template
  with fixed placeholders, never choose whom or when to ping. Any failure
  falls back to the deterministic template.
* GamePresenceRuntime: wires the engine to bot events and writes a status
  file for the Manager.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

import discord

import admin_terminal
import game_presence as gp

CUSTOM_ID_MUTE = "dab:gp:mute"
CUSTOM_ID_ALLOW = "dab:gp:allow"
STATUS_FILE_NAME = "game_presence_status.json"
AI_REWRITE_TIMEOUT_SECONDS = 15.0
MAX_MESSAGE_CHARS = 1500

PREF_MUTED = "Готово: я больше не буду упоминать тебя в игровых предложениях. Включить обратно — кнопка Allow pings."
PREF_ALREADY_MUTED = "Игровые пинги для тебя уже отключены."
PREF_ALLOWED = "Готово: игровые пинги снова включены."
PREF_ALREADY_ALLOWED = "Игровые пинги для тебя уже включены."
PREF_UNAVAILABLE = "Настройки Game Presence сейчас недоступны. Попробуй позже."

_COUNT_WORDS = {3: "трое", 4: "четверо", 5: "пятеро", 6: "шестеро", 7: "семеро", 8: "восьмеро", 9: "девятеро", 10: "десятеро"}
TEMPLATE_PAIR = "{targets}, вы оба уже несколько минут в {game} и не в одном войсе. Может, соберётесь?"
TEMPLATE_GROUP = "{targets}, вас уже {count} в {game}. Может, пора собраться?"
TEMPLATE_JOIN_ONE = "{targets}, {others} уже играют в {game} и сидят в {channel}. Залетай!"
TEMPLATE_JOIN_MANY = "{targets}, {others} уже играют в {game} и сидят в {channel}. Залетайте!"
_PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\}")
_FORBIDDEN_AI_TEXT = re.compile(r"<[@#&!:a]|@everyone|@here|https?://|discord\.gg", re.IGNORECASE)


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


def join_ru(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def template_for(suggestion: gp.Suggestion) -> str:
    if suggestion.kind == "join":
        return TEMPLATE_JOIN_ONE if len(suggestion.outsider_user_ids) == 1 else TEMPLATE_JOIN_MANY
    return TEMPLATE_PAIR if len(suggestion.target_user_ids) == 2 else TEMPLATE_GROUP


def fill(template: str, suggestion: gp.Suggestion, channel_name: str | None) -> str:
    """Substitute placeholders; mentions come only from the suggestion's IDs."""
    if suggestion.kind == "join":
        targets = join_ru([mention(user) for user in suggestion.outsider_user_ids])
        others = join_ru([mention(user) for user in suggestion.voice_member_ids])
    else:
        targets = " ".join(mention(user) for user in suggestion.target_user_ids)
        others = ""
    count = len(suggestion.target_user_ids)
    values = {
        "targets": targets,
        "others": others,
        "game": f"**{safe_text(suggestion.game_display_name)}**",
        "channel": f"**{safe_text(channel_name or 'войсе')}**",
        "count": _COUNT_WORDS.get(count, f"{count} человек"),
    }
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), template)[:MAX_MESSAGE_CHARS]


def render_message(suggestion: gp.Suggestion, channel_name: str | None, rewritten_template: str | None = None) -> str:
    template = rewritten_template if rewritten_template else template_for(suggestion)
    return fill(template, suggestion, channel_name)


def validate_rewrite(candidate: Any, original_template: str) -> str | None:
    """Accept an AI rewrite only if it keeps exactly the original placeholders.

    The rewrite cannot add or drop people: mentions are inserted afterwards
    from user IDs, and any raw mention/link syntax rejects the rewrite.
    """
    if not isinstance(candidate, str):
        return None
    text = " ".join(candidate.split())
    if not text or len(text) > 300 or _FORBIDDEN_AI_TEXT.search(text):
        return None
    wanted = sorted(_PLACEHOLDER_RE.findall(original_template))
    if sorted(_PLACEHOLDER_RE.findall(text)) != wanted:
        return None
    if "{" in _PLACEHOLDER_RE.sub("", text) or "}" in _PLACEHOLDER_RE.sub("", text):
        return None
    if not text.startswith("{targets}"):
        return None
    return text


Rewriter = Callable[[str, gp.Suggestion], Awaitable[str | None]]


def make_ai_rewriter(get_orchestrator: Callable[[], Any]) -> Rewriter:
    """Optional: rephrase the template with the configured AI (CREATIVE route, no tools)."""

    async def rewrite(template: str, suggestion: gp.Suggestion) -> str | None:
        try:
            import ai_orchestrator
            import ai_platform

            orchestrator = get_orchestrator()
            if orchestrator is None:
                return None
            request = ai_orchestrator.OrchestratorRequest(
                messages=(
                    ai_platform.AIMessage(
                        role="system",
                        content=(
                            "Rephrase one short friendly Discord message in Russian. Keep every placeholder in "
                            "curly braces exactly once and unchanged, start with {targets}, add no names, "
                            "mentions, links or emojis-only text. Reply with the message only."
                        ),
                    ),
                    ai_platform.AIMessage(role="user", content=template),
                ),
                task_class="CREATIVE",
                allowed_tool_names=(),
            )
            result = await asyncio.wait_for(orchestrator.orchestrate(request), AI_REWRITE_TIMEOUT_SECONDS)
        except Exception:
            return None
        if getattr(getattr(result, "status", None), "value", None) != "COMPLETED":
            return None
        return validate_rewrite(getattr(result, "content", None), template)

    return rewrite


# --------------------------------------------------------------------------
# notifier and buttons
# --------------------------------------------------------------------------


def preference_view() -> discord.ui.View:
    """Persistent public buttons; clicks are handled by the bot's on_interaction listener."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Mute pings", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_MUTE))
    view.add_item(discord.ui.Button(label="Allow pings", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_ALLOW))
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

    async def send(self, channel: Any, content: str, target_user_ids: tuple[int, ...]) -> Any:
        view = preference_view()
        message = await channel.send(content, view=view, allowed_mentions=allowed_mentions_for(target_user_ids))
        view.stop()  # clicks go through the global listener (works after restarts)
        return message


async def handle_preference_interaction(interaction: Any, preferences: gp.PreferenceBook | None) -> bool:
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
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
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
    ) -> None:
        self.client = client
        self.engine = engine
        self.notifier = notifier or DiscordNotifier()
        self.rewriter = rewriter
        self.runtime_dir = runtime_dir
        self.presence_intent = presence_intent
        self.problem: str | None = None

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

    async def tick(self) -> int:
        """Publish due suggestions; returns how many were posted."""
        config = self.engine.config
        self.problem = None
        if config.enabled and not self.presence_intent:
            self.problem = "Restart the bot: Presence Intent is requested only at startup."
            self.write_status()
            return 0
        posted = 0
        if config.active:
            guild, channel = self._channel()
            if guild is None:
                self.problem = "The bot is not in the selected server."
            elif channel is None or not hasattr(channel, "send"):
                self.problem = "The selected text channel was not found."
            else:
                for suggestion in self.engine.tick():
                    try:
                        await self._publish(channel, guild, suggestion)
                        posted += 1
                    except discord.Forbidden:
                        self.problem = "Discord refused to post in the channel (check the bot's permissions)."
                    except Exception as exc:
                        self.problem = f"Posting failed: {type(exc).__name__}."
        self.write_status()
        return posted

    async def _publish(self, channel: Any, guild: Any, suggestion: gp.Suggestion) -> None:
        voice = guild.get_channel(suggestion.voice_channel_id) if suggestion.voice_channel_id else None
        template = template_for(suggestion)
        rewritten = None
        if self.engine.config.ai_rewrite and self.rewriter is not None:
            try:
                # Validated here as well: no rewriter is trusted to keep the rules.
                rewritten = validate_rewrite(await self.rewriter(template, suggestion), template)
            except Exception:
                rewritten = None
        content = render_message(suggestion, getattr(voice, "name", None), rewritten)
        await self.notifier.send(channel, content, suggestion.target_user_ids)
        self.engine.mark_sent(suggestion)

    # -- status for the Manager -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        config = self.engine.config
        guild, channel = self._channel() if config.active else (None, None)
        return {
            "enabled": config.enabled,
            "presence_intent": self.presence_intent,
            "guild_id": str(config.guild_id) if config.guild_id else None,
            "guild_name": getattr(guild, "name", None),
            "channel_id": str(config.channel_id) if config.channel_id else None,
            "channel_name": getattr(channel, "name", None),
            "problem": self.problem,
            **self.engine.status(),
        }

    def write_status(self) -> None:
        if self.runtime_dir is None:
            return
        try:
            admin_terminal.write_runtime_json(self.runtime_dir, STATUS_FILE_NAME, self.status())
        except Exception:
            pass
