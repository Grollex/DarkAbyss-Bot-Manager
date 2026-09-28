import argparse
import json
import msvcrt
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import admin_instance
import app_paths
import bot_registry
import instance_store

bot_lock_handle = None


@dataclass(frozen=True)
class AdminRuntime:
    instance_id: str
    config_path: Path
    token_path: Path
    lock_path: Path


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
    if isinstance(value, bool):
        raise ValueError(f'"{field_name}" must be a numeric Discord ID.')

    if isinstance(value, int):
        snowflake = value
    elif isinstance(value, str) and value.isdigit():
        snowflake = int(value)
    else:
        raise ValueError(f'"{field_name}" must be a numeric Discord ID.')

    if snowflake <= 0:
        raise ValueError(f'"{field_name}" must be a positive Discord ID.')
    return snowflake


def parse_snowflake_list(value: object, field_name: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f'"{field_name}" must be a JSON array.')
    return [parse_snowflake(item, field_name) for item in value]


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

    return config


def load_config(runtime: AdminRuntime | None = None) -> dict:
    selected_runtime = runtime or get_runtime()

    try:
        loaded = json.loads(selected_runtime.config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Admin config not found or unreadable: {selected_runtime.config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: root value must be an object.")

    try:
        return validate_config(loaded)
    except ValueError as exc:
        raise RuntimeError(f"Invalid admin config {selected_runtime.config_path}: {exc}") from exc


def acquire_single_instance_lock(runtime: AdminRuntime | None = None) -> bool:
    global bot_lock_handle
    selected_runtime = runtime or get_runtime()
    bot_lock_handle = selected_runtime.lock_path.open("a+b")
    try:
        msvcrt.locking(bot_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
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

    if config.get("allow_server_administrators") and interaction.user.guild_permissions.administrator:
        return True

    allowed_users = set(config.get("allowed_user_ids", []))
    if interaction.user.id in allowed_users:
        return True

    allowed_roles = set(config.get("allowed_role_ids", []))
    return any(role.id in allowed_roles for role in interaction.user.roles)


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


class AdminBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Discord-only Admin Bot is online as {self.user}")


bot = AdminBot()


ACTION_CHOICES = [
    app_commands.Choice(name="send_message", value="send_message"),
    app_commands.Choice(name="purge_messages", value="purge_messages"),
    app_commands.Choice(name="timeout_member", value="timeout_member"),
    app_commands.Choice(name="clear_timeout", value="clear_timeout"),
    app_commands.Choice(name="kick_member", value="kick_member"),
    app_commands.Choice(name="ban_member", value="ban_member"),
    app_commands.Choice(name="unban_user", value="unban_user"),
    app_commands.Choice(name="add_role", value="add_role"),
    app_commands.Choice(name="remove_role", value="remove_role"),
    app_commands.Choice(name="create_text_channel", value="create_text_channel"),
    app_commands.Choice(name="create_voice_channel", value="create_voice_channel"),
    app_commands.Choice(name="rename_channel", value="rename_channel"),
    app_commands.Choice(name="delete_channel", value="delete_channel"),
    app_commands.Choice(name="create_role", value="create_role"),
    app_commands.Choice(name="delete_role", value="delete_role"),
    app_commands.Choice(name="lock_channel", value="lock_channel"),
    app_commands.Choice(name="unlock_channel", value="unlock_channel"),
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
    action_succeeded = False

    try:
        result = await run_discord_action(
            interaction=interaction,
            action=action_name,
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
        action_succeeded = True
    except discord.Forbidden:
        result = "Discord refused the action. Check the bot role position and permissions."
    except discord.HTTPException as exc:
        result = f"Discord API error: {exc}"
    except ValueError as exc:
        result = str(exc)
    except Exception as exc:
        result = f"Unexpected error: {type(exc).__name__}: {exc}"

    audit_failure = await send_audit(interaction, config, action_name, result)
    if action_succeeded and audit_failure:
        result = f"{result}\n\nAction completed, but audit logging failed."

    await interaction.followup.send(clip_discord_message(result), ephemeral=True)


async def run_discord_action(
    *,
    interaction: discord.Interaction,
    action: str,
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
) -> str:
    guild = interaction.guild
    if guild is None:
        raise ValueError("Guild is required.")

    if action == "send_message":
        if channel is None or not content:
            raise ValueError("send_message requires channel and content.")
        await channel.send(content)
        return f"Message sent to #{channel.name}."

    if action == "purge_messages":
        if channel is None:
            raise ValueError("purge_messages requires channel.")
        deleted = await channel.purge(limit=count or 10, reason=reason)
        return f"Deleted {len(deleted)} messages from #{channel.name}."

    if action == "timeout_member":
        if member is None:
            raise ValueError("timeout_member requires member.")
        until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes or 10)
        await member.timeout(until, reason=reason)
        return f"Timed out {member} for {duration_minutes or 10} minutes."

    if action == "clear_timeout":
        if member is None:
            raise ValueError("clear_timeout requires member.")
        await member.timeout(None, reason=reason)
        return f"Cleared timeout for {member}."

    if action == "kick_member":
        if member is None:
            raise ValueError("kick_member requires member.")
        await member.kick(reason=reason)
        return f"Kicked {member}."

    if action == "ban_member":
        if member is None:
            raise ValueError("ban_member requires member.")
        await member.ban(reason=reason, delete_message_seconds=0)
        return f"Banned {member}."

    if action == "unban_user":
        target_id = parse_snowflake(user_id, "user_id")
        user = await bot.fetch_user(target_id)
        await guild.unban(user, reason=reason)
        return f"Unbanned {user}."

    if action == "add_role":
        if member is None or role is None:
            raise ValueError("add_role requires member and role.")
        await member.add_roles(role, reason=reason)
        return f"Added role {role.name} to {member}."

    if action == "remove_role":
        if member is None or role is None:
            raise ValueError("remove_role requires member and role.")
        await member.remove_roles(role, reason=reason)
        return f"Removed role {role.name} from {member}."

    if action == "create_text_channel":
        if not name:
            raise ValueError("create_text_channel requires name.")
        created = await guild.create_text_channel(name=name, reason=reason)
        return f"Created text channel #{created.name}."

    if action == "create_voice_channel":
        if not name:
            raise ValueError("create_voice_channel requires name.")
        created = await guild.create_voice_channel(name=name, reason=reason)
        return f"Created voice channel {created.name}."

    if action == "rename_channel":
        target_channel = select_channel(channel, voice_channel, "rename_channel")
        if not name:
            raise ValueError("rename_channel requires name.")
        old_name = target_channel.name
        await target_channel.edit(name=name, reason=reason)
        return f"Renamed #{old_name} to #{name}."

    if action == "delete_channel":
        target_channel = select_channel(channel, voice_channel, "delete_channel")
        channel_name = target_channel.name
        await target_channel.delete(reason=reason)
        return f"Deleted channel #{channel_name}."

    if action == "create_role":
        if not name:
            raise ValueError("create_role requires name.")
        created = await guild.create_role(name=name, reason=reason)
        return f"Created role {created.name}."

    if action == "delete_role":
        if role is None:
            raise ValueError("delete_role requires role.")
        role_name = role.name
        await role.delete(reason=reason)
        return f"Deleted role {role_name}."

    if action == "lock_channel":
        if voice_channel is not None:
            raise ValueError("lock_channel works only with text channel.")
        if channel is None:
            raise ValueError("lock_channel requires channel.")
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
        return f"Locked #{channel.name} for @everyone."

    if action == "unlock_channel":
        if voice_channel is not None:
            raise ValueError("unlock_channel works only with text channel.")
        if channel is None:
            raise ValueError("unlock_channel requires channel.")
        overwrite = channel.overwrites_for(guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
        return f"Unlocked #{channel.name} for @everyone."

    raise ValueError(f"Unsupported action: {action}")


def select_channel(
    text_channel: Optional[discord.TextChannel],
    voice_channel: Optional[discord.VoiceChannel],
    action: str,
) -> discord.TextChannel | discord.VoiceChannel:
    if text_channel and voice_channel:
        raise ValueError(f"{action} requires only one of channel or voice_channel.")
    if text_channel:
        return text_channel
    if voice_channel:
        return voice_channel
    raise ValueError(f"{action} requires channel or voice_channel.")


if __name__ == "__main__":
    try:
        args = parse_args()
        runtime = resolve_runtime(args.instance)
        set_runtime(runtime)
        load_config(runtime)
        token = load_token(runtime)
    except RuntimeError as exc:
        print(exc)
        raise SystemExit(1) from exc

    if acquire_single_instance_lock(runtime):
        bot.run(token)
