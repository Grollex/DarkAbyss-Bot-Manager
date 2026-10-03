"""Game Presence Bot: one bot instance with its own Discord application.

Separate bot type (bots/game_presence) and process; nothing here is shared
with the Admin bot except infrastructure modules. Everything the bot owns
lives in its instance folder:

    instances/<id>/config.json            Game Presence settings (top-level keys)
    instances/<id>/secrets/token.txt      Discord token of THIS application
    instances/<id>/data/ai_selection.json AI source: Base Set (default) or own connections
    instances/<id>/data/ai_usage.json     this bot's provider-reported AI usage
    instances/<id>/data/game_presence_state.json   opt-outs, history, cooldowns
    instances/<id>/runtime/               lock, bot_status.json, game_presence_status.json

Gateway intents: guilds, members (privileged), presences (privileged) and
voice states. No Message Content Intent. Interactions need no intent.

The optional AI rewrite only rephrases a template: requests carry no tool
schemas and run without a tool executor, so this bot can never run Admin tools.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import msvcrt
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord.ext import tasks

import admin_features
import admin_terminal
import app_paths
import bot_registry
import config_store
import game_presence
import game_presence_discord
import instance_store
import runtime_layout

BOT_TYPE_ID = "game_presence"
LOCK_FILE_NAME = "game_presence_bot.lock"
STATE_FILE_NAME = "game_presence_state.json"
TICK_SECONDS = 15
STATUS_REFRESH_EVERY_TICKS = 4  # bot_status.json (servers/channels) every minute

REQUIRED_PRIVILEGED_INTENTS = ("Presence Intent", "Server Members Intent")
PRIVILEGED_INTENTS_HELP = (
    "PrivilegedIntentsRequired: Discord refused a privileged gateway intent. The Game Presence Bot needs BOTH privileged intents: "
    "'Presence Intent' (who plays what) and 'Server Members Intent' (members of the server). Enable them in "
    "Discord Developer Portal -> your Game Presence application -> Bot -> Privileged Gateway Intents, then "
    "start the bot again. 'Message Content Intent' is NOT needed."
)
LOGIN_FAILURE_HELP = (
    "LoginFailure: Discord rejected the Game Presence bot token (401 Unauthorized). The token was reset or "
    "mistyped. Developer Portal -> your Game Presence application -> Bot -> Reset Token, then paste the new "
    "token in Manager -> Bot Setup for this bot and start it again."
)


class PresenceStateError(RuntimeError):
    """The persisted Game Presence state is unreadable; posting stops (fail closed)."""


@dataclass(frozen=True)
class GamePresenceRuntimeContext:
    instance_id: str
    token_path: Path
    lock_path: Path
    runtime_dir: Path
    data_dir: Path


# --------------------------------------------------------------------------
# instance, token, config
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Game Presence Bot instance.")
    parser.add_argument("--instance", required=True)
    return parser.parse_args(argv)


def resolve_runtime(instance_id: str) -> GamePresenceRuntimeContext:
    try:
        instance = instance_store.load_instance(instance_id)
    except (bot_registry.BotRegistryError, instance_store.InstanceStoreError, OSError) as exc:
        raise RuntimeError(f"Invalid Game Presence instance {instance_id!r}: {exc}") from exc
    if instance.bot_type != BOT_TYPE_ID:
        raise RuntimeError(
            f"Invalid Game Presence instance {instance_id!r}: bot_type {instance.bot_type!r}; expected {BOT_TYPE_ID!r}."
        )
    return GamePresenceRuntimeContext(
        instance_id=instance.id,
        token_path=instance.paths.token,
        lock_path=instance.paths.runtime_dir / LOCK_FILE_NAME,
        runtime_dir=instance.paths.runtime_dir,
        data_dir=instance.paths.data_dir,
    )


def load_token(runtime: GamePresenceRuntimeContext) -> str:
    if not runtime.token_path.is_file():
        raise RuntimeError("Discord token file of this bot is missing. Set the token in Manager -> Bot Setup.")
    token = runtime.token_path.read_text(encoding="utf-8").strip()
    if not token or token == app_paths.TOKEN_PLACEHOLDER:
        raise RuntimeError("This Game Presence bot has no Discord token yet. Set it in Manager -> Bot Setup.")
    return token


def load_settings(instance_id: str) -> tuple[game_presence.GamePresenceConfig, str | None]:
    """(config, problem). Invalid config fails closed: inactive config + reason."""
    try:
        effective = config_store.load_effective_config(instance_id)
        data = game_presence.normalize_bot_config(effective)
        config = game_presence.parse_bot_config(data)
    except Exception as exc:
        return game_presence.GamePresenceConfig(), f"Invalid Game Presence config: {exc}"
    if data["enabled"] and not game_presence.is_configured(data):
        return config, game_presence.NOT_CONFIGURED_TEXT
    return config, None


def build_intents() -> discord.Intents:
    """Only what Game Presence reads; no Message Content Intent."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.presences = True
    intents.voice_states = True
    return intents


def privileged_intents_help() -> str:
    return f"Required privileged intents: {', '.join(REQUIRED_PRIVILEGED_INTENTS)}.\n{PRIVILEGED_INTENTS_HELP}"


# --------------------------------------------------------------------------
# persisted state (opt-outs, history, cooldowns)
# --------------------------------------------------------------------------


class PresenceStateStore(admin_features.FeatureStore):
    """FeatureStore that fails closed: an unreadable state file (it holds the
    members' opt-outs) stops posting instead of being reset to "everyone allowed"."""

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        data: dict[str, Any] = {"version": admin_features.STORE_VERSION, "guilds": {}}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PresenceStateError(state_problem_text(self.path.name, "is unreadable")) from exc
            if not isinstance(raw, dict) or not isinstance(raw.get("guilds"), dict):
                raise PresenceStateError(state_problem_text(self.path.name, "has an invalid shape"))
            data = {"version": admin_features.STORE_VERSION, "guilds": raw["guilds"]}
        self._data = data
        return data

    def verify(self) -> None:
        """Raise PresenceStateError when the state cannot be used."""
        with self._lock:
            self._load()


def state_problem_text(file_name: str, reason: str) -> str:
    return (
        f"Game Presence state file {file_name} {reason}. Posting is paused and Mute/Allow buttons answer "
        "'unavailable' so nobody's opt-out is lost. Restore the file from a backup or remove it (removing "
        "forgets opt-outs and cooldowns), then restart the bot."
    )


def state_store_for(data_dir: Path) -> PresenceStateStore:
    return PresenceStateStore(Path(data_dir) / STATE_FILE_NAME)


# --------------------------------------------------------------------------
# optional AI wording (Base Set or this bot's own connections; never tools)
# --------------------------------------------------------------------------


class TextOnlyAI:
    """The only AI surface of this bot: every request goes out without tool
    schemas and without a tool executor."""

    def __init__(self, orchestrator: Any) -> None:
        self._orchestrator = orchestrator

    async def orchestrate(self, request: Any) -> Any:
        return await self._orchestrator.orchestrate(dataclasses.replace(request, allowed_tool_names=()))


class InstanceAI:
    """Lazily builds the orchestrator over THIS bot's AI source (Base Set or own connections)."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self._lock = threading.Lock()
        self._ai: TextOnlyAI | None = None
        self._failed = False

    def get(self) -> TextOnlyAI | None:
        with self._lock:
            if self._ai is None and not self._failed:
                try:
                    import ai_orchestrator
                    import ai_storage

                    stores = ai_storage.for_instance_id(self.instance_id)
                    self._ai = TextOnlyAI(ai_orchestrator.AIOrchestrator(stores=stores))
                except Exception as exc:
                    self._failed = True
                    print(f"AI wording unavailable for this bot: {type(exc).__name__}")
            return self._ai


# --------------------------------------------------------------------------
# the bot
# --------------------------------------------------------------------------


class GamePresenceBot(discord.Client):
    def __init__(self, runtime: GamePresenceRuntimeContext) -> None:
        super().__init__(intents=build_intents())
        self.context = runtime
        self.store = state_store_for(runtime.data_dir)
        self.preferences = game_presence.PreferenceBook(game_presence.FeatureStorePresenceStore(self.store))
        self.ai = InstanceAI(runtime.instance_id)
        self.presence_runtime = game_presence_discord.GamePresenceRuntime(
            self,
            game_presence.GamePresenceEngine(game_presence.FeatureStorePresenceStore(self.store)),
            rewriter=game_presence_discord.make_ai_rewriter(self.ai.get),
            runtime_dir=runtime.runtime_dir,
            presence_intent=True,
        )
        self._ticks = 0
        self._last_bot_status: str | None = None
        self.reload_config()

    # -- config / status ------------------------------------------------------

    def reload_config(self) -> None:
        config, problem = load_settings(self.context.instance_id)
        self.presence_runtime.config_problem = problem
        self.presence_runtime.apply_config(config)

    def refresh_bot_status(self) -> None:
        """Servers/channels for the Manager pickers (same file as the Admin bot)."""
        try:
            status = admin_terminal.build_bot_status(self)
            serialized = json.dumps(status, sort_keys=True)
            if serialized != self._last_bot_status:
                admin_terminal.write_bot_status(self.context.runtime_dir, status)
                self._last_bot_status = serialized
        except Exception as exc:  # pragma: no cover - status is best effort
            print(f"Status error: {type(exc).__name__}")

    def state_problem(self) -> str | None:
        """Explanation when the persisted state is unusable (fail closed), else None."""
        try:
            self.store.verify()
        except PresenceStateError as exc:
            return str(exc)
        return None

    def publish_state_problem(self) -> bool:
        """Write the state problem into the status file for the Manager; True if there is one."""
        problem = self.state_problem()
        if problem is None:
            return False
        self.presence_runtime.problem = problem
        self.presence_runtime.diagnose("state_problem", problem)
        self.presence_runtime.write_status()
        return True

    async def run_tick(self) -> None:
        self._ticks += 1
        if self._ticks % STATUS_REFRESH_EVERY_TICKS == 0:
            self.refresh_bot_status()
        self.reload_config()
        if self.publish_state_problem():
            return  # fail closed: nothing is evaluated or posted
        try:
            await self.presence_runtime.tick()
        except PresenceStateError as exc:
            self.presence_runtime.problem = str(exc)
            self.presence_runtime.write_status()
        except Exception as exc:  # never stop the loop
            self.presence_runtime.problem = f"Game Presence error: {type(exc).__name__}."
            self.presence_runtime.write_status()

    # -- lifecycle --------------------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def presence_loop(self) -> None:
        await self.run_tick()

    @presence_loop.before_loop
    async def _before_presence_loop(self) -> None:
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        if not self.presence_loop.is_running():
            self.presence_loop.start()

    async def on_ready(self) -> None:
        print(f"Game Presence Bot is online as {self.user}")
        self.refresh_bot_status()
        self.presence_runtime.seed()
        if not self.publish_state_problem():
            self.presence_runtime.write_status()

    # -- Discord events -----------------------------------------------------------

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        self.presence_runtime.on_presence_update(before, after)

    async def on_voice_state_update(self, member: discord.Member, before: Any, after: Any) -> None:
        self.presence_runtime.on_voice_state_update(member, before, after)

    async def on_member_join(self, member: discord.Member) -> None:
        self.presence_runtime.on_member_join(member)

    async def on_member_remove(self, member: discord.Member) -> None:
        self.presence_runtime.on_member_remove(member)

    async def on_guild_join(self, _guild: Any) -> None:
        self.refresh_bot_status()

    async def on_guild_remove(self, _guild: Any) -> None:
        self.refresh_bot_status()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Persistent Mute/Allow buttons: only interaction.user's own preference,
        # stored in THIS instance's state file.
        if interaction.type is not discord.InteractionType.component:
            return
        try:
            await game_presence_discord.handle_preference_interaction(interaction, self.preferences)
        except Exception as exc:  # pragma: no cover
            print(f"Game Presence button error: {type(exc).__name__}")


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

_lock_handle = None


def acquire_single_instance_lock(runtime: GamePresenceRuntimeContext) -> bool:
    global _lock_handle
    runtime.lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = runtime.lock_path.open("a+b")
    try:
        handle.seek(0)  # msvcrt locks from the current position
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        print("This Game Presence bot is already running. Exiting.")
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

    client = GamePresenceBot(runtime)
    if client.presence_runtime.config_problem:
        print(client.presence_runtime.config_problem)
    state_problem = client.state_problem()
    if state_problem:
        # The bot still connects (buttons answer, the Manager sees the status)
        # but posts nothing until the file is fixed.
        print(state_problem, file=sys.stderr)
    print("Game Presence Bot requests Discord intents: guilds, members, presences, voice states (no message content).")

    if not acquire_single_instance_lock(runtime):
        return 0
    try:
        client.run(token)
    except discord.PrivilegedIntentsRequired:
        # Actionable text instead of a traceback dialog in the packaged app.
        print(privileged_intents_help(), file=sys.stderr)
        _write_problem(client, privileged_intents_help())
        return 1
    except discord.LoginFailure:
        print(LOGIN_FAILURE_HELP, file=sys.stderr)
        return 1
    return 0


def _write_problem(client: GamePresenceBot, text: str) -> None:
    try:
        client.presence_runtime.problem = text
        client.presence_runtime.presence_intent = False
        client.presence_runtime.write_status()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
