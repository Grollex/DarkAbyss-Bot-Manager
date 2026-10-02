"""AI-6 persistent bot features (no AI at runtime).

* FeatureStore: small JSON file in the instance data folder (atomic writes).
* Role button menus and verification buttons: members click a button to get
  or drop a role. Only roles without moderator/admin permissions and below the
  bot's top role are ever handed out, re-checked on every click.
* Welcome message and auto roles for new members.
* Scheduled (repeating) messages, posted with all mentions suppressed.

AI tools only CONFIGURE these features (each change goes through the normal
confirmation path); the runtime handlers below never call an AI provider.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import discord

C: Any = None  # admin_tools core module, set by build_tools()

FEATURE_STORE_FILENAME = "admin_features.json"
STORE_VERSION = 1
CUSTOM_ID_PREFIX = "dab:rm:"
MAX_MENUS = 25
MAX_SCHEDULES = 10
MIN_SCHEDULE_MINUTES = 10
MAX_SCHEDULE_MINUTES = 10080
MAX_WELCOME_ROLES = 5
MENU_MODES = ("toggle", "single", "add_only")
# Discord member type accepted for button clicks. Overridable in tests only.
MEMBER_TYPES: tuple[type, ...] = (discord.Member,)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


class FeatureStore:
    """Per-instance JSON store: {"version": 1, "guilds": {"<guild_id>": {key: value}}}."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        data: dict[str, Any] = {"version": STORE_VERSION, "guilds": {}}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("guilds"), dict):
                    data = {"version": STORE_VERSION, "guilds": raw["guilds"]}
                else:
                    raise ValueError("bad shape")
            except (OSError, ValueError):
                # Keep the unreadable file for inspection instead of overwriting it.
                try:
                    self.path.replace(self.path.with_name(f"{self.path.stem}.corrupt-{int(time.time())}.json"))
                except OSError:
                    pass
        self._data = data
        return data

    def _save(self) -> None:
        data = self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, guild_id: Any, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._load()["guilds"].get(str(guild_id), {}).get(key, default)
            return json.loads(json.dumps(value)) if value is not None else default

    def set(self, guild_id: Any, key: str, value: Any) -> None:
        with self._lock:
            guilds = self._load()["guilds"]
            guild = guilds.setdefault(str(guild_id), {})
            if value is None:
                guild.pop(key, None)
                if not guild:
                    guilds.pop(str(guild_id), None)
            else:
                guild[key] = json.loads(json.dumps(value))
            self._save()

    def guild_ids(self) -> list[str]:
        with self._lock:
            return list(self._load()["guilds"])


def store_for_data_dir(data_dir: Any) -> FeatureStore | None:
    if data_dir is None:
        return None
    return FeatureStore(Path(data_dir) / FEATURE_STORE_FILENAME)


# --------------------------------------------------------------------------
# runtime: role buttons, welcome, schedules
# --------------------------------------------------------------------------


def _dangerous_permissions(role: Any) -> list[str]:
    import admin_tools_server

    value = getattr(getattr(role, "permissions", None), "value", None)
    if not isinstance(value, int):
        return []
    names = {name for name, enabled in discord.Permissions(value) if enabled}
    return sorted(names & admin_tools_server.SELF_ASSIGN_FORBIDDEN_PERMISSIONS)


def _bot_can_hand_out(guild: Any, role: Any) -> bool:
    if role is None or getattr(role, "managed", False) or (callable(getattr(role, "is_default", None)) and role.is_default()):
        return False
    if _dangerous_permissions(role):
        return False
    bot_top = getattr(getattr(getattr(guild, "me", None), "top_role", None), "position", None)
    return isinstance(bot_top, int) and isinstance(getattr(role, "position", None), int) and role.position < bot_top


async def _respond(interaction: Any, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass


async def handle_component_interaction(interaction: Any, store: FeatureStore | None) -> bool:
    """Handle a role-menu/verification button click. Returns True if it was ours."""
    data = getattr(interaction, "data", None) or {}
    custom_id = data.get("custom_id") if isinstance(data, dict) else None
    if not isinstance(custom_id, str) or not custom_id.startswith(CUSTOM_ID_PREFIX):
        return False
    # Acknowledge first: role changes are Discord API calls and Discord shows
    # "This interaction failed" if a click is not acknowledged within 3 seconds.
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    guild = getattr(interaction, "guild", None)
    member = getattr(interaction, "user", None)
    if store is None or guild is None or not isinstance(member, MEMBER_TYPES):
        await _respond(interaction, "This button is not available right now.")
        return True
    try:
        _, _, menu_id, role_id_text = custom_id.split(":", 3)
        role_id = int(role_id_text)
    except ValueError:
        await _respond(interaction, "This button is not valid.")
        return True
    menus = store.get(guild.id, "role_menus", {}) or {}
    menu = menus.get(menu_id)
    if not isinstance(menu, dict) or role_id not in [int(value) for value in menu.get("role_ids", [])]:
        await _respond(interaction, "This role menu is no longer active.")
        return True
    role = guild.get_role(role_id)
    if not _bot_can_hand_out(guild, role):
        await _respond(interaction, "This role can no longer be handed out by the bot. Please tell a moderator.")
        return True
    mode = menu.get("mode", "toggle")
    has_role = any(getattr(item, "id", None) == role_id for item in member.roles)
    reason = f"Role menu {menu_id}"
    try:
        if has_role and mode == "toggle":
            await member.remove_roles(role, reason=reason)
            await _respond(interaction, f"Removed role {role.name}.")
            return True
        if has_role:
            await _respond(interaction, f"You already have {role.name}.")
            return True
        if mode == "single":
            others = [item for item in member.roles if str(item.id) in menu.get("role_ids", []) and item.id != role_id]
            others = [item for item in others if _bot_can_hand_out(guild, item)]
            if others:
                await member.remove_roles(*others, reason=reason)
        await member.add_roles(role, reason=reason)
        remove_id = menu.get("remove_role_id")
        if remove_id:
            remove_role = guild.get_role(int(remove_id))
            if remove_role is not None and any(item.id == remove_role.id for item in member.roles) and _bot_can_hand_out(guild, remove_role):
                await member.remove_roles(remove_role, reason=reason)
        await _respond(interaction, menu.get("success_text") or f"Added role {role.name}.")
    except discord.HTTPException:
        await _respond(interaction, "Discord refused the role change. Please tell a moderator.")
    return True


def render_welcome(template: str, member: Any) -> str:
    guild = getattr(member, "guild", None)
    values = {
        "{user}": getattr(member, "mention", ""),
        "{username}": getattr(member, "display_name", "") or getattr(member, "name", ""),
        "{server}": getattr(guild, "name", ""),
        "{member_count}": str(getattr(guild, "member_count", "") or ""),
    }
    text = template
    for key, value in values.items():
        text = text.replace(key, str(value))
    return text[:2000]


async def handle_member_join(member: Any, store: FeatureStore | None) -> None:
    if store is None:
        return
    guild = getattr(member, "guild", None)
    if guild is None:
        return
    config = store.get(guild.id, "welcome")
    if not isinstance(config, dict) or not config.get("enabled"):
        return
    if not getattr(member, "bot", False):
        roles = [guild.get_role(int(value)) for value in config.get("auto_role_ids", [])]
        roles = [role for role in roles if _bot_can_hand_out(guild, role)]
        if roles:
            try:
                await member.add_roles(*roles, reason="Welcome auto roles")
            except discord.HTTPException:
                pass
    channel_id = config.get("channel_id")
    template = config.get("message")
    if channel_id and template:
        channel = guild.get_channel(int(channel_id))
        if channel is not None:
            try:
                await channel.send(
                    render_welcome(template, member),
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]),
                )
            except discord.HTTPException:
                pass


async def run_due_schedules(client: Any, store: FeatureStore | None, now: float | None = None) -> int:
    """Post every due scheduled message once. Returns how many were posted."""
    if store is None:
        return 0
    now = time.time() if now is None else now
    posted = 0
    for guild_id in store.guild_ids():
        schedules = store.get(guild_id, "schedules", {}) or {}
        if not schedules:
            continue
        guild = client.get_guild(int(guild_id)) if hasattr(client, "get_guild") else None
        changed = False
        for schedule_id, item in list(schedules.items()):
            if not isinstance(item, dict) or float(item.get("next_run", 0)) > now:
                continue
            interval = max(MIN_SCHEDULE_MINUTES, int(item.get("interval_minutes", MIN_SCHEDULE_MINUTES))) * 60
            item["next_run"] = now + interval
            changed = True
            channel = guild.get_channel(int(item.get("channel_id", 0))) if guild is not None else None
            messages = [text for text in item.get("messages", []) if isinstance(text, str) and text]
            if channel is None or not messages:
                continue
            if item.get("random_order"):
                text = random.choice(messages)
            else:
                index = int(item.get("index", 0)) % len(messages)
                text = messages[index]
                item["index"] = index + 1
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                posted += 1
            except discord.HTTPException:
                continue
            if item.get("posts_left") is not None:
                item["posts_left"] = int(item["posts_left"]) - 1
                if item["posts_left"] <= 0:
                    schedules.pop(schedule_id, None)
        if changed:
            store.set(guild_id, "schedules", schedules or None)
    return posted


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def _require_store(context: Any) -> FeatureStore:
    if context.feature_store is None:
        raise C.AdminToolError("The bot feature store is unavailable in this context.")
    return context.feature_store


def _text_channel(context: Any, value: Any) -> Any:
    import admin_tools_content

    channel = admin_tools_content.resolve_any_channel(context, value)
    if C._normalized_channel_type(channel) not in ("text", "news"):
        raise ValueError("channel_id must be a text or announcement channel.")
    return channel


def _menu_view(menu_id: str, options: list[dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for option in options:
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=option["label"],
                emoji=option.get("emoji") or None,
                custom_id=f"{CUSTOM_ID_PREFIX}{menu_id}:{option['role_id']}",
            )
        )
    return view


async def _post_menu(context: Any, channel: Any, menu_id: str, title: str, text: str | None, color: Any, options: list[dict[str, Any]]) -> Any:
    import admin_tools_content

    spec: dict[str, Any] = {"title": title}
    if text:
        spec["description"] = text
    if color:
        spec["color"] = color
    view = _menu_view(menu_id, options)
    message = await channel.send(
        embed=admin_tools_content.build_embed(spec),
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    # Clicks are handled by the global component listener, not by this view.
    view.stop()
    return message


async def _create_role_menu(context: Any, arguments: dict[str, Any]) -> Any:
    import admin_tools_server

    guild = C._require_guild(context)
    store = _require_store(context)
    menus = store.get(guild.id, "role_menus", {}) or {}
    if len(menus) >= MAX_MENUS:
        raise C.AdminToolError(f"At most {MAX_MENUS} role menus per server; delete one first.")
    channel = _text_channel(context, arguments.get("channel_id"))
    options = []
    for option in arguments["options"]:
        role = C._resolve_role(context, option["role_id"])
        admin_tools_server.ensure_self_assignable_role(context, role)
        options.append({"role_id": str(role.id), "label": option.get("label") or role.name, "emoji": option.get("emoji")})
    if len({item["role_id"] for item in options}) != len(options):
        raise ValueError("Each role may appear only once in a menu.")
    menu_id = secrets.token_hex(4)
    message = await _post_menu(context, channel, menu_id, arguments["title"], arguments.get("description"), arguments.get("color"), options)
    menus[menu_id] = {
        "kind": "role_menu",
        "channel_id": str(channel.id),
        "message_id": str(message.id),
        "mode": arguments.get("mode") or "toggle",
        "role_ids": [item["role_id"] for item in options],
        "title": arguments["title"],
    }
    store.set(guild.id, "role_menus", menus)
    return C.ToolResult(True, "create_role_menu", f"Role menu posted in #{channel.name} ({len(options)} role(s)).", {"menu_id": menu_id})


async def _setup_verification(context: Any, arguments: dict[str, Any]) -> Any:
    import admin_tools_server

    guild = C._require_guild(context)
    store = _require_store(context)
    menus = store.get(guild.id, "role_menus", {}) or {}
    if len(menus) >= MAX_MENUS:
        raise C.AdminToolError(f"At most {MAX_MENUS} role menus per server; delete one first.")
    channel = _text_channel(context, arguments.get("channel_id"))
    role = C._resolve_role(context, arguments["verified_role_id"])
    admin_tools_server.ensure_self_assignable_role(context, role)
    remove_role_id = None
    if arguments.get("remove_role_id"):
        remove_role = C._resolve_role(context, arguments["remove_role_id"])
        admin_tools_server.ensure_self_assignable_role(context, remove_role)
        remove_role_id = str(remove_role.id)
    menu_id = secrets.token_hex(4)
    options = [{"role_id": str(role.id), "label": arguments.get("button_label") or "Verify", "emoji": arguments.get("emoji")}]
    message = await _post_menu(
        context,
        channel,
        menu_id,
        arguments.get("title") or "Verification",
        arguments.get("text") or "Press the button to get access to the server.",
        arguments.get("color"),
        options,
    )
    menus[menu_id] = {
        "kind": "verification",
        "channel_id": str(channel.id),
        "message_id": str(message.id),
        "mode": "add_only",
        "role_ids": [str(role.id)],
        "remove_role_id": remove_role_id,
        "success_text": arguments.get("success_text") or "You are verified. Welcome!",
        "title": arguments.get("title") or "Verification",
    }
    store.set(guild.id, "role_menus", menus)
    return C.ToolResult(True, "setup_verification", f"Verification button posted in #{channel.name}.", {"menu_id": menu_id})


async def _delete_role_menu(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = _require_store(context)
    menus = store.get(guild.id, "role_menus", {}) or {}
    menu = menus.pop(arguments["menu_id"], None)
    if menu is None:
        raise C.AdminToolError("Role menu not found; see list_bot_features.")
    store.set(guild.id, "role_menus", menus or None)
    note = ""
    if arguments.get("delete_message", True) is not False:
        channel = guild.get_channel(int(menu["channel_id"])) if hasattr(guild, "get_channel") else None
        try:
            if channel is not None:
                message = await channel.fetch_message(int(menu["message_id"]))
                await message.delete()
                note = " and its message"
        except discord.HTTPException:
            note = " (its message was already gone)"
    return C.ToolResult(True, "delete_role_menu", f"Deleted role menu {arguments['menu_id']}{note}.")


async def _set_welcome(context: Any, arguments: dict[str, Any]) -> Any:
    import admin_tools_server

    guild = C._require_guild(context)
    store = _require_store(context)
    current = store.get(guild.id, "welcome", {}) or {}
    if arguments.get("enabled") is False:
        store.set(guild.id, "welcome", {**current, "enabled": False} if current else None)
        return C.ToolResult(True, "set_welcome", "Welcome message and auto roles disabled.")
    config = dict(current)
    config["enabled"] = True
    if arguments.get("channel_id") is not None:
        config["channel_id"] = str(_text_channel(context, arguments["channel_id"]).id)
    if arguments.get("message") is not None:
        config["message"] = arguments["message"]
    if arguments.get("auto_role_ids") is not None:
        roles = [C._resolve_role(context, value) for value in arguments["auto_role_ids"]]
        for role in roles:
            admin_tools_server.ensure_self_assignable_role(context, role)
        config["auto_role_ids"] = [str(role.id) for role in roles]
    if config.get("message") and not config.get("channel_id"):
        raise ValueError("A welcome message needs channel_id.")
    if not config.get("message") and not config.get("auto_role_ids"):
        raise ValueError("Provide a welcome message (with channel_id) and/or auto_role_ids.")
    store.set(guild.id, "welcome", config)
    parts = []
    if config.get("message"):
        parts.append(f"welcome message in <#{config['channel_id']}>")
    if config.get("auto_role_ids"):
        parts.append(f"{len(config['auto_role_ids'])} auto role(s)")
    return C.ToolResult(True, "set_welcome", "Welcome enabled: " + ", ".join(parts) + ".")


async def _create_scheduled_message(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = _require_store(context)
    schedules = store.get(guild.id, "schedules", {}) or {}
    if len(schedules) >= MAX_SCHEDULES:
        raise C.AdminToolError(f"At most {MAX_SCHEDULES} scheduled messages per server; delete one first.")
    channel = _text_channel(context, arguments.get("channel_id"))
    interval = arguments["interval_minutes"]
    start_in = arguments.get("start_in_minutes")
    schedule_id = secrets.token_hex(4)
    schedules[schedule_id] = {
        "channel_id": str(channel.id),
        "messages": list(arguments["messages"]),
        "interval_minutes": interval,
        "random_order": arguments.get("random_order") is True,
        "next_run": time.time() + 60 * (interval if start_in is None else start_in),
        "posts_left": arguments.get("max_posts"),
        "created_by": context.requesting_user_id,
    }
    store.set(guild.id, "schedules", schedules)
    return C.ToolResult(
        True,
        "create_scheduled_message",
        f"Scheduled {len(arguments['messages'])} message variant(s) in #{channel.name} every {interval} minute(s).",
        {"schedule_id": schedule_id},
    )


async def _delete_scheduled_message(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = _require_store(context)
    schedules = store.get(guild.id, "schedules", {}) or {}
    if schedules.pop(arguments["schedule_id"], None) is None:
        raise C.AdminToolError("Scheduled message not found; see list_bot_features.")
    store.set(guild.id, "schedules", schedules or None)
    return C.ToolResult(True, "delete_scheduled_message", f"Deleted scheduled message {arguments['schedule_id']}.")


async def _list_bot_features(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    store = _require_store(context)
    menus = store.get(guild.id, "role_menus", {}) or {}
    schedules = store.get(guild.id, "schedules", {}) or {}
    blueprint = store.get(guild.id, "last_blueprint")
    return {
        "role_menus": [
            {"menu_id": key, "kind": value.get("kind"), "channel_id": value.get("channel_id"), "mode": value.get("mode"),
             "role_ids": value.get("role_ids"), "title": value.get("title")}
            for key, value in menus.items()
        ],
        "welcome": store.get(guild.id, "welcome"),
        "scheduled_messages": [
            {"schedule_id": key, "channel_id": value.get("channel_id"), "interval_minutes": value.get("interval_minutes"),
             "variants": len(value.get("messages", [])), "posts_left": value.get("posts_left")}
            for key, value in schedules.items()
        ],
        "lockdown_active": store.get(guild.id, "lockdown") is not None,
        "undoable_blueprint": None if not isinstance(blueprint, dict) else {
            key: len(values) for key, values in (blueprint.get("created") or {}).items()
        },
    }


def build_tools(core: Any) -> list[tuple[Any, Any]]:
    global C
    C = core
    T = core.ToolDefinition
    S = core._object_schema
    sf = core._snowflake_property
    text = core._string_property
    boolean = lambda description: {"type": "boolean", "description": description}  # noqa: E731
    channel_id = core.CHANNEL_ID_PROPERTY
    feature_id = {"type": "string", "pattern": "^[0-9a-f]{8}$", "description": "ID from list_bot_features."}
    return [
        (T("list_bot_features", "read", "read", "Show configured role menus, verification, welcome, scheduled messages, lockdown and undoable blueprint.", S(), "features"), _list_bot_features),
        (T("create_role_menu", "write", "normal", "Post a button menu where members pick their own roles (no moderator roles).", S({
            "channel_id": channel_id,
            "title": text("Menu title.", max_length=256),
            "description": text("Menu text.", min_length=0, max_length=2000),
            "color": core.COLOR_PROPERTY,
            "mode": {"type": "string", "enum": list(MENU_MODES), "description": "toggle (default), single (one role from the menu) or add_only."},
            "options": {"type": "array", "minItems": 1, "maxItems": 25, "description": "Buttons.", "items": {
                "type": "object", "additionalProperties": False, "required": ["role_id"],
                "properties": {
                    "role_id": sf("ID of the role given by this button."),
                    "label": text("Button text (default: role name).", max_length=80),
                    "emoji": text("Optional emoji.", max_length=64),
                },
            }},
        }, ["channel_id", "title", "options"]), "features"), _create_role_menu),
        (T("setup_verification", "write", "normal", "Post a verification button that gives a role (and optionally removes an 'unverified' role).", S({
            "channel_id": channel_id,
            "verified_role_id": sf("ID of the role given on click."),
            "remove_role_id": sf("ID of a role removed on click."),
            "title": text("Title.", max_length=256),
            "text": text("Instructions.", min_length=0, max_length=2000),
            "button_label": text("Button text.", max_length=80),
            "emoji": text("Optional emoji.", max_length=64),
            "success_text": text("Reply after verifying.", max_length=300),
            "color": core.COLOR_PROPERTY,
        }, ["channel_id", "verified_role_id"]), "features"), _setup_verification),
        (T("delete_role_menu", "write", "destructive", "Deactivate a role menu or verification button (and delete its message).", S({
            "menu_id": feature_id, "delete_message": boolean("Also delete the menu message (default true)."),
        }, ["menu_id"]), "features"), _delete_role_menu),
        (T("set_welcome", "write", "normal", "Configure the welcome message for new members ({user}, {username}, {server}, {member_count}) and auto roles; enabled=false turns it off.", S({
            "enabled": boolean("false disables welcome and auto roles."),
            "channel_id": sf("Channel for welcome messages."),
            "message": text("Welcome text template.", max_length=1500),
            "auto_role_ids": {"type": "array", "items": dict(core.SNOWFLAKE_ITEM), "maxItems": MAX_WELCOME_ROLES, "uniqueItems": True,
                              "description": "IDs of roles every new (non-bot) member gets."},
        }), "features"), _set_welcome),
        (T("create_scheduled_message", "write", "normal", f"Post a message repeatedly (every {MIN_SCHEDULE_MINUTES}+ minutes), cycling or random among variants; mentions are suppressed.", S({
            "channel_id": channel_id,
            "messages": {"type": "array", "minItems": 1, "maxItems": 20, "items": text("Message text.", max_length=2000), "description": "Message variants."},
            "interval_minutes": core._integer_property("Interval in minutes.", minimum=MIN_SCHEDULE_MINUTES, maximum=MAX_SCHEDULE_MINUTES),
            "random_order": boolean("Pick a random variant each time."),
            "start_in_minutes": core._integer_property("First post after N minutes (default: one interval).", minimum=0, maximum=MAX_SCHEDULE_MINUTES),
            "max_posts": core._integer_property("Stop after N posts (default: no limit).", maximum=10000),
        }, ["channel_id", "messages", "interval_minutes"]), "features"), _create_scheduled_message),
        (T("delete_scheduled_message", "write", "destructive", "Stop and delete a scheduled message.", S({"schedule_id": feature_id}, ["schedule_id"]), "features"), _delete_scheduled_message),
    ]
