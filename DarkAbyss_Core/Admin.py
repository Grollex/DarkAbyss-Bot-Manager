import argparse
import msvcrt
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import admin_instance
import admin_terminal
import admin_tools
import admin_features  # after admin_tools (it loads the tool extensions)
import app_paths
import bot_events
import bot_i18n
import bot_registry
import config_store
import content_filter
import message_policy
import instance_store
import runtime_layout
import social_awareness
import social_memory
from bot_i18n import t

ADMIN_DEFAULT_LANGUAGE = "en"

try:
    # /ai transport. It imports no AI/provider module at import time; even if
    # it cannot be imported, /execute and the rest of the bot keep working.
    import admin_ai
except Exception as _admin_ai_exc:  # pragma: no cover - defensive isolation
    print(f"AI transport unavailable: {type(_admin_ai_exc).__name__}")
    admin_ai = None

bot_lock_handle = None


@dataclass(frozen=True)
class AdminRuntime:
    instance_id: str
    config_path: Path
    token_path: Path
    lock_path: Path
    # Instance data folder (bot feature store). None keeps features disabled.
    data_dir: Path | None = None


runtime_context: AdminRuntime | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Discord Admin Bot instance.")
    parser.add_argument("--instance", default=admin_instance.DEFAULT_ADMIN_INSTANCE_ID)
    return parser.parse_args(argv)


def resolve_runtime(instance_id: str = admin_instance.DEFAULT_ADMIN_INSTANCE_ID) -> AdminRuntime:
    try:
        if instance_id == admin_instance.DEFAULT_ADMIN_INSTANCE_ID:
            instance = admin_instance.ensure_admin_instance(instance_id)
        else:
            instance = instance_store.load_instance(instance_id)
    except (
        admin_instance.AdminInstanceError,
        bot_registry.BotRegistryError,
        instance_store.InstanceStoreError,
        OSError,
    ) as exc:
        raise RuntimeError(f"Invalid admin instance {instance_id!r}: {exc}") from exc

    if instance.bot_type != admin_instance.ADMIN_BOT_TYPE_ID:
        raise RuntimeError(
            f"Invalid admin instance {instance_id!r}: "
            f"bot_type {instance.bot_type!r}; expected 'admin'."
        )

    return AdminRuntime(
        instance_id=instance.id,
        config_path=instance.paths.config,
        token_path=instance.paths.token,
        lock_path=instance.paths.runtime_dir / "admin_bot.lock",
        data_dir=instance.paths.data_dir,
    )


def set_runtime(runtime: AdminRuntime) -> None:
    global runtime_context
    runtime_context = runtime


def get_runtime() -> AdminRuntime:
    if runtime_context is None:
        raise RuntimeError("Admin runtime is not initialized.")
    return runtime_context


def load_token(runtime: AdminRuntime | None = None) -> str:
    selected_runtime = runtime or get_runtime()
    if not selected_runtime.token_path.exists():
        raise RuntimeError(f"Token file not found: {selected_runtime.token_path}")
    token = selected_runtime.token_path.read_text(encoding="utf-8").strip()
    if not token or token == app_paths.TOKEN_PLACEHOLDER:
        raise RuntimeError(f"Put the Discord bot token into {selected_runtime.token_path}")
    return token


def parse_snowflake(value: object, field_name: str) -> int:
    return admin_tools.parse_snowflake(value, field_name)


def parse_snowflake_list(value: object, field_name: str) -> list[int]:
    return admin_tools.parse_snowflake_list(value, field_name)


def validate_config(config: dict) -> dict:
    if not isinstance(config.get("allow_server_administrators"), bool):
        raise ValueError(t("\"allow_server_administrators\" must be a boolean."))

    config["allowed_user_ids"] = parse_snowflake_list(
        config.get("allowed_user_ids"),
        "allowed_user_ids",
    )
    config["allowed_role_ids"] = parse_snowflake_list(
        config.get("allowed_role_ids"),
        "allowed_role_ids",
    )

    audit_channel_id = config.get("audit_channel_id")
    if audit_channel_id is not None:
        config["audit_channel_id"] = parse_snowflake(audit_channel_id, "audit_channel_id")

    # Explicit /ai whitelist, separate from the /execute fields above. Missing
    # fields (older effective configs) mean an empty whitelist: nobody may use /ai.
    config["ai_allowed_user_ids"] = parse_snowflake_list(
        config.get("ai_allowed_user_ids", []),
        "ai_allowed_user_ids",
    )
    config["ai_allowed_role_ids"] = parse_snowflake_list(
        config.get("ai_allowed_role_ids", []),
        "ai_allowed_role_ids",
    )

    # AI-5 natural control channel. Missing/null = natural-message AI disabled
    # (and Message Content Intent is not requested).
    ai_control_channel_id = config.get("ai_control_channel_id")
    config["ai_control_channel_id"] = (
        None if ai_control_channel_id is None else parse_snowflake(ai_control_channel_id, "ai_control_channel_id")
    )

    # AI-6.2: "plan" = approve the AI's plan once (destructive actions are still
    # confirmed one by one with exact arguments); "strict" = approve every change.
    confirmation_mode = config.get("ai_confirmation_mode", "plan")
    if confirmation_mode not in ("plan", "strict"):
        raise ValueError(t("\"ai_confirmation_mode\" must be \"plan\" or \"strict\"."))
    config["ai_confirmation_mode"] = confirmation_mode

    # Lets AI tools read message text (purge filters, summaries) without the
    # control channel. Requires the privileged Message Content Intent.
    read_content = config.get("ai_read_message_content", False)
    if not isinstance(read_content, bool):
        raise ValueError(t("\"ai_read_message_content\" must be a boolean."))
    config["ai_read_message_content"] = read_content

    # @Kairo mentions (bot user or its role) start AI requests; an empty
    # channel list means every channel the bot can read.
    mention_enabled = config.get("ai_mention_enabled", False)
    if not isinstance(mention_enabled, bool):
        raise ValueError(t("\"ai_mention_enabled\" must be a boolean."))
    config["ai_mention_enabled"] = mention_enabled
    config["ai_mention_channel_ids"] = parse_snowflake_list(
        config.get("ai_mention_channel_ids", []),
        "ai_mention_channel_ids",
    )

    # Language of everything this bot writes in Discord (older configs: English).
    try:
        config["language"] = bot_i18n.normalize_language(config.get("language"), ADMIN_DEFAULT_LANGUAGE)
    except bot_i18n.LanguageError as exc:
        raise ValueError(t("\"language\" {exc}", exc=exc)) from exc

    # Social Awareness (optional): null = never chosen (off; the Manager asks once).
    social_awareness.validate_config_fields(config, parse_snowflake_list)
    # Content filter: on by default, but it only checks members someone named.
    content_filter.validate_config_fields(config)
    # Admin-defined custom message rules (generic engine; the rules live in the local config).
    message_policy.validate_config_fields(config)

    return config


def load_config(runtime: AdminRuntime | None = None) -> dict:
    selected_runtime = runtime or get_runtime()

    try:
        loaded = config_store.load_effective_config(selected_runtime.instance_id)
    except config_store.ConfigStoreError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc

    try:
        config = validate_config(loaded)
    except ValueError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc
    bot_i18n.set_bot_language(config["language"])
    return config


def acquire_single_instance_lock(runtime: AdminRuntime | None = None) -> bool:
    global bot_lock_handle
    selected_runtime = runtime or get_runtime()
    bot_lock_handle = selected_runtime.lock_path.open("a+b")
    try:
        # "a+b" opens at end of file; msvcrt.locking locks from the CURRENT
        # position, so always lock byte 0 or two processes lock different bytes.
        bot_lock_handle.seek(0)
        msvcrt.locking(bot_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        bot_lock_handle.close()
        bot_lock_handle = None
        print("Another Admin Bot instance is already running. Exiting.")
        return False
    bot_lock_handle.seek(0)
    bot_lock_handle.truncate()
    bot_lock_handle.write(str(Path.cwd()).encode("utf-8", errors="ignore"))
    bot_lock_handle.flush()
    return True


def clip_discord_message(text: str, limit: int = 1900) -> str:
    return text if len(text) <= limit else text[:limit] + "\n..."


def actor_has_access(interaction: discord.Interaction, config: dict) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return admin_tools.actor_has_access(interaction.user, config)


def _snowflake_argument(value: object | None) -> str | None:
    if value is None:
        return None
    object_id = getattr(value, "id", None)
    return str(object_id) if object_id is not None else None


def build_execute_tool_arguments(
    *,
    action_name: str,
    member: Optional[discord.Member],
    user_id: Optional[str],
    role: Optional[discord.Role],
    channel: Optional[discord.TextChannel],
    voice_channel: Optional[discord.VoiceChannel],
    name: Optional[str],
    reason: str,
    count: Optional[int],
    duration_minutes: Optional[int],
    content: Optional[str],
) -> dict[str, object]:
    arguments: dict[str, object] = {"reason": reason}

    if action_name in {"send_message", "purge_messages", "lock_channel", "unlock_channel"}:
        selected_channel = voice_channel if action_name in {"lock_channel", "unlock_channel"} and voice_channel else channel
        channel_id = _snowflake_argument(selected_channel)
        if channel_id is not None:
            arguments["channel_id"] = channel_id

    if action_name in {"rename_channel", "delete_channel"}:
        if channel is not None and voice_channel is not None:
            raise ValueError(t("{action_name} requires only one of channel or voice_channel.", action_name=action_name))
        selected_channel = channel or voice_channel
        channel_id = _snowflake_argument(selected_channel)
        if channel_id is not None:
            arguments["channel_id"] = channel_id

    if action_name in {"timeout_member", "clear_timeout", "kick_member", "ban_member", "add_role", "remove_role"}:
        member_id = _snowflake_argument(member)
        if member_id is not None:
            arguments["member_id"] = member_id

    if action_name in {"add_role", "remove_role", "delete_role"}:
        role_id = _snowflake_argument(role)
        if role_id is not None:
            arguments["role_id"] = role_id

    if action_name == "unban_user" and user_id is not None:
        arguments["user_id"] = user_id

    if action_name in {"create_text_channel", "create_voice_channel", "rename_channel", "create_role"} and name is not None:
        arguments["name"] = name

    if action_name == "send_message" and content is not None:
        arguments["content"] = content

    if action_name == "purge_messages" and count is not None:
        arguments["count"] = count

    if action_name == "timeout_member" and duration_minutes is not None:
        arguments["duration_minutes"] = duration_minutes

    return arguments


async def send_audit(
    interaction: discord.Interaction,
    config: dict,
    action: str,
    result: str,
) -> Optional[str]:
    channel_id = config.get("audit_channel_id")
    if not channel_id or not interaction.guild:
        return None

    channel = interaction.guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        failure = f"Audit channel {channel_id} is not an accessible text channel."
        print(f"Audit logging failed: {failure}")
        return failure

    try:
        embed = discord.Embed(
            title=t("Admin action"),
            color=0x2F80ED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name=t("Action"), value=action, inline=False)
        embed.add_field(name=t("By"), value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name=t("Result"), value=clip_discord_message(result, 1024), inline=False)
        await channel.send(embed=embed)
    except Exception as exc:
        failure = f"Audit logging failed: {type(exc).__name__}: {exc}"
        print(failure)
        return failure

    return None


def natural_ai_enabled(config: dict) -> bool:
    return config.get("ai_control_channel_id") is not None


def configure_message_content_intent(client: discord.Client, enabled: bool) -> None:
    """Set the Message Content Intent BEFORE the gateway connection.

    discord.py exposes ``Client.intents`` only as a copy, so the IDENTIFY
    intents held by the connection state are updated directly. Natural AI off
    keeps the pre-AI-5 behaviour (intent not requested), so /execute and /ai
    never depend on the privileged Message Content Intent.
    """
    client._connection._intents.message_content = bool(enabled)


LOGIN_FAILURE_HELP = (
    "LoginFailure: Discord rejected the bot token (401 Unauthorized). The token was reset or mistyped. "
    "Developer Portal -> Application -> Bot -> Reset Token, then paste the new token in Manager -> "
    "Bot Setup -> Discord bot token and start the bot again."
)


def message_content_requested(config: dict) -> bool:
    """Message Content Intent is needed by the control channel, AI message reading,
    @mention requests and Social Awareness."""
    # Role pings (@Kairo role) arrive without text unless the intent is on.
    return (
        natural_ai_enabled(config)
        or config.get("ai_read_message_content") is True
        or config.get("ai_mention_enabled") is True
        or config.get(social_awareness.CONFIG_ENABLED) is True
    )


PRIVILEGED_INTENTS_HELP = (
    "Discord refused a privileged gateway intent. With the AI control channel, @mention requests, "
    "'AI can read message text', Social Awareness or a content filter with named members this bot requires BOTH privileged intents: "
    "'Server Members Intent' (always required by the Admin bot) and 'Message Content Intent' (required "
    "only for the AI control channel, @mention requests, AI message reading and Social Awareness). Enable "
    "the missing one(s) in Discord Developer Portal -> Application -> Bot -> Privileged Gateway Intents. "
    "To run without Message Content Intent, clear 'AI control channel ID' and turn off '@mention requests' "
    "and 'AI can read message text' in Manager Setup Bot and Social Awareness on the Kairo page (Server "
    "Members Intent is still required). /execute and /ai do not need Message Content Intent."
)


class BotLanguageTranslator(app_commands.Translator):
    """Slash command and parameter descriptions in THIS bot's language for every
    Discord locale (names and choice values stay as they are). Applied when the
    commands are synced at start, so a language change needs a restart here."""

    DESCRIPTIONS = frozenset(
        {
            app_commands.TranslationContextLocation.command_description,
            app_commands.TranslationContextLocation.group_description,
            app_commands.TranslationContextLocation.parameter_description,
        }
    )

    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, context: app_commands.TranslationContext) -> str | None:
        if context.location not in self.DESCRIPTIONS or bot_i18n.bot_language() == "en":
            return None
        text = bot_i18n.t(string.message)
        return text[:100] if text != string.message else None


class AdminBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.tree.set_translator(BotLanguageTranslator())
        await self.tree.sync()
        if feature_store is not None and not scheduled_messages_loop.is_running():
            scheduled_messages_loop.start()
        if terminal is not None and not terminal_loop.is_running():
            terminal_loop.start()
        if not config_refresh_loop.is_running():
            config_refresh_loop.start()
        if social is not None and not social_loop.is_running():
            social_loop.start()

    async def on_ready(self) -> None:
        print(f"Discord-only Admin Bot is online as {self.user}")
        refresh_terminal_status()
        if social is not None:
            names = [getattr(self.user, "name", ""), getattr(self.user, "global_name", None) or ""]
            names += [getattr(getattr(guild, "me", None), "display_name", "") for guild in self.guilds]
            social.set_identity(getattr(self.user, "id", None), names)
        send_heartbeat(force=True)

    async def close(self) -> None:
        if events_publisher is not None:
            events_publisher.stopped()  # the other DarkAbyss bots see "not running" at once
        await super().close()


bot = AdminBot()

# AI-6 persistent bot features (role menus, verification, welcome, schedules).
# Set in main() from the instance data folder; None disables them.
feature_store: admin_features.FeatureStore | None = None

# Kairo Social Awareness (social_awareness.py). Set in main() when the AI
# transport exists; it stays silent while switched off in the config.
social: social_awareness.SocialAwareness | None = None

# Content filter (content_filter.py): who is filtered, and the runtime that
# judges their messages. Set in main(); None when the bot has no data folder.
filter_store: content_filter.FilterStore | None = None
content_filter_runtime: content_filter.ContentFilter | None = None
message_policy_runtime: message_policy.MessagePolicyEngine | None = None

# This bot on the DarkAbyss bot event bus (heartbeat: "Kairo is running"). Set in main().
events_publisher: bot_events.EventPublisher | None = None
instance_display_name = ""


def send_heartbeat(force: bool = False) -> None:
    if events_publisher is None:
        return
    user = getattr(bot, "user", None)
    events_publisher.heartbeat(
        display_name=instance_display_name,
        discord_user_id=getattr(user, "id", None),
        discord_name=getattr(user, "display_name", None) or getattr(user, "name", "") or "",
        guild_ids=[guild.id for guild in getattr(bot, "guilds", [])],
        force=force,
    )

# Manager request terminal (file mailbox in the instance runtime folder).
# Set in main(); None disables it.
terminal: admin_terminal.BotTerminal | None = None
TERMINAL_STATUS_EVERY_TICKS = 60  # 0.5 s ticks -> refresh server list every 30 s
_terminal_ticks = 0


def refresh_terminal_status() -> None:
    if terminal is None:
        return
    try:
        terminal.refresh_status()
    except Exception as exc:  # pragma: no cover - status is best effort
        print(f"Terminal status error: {type(exc).__name__}")


@tasks.loop(seconds=0.5)
async def terminal_loop() -> None:
    global _terminal_ticks
    if terminal is None:
        return
    _terminal_ticks += 1
    if _terminal_ticks % TERMINAL_STATUS_EVERY_TICKS == 0:
        refresh_terminal_status()
        admin_terminal.purge_old_events(terminal.runtime_dir)
    try:
        await terminal.poll()
    except Exception as exc:  # pragma: no cover - never stop the mailbox
        print(f"Terminal error: {type(exc).__name__}")


@terminal_loop.before_loop
async def _terminal_wait_until_ready() -> None:
    await bot.wait_until_ready()


@bot.listen("on_interaction")
async def feature_component_listener(interaction: discord.Interaction) -> None:
    if interaction.type is not discord.InteractionType.component:
        return
    try:
        await admin_features.handle_component_interaction(interaction, feature_store)
    except Exception as exc:  # pragma: no cover - never let a button break the event loop
        print(f"Role menu error: {type(exc).__name__}")


@bot.listen("on_member_join")
async def feature_member_join_listener(member: discord.Member) -> None:
    try:
        await admin_features.handle_member_join(member, feature_store)
    except Exception as exc:  # pragma: no cover
        print(f"Welcome feature error: {type(exc).__name__}")


CONFIG_REFRESH_SECONDS = 15


def refresh_live_config() -> dict | None:
    """Live settings without a command: the bot language (role menu, welcome and
    schedule texts too) and Social Awareness pick up a saved change within 15 s."""
    try:
        config = load_config()
    except RuntimeError:
        config = None  # invalid config: keep the language, Social Awareness fails closed
    if social is not None:
        social.apply_config(config)
    if content_filter_runtime is not None:
        content_filter_runtime.apply_config(config)
    if message_policy_runtime is not None:
        message_policy_runtime.apply_config(config)
    return config


@tasks.loop(seconds=CONFIG_REFRESH_SECONDS)
async def config_refresh_loop() -> None:
    refresh_live_config()
    send_heartbeat()  # at most once a minute (bot_events.HEARTBEAT_SECONDS)


@config_refresh_loop.before_loop
async def _config_refresh_wait() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=5)
async def social_loop() -> None:
    if social is None:
        return
    try:
        await social.tick()
    except Exception as exc:  # pragma: no cover - never stop the loop
        print(f"Social Awareness error: {type(exc).__name__}")


@social_loop.before_loop
async def _social_wait() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def scheduled_messages_loop() -> None:
    try:
        await admin_features.run_due_schedules(bot, feature_store)
    except Exception as exc:  # pragma: no cover
        print(f"Scheduled messages error: {type(exc).__name__}")


@scheduled_messages_loop.before_loop
async def _wait_until_ready() -> None:
    await bot.wait_until_ready()


ACTION_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in admin_tools.EXECUTE_TOOL_NAMES
]


@bot.tree.command(name="execute", description="Execute a whitelisted Discord admin action")
@app_commands.choices(action=ACTION_CHOICES)
@app_commands.describe(
    action="Discord-only action to execute",
    member="Member target for moderation or role actions",
    user_id="Numeric Discord user ID for unban_user",
    role="Role target for role actions",
    channel="Text channel target for message, text-channel, lock, and unlock actions",
    voice_channel="Voice channel target for rename_channel or delete_channel",
    name="Name for create/rename actions",
    reason="Audit reason",
    count="Message count for purge_messages, 1-100",
    duration_minutes="Timeout duration in minutes",
    content="Text for send_message",
)
async def execute(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    member: Optional[discord.Member] = None,
    user_id: Optional[str] = None,
    role: Optional[discord.Role] = None,
    channel: Optional[discord.TextChannel] = None,
    voice_channel: Optional[discord.VoiceChannel] = None,
    name: Optional[str] = None,
    reason: Optional[str] = None,
    count: Optional[app_commands.Range[int, 1, 100]] = None,
    duration_minutes: Optional[app_commands.Range[int, 1, 40320]] = None,
    content: Optional[str] = None,
) -> None:
    try:
        config = load_config()
    except RuntimeError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if not actor_has_access(interaction, config):
        await interaction.response.send_message(t("Access denied."), ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message(t("This command works only inside a Discord server."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    action_name = action.value
    reason_text = reason or t("Requested by {user} via /execute", user=interaction.user)
    context = admin_tools.AdminToolContext(
        guild=interaction.guild,
        fetch_user=bot.fetch_user,
        source="/execute",
        requesting_user_id=interaction.user.id,
        requesting_user_name=str(interaction.user),
        # Same anti-escalation as /ai: whitelisted non-owners cannot act on
        # roles/members at or above their own highest role.
        enforce_hierarchy=True,
    )
    try:
        arguments = build_execute_tool_arguments(
            action_name=action_name,
            member=member,
            user_id=user_id,
            role=role,
            channel=channel,
            voice_channel=voice_channel,
            name=name,
            reason=reason_text,
            count=count,
            duration_minutes=duration_minutes,
            content=content,
        )
    except ValueError as exc:
        tool_result = admin_tools.ToolResult(False, action_name, str(exc))
    else:
        tool_result = await admin_tools.execute_tool(context, action_name, arguments)
    result = tool_result.message

    audit_failure = await send_audit(interaction, config, action_name, result)
    if tool_result.ok and audit_failure:
        result = f"{result}\n\n" + t("Action completed, but audit logging failed.")

    await interaction.followup.send(clip_discord_message(result), ephemeral=True)


AI_MODE_CHOICES = [
    app_commands.Choice(name="routine", value="routine"),
    app_commands.Choice(name="planner", value="planner"),
    app_commands.Choice(name="creative", value="creative"),
]

ai_transport = (
    admin_ai.AITransport(
        load_config=lambda: load_config(),
        fetch_user=lambda user_id: bot.fetch_user(user_id),
        audit=send_audit,
        planning=True,
    )
    if admin_ai is not None
    else None
)


@bot.tree.command(name="ai", description="Ask the AI assistant (explicit AI whitelist only)")
@app_commands.guild_only()
@app_commands.choices(mode=AI_MODE_CHOICES)
@app_commands.describe(
    prompt="What you want the AI to do (max 2000 characters)",
    mode="Executor mode: routine (default), planner, or creative",
    file="Optional image for tools such as server icon, banner, emoji or sticker",
    file2="Optional second image",
)
async def ai(
    interaction: discord.Interaction,
    prompt: app_commands.Range[str, 1, 2000],
    mode: Optional[app_commands.Choice[str]] = None,
    file: Optional[discord.Attachment] = None,
    file2: Optional[discord.Attachment] = None,
) -> None:
    if ai_transport is None:
        await interaction.response.send_message(
            t("AI is currently unavailable."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    files = [item for item in (file, file2) if item is not None]
    await ai_transport.handle_ai_command(interaction, prompt, mode.value if mode else None, files)


@bot.tree.command(name="ai_reset", description="Forget your recent AI conversation in this channel")
@app_commands.guild_only()
async def ai_reset(interaction: discord.Interaction) -> None:
    if ai_transport is None:
        await interaction.response.send_message(
            t("AI is currently unavailable."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    await ai_transport.handle_reset_command(interaction)


@bot.listen("on_message")
async def content_filter_listener(message: discord.Message) -> None:
    # Only members named with /filter add (or in the Manager) are judged.
    if content_filter_runtime is None:
        return
    try:
        await content_filter_runtime.observe(message)
    except Exception as exc:  # pragma: no cover - never let the filter break the event loop
        print(f"Content filter error: {type(exc).__name__}")


@bot.listen("on_message")
async def message_policy_listener(message: discord.Message) -> None:
    # Admin-defined custom rules (off and empty by default).
    if message_policy_runtime is None:
        return
    try:
        await message_policy_runtime.observe(message)
    except Exception as exc:  # pragma: no cover
        print(f"Message policy error: {type(exc).__name__}")


@bot.listen("on_message_edit")
async def content_filter_edit_listener(before: discord.Message, after: discord.Message) -> None:
    # An insult added by editing counts like a new message.
    if content_filter_runtime is None or getattr(before, "content", None) == getattr(after, "content", None):
        return
    try:
        await content_filter_runtime.observe(after)
    except Exception as exc:  # pragma: no cover
        print(f"Content filter error: {type(exc).__name__}")
    if message_policy_runtime is not None and getattr(before, "content", None) != getattr(after, "content", None):
        try:
            await message_policy_runtime.observe(after)
        except Exception as exc:  # pragma: no cover
            print(f"Message policy error: {type(exc).__name__}")


async def filter_timeout(member: Any, minutes: int, reason: str) -> None:
    await member.timeout(timedelta(minutes=minutes), reason=reason)


async def filter_reply(message: Any, text: str, member: Any) -> Any:
    """Reply to the offending message; only that member is pinged."""
    mentions = discord.AllowedMentions(everyone=False, roles=False, replied_user=False, users=[member])
    try:
        return await message.reply(text, mention_author=False, allowed_mentions=mentions)
    except discord.HTTPException:
        return await message.channel.send(text, allowed_mentions=mentions)


async def policy_delete(message: Any) -> bool:
    """Delete the original message so the reply shows over a 'deleted message'."""
    await message.delete()
    return True


async def filter_audit(guild: Any, text: str) -> None:
    channel_id = content_filter_runtime.settings.audit_channel_id if content_filter_runtime is not None else None
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title=t("Content filter"), description=text[:4000], color=0xE67E22, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


filter_commands = app_commands.Group(name="filter", description="Content filter: mute named members for insults and hostility", guild_only=True)


async def _filter_access(interaction: discord.Interaction) -> dict | None:
    """The config when the user may manage the filter here; otherwise answers and returns None."""
    try:
        config = load_config()
    except RuntimeError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return None
    if not actor_has_access(interaction, config):
        await interaction.response.send_message(t("Access denied."), ephemeral=True)
        return None
    if filter_store is None:
        await interaction.response.send_message(t("The content filter is not available on this bot."), ephemeral=True)
        return None
    return config


def filter_notes(config: dict) -> str:
    if config.get(content_filter.CONFIG_ENABLED) is False:
        return "\n" + t("The content filter is switched off on the Manager's Content Filter page: nothing is checked until it is on.")
    if content_filter_runtime is not None and not content_filter_runtime.message_content:
        return "\n" + t(
            "Restart the bot once: it needs the Message Content Intent to read messages (enable it in the Discord Developer Portal)."
        )
    return ""


@filter_commands.command(name="add", description="Start filtering a member's messages")
@app_commands.describe(member="Member to filter", note="Optional note: why this member is filtered")
async def filter_add(interaction: discord.Interaction, member: discord.Member, note: Optional[str] = None) -> None:
    config = await _filter_access(interaction)
    if config is None:
        return
    if member.bot:
        await interaction.response.send_message(t("Bots are not filtered."), ephemeral=True)
        return
    context = admin_tools.AdminToolContext(
        guild=interaction.guild,
        source="/filter",
        requesting_user_id=interaction.user.id,
        requesting_user_name=str(interaction.user),
        enforce_hierarchy=True,
    )
    try:
        admin_tools.ensure_member_actionable(context, member, action="filter")
        added = filter_store.watch(interaction.guild.id, member.id, name=member.display_name, added_by=str(interaction.user.id), note=note or "")
    except (admin_tools.AdminToolError, ValueError, content_filter.FilterStoreError) as exc:
        await interaction.response.send_message(str(exc), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    text = (
        t("Now filtering {member}: insults and hostility get a mute of 30 minutes to 3 hours.", member=member.mention)
        if added
        else t("{member} is already filtered.", member=member.mention)
    )
    await interaction.response.send_message(text + filter_notes(config), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    await send_audit(interaction, config, "/filter add", t("Now filtering {member}", member=f"{member} ({member.id})"))


@filter_commands.command(name="remove", description="Stop filtering a member")
@app_commands.describe(member="Member to stop filtering")
async def filter_remove(interaction: discord.Interaction, member: discord.User) -> None:
    config = await _filter_access(interaction)
    if config is None:
        return
    try:
        removed = filter_store.unwatch(interaction.guild.id, member.id)
    except content_filter.FilterStoreError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    text = t("Stopped filtering {member}.", member=member.mention) if removed else t("{member} was not filtered.", member=member.mention)
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if removed:
        await send_audit(interaction, config, "/filter remove", t("Stopped filtering {member}.", member=f"{member} ({member.id})"))


@filter_commands.command(name="list", description="Show the filtered members of this server")
async def filter_list(interaction: discord.Interaction) -> None:
    config = await _filter_access(interaction)
    if config is None:
        return
    try:
        guild = filter_store.guild(interaction.guild.id)
    except content_filter.FilterStoreError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    if not guild.watched:
        text = t("Nobody is filtered on this server.")
    else:
        lines = [t("Filtered members:")]
        for member in guild.watched.values():
            mutes = sum(1 for action in guild.actions if action.user_id == member.user_id and action.result == "muted")
            lines.append(f"- <@{member.user_id}>" + (f" ({member.note})" if member.note else "") + t(" — mutes: {count}", count=mutes))
        text = "\n".join(lines)
    await interaction.response.send_message(clip_discord_message(text + filter_notes(config)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(filter_commands)


@bot.listen("on_raw_reaction_add")
async def social_reaction_listener(payload: discord.RawReactionActionEvent) -> None:
    # How people react to Kairo's own autonomous messages (feedback for Social Awareness).
    if social is None:
        return
    try:
        member = getattr(payload, "member", None)
        social.observe_reaction(payload.guild_id, payload.channel_id, payload.message_id, payload.user_id, str(payload.emoji), bool(getattr(member, "bot", False)))
    except Exception as exc:  # pragma: no cover
        print(f"Social Awareness error: {type(exc).__name__}")


@bot.listen("on_message")
async def ai_control_channel_listener(message: discord.Message) -> None:
    # Cheap gates first: nothing happens unless the control channel or
    # @mention requests are enabled.
    if ai_transport is None:
        return
    if social is not None:
        try:
            social.observe_message(message)  # RAM only; does nothing while switched off
        except Exception as exc:  # pragma: no cover
            print(f"Social Awareness error: {type(exc).__name__}")
    try:
        if ai_transport.control_channel_id is not None:
            await ai_transport.handle_control_message(message)
        if ai_transport.mention_enabled:
            await ai_transport.handle_mention_message(message)
    except Exception as exc:  # pragma: no cover - never let AI break the event loop
        print(f"AI message error: {type(exc).__name__}")


def is_normal_ai_request(message: Any) -> bool:
    """Messages the normal AI flow answers (control channel, @mention requests)."""
    if ai_transport is None:
        return False
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if ai_transport.control_channel_id is not None and channel_id == ai_transport.control_channel_id:
        return True
    return ai_transport.mention_enabled and ai_transport.mention_prompt(message) is not None


async def social_send(guild_id: int, channel_id: int, content: str, reply_to: int | None) -> int | None:
    """Post one Social Awareness message: never pings anyone (no mentions at all)."""
    guild = bot.get_guild(guild_id)
    lookup = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    channel = lookup(channel_id) if callable(lookup) else None
    if guild is None or channel is None or not hasattr(channel, "send"):
        return None
    permissions = channel.permissions_for(guild.me)
    in_thread = isinstance(channel, discord.Thread)
    if not permissions.view_channel or not (permissions.send_messages_in_threads if in_thread else permissions.send_messages):
        return None
    kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
    if reply_to is not None:
        kwargs["reference"] = discord.MessageReference(message_id=reply_to, channel_id=channel_id, guild_id=guild_id, fail_if_not_exists=False)
        kwargs["mention_author"] = False
    message = await channel.send(content, **kwargs)
    return getattr(message, "id", None)


async def social_react(guild_id: int, channel_id: int, message_id: int, emoji: str) -> bool:
    """One Social Awareness reaction (or the 🤐 nod to a quiet wish), only where the bot may react."""
    guild = bot.get_guild(guild_id)
    lookup = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    channel = lookup(channel_id) if callable(lookup) else None
    if guild is None or channel is None or not hasattr(channel, "get_partial_message"):
        return False
    permissions = channel.permissions_for(guild.me)
    if not permissions.view_channel or not permissions.add_reactions or not permissions.read_message_history:
        return False
    await channel.get_partial_message(message_id).add_reaction(emoji)
    return True


_instances_cache: tuple[float, list[tuple[str, str, str]]] = (0.0, [])


def _darkabyss_instances() -> list[tuple[str, str, str]]:
    """(instance id, bot type, Manager name) of every DarkAbyss bot (metadata only, no secrets)."""
    global _instances_cache
    import time as _time

    now = _time.monotonic()
    if now - _instances_cache[0] < 300 and _instances_cache[1]:
        return _instances_cache[1]
    try:
        listed = [(item.id, item.bot_type, item.display_name) for item in instance_store.list_instances()]
    except Exception:
        listed = _instances_cache[1]
    _instances_cache = (now, listed)
    return listed


def _member_name(guild_id: int, user_id: int) -> str | None:
    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild is not None else None
    return getattr(member, "display_name", None)


def _place(guild_id: int, channel_id: int | None) -> tuple[str, str]:
    guild = bot.get_guild(guild_id)
    lookup = getattr(guild, "get_channel_or_thread", None)
    channel = lookup(channel_id) if callable(lookup) and channel_id is not None else None
    return getattr(guild, "name", "") or "", f"#{getattr(channel, 'name', '')}" if channel is not None else ""


def build_social_awareness(runtime: AdminRuntime, message_content: bool) -> social_awareness.SocialAwareness | None:
    if ai_transport is None:
        return None
    try:
        events = bot_events.EventReader(own_instance_id=runtime.instance_id)
    except Exception:
        events = None
    data_dir = getattr(runtime, "data_dir", None)
    return social_awareness.SocialAwareness(
        get_orchestrator=ai_transport.get_orchestrator,
        send=social_send,
        react=social_react,
        events=events,
        # Server Lore, feedback and quiet wishes of THIS Kairo instance.
        memory=social_memory.SocialMemory(Path(data_dir) / social_memory.FILE_NAME) if data_dir is not None else None,
        instances=_darkabyss_instances,
        own_instance_id=runtime.instance_id,
        is_ai_request=is_normal_ai_request,
        resolve_name=_member_name,
        describe_place=_place,
        knows_guild=lambda guild_id: bot.get_guild(guild_id) is not None,
        message_content=message_content,
        runtime_dir=Path(runtime.lock_path).parent,
        write_status=admin_terminal.write_runtime_json,
    )


def resolve_ai_stores(runtime: AdminRuntime) -> Any:
    """This bot's AI: the connections it may use (base set or its own choice) and
    its usage store; None leaves AI unavailable (fail closed)."""
    try:
        import ai_storage

        for message in ai_storage.run_migrations():
            print(message)
        return ai_storage.for_instance_id(runtime.instance_id)
    except Exception as exc:
        print(f"AI storage unavailable for this bot: {type(exc).__name__}")
        return None


def main(argv: list[str] | None = None) -> int:
    runtime_layout.line_buffered_output()
    try:
        args = parse_args(argv)
        runtime = resolve_runtime(args.instance)
        set_runtime(runtime)
        config = load_config(runtime)
        token = load_token(runtime)
    except RuntimeError as exc:
        print(exc)
        return 1

    global feature_store, terminal, social, events_publisher, instance_display_name, filter_store, content_filter_runtime, message_policy_runtime
    feature_store = admin_features.store_for_data_dir(getattr(runtime, "data_dir", None))
    data_dir = getattr(runtime, "data_dir", None)
    filter_store = content_filter.FilterStore(Path(data_dir) / content_filter.FILE_NAME) if data_dir is not None else None
    try:
        events_publisher = bot_events.EventPublisher(runtime.instance_id, admin_instance.ADMIN_BOT_TYPE_ID)
        instance_display_name = instance_store.load_instance(runtime.instance_id).display_name
    except Exception:
        instance_display_name = instance_display_name or ""
    if ai_transport is not None:
        ai_transport.feature_store = feature_store
        ai_transport.content_filter = filter_store
        ai_transport.ai_stores = resolve_ai_stores(runtime)
        lock_path = getattr(runtime, "lock_path", None)
        if lock_path is not None:
            terminal = admin_terminal.BotTerminal(Path(lock_path).parent, ai_transport, bot)

    natural_ai = natural_ai_enabled(config) and ai_transport is not None
    filter_needs_content = content_filter.needs_message_content(config, filter_store)
    policy_needs_content = message_policy.needs_message_content(config) and ai_transport is not None
    read_content = (message_content_requested(config) and ai_transport is not None) or filter_needs_content or policy_needs_content
    configure_message_content_intent(bot, read_content)
    if ai_transport is not None:
        ai_transport.set_control_channel(config.get("ai_control_channel_id") if natural_ai else None)
        ai_transport.message_content_enabled = read_content
        ai_transport.mention_enabled = config.get("ai_mention_enabled") is True
    if natural_ai:
        print("AI control channel enabled; requesting Discord Message Content Intent.")
    elif read_content:
        print("AI message reading enabled; requesting Discord Message Content Intent.")
    if filter_needs_content:
        print("Content filter has named members; requesting Discord Message Content Intent.")
    if policy_needs_content:
        print("Custom message rules are on; requesting Discord Message Content Intent.")
    content_filter_runtime = content_filter.ContentFilter(
        store=filter_store,
        get_orchestrator=(ai_transport.get_orchestrator if ai_transport is not None else (lambda: None)),
        timeout=filter_timeout,
        reply=filter_reply,
        audit=filter_audit,
        message_content=read_content,
        runtime_dir=Path(runtime.lock_path).parent,
        write_status=admin_terminal.write_runtime_json,
        on_mute=lambda guild_id, channel_id, user_id: social.moderated(guild_id, channel_id, user_id) if social is not None else None,
    )
    content_filter_runtime.apply_config(config)
    message_policy_runtime = message_policy.MessagePolicyEngine(
        get_orchestrator=(ai_transport.get_orchestrator if ai_transport is not None else (lambda: None)),
        reply=filter_reply,
        delete=policy_delete,
        audit=filter_audit,
        message_content=read_content,
        runtime_dir=Path(runtime.lock_path).parent,
        write_status=admin_terminal.write_runtime_json,
    )
    message_policy_runtime.apply_config(config)
    social = build_social_awareness(runtime, read_content)
    if social is not None:
        social.apply_config(config)
        if social.settings.enabled:
            print("Social Awareness is on (reads messages in RAM only; usually stays silent).")

    if acquire_single_instance_lock(runtime):
        try:
            bot.run(token)
        except discord.PrivilegedIntentsRequired:
            if not read_content:
                raise  # unchanged pre-AI-5 behaviour (e.g. Server Members Intent missing)
            print(PRIVILEGED_INTENTS_HELP)
            return 1
        except discord.LoginFailure:
            # A packaged (windowed) bot would otherwise show a raw traceback dialog.
            print(LOGIN_FAILURE_HELP, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
