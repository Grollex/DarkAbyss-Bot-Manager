"""Manager <-> bot request terminal: a local file mailbox (no network listener).

The Manager GUI cannot talk to Discord itself; the running bot process owns the
connection. Requests travel through files in the instance ``runtime`` folder:

    runtime/ai_terminal/requests/<request_id>.json   Manager -> bot (prompt)
    runtime/ai_terminal/decisions/<token>.json       Manager -> bot (button click)
    runtime/ai_terminal/events/<request_id>.jsonl    bot -> Manager (replies)
    runtime/bot_status.json                          bot -> Manager (servers/channels)

Only processes that can already read the instance folder (and therefore the
bot token) can use it; the bot opens no port and no HTTP API. Requests are
executed for the local *operator* (the person running the Manager, who owns
the bot): the AI whitelist and Discord role hierarchy limits do not apply,
but every plan / destructive-action confirmation still has to be approved in
the Manager.

This module imports no discord/AI code so the Manager can use it directly.
``ManagerInteraction`` imitates the small part of ``discord.Interaction`` the
AI transport uses, so plans, confirmations and engine switches reuse the
exact Discord code paths.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

TERMINAL_DIR_NAME = "ai_terminal"
STATUS_FILE_NAME = "bot_status.json"
OPERATOR_USER_ID = 0
OPERATOR_NAME = "Manager operator"
MAX_PROMPT_CHARS = 2000
REQUEST_MAX_AGE_SECONDS = 300.0
EVENT_FILE_MAX_AGE_SECONDS = 24 * 3600.0
MAX_STATUS_CHANNELS = 300
MAX_EVENT_TEXT_CHARS = 4000
TASK_MODES = ("routine", "planner", "creative")


class TerminalError(Exception):
    """Invalid terminal request or mailbox state."""


# --------------------------------------------------------------------------
# paths and files
# --------------------------------------------------------------------------


def runtime_dir_for_logs(logs_dir: Path | str) -> Path:
    """Instance runtime folder next to its logs folder (instances/<id>/runtime)."""
    return Path(logs_dir).parent / "runtime"


def terminal_dirs(runtime_dir: Path | str) -> tuple[Path, Path, Path]:
    root = Path(runtime_dir) / TERMINAL_DIR_NAME
    return root / "requests", root / "events", root / "decisions"


def new_id() -> str:
    return secrets.token_hex(8)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and 4 <= len(value) <= 64 and all(char in "0123456789abcdef" for char in value)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _snowflake_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, int) else value
    return text if isinstance(text, str) and text.isdigit() and int(text) > 0 else None


# --------------------------------------------------------------------------
# Manager side
# --------------------------------------------------------------------------


def submit_request(
    runtime_dir: Path | str,
    *,
    guild_id: Any,
    prompt: str,
    channel_id: Any = None,
    mode: str = "routine",
    now: float | None = None,
) -> str:
    """Queue one prompt for the running bot; returns the request ID."""
    guild = _snowflake_text(guild_id)
    if guild is None:
        raise TerminalError("Choose a server first.")
    channel = None if channel_id in (None, "") else _snowflake_text(channel_id)
    if channel_id not in (None, "") and channel is None:
        raise TerminalError("Channel ID must be a Discord ID.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TerminalError("Write a request first.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise TerminalError(f"Requests are limited to {MAX_PROMPT_CHARS} characters.")
    if mode not in TASK_MODES:
        raise TerminalError("Unknown AI mode.")
    request_id = new_id()
    requests_dir, _events, _decisions = terminal_dirs(runtime_dir)
    _atomic_write_json(
        requests_dir / f"{request_id}.json",
        {
            "request_id": request_id,
            "guild_id": guild,
            "channel_id": channel,
            "prompt": prompt,
            "mode": mode,
            "created_at": time.time() if now is None else now,
        },
    )
    return request_id


def submit_decision(runtime_dir: Path | str, *, request_id: str, token: str, approved: bool) -> None:
    if not _valid_id(request_id) or not _valid_id(token) or type(approved) is not bool:
        raise TerminalError("Invalid decision.")
    _requests, _events, decisions_dir = terminal_dirs(runtime_dir)
    _atomic_write_json(
        decisions_dir / f"{token}.json",
        {"request_id": request_id, "token": token, "approved": approved, "created_at": time.time()},
    )


def read_events(runtime_dir: Path | str, request_id: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Events appended since byte ``offset`` (only complete lines)."""
    if not _valid_id(request_id):
        return [], offset
    _requests, events_dir, _decisions = terminal_dirs(runtime_dir)
    path = events_dir / f"{request_id}.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    events = []
    for line in data[: end + 1].splitlines():
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, offset + end + 1


def read_bot_status(runtime_dir: Path | str) -> dict[str, Any] | None:
    status = _read_json(Path(runtime_dir) / STATUS_FILE_NAME)
    return status if isinstance(status, dict) else None


# --------------------------------------------------------------------------
# bot side: files
# --------------------------------------------------------------------------


def build_bot_status(client: Any) -> dict[str, Any]:
    """Servers and text-like channels the bot can see (for the Manager pickers)."""
    guilds = []
    for guild in sorted(getattr(client, "guilds", None) or [], key=lambda item: str(getattr(item, "name", ""))):
        channels = []
        for channel in getattr(guild, "channels", None) or []:
            kind = getattr(getattr(channel, "type", None), "name", None) or str(getattr(channel, "type", ""))
            if str(kind).lower() not in ("text", "news", "voice", "forum", "stage_voice"):
                continue
            channels.append(
                {
                    "id": str(getattr(channel, "id", "")),
                    "name": str(getattr(channel, "name", "")),
                    "type": str(kind).lower(),
                    "position": getattr(channel, "position", 0) if isinstance(getattr(channel, "position", 0), int) else 0,
                }
            )
        channels.sort(key=lambda item: (item["type"] != "text", item["position"], item["name"]))
        guilds.append(
            {
                "id": str(getattr(guild, "id", "")),
                "name": str(getattr(guild, "name", "")),
                "member_count": getattr(guild, "member_count", None),
                "channels": channels[:MAX_STATUS_CHANNELS],
            }
        )
    user = getattr(client, "user", None)
    return {
        "bot_name": str(user) if user is not None else None,
        "bot_id": str(getattr(user, "id", "")) if user is not None else None,
        "guilds": guilds,
    }


def write_bot_status(runtime_dir: Path | str, status: dict[str, Any]) -> None:
    _atomic_write_json(Path(runtime_dir) / STATUS_FILE_NAME, {**status, "updated_at": time.time()})


def take_requests(runtime_dir: Path | str, now: float | None = None) -> list[dict[str, Any]]:
    """Read and remove queued requests; stale or malformed ones are dropped."""
    requests_dir, _events, _decisions = terminal_dirs(runtime_dir)
    now = time.time() if now is None else now
    taken = []
    for path in sorted(requests_dir.glob("*.json")) if requests_dir.is_dir() else []:
        payload = _read_json(path)
        try:
            path.unlink()
        except OSError:
            continue
        if not isinstance(payload, dict) or not _valid_id(payload.get("request_id")):
            continue
        created = payload.get("created_at")
        if not isinstance(created, (int, float)) or now - created > REQUEST_MAX_AGE_SECONDS:
            EventWriter(runtime_dir, payload["request_id"]).emit("error", text="The request expired before the bot picked it up.")
            EventWriter(runtime_dir, payload["request_id"]).emit("idle")
            continue
        taken.append(payload)
    return taken


def take_decisions(runtime_dir: Path | str) -> list[dict[str, Any]]:
    _requests, _events, decisions_dir = terminal_dirs(runtime_dir)
    taken = []
    for path in sorted(decisions_dir.glob("*.json")) if decisions_dir.is_dir() else []:
        payload = _read_json(path)
        try:
            path.unlink()
        except OSError:
            continue
        if (
            isinstance(payload, dict)
            and _valid_id(payload.get("request_id"))
            and _valid_id(payload.get("token"))
            and type(payload.get("approved")) is bool
        ):
            taken.append(payload)
    return taken


def purge_old_events(runtime_dir: Path | str, now: float | None = None) -> None:
    _requests, events_dir, _decisions = terminal_dirs(runtime_dir)
    now = time.time() if now is None else now
    for path in events_dir.glob("*.jsonl") if events_dir.is_dir() else []:
        try:
            if now - path.stat().st_mtime > EVENT_FILE_MAX_AGE_SECONDS:
                path.unlink()
        except OSError:
            pass


class EventWriter:
    """Appends JSON-line events for one request."""

    def __init__(self, runtime_dir: Path | str, request_id: str) -> None:
        _requests, events_dir, _decisions = terminal_dirs(runtime_dir)
        self.path = events_dir / f"{request_id}.jsonl"

    def emit(self, event_type: str, **fields: Any) -> None:
        event = {"type": event_type, "at": time.time(), **fields}
        if isinstance(event.get("text"), str):
            event["text"] = event["text"][:MAX_EVENT_TEXT_CHARS]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# operator interaction adapter
# --------------------------------------------------------------------------


class OperatorUser:
    """The local Manager operator (owner of the bot token)."""

    id = OPERATOR_USER_ID
    name = OPERATOR_NAME
    display_name = OPERATOR_NAME
    bot = False
    roles: tuple[Any, ...] = ()
    mention = OPERATOR_NAME
    guild_permissions = SimpleNamespace(administrator=True, value=8)

    def __str__(self) -> str:
        return OPERATOR_NAME


def is_manager_operator(obj: Any) -> bool:
    """True only for ManagerInteraction objects (never for Discord interactions)."""
    return (
        isinstance(obj, ManagerInteraction)
        and getattr(getattr(obj, "user", None), "id", None) == OPERATOR_USER_ID
    )


class _ManagerMessage:
    """Returned by followup.send; views edit it on timeout."""

    def __init__(self, terminal: "BotTerminal", request_id: str, token: str | None) -> None:
        self._terminal = terminal
        self._request_id = request_id
        self._token = token

    async def edit(self, **kwargs: Any) -> None:
        if self._token is not None and kwargs.get("view", "unset") is None:
            self._terminal.expire(self._request_id, self._token)


class ManagerResponse:
    def __init__(self, interaction: "ManagerInteraction") -> None:
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs: Any) -> None:
        self._done = True

    async def send_message(self, content: str = "", **kwargs: Any) -> None:
        self._done = True
        self._interaction.events.emit("message", text=str(content or ""))

    async def edit_message(self, *, content: Any = None, **kwargs: Any) -> None:
        self._done = True
        token = self._interaction.token
        if token is not None:
            self._interaction.events.emit("resolved", token=token, text=str(content or ""))


class ManagerFollowup:
    def __init__(self, interaction: "ManagerInteraction") -> None:
        self._interaction = interaction

    async def send(self, content: str = "", *, view: Any = None, **kwargs: Any) -> Any:
        interaction = self._interaction
        if view is None:
            interaction.events.emit("message", text=str(content or ""))
            return _ManagerMessage(interaction.terminal, interaction.request_id, None)
        token = interaction.terminal.register_view(interaction.request_id, view)
        labels = [str(getattr(item, "label", "") or "") for item in getattr(view, "children", None) or []]
        approve_label = labels[0] if labels and labels[0] else "Approve"
        cancel_label = labels[1] if len(labels) > 1 and labels[1] else "Cancel"
        interaction.events.emit(
            "approval",
            token=token,
            text=str(content or ""),
            buttons=[{"label": approve_label, "approved": True}, {"label": cancel_label, "approved": False}],
        )
        return _ManagerMessage(interaction.terminal, interaction.request_id, token)


class ManagerInteraction:
    """Minimal interaction-like object for one Manager request or decision."""

    def __init__(
        self,
        terminal: "BotTerminal",
        request_id: str,
        guild: Any,
        channel: Any = None,
        token: str | None = None,
    ) -> None:
        self.terminal = terminal
        self.request_id = request_id
        self.events = EventWriter(terminal.runtime_dir, request_id)
        self.user = OperatorUser()
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.channel = channel
        self.channel_id = getattr(channel, "id", None)
        self.token = token
        self.response = ManagerResponse(self)
        self.followup = ManagerFollowup(self)


# --------------------------------------------------------------------------
# bot side: coordinator
# --------------------------------------------------------------------------


class BotTerminal:
    """Polls the mailbox and drives the AI transport for operator requests."""

    def __init__(self, runtime_dir: Path | str, transport: Any, client: Any, *, clock: Callable[[], float] | None = None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.transport = transport
        self.client = client
        self._clock = clock or time.time
        # token -> (request_id, view, guild_id, channel_id, created_at)
        self._pending: dict[str, tuple[str, Any, Any, Any, float]] = {}
        self._last_status: str | None = None
        self._tasks: set[Any] = set()

    def register_view(self, request_id: str, view: Any) -> str:
        token = new_id()
        self._pending[token] = (request_id, view, None, None, self._clock())
        return token

    def expire(self, request_id: str, token: str) -> None:
        if self._pending.pop(token, None) is not None:
            EventWriter(self.runtime_dir, request_id).emit("expired", token=token)

    def refresh_status(self) -> None:
        status = build_bot_status(self.client)
        serialized = json.dumps(status, sort_keys=True)
        if serialized != self._last_status:
            write_bot_status(self.runtime_dir, status)
            self._last_status = serialized

    async def poll(self, spawn: Callable[[Awaitable[Any]], Any] | None = None) -> None:
        """One mailbox pass. ``spawn`` schedules work (asyncio.create_task by default)."""
        if spawn is None:
            import asyncio

            def spawn(coroutine: Awaitable[Any]) -> Any:
                task = asyncio.ensure_future(coroutine)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return task

        for request in take_requests(self.runtime_dir, self._clock()):
            spawn(self.handle_request(request))
        for decision in take_decisions(self.runtime_dir):
            spawn(self.handle_decision(decision))

    def _resolve(self, guild_id: Any, channel_id: Any) -> tuple[Any, Any, str | None]:
        getter = getattr(self.client, "get_guild", None)
        guild = getter(int(guild_id)) if callable(getter) and guild_id else None
        if guild is None:
            return None, None, "The bot is not in that server (or it is not connected yet)."
        channel = None
        if channel_id:
            for name in ("get_channel_or_thread", "get_channel"):
                lookup = getattr(guild, name, None)
                channel = lookup(int(channel_id)) if callable(lookup) else None
                if channel is not None:
                    break
            if channel is None:
                return guild, None, "That channel was not found in the selected server."
        return guild, channel, None

    async def handle_request(self, request: dict[str, Any]) -> None:
        request_id = request["request_id"]
        events = EventWriter(self.runtime_dir, request_id)
        try:
            guild, channel, error = self._resolve(request.get("guild_id"), request.get("channel_id"))
            prompt = request.get("prompt")
            mode = request.get("mode") if request.get("mode") in TASK_MODES else "routine"
            if error is not None:
                events.emit("error", text=error)
            elif not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
                events.emit("error", text="The request text is empty or too long.")
            elif self.transport is None:
                events.emit("error", text="AI is unavailable in this bot process.")
            else:
                events.emit("status", text="Working...")
                interaction = ManagerInteraction(self, request_id, guild, channel)
                await self.transport.handle_manager_request(interaction, prompt, mode)
        except Exception as exc:  # pragma: no cover - never break the polling loop
            events.emit("error", text=f"The request stopped unexpectedly ({type(exc).__name__}).")
        finally:
            events.emit("idle")

    async def handle_decision(self, decision: dict[str, Any]) -> None:
        request_id = decision["request_id"]
        events = EventWriter(self.runtime_dir, request_id)
        entry = self._pending.get(decision["token"])
        try:
            if entry is None or entry[0] != request_id:
                events.emit("resolved", token=decision["token"], text="This confirmation is no longer active.")
                return
            _request_id, view, _guild_id, _channel_id, _created = entry
            self._pending.pop(decision["token"], None)
            state = getattr(view, "state", None)
            run = getattr(state, "run", None)
            binding = getattr(run, "binding", None) or getattr(state, "binding", None)
            guild, channel, error = self._resolve(
                getattr(binding, "guild_id", None), getattr(binding, "channel_id", None)
            )
            if error is not None and guild is None:
                events.emit("resolved", token=decision["token"], text=error)
                return
            interaction = ManagerInteraction(self, request_id, guild, channel, token=decision["token"])
            if channel is None:
                interaction.channel_id = getattr(binding, "channel_id", None)
            events.emit("status", text="Working...")
            await self.transport.dispatch_view_decision(interaction, view, decision["approved"])
        except Exception as exc:  # pragma: no cover
            events.emit("error", text=f"The decision failed unexpectedly ({type(exc).__name__}).")
        finally:
            events.emit("idle")
