"""Stream Director Bot: one instance with its own Discord application.

Separate bot type (bots/stream_director) and process. Everything it owns
lives in its instance folder:

    instances/<id>/config.json                 Stream Director settings (top-level keys)
    instances/<id>/secrets/token.txt           Discord token of THIS application
    instances/<id>/secrets/twitch_oauth.json   Twitch tokens (written by the Manager's Connect)
    instances/<id>/data/stream_director_state.json   sessions, community, challenges, polls, inbox
    instances/<id>/runtime/                    lock, bot_status.json, stream_director_status.json
    runtime/bot_events/<id>.json               stream started/ended (read by Kairo's Social Awareness)

Gateway intents: guilds and guild messages (to count activity in the session
thread; no Message Content Intent, nothing privileged). Slash commands and
buttons need no intent. Twitch runs in its own task: no Client ID, no
account or no network only shows up in the status, Discord keeps working and
sessions can be started with /stream start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import msvcrt
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

import admin_terminal
import app_paths
import bot_events
import bot_registry
import config_store
import instance_store
import runtime_layout
import stream_director as sd
import stream_director_config as sdc
import stream_director_discord as sdd
import stream_director_store as sds
import stream_director_twitch as sdt

BOT_TYPE_ID = "stream_director"
LOCK_FILE_NAME = "stream_director_bot.lock"
STATUS_FILE_NAME = "stream_director_status.json"
TICK_SECONDS = 5
CONFIG_EVERY_TICKS = 3
STATUS_EVERY_TICKS = 3
BOT_STATUS_EVERY_TICKS = 12

LOGIN_FAILURE_HELP = (
    "LoginFailure: Discord rejected the Stream Director bot token (401 Unauthorized). Developer Portal -> your "
    "Stream Director application -> Bot -> Reset Token, then paste the new token in Manager -> Bot Setup."
)


@dataclass(frozen=True)
class StreamDirectorRuntimeContext:
    instance_id: str
    token_path: Path
    lock_path: Path
    runtime_dir: Path
    data_dir: Path
    secrets_dir: Path
    display_name: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Stream Director bot instance.")
    parser.add_argument("--instance", required=True)
    return parser.parse_args(argv)


def resolve_runtime(instance_id: str) -> StreamDirectorRuntimeContext:
    try:
        instance = instance_store.load_instance(instance_id)
    except (bot_registry.BotRegistryError, instance_store.InstanceStoreError, OSError) as exc:
        raise RuntimeError(f"Invalid Stream Director instance {instance_id!r}: {exc}") from exc
    if instance.bot_type != BOT_TYPE_ID:
        raise RuntimeError(f"Invalid Stream Director instance {instance_id!r}: bot_type {instance.bot_type!r}; expected {BOT_TYPE_ID!r}.")
    return StreamDirectorRuntimeContext(
        instance_id=instance.id,
        token_path=instance.paths.token,
        lock_path=instance.paths.runtime_dir / LOCK_FILE_NAME,
        runtime_dir=instance.paths.runtime_dir,
        data_dir=instance.paths.data_dir,
        secrets_dir=instance.paths.secrets_dir,
        display_name=instance.display_name,
    )


def load_token(runtime: StreamDirectorRuntimeContext) -> str:
    if not runtime.token_path.is_file():
        raise RuntimeError("Discord token file of this bot is missing. Set the token in Manager -> Bot Setup.")
    token = runtime.token_path.read_text(encoding="utf-8").strip()
    if not token or token == app_paths.TOKEN_PLACEHOLDER:
        raise RuntimeError("This Stream Director bot has no Discord token yet. Set it in Manager -> Bot Setup.")
    return token


def load_settings(instance_id: str) -> tuple[sdc.StreamDirectorConfig, str | None]:
    """(config, problem). An invalid config fails closed: nothing is posted."""
    try:
        data = sdc.normalize_config(config_store.load_effective_config(instance_id))
    except Exception as exc:
        return sdc.StreamDirectorConfig(), f"Invalid Stream Director config: {exc}"
    config = sdc.parse_config(data)
    if data["enabled"] and not sdc.is_configured(data):
        return config, sdc.NOT_CONFIGURED_TEXT
    return config, None


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True  # count messages in the session thread (no content)
    return intents


def diagnose(config: sdc.StreamDirectorConfig, config_problem: str | None, director: sd.Director, channel_problem: str | None) -> dict[str, str]:
    if not director.available:
        return {"code": "state_problem", "text": director.problem or "State file problem."}
    if config_problem and config_problem != sdc.NOT_CONFIGURED_TEXT:
        return {"code": "config_problem", "text": config_problem}
    if not config.enabled:
        return {"code": "paused", "text": "Paused in the Manager: sessions are tracked, nothing is posted."}
    if not config.configured:
        return {"code": "not_configured", "text": sdc.NOT_CONFIGURED_TEXT}
    if channel_problem:
        code = "permission_denied" if "missing permissions" in channel_problem else "channel_unavailable"
        return {"code": code, "text": channel_problem}
    session = director.session
    if session is not None:
        return {"code": session["status"], "text": "Stream session running." if session["status"] == "live" else "Stream offline, waiting a few minutes before the recap."}
    return {"code": "waiting", "text": "Waiting for the next stream."}


def _event_publisher(instance_id: str) -> bot_events.EventPublisher | None:
    try:
        return bot_events.EventPublisher(instance_id, BOT_TYPE_ID)
    except Exception:
        return None


class StreamDirectorBot(discord.Client):
    def __init__(self, runtime: StreamDirectorRuntimeContext) -> None:
        super().__init__(intents=build_intents())
        self.context = runtime
        config, self.config_problem = load_settings(runtime.instance_id)
        self.director = sd.Director(sds.StateStore(runtime.data_dir), config)
        self.tokens = sdt.TokenStore(runtime.secrets_dir)
        self.twitch = sdt.TwitchSupervisor(self.tokens, lambda: self.director.config.twitch_client_id, self.on_twitch_event, self.on_twitch_reconcile)
        self.front = sdd.StreamDirectorDiscord(
            self,
            self.director,
            twitch_login=lambda: (self.twitch.status.account or {}).get("login"),
            vod_lookup=self.twitch.vod_for,
            events=_event_publisher(runtime.instance_id),
        )
        self.tree = app_commands.CommandTree(self)
        self.front.build_commands(self.tree)
        self._ticks = 0
        self._synced_guild: int | None = None
        self._twitch_task: asyncio.Task | None = None
        self._last_bot_status: str | None = None

    # -- Twitch callbacks ---------------------------------------------------------------------

    async def on_twitch_event(self, event: sd.StreamEvent) -> None:
        await self.front.apply(self.director.handle_event(event))

    async def on_twitch_reconcile(self, live: sd.LiveStream | None, followers: int | None) -> None:
        await self.front.apply(self.director.reconcile(live, followers))

    # -- config / status ------------------------------------------------------------------------

    def reload_config(self) -> None:
        config, problem = load_settings(self.context.instance_id)
        self.config_problem = problem
        self.director.config = config

    def channel_problem(self) -> str | None:
        if not self.director.config.configured or not self.is_ready():
            return None
        return self.front.check_channel()

    def status(self) -> dict[str, Any]:
        config = self.director.config
        channel_problem = self.channel_problem()
        channel = self.front.channel()
        return {
            "diagnosis": diagnose(config, self.config_problem, self.director, channel_problem),
            "problem": self.front.problem,
            "config_problem": self.config_problem,
            "guild_name": getattr(getattr(channel, "guild", None), "name", None),
            "channel_name": getattr(channel, "name", None),
            "missing_permissions": list(self.front.missing_permissions),
            "commands_synced": self._synced_guild == config.guild_id and config.guild_id is not None,
            "twitch": self.twitch.status.to_json(),
            "director": self.director.status_summary(),
        }

    def write_status(self) -> None:
        try:
            admin_terminal.write_runtime_json(self.context.runtime_dir, STATUS_FILE_NAME, self.status())
        except Exception as exc:  # pragma: no cover - status is best effort
            print(f"Status error: {type(exc).__name__}")

    def refresh_bot_status(self) -> None:
        """Servers/channels for the Manager pickers (same file as the other bot types)."""
        try:
            status = admin_terminal.build_bot_status(self)
            serialized = json.dumps(status, sort_keys=True)
            if serialized != self._last_bot_status:
                admin_terminal.write_bot_status(self.context.runtime_dir, status)
                self._last_bot_status = serialized
        except Exception as exc:  # pragma: no cover
            print(f"Status error: {type(exc).__name__}")

    async def sync_commands(self) -> None:
        guild_id = self.director.config.guild_id
        if guild_id is None or guild_id == self._synced_guild:
            return
        guild = discord.Object(id=guild_id)
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self._synced_guild = guild_id
            print("Stream Director slash commands are ready in the configured server.")
        except discord.HTTPException as exc:
            self.front.problem = f"Slash commands could not be registered (HTTP {exc.status}); invite the bot with the applications.commands scope."

    def heartbeat(self, force: bool = False) -> None:
        """"Running" for the other DarkAbyss bots (Kairo's awareness); at most once a minute."""
        events = self.front.events
        if events is None:
            return
        user = getattr(self, "user", None)
        events.heartbeat(
            display_name=self.context.display_name,
            discord_user_id=getattr(user, "id", None),
            discord_name=getattr(user, "display_name", None) or getattr(user, "name", "") or "",
            guild_ids=[guild.id for guild in getattr(self, "guilds", [])],
            force=force,
        )

    async def run_tick(self) -> None:
        self._ticks += 1
        self.heartbeat()
        if self._ticks % CONFIG_EVERY_TICKS == 0:
            self.reload_config()
            await self.sync_commands()
        if self._ticks % BOT_STATUS_EVERY_TICKS == 0:
            self.refresh_bot_status()
        try:
            await self.front.apply(self.director.tick())
            await self.front.flush()
        except Exception as exc:  # never stop the loop
            self.front.problem = f"Stream Director error: {type(exc).__name__}."
        if self._ticks % STATUS_EVERY_TICKS == 0:
            self.write_status()

    # -- lifecycle --------------------------------------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def director_loop(self) -> None:
        await self.run_tick()

    @director_loop.before_loop
    async def _before_loop(self) -> None:
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        if not self.director_loop.is_running():
            self.director_loop.start()

    async def on_ready(self) -> None:
        print(f"Stream Director Bot is online as {self.user}")
        self.heartbeat(force=True)
        self.refresh_bot_status()
        await self.sync_commands()
        if self._twitch_task is None or self._twitch_task.done():
            self._twitch_task = asyncio.create_task(self.twitch.run())
        self.write_status()

    async def close(self) -> None:
        self.twitch.stop()
        if self.front.events is not None:
            self.front.events.stopped()
        try:
            self.write_status()
        finally:
            await super().close()

    # -- Discord events -------------------------------------------------------------------------------

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return  # slash commands are handled by the command tree
        try:
            await self.front.handle_interaction(interaction)
        except Exception as exc:  # pragma: no cover
            print(f"Stream Director button error: {type(exc).__name__}")

    async def on_message(self, message: discord.Message) -> None:
        session = self.director.session
        if session is None or getattr(message.author, "bot", False):
            return
        if str(getattr(message.channel, "id", "")) == str((session.get("discord") or {}).get("thread_id")):
            self.director.note_discord_message()

    async def on_guild_join(self, _guild: Any) -> None:
        self.refresh_bot_status()

    async def on_guild_remove(self, _guild: Any) -> None:
        self.refresh_bot_status()


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

_lock_handle = None


def acquire_single_instance_lock(runtime: StreamDirectorRuntimeContext) -> bool:
    global _lock_handle
    runtime.lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = runtime.lock_path.open("a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        print("This Stream Director bot is already running. Exiting.")
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(time.time()).encode("ascii"))
    handle.flush()
    _lock_handle = handle
    return True


def main(argv: list[str] | None = None) -> int:
    runtime_layout.line_buffered_output()
    try:
        args = parse_args(argv)
        runtime = resolve_runtime(args.instance)
        token = load_token(runtime)
    except RuntimeError as exc:
        print(exc)
        return 1

    client = StreamDirectorBot(runtime)
    if client.config_problem:
        print(client.config_problem)
    if not client.director.available:
        # Connects anyway (buttons answer, the Manager sees the status) but changes nothing.
        print(client.director.problem, file=sys.stderr)
    print("Stream Director Bot requests Discord intents: guilds, guild messages (no message content, nothing privileged).")

    if not acquire_single_instance_lock(runtime):
        return 0
    try:
        client.run(token)
    except discord.LoginFailure:
        print(LOGIN_FAILURE_HELP, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
