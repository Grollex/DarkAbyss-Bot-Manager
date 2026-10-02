import argparse
import msvcrt
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import admin_instance
import admin_tools
import admin_features  # after admin_tools (it loads the tool extensions)
import app_paths
import bot_registry
import config_store
import instance_store

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
        raise ValueError('"allow_server_administrators" must be a boolean.')

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
        raise ValueError('"ai_confirmation_mode" must be "plan" or "strict".')
    config["ai_confirmation_mode"] = confirmation_mode

    # Lets AI tools read message text (purge filters, summaries) without the
    # control channel. Requires the privileged Message Content Intent.
    read_content = config.get("ai_read_message_content", False)
    if not isinstance(read_content, bool):
        raise ValueError('"ai_read_message_content" must be a boolean.')
    config["ai_read_message_content"] = read_content

    return config


def load_config(runtime: AdminRuntime | None = None) -> dict:
    selected_runtime = runtime or get_runtime()

    try:
        loaded = config_store.load_effective_config(selected_runtime.instance_id)
    except config_store.ConfigStoreError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc

    try:
        return validate_config(loaded)
    except ValueError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc


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
            raise ValueError(f"{action_name} requires only one of channel or voice_channel.")
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
            title="Admin action",
            color=0x2F80ED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Action", value=action, inline=False)
        embed.add_field(name="By", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Result", value=clip_discord_message(result, 1024), inline=False)
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
    """Message Content Intent is needed by the control channel or by AI message reading."""
    return natural_ai_enabled(config) or config.get("ai_read_message_content") is True


PRIVILEGED_INTENTS_HELP = (
    "Discord refused a privileged gateway intent. With the AI control channel or 'AI can read message "
    "text' enabled this bot requires BOTH privileged intents: 'Server Members Intent' (always required "
    "by the Admin bot) and 'Message Content Intent' (required only for the AI control channel and AI "
    "message reading). Enable the missing one(s) in Discord Developer Portal -> Application -> Bot -> "
    "Privileged Gateway Intents. To run without Message Content Intent, clear 'AI control channel ID' "
    "and turn off 'AI can read message text' in Manager Setup Bot (Server Members Intent is still "
    "required). /execute and /ai do not need Message Content Intent."
)


class AdminBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        if feature_store is not None and not scheduled_messages_loop.is_running():
            scheduled_messages_loop.start()

    async def on_ready(self) -> None:
        print(f"Discord-only Admin Bot is online as {self.user}")


bot = AdminBot()

# AI-6 persistent bot features (role menus, verification, welcome, schedules).
# Set in main() from the instance data folder; None disables them.
feature_store: admin_features.FeatureStore | None = None


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
        await interaction.response.send_message("Access denied.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command works only inside a Discord server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    action_name = action.value
    reason_text = reason or f"Requested by {interaction.user} via /execute"
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
        result = f"{result}\n\nAction completed, but audit logging failed."

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
            "AI is currently unavailable.",
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
            "AI is currently unavailable.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    await ai_transport.handle_reset_command(interaction)


@bot.listen("on_message")
async def ai_control_channel_listener(message: discord.Message) -> None:
    # Cheap gate first: nothing happens unless the natural AI channel is enabled.
    if ai_transport is None or ai_transport.control_channel_id is None:
        return
    try:
        await ai_transport.handle_control_message(message)
    except Exception as exc:  # pragma: no cover - never let AI break the event loop
        print(f"AI control channel error: {type(exc).__name__}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        runtime = resolve_runtime(args.instance)
        set_runtime(runtime)
        config = load_config(runtime)
        token = load_token(runtime)
    except RuntimeError as exc:
        print(exc)
        return 1

    global feature_store
    feature_store = admin_features.store_for_data_dir(getattr(runtime, "data_dir", None))
    if ai_transport is not None:
        ai_transport.feature_store = feature_store

    natural_ai = natural_ai_enabled(config) and ai_transport is not None
    read_content = message_content_requested(config) and ai_transport is not None
    configure_message_content_intent(bot, read_content)
    if ai_transport is not None:
        ai_transport.set_control_channel(config.get("ai_control_channel_id") if natural_ai else None)
        ai_transport.message_content_enabled = read_content
    if natural_ai:
        print("AI control channel enabled; requesting Discord Message Content Intent.")
    elif read_content:
        print("AI message reading enabled; requesting Discord Message Content Intent.")

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
