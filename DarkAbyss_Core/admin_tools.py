from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

import discord

ToolKind = Literal["read", "write"]
ToolRisk = Literal["read", "normal", "destructive"]

MAX_RECENT_MESSAGES = 50
DEFAULT_RECENT_MESSAGES = 10
MAX_DISCORD_MESSAGE_LENGTH = 2000
MAX_DISCORD_NAME_LENGTH = 100
MAX_REASON_LENGTH = 512


class AdminToolError(Exception):
    """Raised when a registered admin tool cannot be executed safely."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    kind: ToolKind
    risk: ToolRisk
    description: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool_name: str
    message: str
    data: dict[str, Any] | list[Any] | None = None


@dataclass(frozen=True)
class AdminToolContext:
    guild: Any
    fetch_user: Callable[[int], Awaitable[Any]] | None = None
    source: str = "unknown"
    requesting_user_id: int | None = None
    requesting_user_name: str | None = None


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


def actor_is_explicitly_whitelisted(actor: Any, config: dict[str, Any]) -> bool:
    actor_id = getattr(actor, "id", None)
    if actor_id in set(config.get("allowed_user_ids", [])):
        return True

    allowed_roles = set(config.get("allowed_role_ids", []))
    return any(getattr(role, "id", None) in allowed_roles for role in getattr(actor, "roles", []))


def actor_has_access(actor: Any, config: dict[str, Any]) -> bool:
    if config.get("allow_server_administrators") and getattr(
        getattr(actor, "guild_permissions", None),
        "administrator",
        False,
    ):
        return True
    return actor_is_explicitly_whitelisted(actor, config)


def _object_schema(
    properties: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _snowflake_property(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "pattern": "^[1-9][0-9]*$",
        "description": description,
    }


def _string_property(
    description: str,
    *,
    min_length: int = 1,
    max_length: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "string",
        "minLength": min_length,
        "description": description,
    }
    if max_length is not None:
        schema["maxLength"] = max_length
    return schema


def _name_property(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_DISCORD_NAME_LENGTH,
        "description": description,
    }


def _nullable_string_property(description: str) -> dict[str, Any]:
    return {
        "type": ["string", "null"],
        "maxLength": MAX_REASON_LENGTH,
        "description": description,
    }


def _integer_property(description: str, *, minimum: int = 1, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "integer",
        "minimum": minimum,
        "description": description,
    }
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


REASON_PROPERTY = _nullable_string_property("Optional Discord audit-log reason.")
CHANNEL_ID_PROPERTY = _snowflake_property("Discord channel snowflake ID.")
MEMBER_ID_PROPERTY = _snowflake_property("Discord guild member snowflake ID.")
ROLE_ID_PROPERTY = _snowflake_property("Discord role snowflake ID.")
USER_ID_PROPERTY = _snowflake_property("Discord user snowflake ID.")


TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    "get_guild_summary": ToolDefinition(
        "get_guild_summary",
        "read",
        "read",
        "Return bounded structured facts about the selected Discord guild.",
        _object_schema(),
    ),
    "list_channels": ToolDefinition(
        "list_channels",
        "read",
        "read",
        "Return structured metadata for guild channels.",
        _object_schema(),
    ),
    "get_channel_details": ToolDefinition(
        "get_channel_details",
        "read",
        "read",
        "Return structured metadata for one channel.",
        _object_schema({"channel_id": CHANNEL_ID_PROPERTY}, ["channel_id"]),
    ),
    "list_roles": ToolDefinition(
        "list_roles",
        "read",
        "read",
        "Return structured metadata for guild roles.",
        _object_schema(),
    ),
    "get_role_details": ToolDefinition(
        "get_role_details",
        "read",
        "read",
        "Return structured metadata for one role.",
        _object_schema({"role_id": ROLE_ID_PROPERTY}, ["role_id"]),
    ),
    "get_member_details": ToolDefinition(
        "get_member_details",
        "read",
        "read",
        "Return structured metadata for one member.",
        _object_schema({"member_id": MEMBER_ID_PROPERTY}, ["member_id"]),
    ),
    "get_recent_messages": ToolDefinition(
        "get_recent_messages",
        "read",
        "read",
        "Return bounded recent message metadata without downloading attachments.",
        _object_schema(
            {
                "channel_id": CHANNEL_ID_PROPERTY,
                "limit": _integer_property(
                    f"Maximum messages to return, up to {MAX_RECENT_MESSAGES}.",
                    maximum=MAX_RECENT_MESSAGES,
                ),
            },
            ["channel_id"],
        ),
    ),
    "send_message": ToolDefinition(
        "send_message",
        "write",
        "normal",
        "Send a message to a text channel.",
        _object_schema(
            {
                "channel_id": CHANNEL_ID_PROPERTY,
                "content": _string_property(
                    "Message content to send.",
                    max_length=MAX_DISCORD_MESSAGE_LENGTH,
                ),
                "reason": REASON_PROPERTY,
            },
            ["channel_id", "content"],
        ),
    ),
    "purge_messages": ToolDefinition(
        "purge_messages",
        "write",
        "destructive",
        "Delete recent messages from a text channel.",
        _object_schema(
            {
                "channel_id": CHANNEL_ID_PROPERTY,
                "count": _integer_property("Message count to purge.", maximum=100),
                "reason": REASON_PROPERTY,
            },
            ["channel_id"],
        ),
    ),
    "timeout_member": ToolDefinition(
        "timeout_member",
        "write",
        "normal",
        "Apply a timeout to a guild member.",
        _object_schema(
            {
                "member_id": MEMBER_ID_PROPERTY,
                "duration_minutes": _integer_property("Timeout duration in minutes.", maximum=40320),
                "reason": REASON_PROPERTY,
            },
            ["member_id"],
        ),
    ),
    "clear_timeout": ToolDefinition(
        "clear_timeout",
        "write",
        "normal",
        "Clear a timeout from a guild member.",
        _object_schema({"member_id": MEMBER_ID_PROPERTY, "reason": REASON_PROPERTY}, ["member_id"]),
    ),
    "kick_member": ToolDefinition(
        "kick_member",
        "write",
        "destructive",
        "Kick a member from the guild.",
        _object_schema({"member_id": MEMBER_ID_PROPERTY, "reason": REASON_PROPERTY}, ["member_id"]),
    ),
    "ban_member": ToolDefinition(
        "ban_member",
        "write",
        "destructive",
        "Ban a member from the guild.",
        _object_schema({"member_id": MEMBER_ID_PROPERTY, "reason": REASON_PROPERTY}, ["member_id"]),
    ),
    "unban_user": ToolDefinition(
        "unban_user",
        "write",
        "destructive",
        "Unban a user by numeric Discord ID.",
        _object_schema({"user_id": USER_ID_PROPERTY, "reason": REASON_PROPERTY}, ["user_id"]),
    ),
    "add_role": ToolDefinition(
        "add_role",
        "write",
        "normal",
        "Add a role to a guild member.",
        _object_schema(
            {"member_id": MEMBER_ID_PROPERTY, "role_id": ROLE_ID_PROPERTY, "reason": REASON_PROPERTY},
            ["member_id", "role_id"],
        ),
    ),
    "remove_role": ToolDefinition(
        "remove_role",
        "write",
        "normal",
        "Remove a role from a guild member.",
        _object_schema(
            {"member_id": MEMBER_ID_PROPERTY, "role_id": ROLE_ID_PROPERTY, "reason": REASON_PROPERTY},
            ["member_id", "role_id"],
        ),
    ),
    "create_text_channel": ToolDefinition(
        "create_text_channel",
        "write",
        "normal",
        "Create a text channel.",
        _object_schema({"name": _name_property("New text channel name."), "reason": REASON_PROPERTY}, ["name"]),
    ),
    "create_voice_channel": ToolDefinition(
        "create_voice_channel",
        "write",
        "normal",
        "Create a voice channel.",
        _object_schema({"name": _name_property("New voice channel name."), "reason": REASON_PROPERTY}, ["name"]),
    ),
    "rename_channel": ToolDefinition(
        "rename_channel",
        "write",
        "normal",
        "Rename a text or voice channel.",
        _object_schema(
            {
                "channel_id": CHANNEL_ID_PROPERTY,
                "name": _name_property("New channel name."),
                "reason": REASON_PROPERTY,
            },
            ["channel_id", "name"],
        ),
    ),
    "delete_channel": ToolDefinition(
        "delete_channel",
        "write",
        "destructive",
        "Delete a text or voice channel.",
        _object_schema({"channel_id": CHANNEL_ID_PROPERTY, "reason": REASON_PROPERTY}, ["channel_id"]),
    ),
    "create_role": ToolDefinition(
        "create_role",
        "write",
        "normal",
        "Create a role.",
        _object_schema({"name": _name_property("New role name."), "reason": REASON_PROPERTY}, ["name"]),
    ),
    "delete_role": ToolDefinition(
        "delete_role",
        "write",
        "destructive",
        "Delete a role.",
        _object_schema({"role_id": ROLE_ID_PROPERTY, "reason": REASON_PROPERTY}, ["role_id"]),
    ),
    "lock_channel": ToolDefinition(
        "lock_channel",
        "write",
        "normal",
        "Deny @everyone send-message permission in a text channel.",
        _object_schema({"channel_id": CHANNEL_ID_PROPERTY, "reason": REASON_PROPERTY}, ["channel_id"]),
    ),
    "unlock_channel": ToolDefinition(
        "unlock_channel",
        "write",
        "normal",
        "Clear @everyone send-message override in a text channel.",
        _object_schema({"channel_id": CHANNEL_ID_PROPERTY, "reason": REASON_PROPERTY}, ["channel_id"]),
    ),
}

EXECUTE_TOOL_NAMES = tuple(
    name for name, definition in TOOL_DEFINITIONS.items() if definition.kind == "write"
)


def get_tool_definition(name: str) -> ToolDefinition:
    try:
        return TOOL_DEFINITIONS[name]
    except KeyError as exc:
        raise AdminToolError(f"Unknown admin tool: {name}") from exc


def list_tool_definitions() -> tuple[ToolDefinition, ...]:
    return tuple(TOOL_DEFINITIONS[name] for name in sorted(TOOL_DEFINITIONS))


async def execute_tool(
    context: AdminToolContext,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> ToolResult:
    try:
        definition = get_tool_definition(tool_name)
        safe_arguments = _validate_tool_arguments(definition, arguments)
        handler = _TOOL_HANDLERS[tool_name]
        data = await handler(context, safe_arguments)
    except discord.Forbidden:
        return ToolResult(False, tool_name, "Discord refused the action. Check the bot role position and permissions.")
    except discord.HTTPException as exc:
        return ToolResult(False, tool_name, f"Discord API error: {exc}")
    except (AdminToolError, ValueError) as exc:
        return ToolResult(False, tool_name, str(exc))
    except Exception as exc:
        return ToolResult(False, tool_name, f"Unexpected error: {type(exc).__name__}.")

    if isinstance(data, ToolResult):
        return data
    return ToolResult(True, tool_name, _default_success_message(tool_name), data)


def _validate_tool_arguments(definition: ToolDefinition, arguments: dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise AdminToolError("Tool arguments must be a plain mapping.")
    if not _is_json_compatible(arguments):
        raise AdminToolError("Tool arguments must contain only JSON-compatible values.")

    schema = definition.arguments
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_properties = schema.get("additionalProperties", True)
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise AdminToolError(f"Invalid internal schema for {definition.name}.")

    if additional_properties is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise AdminToolError(f"Unknown argument for {definition.name}: {unknown[0]}")

    for field_name in required:
        if field_name not in arguments:
            raise AdminToolError(f"Missing required argument for {definition.name}: {field_name}")

    for key, value in arguments.items():
        if not isinstance(key, str):
            raise AdminToolError("Tool argument names must be strings.")
        if key in properties:
            _validate_schema_property(key, value, properties[key])
    return dict(arguments)


def _is_json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(item) for key, item in value.items())
    return False


def _validate_schema_property(field_name: str, value: Any, schema: dict[str, Any]) -> None:
    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if not isinstance(expected_types, list):
        raise AdminToolError(f"Invalid internal schema type for {field_name}.")

    if value is None:
        if "null" not in expected_types:
            raise ValueError(f"{field_name} is required.")
        return

    if "string" in expected_types and isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ValueError(f"{field_name} must be at least {min_length} character(s).")
        if isinstance(max_length, int) and len(value) > max_length:
            raise ValueError(f"{field_name} must be {max_length} character(s) or fewer.")
        if schema.get("pattern") == "^[1-9][0-9]*$":
            parse_snowflake(value, field_name)
        return

    if "integer" in expected_types and isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            raise ValueError(f"{field_name} must be {minimum} or greater.")
        if isinstance(maximum, int) and value > maximum:
            raise ValueError(f"{field_name} must be {maximum} or lower.")
        return

    if "number" in expected_types and isinstance(value, (int, float)) and not isinstance(value, bool):
        return

    if "boolean" in expected_types and isinstance(value, bool):
        return

    raise ValueError(f"{field_name} has invalid type.")


def _default_success_message(tool_name: str) -> str:
    return f"{tool_name} completed."


def _require_guild(context: AdminToolContext) -> Any:
    if context.guild is None:
        raise AdminToolError("Guild is required.")
    return context.guild


def _require_name(value: Any, field_name: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_content(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("content must be a non-empty string.")
    return value


def _optional_positive_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _bounded_message_limit(value: Any) -> int:
    limit = _optional_positive_int(value, "limit", DEFAULT_RECENT_MESSAGES)
    if limit > MAX_RECENT_MESSAGES:
        raise ValueError(f"limit must be {MAX_RECENT_MESSAGES} or lower.")
    return limit


def _reason(arguments: dict[str, Any]) -> str | None:
    reason = arguments.get("reason")
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ValueError("reason must be a string.")
    return reason


def _find_by_id(items: Any, item_id: int, label: str) -> Any:
    for item in items:
        if getattr(item, "id", None) == item_id:
            return item
    raise AdminToolError(f"{label} {item_id} was not found.")


def _find_channel(guild: Any, channel_id: int) -> Any:
    getter = getattr(guild, "get_channel", None)
    if callable(getter):
        channel = getter(channel_id)
        if channel is not None:
            return channel
    return _find_by_id(getattr(guild, "channels", []), channel_id, "Channel")


def _find_role(guild: Any, role_id: int) -> Any:
    getter = getattr(guild, "get_role", None)
    if callable(getter):
        role = getter(role_id)
        if role is not None:
            return role
    return _find_by_id(getattr(guild, "roles", []), role_id, "Role")


async def _find_member(guild: Any, member_id: int) -> Any:
    getter = getattr(guild, "get_member", None)
    if callable(getter):
        member = getter(member_id)
        if member is not None:
            return member
    fetcher = getattr(guild, "fetch_member", None)
    if callable(fetcher):
        return await fetcher(member_id)
    return _find_by_id(getattr(guild, "members", []), member_id, "Member")


def _object_type_name(value: Any) -> str:
    if isinstance(value, discord.TextChannel):
        return "text"
    if isinstance(value, discord.VoiceChannel):
        return "voice"
    if isinstance(value, discord.CategoryChannel):
        return "category"
    return getattr(value, "type", type(value).__name__)


def _normalized_channel_type(value: Any) -> str:
    raw_type = _object_type_name(value)
    return str(raw_type).lower()


def _is_text_channel(value: Any) -> bool:
    if isinstance(value, discord.TextChannel):
        return True
    return "text" in _normalized_channel_type(value)


def _is_voice_channel(value: Any) -> bool:
    if isinstance(value, discord.VoiceChannel):
        return True
    return "voice" in _normalized_channel_type(value)


def _is_category_channel(value: Any) -> bool:
    if isinstance(value, discord.CategoryChannel):
        return True
    return "category" in _normalized_channel_type(value)


def _resolve_channel(context: AdminToolContext, channel_id_value: Any) -> Any:
    guild = _require_guild(context)
    channel_id = parse_snowflake(channel_id_value, "channel_id")
    return _find_channel(guild, channel_id)


def _resolve_text_channel(context: AdminToolContext, channel_id_value: Any, tool_name: str) -> Any:
    channel = _resolve_channel(context, channel_id_value)
    if not _is_text_channel(channel):
        raise ValueError(f"{tool_name} works only with text channel.")
    return channel


def _resolve_text_or_voice_channel(context: AdminToolContext, channel_id_value: Any, tool_name: str) -> Any:
    channel = _resolve_channel(context, channel_id_value)
    if _is_category_channel(channel) or not (_is_text_channel(channel) or _is_voice_channel(channel)):
        raise ValueError(f"{tool_name} works only with text or voice channel.")
    return channel


async def _resolve_member(context: AdminToolContext, member_id_value: Any) -> Any:
    guild = _require_guild(context)
    member_id = parse_snowflake(member_id_value, "member_id")
    return await _find_member(guild, member_id)


def _resolve_role(context: AdminToolContext, role_id_value: Any) -> Any:
    guild = _require_guild(context)
    role_id = parse_snowflake(role_id_value, "role_id")
    return _find_role(guild, role_id)


def _permissions_value(value: Any) -> int | None:
    permissions = getattr(value, "permissions", value)
    raw_value = getattr(permissions, "value", None)
    return raw_value if isinstance(raw_value, int) else None


def _serialize_permission_overwrite(target: Any, overwrite: Any) -> dict[str, Any]:
    allow = None
    deny = None
    pair = getattr(overwrite, "pair", None)
    if callable(pair):
        allowed, denied = pair()
        allow = _permissions_value(allowed)
        deny = _permissions_value(denied)
    return {
        "target_id": getattr(target, "id", None),
        "target_name": getattr(target, "name", None),
        "target_type": type(target).__name__,
        "allow": allow,
        "deny": deny,
    }


def _serialize_channel(channel: Any, include_overwrites: bool = False) -> dict[str, Any]:
    data = {
        "id": getattr(channel, "id", None),
        "name": getattr(channel, "name", None),
        "type": str(_object_type_name(channel)),
        "position": getattr(channel, "position", None),
        "parent_id": getattr(getattr(channel, "category", None), "id", None)
        or getattr(getattr(channel, "parent", None), "id", None),
    }
    if include_overwrites:
        overwrites = getattr(channel, "overwrites", {})
        data["permission_overwrites"] = [
            _serialize_permission_overwrite(target, overwrite)
            for target, overwrite in getattr(overwrites, "items", lambda: [])()
        ]
    return data


def _serialize_role(role: Any) -> dict[str, Any]:
    return {
        "id": getattr(role, "id", None),
        "name": getattr(role, "name", None),
        "position": getattr(role, "position", None),
        "managed": bool(getattr(role, "managed", False)),
        "color": str(getattr(role, "color", "")) if getattr(role, "color", None) is not None else None,
        "permissions": _permissions_value(getattr(role, "permissions", None)),
    }


def _serialize_member(member: Any) -> dict[str, Any]:
    return {
        "id": getattr(member, "id", None),
        "display_name": getattr(member, "display_name", None) or getattr(member, "name", None),
        "role_ids": [getattr(role, "id", None) for role in getattr(member, "roles", [])],
        "guild_permissions": _permissions_value(getattr(member, "guild_permissions", None)),
        "timed_out_until": _isoformat_or_none(getattr(member, "timed_out_until", None)),
    }


def _serialize_attachment(attachment: Any) -> dict[str, Any]:
    return {
        "id": getattr(attachment, "id", None),
        "filename": getattr(attachment, "filename", None),
        "size": getattr(attachment, "size", None),
        "content_type": getattr(attachment, "content_type", None),
    }


def _serialize_message(message: Any) -> dict[str, Any]:
    author = getattr(message, "author", None)
    return {
        "id": getattr(message, "id", None),
        "author_id": getattr(author, "id", None),
        "author_display_name": getattr(author, "display_name", None) or getattr(author, "name", None),
        "timestamp": _isoformat_or_none(getattr(message, "created_at", None)),
        "content": getattr(message, "content", ""),
        "attachments": [_serialize_attachment(item) for item in getattr(message, "attachments", [])],
    }


def _isoformat_or_none(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


async def _get_guild_summary(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    channels = list(getattr(guild, "channels", []))
    roles = list(getattr(guild, "roles", []))
    return {
        "id": getattr(guild, "id", None),
        "name": getattr(guild, "name", None),
        "member_count": getattr(guild, "member_count", None),
        "channel_count": len(channels),
        "category_count": sum(1 for channel in channels if "category" in str(_object_type_name(channel)).lower()),
        "role_count": len(roles),
    }


async def _list_channels(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    return {"channels": [_serialize_channel(channel) for channel in getattr(guild, "channels", [])]}


async def _get_channel_details(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    channel_id = parse_snowflake(arguments.get("channel_id"), "channel_id")
    return _serialize_channel(_find_channel(guild, channel_id), include_overwrites=True)


async def _list_roles(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    return {"roles": [_serialize_role(role) for role in getattr(guild, "roles", [])]}


async def _get_role_details(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    role_id = parse_snowflake(arguments.get("role_id"), "role_id")
    return _serialize_role(_find_role(guild, role_id))


async def _get_member_details(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = _require_guild(context)
    member_id = parse_snowflake(arguments.get("member_id"), "member_id")
    return _serialize_member(await _find_member(guild, member_id))


async def _get_recent_messages(context: AdminToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_message_limit(arguments.get("limit"))
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "get_recent_messages")
    history = getattr(channel, "history", None)
    if not callable(history):
        raise AdminToolError("Channel does not expose message history.")
    messages = []
    async for message in history(limit=limit):
        messages.append(_serialize_message(message))
    return {"messages": messages, "limit": limit}


async def _send_message(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "send_message")
    content = _require_content(arguments.get("content"))
    await channel.send(content)
    return ToolResult(True, "send_message", f"Message sent to #{channel.name}.")


async def _purge_messages(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "purge_messages")
    count = _optional_positive_int(arguments.get("count"), "count", 10)
    if count > 100:
        raise ValueError("count must be 100 or lower.")
    deleted = await channel.purge(limit=count, reason=_reason(arguments))
    return ToolResult(True, "purge_messages", f"Deleted {len(deleted)} messages from #{channel.name}.")


async def _timeout_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    duration = _optional_positive_int(arguments.get("duration_minutes"), "duration_minutes", 10)
    until = datetime.now(timezone.utc) + timedelta(minutes=duration)
    await member.timeout(until, reason=_reason(arguments))
    return ToolResult(True, "timeout_member", f"Timed out {member} for {duration} minutes.")


async def _clear_timeout(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    await member.timeout(None, reason=_reason(arguments))
    return ToolResult(True, "clear_timeout", f"Cleared timeout for {member}.")


async def _kick_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    await member.kick(reason=_reason(arguments))
    return ToolResult(True, "kick_member", f"Kicked {member}.")


async def _ban_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    await member.ban(reason=_reason(arguments), delete_message_seconds=0)
    return ToolResult(True, "ban_member", f"Banned {member}.")


async def _unban_user(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    if context.fetch_user is None:
        raise AdminToolError("fetch_user is required for unban_user.")
    target_id = parse_snowflake(arguments.get("user_id"), "user_id")
    user = await context.fetch_user(target_id)
    await guild.unban(user, reason=_reason(arguments))
    return ToolResult(True, "unban_user", f"Unbanned {user}.")


async def _add_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    role = _resolve_role(context, arguments.get("role_id"))
    await member.add_roles(role, reason=_reason(arguments))
    return ToolResult(True, "add_role", f"Added role {role.name} to {member}.")


async def _remove_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    role = _resolve_role(context, arguments.get("role_id"))
    await member.remove_roles(role, reason=_reason(arguments))
    return ToolResult(True, "remove_role", f"Removed role {role.name} from {member}.")


async def _create_text_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    name = _require_name(arguments.get("name"))
    created = await guild.create_text_channel(name=name, reason=_reason(arguments))
    return ToolResult(True, "create_text_channel", f"Created text channel #{created.name}.")


async def _create_voice_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    name = _require_name(arguments.get("name"))
    created = await guild.create_voice_channel(name=name, reason=_reason(arguments))
    return ToolResult(True, "create_voice_channel", f"Created voice channel {created.name}.")


async def _rename_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    target_channel = _resolve_text_or_voice_channel(context, arguments.get("channel_id"), "rename_channel")
    name = _require_name(arguments.get("name"))
    old_name = target_channel.name
    await target_channel.edit(name=name, reason=_reason(arguments))
    return ToolResult(True, "rename_channel", f"Renamed #{old_name} to #{name}.")


async def _delete_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    target_channel = _resolve_text_or_voice_channel(context, arguments.get("channel_id"), "delete_channel")
    channel_name = target_channel.name
    await target_channel.delete(reason=_reason(arguments))
    return ToolResult(True, "delete_channel", f"Deleted channel #{channel_name}.")


async def _create_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    name = _require_name(arguments.get("name"))
    created = await guild.create_role(name=name, reason=_reason(arguments))
    return ToolResult(True, "create_role", f"Created role {created.name}.")


async def _delete_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    role = _resolve_role(context, arguments.get("role_id"))
    role_name = role.name
    await role.delete(reason=_reason(arguments))
    return ToolResult(True, "delete_role", f"Deleted role {role_name}.")


async def _lock_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "lock_channel")
    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = False
    await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=_reason(arguments))
    return ToolResult(True, "lock_channel", f"Locked #{channel.name} for @everyone.")


async def _unlock_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "unlock_channel")
    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = None
    await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=_reason(arguments))
    return ToolResult(True, "unlock_channel", f"Unlocked #{channel.name} for @everyone.")


_TOOL_HANDLERS: dict[str, Callable[[AdminToolContext, dict[str, Any]], Awaitable[Any]]] = {
    "get_guild_summary": _get_guild_summary,
    "list_channels": _list_channels,
    "get_channel_details": _get_channel_details,
    "list_roles": _list_roles,
    "get_role_details": _get_role_details,
    "get_member_details": _get_member_details,
    "get_recent_messages": _get_recent_messages,
    "send_message": _send_message,
    "purge_messages": _purge_messages,
    "timeout_member": _timeout_member,
    "clear_timeout": _clear_timeout,
    "kick_member": _kick_member,
    "ban_member": _ban_member,
    "unban_user": _unban_user,
    "add_role": _add_role,
    "remove_role": _remove_role,
    "create_text_channel": _create_text_channel,
    "create_voice_channel": _create_voice_channel,
    "rename_channel": _rename_channel,
    "delete_channel": _delete_channel,
    "create_role": _create_role,
    "delete_role": _delete_role,
    "lock_channel": _lock_channel,
    "unlock_channel": _unlock_channel,
}
