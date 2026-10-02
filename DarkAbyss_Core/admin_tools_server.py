"""AI-6 Admin Tools: server structure, roles, members, moderation, settings, AutoMod.

Every tool here is Discord-only: no filesystem, network (except Discord API
calls through discord.py), process or host access. Definitions and handlers
are registered by ``admin_tools`` via ``build_tools(core)``; ``core`` is the
admin_tools module itself, passed explicitly to avoid an import cycle.

Every write goes through the same execution path as the legacy tools
(validation, AI confirmation, audit). For AI requests the core guards limit
role/member/permission changes to what the requesting member could do.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord

C: Any = None  # admin_tools core module, set by build_tools()

MAX_LIST_MEMBERS = 100
MAX_BULK_MEMBERS = 500
MAX_AUDIT_ENTRIES = 50
MAX_BANS = 100
MAX_AUTOMOD_KEYWORDS = 200
LOCKDOWN_FLAGS = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "add_reactions",
    "speak",
    "send_polls",
)
# Roles anyone can obtain (onboarding options, role menus, verification, auto
# roles) must not carry any of these.
SELF_ASSIGN_FORBIDDEN_PERMISSIONS = frozenset(
    {
        "administrator",
        "manage_guild",
        "manage_roles",
        "manage_channels",
        "manage_webhooks",
        "manage_messages",
        "manage_threads",
        "manage_nicknames",
        "manage_expressions",
        "manage_events",
        "kick_members",
        "ban_members",
        "moderate_members",
        "mute_members",
        "deafen_members",
        "move_members",
        "mention_everyone",
        "view_audit_log",
        "view_guild_insights",
    }
)
VERIFICATION_LEVELS = ("none", "low", "medium", "high", "highest")
CONTENT_FILTERS = ("disabled", "no_role", "all_members")
NOTIFICATION_LEVELS = ("all_messages", "only_mentions")
AFK_TIMEOUTS = (60, 300, 900, 1800, 3600)
GUILD_CLEARABLE_FIELDS = ("description", "afk_channel", "system_channel", "rules_channel", "public_updates_channel")


# --------------------------------------------------------------------------
# small schema/result helpers
# --------------------------------------------------------------------------


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return C._object_schema(properties, required)


def _sf(description: str) -> dict[str, Any]:
    return C._snowflake_property(description)


def _str(description: str, *, min_length: int = 1, max_length: int | None = None) -> dict[str, Any]:
    return C._string_property(description, min_length=min_length, max_length=max_length)


def _int(description: str, *, minimum: int = 1, maximum: int | None = None) -> dict[str, Any]:
    return C._integer_property(description, minimum=minimum, maximum=maximum)


def _bool(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _enum(values: Any, description: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": description}


def _int_enum(values: Any, description: str) -> dict[str, Any]:
    return {"type": "integer", "enum": list(values), "description": description}


def _ids(description: str, *, max_items: int = 25) -> dict[str, Any]:
    return {"type": "array", "items": dict(C.SNOWFLAKE_ITEM), "maxItems": max_items, "uniqueItems": True, "description": description}


def _strings(description: str, *, max_items: int, max_length: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": max_length},
        "maxItems": max_items,
        "description": description,
    }


def _perm_list(description: str) -> dict[str, Any]:
    schema = dict(C.PERMISSION_LIST_PROPERTY)
    schema["description"] = description
    return schema


def _ok(tool: str, message: str, data: Any = None) -> Any:
    return C.ToolResult(True, tool, message, data)


def _sid(value: Any) -> str | None:
    return C._snowflake_to_string(getattr(value, "id", None))


def _reason(arguments: dict[str, Any]) -> str | None:
    return C._reason(arguments)


def resolve_any_channel(context: Any, value: Any, field: str = "channel_id") -> Any:
    guild = C._require_guild(context)
    channel_id = C.parse_snowflake(value, field)
    getter = getattr(guild, "get_channel_or_thread", None)
    if callable(getter):
        channel = getter(channel_id)
        if channel is not None:
            return channel
    return C._find_channel(guild, channel_id)


def channel_type(channel: Any) -> str:
    return C._normalized_channel_type(channel)


def ensure_self_assignable_role(context: Any, role: Any) -> None:
    """Roles that members obtain by themselves must be harmless and below the requester."""
    if callable(getattr(role, "is_default", None)) and role.is_default():
        raise C.AdminToolError("@everyone cannot be used here.")
    C.ensure_role_manageable(context, role, action="hand out")
    dangerous = sorted(set(C.permission_names(getattr(role, "permissions", None))) & SELF_ASSIGN_FORBIDDEN_PERMISSIONS)
    if dangerous:
        raise C.AdminToolError(
            f"Role {getattr(role, 'name', '?')} has moderator/admin permissions ({', '.join(dangerous)}) "
            "and cannot be self-assigned."
        )


def _role_summary(role: Any) -> dict[str, Any]:
    return {"id": _sid(role), "name": getattr(role, "name", None), "position": getattr(role, "position", None)}


def _enum_name(value: Any) -> Any:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else (str(value) if value is not None else None)


# --------------------------------------------------------------------------
# core (read)
# --------------------------------------------------------------------------


async def _list_members(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    limit = arguments.get("limit") or 50
    role = C._resolve_role(context, arguments["role_id"]) if arguments.get("role_id") else None
    needle = (arguments.get("name_contains") or "").lower()
    include_bots = arguments.get("include_bots") is not False
    members = list(getattr(role, "members", None) if role is not None else getattr(guild, "members", []) or [])
    selected = []
    for member in members:
        if not include_bots and getattr(member, "bot", False):
            continue
        names = f"{getattr(member, 'name', '')} {getattr(member, 'display_name', '')}".lower()
        if needle and needle not in names:
            continue
        selected.append(member)
    order = arguments.get("sort") or "name"
    if order == "name":
        selected.sort(key=lambda item: str(getattr(item, "display_name", "") or "").lower())
    else:
        def joined(item: Any) -> float:
            value = getattr(item, "joined_at", None)
            return value.timestamp() if hasattr(value, "timestamp") else 0.0

        selected.sort(key=joined, reverse=order == "joined_newest")
    return {
        "total_matching": len(selected),
        "members": [
            {
                "id": _sid(member),
                "display_name": getattr(member, "display_name", None),
                "username": getattr(member, "name", None),
                "bot": bool(getattr(member, "bot", False)),
                "top_role": getattr(getattr(member, "top_role", None), "name", None),
                "joined_at": C._isoformat_or_none(getattr(member, "joined_at", None)),
            }
            for member in selected[:limit]
        ],
    }


async def _get_access_context(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    bot = C._bot_member(guild)
    data: dict[str, Any] = {
        "guild_owner_id": C._snowflake_to_string(getattr(guild, "owner_id", None)),
        "premium_tier": getattr(guild, "premium_tier", None),
        "community": "COMMUNITY" in (getattr(guild, "features", None) or []),
        "bot": None,
        "requester": None,
    }
    if bot is not None:
        data["bot"] = {
            "id": _sid(bot),
            "top_role": _role_summary(getattr(bot, "top_role", None)),
            "permissions": C.permission_names(getattr(bot, "guild_permissions", None)),
        }
    getter = getattr(guild, "get_member", None)
    requester = getter(context.requesting_user_id) if callable(getter) and context.requesting_user_id else None
    if requester is not None:
        data["requester"] = {
            "id": _sid(requester),
            "is_owner": C._is_guild_owner(guild, requester),
            "top_role": _role_summary(getattr(requester, "top_role", None)),
            "permissions": C.permission_names(getattr(requester, "guild_permissions", None)),
        }
    return data


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------


async def _create_category(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    name = C._require_name(arguments.get("name"))
    options: dict[str, Any] = {}
    if arguments.get("position") is not None:
        options["position"] = arguments["position"]
    if arguments.get("private_to_role_ids"):
        options["overwrites"] = C.private_overwrites(context, arguments["private_to_role_ids"])
    created = await guild.create_category(name, reason=_reason(arguments), **options)
    return _ok("create_category", f"Created category {created.name}.", {"category_id": _sid(created)})


async def _create_stage_channel(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    name = C._require_name(arguments.get("name"))
    options: dict[str, Any] = {}
    if arguments.get("category_id") is not None:
        options["category"] = C.resolve_category(context, arguments["category_id"])
    if arguments.get("position") is not None:
        options["position"] = arguments["position"]
    if arguments.get("private_to_role_ids"):
        options["overwrites"] = C.private_overwrites(context, arguments["private_to_role_ids"])
    created = await guild.create_stage_channel(name, reason=_reason(arguments), **options)
    return _ok("create_stage_channel", f"Created stage channel {created.name}.", {"channel_id": _sid(created)})


def _forum_tags(values: Any) -> list[Any]:
    tags = []
    for item in values or []:
        tags.append(
            discord.ForumTag(
                name=item["name"],
                emoji=item.get("emoji") or None,
                moderated=bool(item.get("moderated", False)),
            )
        )
    return tags


async def _create_forum_channel(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    name = C._require_name(arguments.get("name"))
    options: dict[str, Any] = {}
    if arguments.get("category_id") is not None:
        options["category"] = C.resolve_category(context, arguments["category_id"])
    for source, target in (("topic", "topic"), ("slowmode_seconds", "slowmode_delay"), ("position", "position")):
        if arguments.get(source) is not None:
            options[target] = arguments[source]
    if arguments.get("nsfw") is not None:
        options["nsfw"] = bool(arguments["nsfw"])
    if arguments.get("tags"):
        options["available_tags"] = _forum_tags(arguments["tags"])
    if arguments.get("private_to_role_ids"):
        options["overwrites"] = C.private_overwrites(context, arguments["private_to_role_ids"])
    created = await guild.create_forum(name, reason=_reason(arguments), **options)
    return _ok("create_forum_channel", f"Created forum #{created.name}.", {"channel_id": _sid(created)})


async def _edit_channel(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    kind = channel_type(channel)
    if kind.endswith("thread"):
        raise ValueError("Use edit_thread for threads.")
    options: dict[str, Any] = {}
    if arguments.get("name") is not None:
        options["name"] = C._require_name(arguments["name"])
    if arguments.get("position") is not None:
        options["position"] = arguments["position"]
    text_like = kind in ("text", "news", "forum", "media")
    voice_like = kind in ("voice", "stage_voice")
    if kind != "category":
        if arguments.get("category_id") is not None:
            options["category"] = C.resolve_category(context, arguments["category_id"])
        elif arguments.get("remove_from_category") is True:
            options["category"] = None
        if arguments.get("sync_permissions") is True:
            options["sync_permissions"] = True
        if arguments.get("nsfw") is not None:
            options["nsfw"] = bool(arguments["nsfw"])
    for field, target, allowed in (
        ("topic", "topic", text_like),
        ("slowmode_seconds", "slowmode_delay", text_like or voice_like),
        ("user_limit", "user_limit", voice_like),
        ("bitrate", "bitrate", voice_like),
    ):
        if arguments.get(field) is not None:
            if not allowed:
                raise ValueError(f"{field} does not apply to a {kind} channel.")
            options[target] = arguments[field]
    if not options:
        raise ValueError("Nothing to change.")
    await channel.edit(reason=_reason(arguments), **options)
    return _ok("edit_channel", f"Updated channel {channel.name} ({', '.join(sorted(options))}).")


async def _clone_channel(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if channel_type(channel) in ("category",) or channel_type(channel).endswith("thread"):
        raise ValueError("clone_channel works with text, voice, stage and forum channels.")
    name = C._require_name(arguments["name"]) if arguments.get("name") else None
    created = await channel.clone(name=name, reason=_reason(arguments))
    return _ok("clone_channel", f"Cloned {channel.name} as {created.name}.", {"channel_id": _sid(created)})


async def _delete_any_channel(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    kind = channel_type(channel)
    if kind == "category":
        raise ValueError("Use delete_category for categories.")
    name = channel.name
    await channel.delete(reason=_reason(arguments))
    return _ok("delete_any_channel", f"Deleted {kind} channel {name}.")


async def _delete_category(context: Any, arguments: dict[str, Any]) -> Any:
    category = C.resolve_category(context, arguments.get("category_id"))
    deleted_children = 0
    if arguments.get("delete_channels_inside") is True:
        children = list(getattr(category, "channels", []) or [])
        if len(children) > 50:
            raise ValueError("The category has more than 50 channels; delete them in smaller steps.")
        for child in children:
            await child.delete(reason=_reason(arguments))
            deleted_children += 1
    name = category.name
    await category.delete(reason=_reason(arguments))
    suffix = f" and {deleted_children} channel(s) inside it" if deleted_children else " (its channels were kept)"
    return _ok("delete_category", f"Deleted category {name}{suffix}.")


async def _set_channel_permissions(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if channel_type(channel).endswith("thread"):
        raise ValueError("Threads use their parent channel's permissions.")
    target_type = arguments["target_type"]
    if target_type == "everyone":
        target = guild.default_role
    elif target_type == "role":
        if not arguments.get("target_id"):
            raise ValueError("target_id is required for target_type role.")
        target = C._resolve_role(context, arguments["target_id"])
        if not (callable(getattr(target, "is_default", None)) and target.is_default()):
            C.ensure_role_manageable(context, target, action="change permissions for")
    else:
        if not arguments.get("target_id"):
            raise ValueError("target_id is required for target_type member.")
        target = await C._resolve_member(context, arguments["target_id"])
        C.ensure_member_actionable(context, target, action="change channel permissions for", allow_self=True)
    allow = C.parse_permission_names(arguments.get("allow"), "allow")
    deny = C.parse_permission_names(arguments.get("deny"), "deny")
    neutral = C.parse_permission_names(arguments.get("reset"), "reset")
    overlap = (set(allow) & set(deny)) | (set(allow) & set(neutral)) | (set(deny) & set(neutral))
    if overlap:
        raise ValueError(f"A permission appears in more than one list: {', '.join(sorted(overlap))}.")
    C.ensure_permissions_grantable(context, C.permissions_from_names(allow))
    if arguments.get("clear") is True:
        if allow or deny or neutral:
            raise ValueError("clear cannot be combined with allow/deny/reset.")
        await channel.set_permissions(target, overwrite=None, reason=_reason(arguments))
        return _ok("set_channel_permissions", f"Removed the permission overwrite for {target} in {channel.name}.")
    if not (allow or deny or neutral):
        raise ValueError("Provide allow, deny, reset or clear.")
    overwrite = channel.overwrites_for(target)
    updates = {**{name: True for name in allow}, **{name: False for name in deny}, **{name: None for name in neutral}}
    overwrite.update(**updates)
    await channel.set_permissions(target, overwrite=overwrite, reason=_reason(arguments))
    return _ok("set_channel_permissions", f"Updated permissions for {target} in {channel.name}.")


# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------


async def _edit_role(context: Any, arguments: dict[str, Any]) -> Any:
    role = C._resolve_role(context, arguments.get("role_id"))
    is_default = callable(getattr(role, "is_default", None)) and role.is_default()
    C.ensure_role_manageable(context, role, action="edit")
    options: dict[str, Any] = {}
    if arguments.get("name") is not None:
        if is_default:
            raise ValueError("@everyone cannot be renamed.")
        options["name"] = C._require_name(arguments["name"])
    if arguments.get("color") is not None:
        options["colour"] = C.parse_color(arguments["color"])
    for flag in ("hoist", "mentionable"):
        if arguments.get(flag) is not None:
            options[flag] = bool(arguments[flag])
    replace = arguments.get("permissions")
    add = C.parse_permission_names(arguments.get("add_permissions"), "add_permissions")
    remove = C.parse_permission_names(arguments.get("remove_permissions"), "remove_permissions")
    if replace is not None or add or remove:
        if replace is not None and (add or remove):
            raise ValueError("Use either permissions (replace all) or add_permissions/remove_permissions.")
        if set(add) & set(remove):
            raise ValueError("A permission cannot be both added and removed.")
        current = C._permissions_value(getattr(role, "permissions", None)) or 0
        if replace is not None:
            new_permissions = C.permissions_from_names(C.parse_permission_names(replace, "permissions"))
        else:
            new_permissions = discord.Permissions(current)
            new_permissions.update(**{name: True for name in add}, **{name: False for name in remove})
        # Only NEWLY granted bits must be grantable; keeping existing ones is fine.
        newly_granted = discord.Permissions(new_permissions.value & ~current)
        C.ensure_permissions_grantable(context, newly_granted)
        if new_permissions.administrator:
            raise C.AdminToolError("Granting Administrator is not allowed through tools; do it manually in Discord.")
        options["permissions"] = new_permissions
    if not options:
        raise ValueError("Nothing to change.")
    await role.edit(reason=_reason(arguments), **options)
    return _ok("edit_role", f"Updated role {role.name} ({', '.join(sorted(options))}).")


async def _set_role_position(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    role = C._resolve_role(context, arguments.get("role_id"))
    if callable(getattr(role, "is_default", None)) and role.is_default():
        raise ValueError("@everyone cannot be moved.")
    C.ensure_role_manageable(context, role, action="move")
    position = arguments["position"]
    bot_top = C._top_role_position(C._bot_member(guild))
    if bot_top is not None and position >= bot_top:
        raise C.AdminToolError("The target position is at or above the bot's highest role.")
    if context.enforce_hierarchy:
        requester = C.requester_member(context)
        if not C._is_guild_owner(guild, requester):
            requester_top = C._top_role_position(requester)
            if requester_top is None or position >= requester_top:
                raise C.AdminToolError("The target position is at or above your highest role.")
    await role.edit(position=position, reason=_reason(arguments))
    return _ok("set_role_position", f"Moved role {role.name} to position {position}.")


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------


async def _set_nickname(context: Any, arguments: dict[str, Any]) -> Any:
    member = await C._resolve_member(context, arguments.get("member_id"))
    C.ensure_member_actionable(context, member, action="rename", allow_self=True)
    nickname = arguments.get("nickname")
    nickname = nickname.strip() if isinstance(nickname, str) and nickname.strip() else None
    await member.edit(nick=nickname, reason=_reason(arguments))
    return _ok("set_nickname", f"{'Set' if nickname else 'Cleared'} nickname for {member}.")


async def _bulk_update_role(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    role = C._resolve_role(context, arguments.get("role_id"))
    C._ensure_assignable_role(context, role)
    action = arguments["action"]
    max_members = arguments["max_members"]
    filter_role = C._resolve_role(context, arguments["only_with_role_id"]) if arguments.get("only_with_role_id") else None
    include_bots = arguments.get("include_bots") is True
    candidates = []
    for member in list(getattr(guild, "members", []) or []):
        if getattr(member, "bot", False) and not include_bots:
            continue
        roles = list(getattr(member, "roles", []) or [])
        role_ids = {getattr(item, "id", None) for item in roles}
        if filter_role is not None and getattr(filter_role, "id", None) not in role_ids:
            continue
        has_role = getattr(role, "id", None) in role_ids
        if (action == "add" and has_role) or (action == "remove" and not has_role):
            continue
        candidates.append(member)
    if len(candidates) > max_members:
        raise C.AdminToolError(
            f"{len(candidates)} members match, more than max_members={max_members}; nothing was changed. "
            "Raise max_members (up to 500) if this is intended."
        )
    changed = failed = 0
    for member in candidates:
        try:
            if action == "add":
                await member.add_roles(role, reason=_reason(arguments))
            else:
                await member.remove_roles(role, reason=_reason(arguments))
            changed += 1
        except discord.HTTPException:
            failed += 1
    verb = "Added" if action == "add" else "Removed"
    message = f"{verb} role {role.name} {'to' if action == 'add' else 'from'} {changed} member(s)."
    if failed:
        message += f" {failed} member(s) could not be changed."
    return C.ToolResult(failed == 0, "bulk_update_role", message, {"changed": changed, "failed": failed})


async def _move_member_voice(context: Any, arguments: dict[str, Any]) -> Any:
    member = await C._resolve_member(context, arguments.get("member_id"))
    C.ensure_member_actionable(context, member, action="move", allow_self=True)
    if getattr(member, "voice", None) is None:
        raise C.AdminToolError(f"{member} is not in a voice channel.")
    if arguments.get("channel_id"):
        target = resolve_any_channel(context, arguments["channel_id"])
        if channel_type(target) not in ("voice", "stage_voice"):
            raise ValueError("channel_id must be a voice or stage channel.")
        await member.move_to(target, reason=_reason(arguments))
        return _ok("move_member_voice", f"Moved {member} to {target.name}.")
    await member.move_to(None, reason=_reason(arguments))
    return _ok("move_member_voice", f"Disconnected {member} from voice.")


async def _set_member_voice_state(context: Any, arguments: dict[str, Any]) -> Any:
    member = await C._resolve_member(context, arguments.get("member_id"))
    C.ensure_member_actionable(context, member, action="server-mute or deafen")
    options = {key: bool(arguments[key]) for key in ("mute", "deafen") if arguments.get(key) is not None}
    if not options:
        raise ValueError("Provide mute and/or deafen.")
    await member.edit(reason=_reason(arguments), **options)
    return _ok("set_member_voice_state", f"Updated voice state of {member}: {options}.")


# --------------------------------------------------------------------------
# moderation
# --------------------------------------------------------------------------


async def _list_bans(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    limit = arguments.get("limit") or 50
    bans = []
    async for entry in guild.bans(limit=limit):
        user = getattr(entry, "user", None)
        bans.append({"user_id": _sid(user), "username": getattr(user, "name", None), "reason": getattr(entry, "reason", None)})
    return {"bans": bans}


async def _get_audit_log(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    options: dict[str, Any] = {"limit": arguments.get("limit") or 20}
    if arguments.get("user_id"):
        options["user"] = discord.Object(id=C.parse_snowflake(arguments["user_id"], "user_id"))
    if arguments.get("action"):
        action = getattr(discord.AuditLogAction, arguments["action"], None)
        if not isinstance(action, discord.AuditLogAction):
            raise ValueError("action must be a Discord audit log action name such as ban, kick, channel_create, role_update.")
        options["action"] = action
    entries = []
    async for entry in guild.audit_logs(**options):
        target = getattr(entry, "target", None)
        entries.append(
            {
                "id": _sid(entry),
                "action": _enum_name(getattr(entry, "action", None)),
                "user_id": _sid(getattr(entry, "user", None)),
                "user_name": getattr(getattr(entry, "user", None), "name", None),
                "target_id": _sid(target),
                "target": str(target)[:100] if target is not None else None,
                "reason": getattr(entry, "reason", None),
                "created_at": C._isoformat_or_none(getattr(entry, "created_at", None)),
            }
        )
    return {"entries": entries}


def _require_store(context: Any) -> Any:
    store = context.feature_store
    if store is None:
        raise C.AdminToolError("The bot feature store is unavailable in this context.")
    return store


async def _lockdown_server(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = _require_store(context)
    if store.get(guild.id, "lockdown") is not None:
        raise C.AdminToolError("A server lockdown is already active; use end_lockdown first.")
    everyone = guild.default_role
    previous = C._permissions_value(getattr(everyone, "permissions", None)) or 0
    new_permissions = discord.Permissions(previous)
    new_permissions.update(**{flag: False for flag in LOCKDOWN_FLAGS})
    store.set(
        guild.id,
        "lockdown",
        {"previous_permissions": previous, "flags": list(LOCKDOWN_FLAGS), "by": context.requesting_user_id},
    )
    try:
        await everyone.edit(permissions=new_permissions, reason=_reason(arguments))
    except Exception:
        store.set(guild.id, "lockdown", None)
        raise
    return _ok(
        "lockdown_server",
        "Server lockdown active: @everyone can no longer send messages, react, create threads or speak "
        "(channel-specific overrides still apply). Use end_lockdown to restore.",
    )


async def _end_lockdown(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = _require_store(context)
    record = store.get(guild.id, "lockdown")
    if not isinstance(record, dict):
        raise C.AdminToolError("No server lockdown is active.")
    previous = discord.Permissions(int(record.get("previous_permissions", 0)))
    everyone = guild.default_role
    current = discord.Permissions(C._permissions_value(getattr(everyone, "permissions", None)) or 0)
    current.update(**{flag: getattr(previous, flag) for flag in record.get("flags", LOCKDOWN_FLAGS) if flag in discord.Permissions.VALID_FLAGS})
    await everyone.edit(permissions=current, reason=_reason(arguments))
    store.set(guild.id, "lockdown", None)
    return _ok("end_lockdown", "Server lockdown ended; @everyone permissions restored.")


# --------------------------------------------------------------------------
# server settings
# --------------------------------------------------------------------------


async def _get_guild_settings(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)

    def channel_id(attr: str) -> Any:
        return _sid(getattr(guild, attr, None))

    return {
        "name": getattr(guild, "name", None),
        "description": getattr(guild, "description", None),
        "verification_level": _enum_name(getattr(guild, "verification_level", None)),
        "default_notifications": _enum_name(getattr(guild, "default_notifications", None)),
        "explicit_content_filter": _enum_name(getattr(guild, "explicit_content_filter", None)),
        "afk_channel_id": channel_id("afk_channel"),
        "afk_timeout": getattr(guild, "afk_timeout", None),
        "system_channel_id": channel_id("system_channel"),
        "rules_channel_id": channel_id("rules_channel"),
        "public_updates_channel_id": channel_id("public_updates_channel"),
        "preferred_locale": str(getattr(guild, "preferred_locale", "") or "") or None,
        "premium_tier": getattr(guild, "premium_tier", None),
        "features": sorted(str(item) for item in (getattr(guild, "features", None) or [])),
        "has_icon": getattr(guild, "icon", None) is not None,
        "has_banner": getattr(guild, "banner", None) is not None,
    }


def _text_channel_for(context: Any, value: Any, field: str) -> Any:
    channel = resolve_any_channel(context, value, field)
    if channel_type(channel) not in ("text", "news"):
        raise ValueError(f"{field} must be a text channel.")
    return channel


async def _edit_guild(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    options: dict[str, Any] = {}
    if arguments.get("name") is not None:
        options["name"] = C._require_name(arguments["name"])
    if arguments.get("description") is not None:
        options["description"] = arguments["description"]
    if arguments.get("verification_level") is not None:
        options["verification_level"] = getattr(discord.VerificationLevel, arguments["verification_level"])
    if arguments.get("default_notifications") is not None:
        options["default_notifications"] = getattr(discord.NotificationLevel, arguments["default_notifications"])
    if arguments.get("explicit_content_filter") is not None:
        options["explicit_content_filter"] = getattr(discord.ContentFilter, arguments["explicit_content_filter"])
    if arguments.get("afk_timeout_seconds") is not None:
        options["afk_timeout"] = arguments["afk_timeout_seconds"]
    if arguments.get("afk_channel_id") is not None:
        channel = resolve_any_channel(context, arguments["afk_channel_id"], "afk_channel_id")
        if channel_type(channel) != "voice":
            raise ValueError("afk_channel_id must be a voice channel.")
        options["afk_channel"] = channel
    for field, target in (
        ("system_channel_id", "system_channel"),
        ("rules_channel_id", "rules_channel"),
        ("public_updates_channel_id", "public_updates_channel"),
    ):
        if arguments.get(field) is not None:
            options[target] = _text_channel_for(context, arguments[field], field)
    if arguments.get("preferred_locale") is not None:
        try:
            options["preferred_locale"] = discord.Locale(arguments["preferred_locale"])
        except ValueError as exc:
            raise ValueError("preferred_locale must be a Discord locale such as ru, en-US, de.") from exc
    for field in arguments.get("clear_fields") or []:
        if field in options:
            raise ValueError(f"{field} cannot be both set and cleared.")
        options[field] = None
    if not options:
        raise ValueError("Nothing to change.")
    await guild.edit(reason=_reason(arguments), **options)
    return _ok("edit_guild", f"Updated server settings: {', '.join(sorted(options))}.")


MAX_GUILD_IMAGE_BYTES = 8 * 1024 * 1024


async def _set_guild_image(context: Any, arguments: dict[str, Any], *, field: str, tool: str) -> Any:
    guild = C._require_guild(context)
    if arguments.get("remove") is True:
        if arguments.get("attachment_id"):
            raise ValueError("Use either attachment_id or remove.")
        await guild.edit(reason=_reason(arguments), **{field: None})
        return _ok(tool, f"Removed the server {field}.")
    if not arguments.get("attachment_id"):
        raise ValueError("attachment_id (an image attached to this request) or remove is required.")
    data = await C.read_request_attachment(
        context, arguments["attachment_id"], max_bytes=MAX_GUILD_IMAGE_BYTES, label=f"Server {field}"
    )
    await guild.edit(reason=_reason(arguments), **{field: data})
    return _ok(tool, f"Updated the server {field}.")


async def _set_guild_icon(context: Any, arguments: dict[str, Any]) -> Any:
    return await _set_guild_image(context, arguments, field="icon", tool="set_guild_icon")


async def _set_guild_banner(context: Any, arguments: dict[str, Any]) -> Any:
    return await _set_guild_image(context, arguments, field="banner", tool="set_guild_banner")


async def _edit_welcome_screen(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    options: dict[str, Any] = {}
    if arguments.get("enabled") is not None:
        options["enabled"] = bool(arguments["enabled"])
    if arguments.get("description") is not None:
        options["description"] = arguments["description"]
    if arguments.get("channels") is not None:
        welcome_channels = []
        for item in arguments["channels"]:
            channel = resolve_any_channel(context, item["channel_id"])
            welcome_channels.append(
                discord.WelcomeChannel(channel=channel, description=item["description"], emoji=item.get("emoji") or None)
            )
        options["welcome_channels"] = welcome_channels
    if not options:
        raise ValueError("Nothing to change.")
    await guild.edit_welcome_screen(reason=_reason(arguments), **options)
    return _ok("edit_welcome_screen", "Updated the server welcome screen.")


async def _edit_onboarding(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    options: dict[str, Any] = {}
    if arguments.get("enabled") is not None:
        options["enabled"] = bool(arguments["enabled"])
    if arguments.get("mode") is not None:
        options["mode"] = getattr(discord.OnboardingMode, arguments["mode"])
    if arguments.get("default_channel_ids") is not None:
        options["default_channels"] = [
            resolve_any_channel(context, value, "default_channel_ids") for value in arguments["default_channel_ids"]
        ]
    if arguments.get("prompts") is not None:
        prompts = []
        for prompt in arguments["prompts"]:
            prompt_options = []
            for option in prompt["options"]:
                roles = []
                for role_id in option.get("role_ids") or []:
                    role = C._resolve_role(context, role_id)
                    ensure_self_assignable_role(context, role)
                    roles.append(role)
                channels = [resolve_any_channel(context, value) for value in option.get("channel_ids") or []]
                if not roles and not channels:
                    raise ValueError(f"Onboarding option {option['title']!r} needs at least one role or channel.")
                kwargs: dict[str, Any] = {
                    "title": option["title"],
                    "description": option.get("description") or None,
                    "roles": roles,
                    "channels": channels,
                }
                if option.get("emoji"):
                    kwargs["emoji"] = option["emoji"]
                prompt_options.append(discord.OnboardingPromptOption(**kwargs))
            prompts.append(
                discord.OnboardingPrompt(
                    type=getattr(discord.OnboardingPromptType, prompt.get("type") or "multiple_choice"),
                    title=prompt["title"],
                    options=prompt_options,
                    single_select=bool(prompt.get("single_select", False)),
                    required=bool(prompt.get("required", False)),
                    in_onboarding=bool(prompt.get("in_onboarding", True)),
                )
            )
        options["prompts"] = prompts
    if not options:
        raise ValueError("Nothing to change.")
    reason = _reason(arguments)
    if reason is not None:
        options["reason"] = reason
    await guild.edit_onboarding(**options)
    return _ok("edit_onboarding", "Updated server onboarding.")


# --------------------------------------------------------------------------
# AutoMod
# --------------------------------------------------------------------------


def _serialize_automod_rule(rule: Any) -> dict[str, Any]:
    trigger = getattr(rule, "trigger", None)
    return {
        "id": _sid(rule),
        "name": getattr(rule, "name", None),
        "enabled": getattr(rule, "enabled", None),
        "trigger_type": _enum_name(getattr(trigger, "type", None)),
        "keywords": list(getattr(trigger, "keyword_filter", None) or [])[:50],
        "allow_list": list(getattr(trigger, "allow_list", None) or [])[:50],
        "mention_limit": getattr(trigger, "mention_limit", None),
        "actions": [_enum_name(getattr(action, "type", None)) for action in getattr(rule, "actions", None) or []],
        "exempt_role_ids": [str(value) for value in getattr(rule, "exempt_role_ids", None) or []],
        "exempt_channel_ids": [str(value) for value in getattr(rule, "exempt_channel_ids", None) or []],
    }


async def _list_automod_rules(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    rules = await guild.fetch_automod_rules()
    return {"rules": [_serialize_automod_rule(rule) for rule in rules]}


def _automod_actions(context: Any, arguments: dict[str, Any], trigger_type: str) -> list[Any]:
    actions = []
    if arguments.get("block_message", True) is not False:
        text = arguments.get("block_message_text") or None
        actions.append(
            discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message, custom_message=text)
        )
    if arguments.get("alert_channel_id"):
        channel = _text_channel_for(context, arguments["alert_channel_id"], "alert_channel_id")
        actions.append(discord.AutoModRuleAction(type=discord.AutoModRuleActionType.send_alert_message, channel_id=channel.id))
    if arguments.get("timeout_seconds"):
        if trigger_type not in ("keyword", "mention_spam"):
            raise ValueError("timeout_seconds works only with keyword or mention_spam rules.")
        actions.append(
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.timeout, duration=timedelta(seconds=arguments["timeout_seconds"])
            )
        )
    if not actions:
        raise ValueError("The rule needs at least one action (block_message, alert_channel_id or timeout_seconds).")
    return actions


def _automod_trigger(arguments: dict[str, Any], trigger_type: str) -> Any:
    if trigger_type == "keyword":
        keywords = arguments.get("keywords") or []
        patterns = arguments.get("regex_patterns") or []
        if not keywords and not patterns:
            raise ValueError("A keyword rule needs keywords or regex_patterns.")
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=keywords,
            regex_patterns=patterns,
            allow_list=arguments.get("allow_list") or [],
        )
    if trigger_type == "keyword_preset":
        presets = arguments.get("presets") or []
        if not presets:
            raise ValueError("A keyword_preset rule needs presets.")
        flags = discord.AutoModPresets(**{name: True for name in presets})
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword_preset, presets=flags, allow_list=arguments.get("allow_list") or []
        )
    if trigger_type == "mention_spam":
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam,
            mention_limit=arguments.get("mention_limit") or 5,
            mention_raid_protection=bool(arguments.get("mention_raid_protection", True)),
        )
    return discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)


def _resolve_ids(context: Any, values: Any, resolver: Any) -> list[Any]:
    return [resolver(context, value) for value in values or []]


async def _create_automod_rule(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    trigger_type = arguments["trigger"]
    rule = await guild.create_automod_rule(
        name=arguments["name"],
        event_type=discord.AutoModRuleEventType.message_send,
        trigger=_automod_trigger(arguments, trigger_type),
        actions=_automod_actions(context, arguments, trigger_type),
        enabled=arguments.get("enabled", True) is not False,
        exempt_roles=_resolve_ids(context, arguments.get("exempt_role_ids"), C._resolve_role),
        exempt_channels=_resolve_ids(context, arguments.get("exempt_channel_ids"), resolve_any_channel),
        reason=_reason(arguments) or "AutoMod rule via Admin bot",
    )
    return _ok("create_automod_rule", f"Created AutoMod rule {rule.name}.", {"rule_id": _sid(rule)})


async def _find_automod_rule(context: Any, value: Any) -> Any:
    guild = C._require_guild(context)
    rule_id = C.parse_snowflake(value, "rule_id")
    for rule in await guild.fetch_automod_rules():
        if getattr(rule, "id", None) == rule_id:
            return rule
    raise C.AdminToolError(f"AutoMod rule {rule_id} was not found.")


async def _edit_automod_rule(context: Any, arguments: dict[str, Any]) -> Any:
    rule = await _find_automod_rule(context, arguments.get("rule_id"))
    options: dict[str, Any] = {}
    if arguments.get("name") is not None:
        options["name"] = arguments["name"]
    if arguments.get("enabled") is not None:
        options["enabled"] = bool(arguments["enabled"])
    if arguments.get("exempt_role_ids") is not None:
        options["exempt_roles"] = _resolve_ids(context, arguments["exempt_role_ids"], C._resolve_role)
    if arguments.get("exempt_channel_ids") is not None:
        options["exempt_channels"] = _resolve_ids(context, arguments["exempt_channel_ids"], resolve_any_channel)
    if any(arguments.get(key) is not None for key in ("keywords", "allow_list", "regex_patterns")):
        trigger = getattr(rule, "trigger", None)
        if _enum_name(getattr(trigger, "type", None)) != "keyword":
            raise ValueError("keywords/allow_list/regex_patterns can only be changed on keyword rules.")
        options["trigger"] = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=arguments["keywords"] if arguments.get("keywords") is not None else list(trigger.keyword_filter),
            regex_patterns=arguments["regex_patterns"] if arguments.get("regex_patterns") is not None else list(trigger.regex_patterns),
            allow_list=arguments["allow_list"] if arguments.get("allow_list") is not None else list(trigger.allow_list),
        )
    if not options:
        raise ValueError("Nothing to change.")
    await rule.edit(reason=_reason(arguments) or "AutoMod rule via Admin bot", **options)
    return _ok("edit_automod_rule", f"Updated AutoMod rule {rule.name}.")


async def _delete_automod_rule(context: Any, arguments: dict[str, Any]) -> Any:
    rule = await _find_automod_rule(context, arguments.get("rule_id"))
    name = rule.name
    await rule.delete(reason=_reason(arguments) or "AutoMod rule via Admin bot")
    return _ok("delete_automod_rule", f"Deleted AutoMod rule {name}.")


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def build_tools(core: Any) -> list[tuple[Any, Any]]:
    global C
    C = core
    T = core.ToolDefinition
    R = core.REASON_PROPERTY
    name_property = core._name_property
    private = core.PRIVATE_ROLE_IDS_PROPERTY
    position = _int("Position (0 = top of its list).", minimum=0, maximum=500)
    tag_item = {
        "type": "object",
        "properties": {
            "name": _str("Tag name.", max_length=20),
            "emoji": _str("Optional unicode emoji.", max_length=32),
            "moderated": _bool("Only moderators can apply it."),
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    automod_common = {
        "allow_list": _strings("Words/phrases that never trigger the rule.", max_items=100, max_length=60),
        "exempt_role_ids": _ids("IDs of roles the rule ignores.", max_items=20),
        "exempt_channel_ids": _ids("Channels the rule ignores.", max_items=50),
    }
    tools = [
        # core (read)
        (T("list_members", "read", "read", "List members, optionally only those with a role or matching a name.", _schema({
            "role_id": _sf("Only members with this role."),
            "name_contains": _str("Case-insensitive part of the username or display name.", max_length=100),
            "include_bots": _bool("Include bots (default true)."),
            "sort": _enum(("name", "joined_newest", "joined_oldest"), "Sort order (default name)."),
            "limit": _int(f"Maximum members to return, up to {MAX_LIST_MEMBERS} (default 50).", maximum=MAX_LIST_MEMBERS),
        }), "core"), _list_members),
        (T("get_access_context", "read", "read", "Bot and requester role positions and permissions; what can be managed.", _schema(), "core"), _get_access_context),
        # channels
        (T("create_category", "write", "normal", "Create a category, optionally private to some roles.", _schema({
            "name": name_property("Category name."), "position": position, "private_to_role_ids": private, "reason": R,
        }, ["name"]), "channels"), _create_category),
        (T("create_stage_channel", "write", "normal", "Create a stage channel.", _schema({
            "name": name_property("Channel name."), "category_id": _sf("Parent category ID."), "position": position,
            "private_to_role_ids": private, "reason": R,
        }, ["name"]), "channels"), _create_stage_channel),
        (T("create_forum_channel", "write", "normal", "Create a forum channel with optional tags.", _schema({
            "name": name_property("Forum name."), "category_id": _sf("Parent category ID."),
            "topic": _str("Guidelines shown in the forum.", min_length=0, max_length=4096),
            "slowmode_seconds": _int("Post slowmode in seconds.", minimum=0, maximum=21600),
            "nsfw": _bool("Age-restricted."), "position": position,
            "tags": {"type": "array", "items": tag_item, "maxItems": 20, "description": "Available post tags."},
            "private_to_role_ids": private, "reason": R,
        }, ["name"]), "channels"), _create_forum_channel),
        (T("edit_channel", "write", "normal", "Edit any channel or category: name, topic, slowmode, NSFW, category, position, voice limits, permission sync.", _schema({
            "channel_id": core.CHANNEL_ID_PROPERTY,
            "name": name_property("New name."),
            "topic": _str("New topic (text/forum).", min_length=0, max_length=1024),
            "slowmode_seconds": _int("Slowmode in seconds.", minimum=0, maximum=21600),
            "nsfw": _bool("Age-restricted."),
            "category_id": _sf("Move into this category."),
            "remove_from_category": _bool("Move out of its category."),
            "position": position,
            "user_limit": _int("Voice user limit (0 = unlimited).", minimum=0, maximum=99),
            "bitrate": _int("Voice bitrate in bits/s.", minimum=8000, maximum=384000),
            "sync_permissions": _bool("Reset permissions to match the category."),
            "reason": R,
        }, ["channel_id"]), "channels"), _edit_channel),
        (T("clone_channel", "write", "normal", "Copy a channel with its settings and permissions.", _schema({
            "channel_id": core.CHANNEL_ID_PROPERTY, "name": name_property("Name of the copy."), "reason": R,
        }, ["channel_id"]), "channels"), _clone_channel),
        (T("delete_any_channel", "write", "destructive", "Delete any non-category channel (text, voice, stage, forum, announcement, thread).", _schema({
            "channel_id": core.CHANNEL_ID_PROPERTY, "reason": R,
        }, ["channel_id"]), "channels"), _delete_any_channel),
        (T("delete_category", "write", "destructive", "Delete a category; optionally also every channel inside it.", _schema({
            "category_id": _sf("Category ID."),
            "delete_channels_inside": _bool("Also delete the channels inside (default false: they are kept)."),
            "reason": R,
        }, ["category_id"]), "channels"), _delete_category),
        (T("set_channel_permissions", "write", "normal", "Set allow/deny/reset permission overwrites for @everyone, a role or a member in a channel or category.", _schema({
            "channel_id": core.CHANNEL_ID_PROPERTY,
            "target_type": _enum(("everyone", "role", "member"), "Who the overwrite applies to."),
            "target_id": _sf("ID of the role or member (not needed for everyone)."),
            "allow": _perm_list("Permissions to explicitly allow."),
            "deny": _perm_list("Permissions to explicitly deny."),
            "reset": _perm_list("Permissions to reset to inherit."),
            "clear": _bool("Remove this target's overwrite completely."),
            "reason": R,
        }, ["channel_id", "target_type"]), "channels"), _set_channel_permissions),
        # roles
        (T("edit_role", "write", "destructive", "Edit a role (also @everyone): name, color, hoist, mentionable, permissions.", _schema({
            "role_id": core.ROLE_ID_PROPERTY,
            "name": name_property("New name."),
            "color": core.COLOR_PROPERTY,
            "hoist": _bool("Show separately in the member list."),
            "mentionable": _bool("Anyone can mention it."),
            "permissions": _perm_list("Replace ALL permissions with exactly this list."),
            "add_permissions": _perm_list("Permissions to add."),
            "remove_permissions": _perm_list("Permissions to remove."),
            "reason": R,
        }, ["role_id"]), "roles"), _edit_role),
        (T("set_role_position", "write", "normal", "Move a role in the hierarchy (higher number = higher).", _schema({
            "role_id": core.ROLE_ID_PROPERTY, "position": _int("New position (1 = just above @everyone).", minimum=1, maximum=250), "reason": R,
        }, ["role_id", "position"]), "roles"), _set_role_position),
        # members
        (T("set_nickname", "write", "normal", "Set or clear a member's server nickname.", _schema({
            "member_id": core.MEMBER_ID_PROPERTY,
            "nickname": {"type": ["string", "null"], "maxLength": 32, "description": "New nickname; empty or null clears it."},
            "reason": R,
        }, ["member_id"]), "members"), _set_nickname),
        (T("bulk_update_role", "write", "destructive", f"Add or remove a role for many members at once (max {MAX_BULK_MEMBERS}).", _schema({
            "role_id": core.ROLE_ID_PROPERTY,
            "action": _enum(("add", "remove"), "Add or remove the role."),
            "only_with_role_id": _sf("Only members who have this other role."),
            "include_bots": _bool("Also change bots (default false)."),
            "max_members": _int("Safety cap: refuse if more members would change.", maximum=MAX_BULK_MEMBERS),
            "reason": R,
        }, ["role_id", "action", "max_members"]), "members"), _bulk_update_role),
        (T("move_member_voice", "write", "normal", "Move a member to another voice channel, or disconnect them if channel_id is omitted.", _schema({
            "member_id": core.MEMBER_ID_PROPERTY, "channel_id": _sf("Target voice/stage channel; omit to disconnect."), "reason": R,
        }, ["member_id"]), "members"), _move_member_voice),
        (T("set_member_voice_state", "write", "normal", "Server-mute and/or server-deafen a member.", _schema({
            "member_id": core.MEMBER_ID_PROPERTY, "mute": _bool("Server mute."), "deafen": _bool("Server deafen."), "reason": R,
        }, ["member_id"]), "members"), _set_member_voice_state),
        # moderation
        (T("list_bans", "read", "read", "List banned users.", _schema({
            "limit": _int(f"Maximum entries, up to {MAX_BANS} (default 50).", maximum=MAX_BANS),
        }), "moderation"), _list_bans),
        (T("get_audit_log", "read", "read", "Read recent audit log entries, optionally by user or action.", _schema({
            "limit": _int(f"Maximum entries, up to {MAX_AUDIT_ENTRIES} (default 20).", maximum=MAX_AUDIT_ENTRIES),
            "user_id": _sf("Only actions by this user."),
            "action": _str("Audit action name, e.g. ban, kick, member_role_update, channel_create, message_delete.", max_length=40),
        }), "moderation"), _get_audit_log),
        (T("lockdown_server", "write", "destructive", "Emergency lockdown: @everyone can no longer send messages, react, create threads or speak.", _schema({"reason": R}), "moderation"), _lockdown_server),
        (T("end_lockdown", "write", "normal", "End the server lockdown and restore @everyone permissions.", _schema({"reason": R}), "moderation"), _end_lockdown),
        # server
        (T("get_guild_settings", "read", "read", "Read server settings (verification, notifications, system/rules/AFK channels, features).", _schema(), "server"), _get_guild_settings),
        (T("edit_guild", "write", "destructive", "Change server settings: name, description, verification, notifications, content filter, AFK, system/rules/updates channels, locale.", _schema({
            "name": name_property("Server name."),
            "description": _str("Server description (Community).", min_length=0, max_length=120),
            "verification_level": _enum(VERIFICATION_LEVELS, "Verification level."),
            "default_notifications": _enum(NOTIFICATION_LEVELS, "Default notification setting."),
            "explicit_content_filter": _enum(CONTENT_FILTERS, "Explicit media content filter."),
            "afk_channel_id": _sf("AFK voice channel."),
            "afk_timeout_seconds": _int_enum(AFK_TIMEOUTS, "AFK timeout in seconds."),
            "system_channel_id": _sf("Channel for join/boost system messages."),
            "rules_channel_id": _sf("Rules channel (Community)."),
            "public_updates_channel_id": _sf("Community updates channel."),
            "preferred_locale": _str("Locale such as ru or en-US.", max_length=10),
            "clear_fields": {"type": "array", "items": _enum(GUILD_CLEARABLE_FIELDS, "Field to clear."), "maxItems": 5, "uniqueItems": True, "description": "Settings to unset."},
            "reason": R,
        }), "server"), _edit_guild),
        (T("set_guild_icon", "write", "normal", "Set the server icon from an image attached to the request, or remove it.", _schema({
            "attachment_id": _sf("ID of an image attached to this request."), "remove": _bool("Remove the icon."), "reason": R,
        }), "server"), _set_guild_icon),
        (T("set_guild_banner", "write", "normal", "Set the server banner (boost level 2) from an attached image, or remove it.", _schema({
            "attachment_id": _sf("ID of an image attached to this request."), "remove": _bool("Remove the banner."), "reason": R,
        }), "server"), _set_guild_banner),
        (T("edit_welcome_screen", "write", "normal", "Edit the Community welcome screen (description and up to 5 recommended channels).", _schema({
            "enabled": _bool("Show the welcome screen."),
            "description": _str("Welcome text.", min_length=0, max_length=140),
            "channels": {"type": "array", "maxItems": 5, "description": "Recommended channels.", "items": {
                "type": "object", "additionalProperties": False, "required": ["channel_id", "description"],
                "properties": {
                    "channel_id": _sf("Channel ID."),
                    "description": _str("Short description.", max_length=42),
                    "emoji": _str("Optional unicode emoji.", max_length=32),
                },
            }},
            "reason": R,
        }), "server"), _edit_welcome_screen),
        (T("edit_onboarding", "write", "destructive", "Configure Community onboarding: default channels and question prompts that give roles/channels (replaces prompts).", _schema({
            "enabled": _bool("Enable onboarding."),
            "mode": _enum(("default", "advanced"), "Onboarding mode."),
            "default_channel_ids": _ids("Channels every new member sees.", max_items=50),
            "prompts": {"type": "array", "maxItems": 15, "description": "Questions (replaces existing).", "items": {
                "type": "object", "additionalProperties": False, "required": ["title", "options"],
                "properties": {
                    "title": _str("Question.", max_length=100),
                    "type": _enum(("multiple_choice", "dropdown"), "Display type."),
                    "single_select": _bool("Only one answer."),
                    "required": _bool("Answer required."),
                    "in_onboarding": _bool("Shown during onboarding (default true)."),
                    "options": {"type": "array", "minItems": 1, "maxItems": 25, "items": {
                        "type": "object", "additionalProperties": False, "required": ["title"],
                        "properties": {
                            "title": _str("Answer title.", max_length=50),
                            "description": _str("Answer description.", min_length=0, max_length=100),
                            "emoji": _str("Optional unicode emoji.", max_length=32),
                            "role_ids": _ids("IDs of roles given for this answer (no moderator permissions).", max_items=10),
                            "channel_ids": _ids("Channels shown for this answer.", max_items=25),
                        },
                    }},
                },
            }},
            "reason": R,
        }), "server"), _edit_onboarding),
        # automod
        (T("list_automod_rules", "read", "read", "List AutoMod rules.", _schema(), "automod"), _list_automod_rules),
        (T("create_automod_rule", "write", "normal", "Create an AutoMod rule (keyword, keyword_preset, spam, mention_spam) with block/alert/timeout actions.", _schema({
            "name": _str("Rule name.", max_length=100),
            "trigger": _enum(("keyword", "keyword_preset", "spam", "mention_spam"), "Trigger type."),
            "keywords": _strings("Blocked words; * wildcards allowed (keyword rules).", max_items=MAX_AUTOMOD_KEYWORDS, max_length=60),
            "regex_patterns": _strings("Rust-flavored regex patterns (keyword rules).", max_items=10, max_length=260),
            "presets": {"type": "array", "items": _enum(("profanity", "sexual_content", "slurs"), "Preset."), "maxItems": 3, "uniqueItems": True, "description": "Word presets (keyword_preset rules)."},
            "mention_limit": _int("Max unique mentions per message (mention_spam).", maximum=50),
            "block_message": _bool("Block the message (default true)."),
            "block_message_text": _str("Custom text shown to the author when blocked.", max_length=150),
            "alert_channel_id": _sf("Send an alert to this channel."),
            "timeout_seconds": _int("Also time out the author (keyword/mention_spam).", maximum=2419200),
            "enabled": _bool("Enable now (default true)."),
            **automod_common,
            "reason": R,
        }, ["name", "trigger"]), "automod"), _create_automod_rule),
        (T("edit_automod_rule", "write", "normal", "Enable/disable or edit an AutoMod rule (name, exemptions, keyword lists).", _schema({
            "rule_id": _sf("Rule ID."),
            "name": _str("New name.", max_length=100),
            "enabled": _bool("Enable or disable."),
            "keywords": _strings("Replace blocked words.", max_items=MAX_AUTOMOD_KEYWORDS, max_length=60),
            "regex_patterns": _strings("Replace regex patterns.", max_items=10, max_length=260),
            **automod_common,
            "reason": R,
        }, ["rule_id"]), "automod"), _edit_automod_rule),
        (T("delete_automod_rule", "write", "destructive", "Delete an AutoMod rule.", _schema({"rule_id": _sf("Rule ID."), "reason": R}, ["rule_id"]), "automod"), _delete_automod_rule),
    ]
    return tools
