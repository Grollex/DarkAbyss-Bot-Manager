from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping

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
    # Capability group used by the AI planner catalog and tool routing.
    category: str = "core"


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
    # Transport option: True for AI-originated execution (/ai) so model-written
    # text can never ping @everyone/@here/users/roles. /execute keeps False.
    suppress_mentions: bool = False
    # AI-6 anti-escalation: when True (AI requests) every role/member/permission
    # change is additionally limited to what the requesting member could do
    # themselves (role hierarchy, granted permissions). /execute keeps False.
    enforce_hierarchy: bool = False
    # Discord attachments of the originating request, keyed by attachment ID
    # string. Tools may only read files from here (never URLs or host paths).
    attachments: Mapping[str, Any] | None = None
    # Persistent per-instance store for bot features (role menus, welcome,
    # schedules, blueprint undo records). None when unavailable.
    feature_store: Any = None


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
COLOR_PROPERTY = {
    "type": "string",
    "pattern": "^#[0-9A-Fa-f]{6}$",
    "description": "Hex color like #FF8800.",
}
SNOWFLAKE_ITEM = {"type": "string", "pattern": "^[1-9][0-9]*$"}
PRIVATE_ROLE_IDS_PROPERTY = {
    "type": "array",
    "items": SNOWFLAKE_ITEM,
    "maxItems": 25,
    "uniqueItems": True,
    "description": "Make it private: hidden from @everyone, visible only to these role IDs (and the bot).",
}
PERMISSION_LIST_PROPERTY = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 64},
    "maxItems": 64,
    "uniqueItems": True,
    "description": "Discord permission flag names, e.g. view_channel, send_messages, connect.",
}
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
        "Delete recent messages from a text channel, optionally only matching ones.",
        _object_schema(
            {
                "channel_id": CHANNEL_ID_PROPERTY,
                "count": _integer_property("How many recent messages to scan (default 10).", maximum=100),
                "author_id": _snowflake_property("Only messages from this user."),
                "bots_only": {"type": "boolean", "description": "Only messages from bots."},
                "contains_text": _string_property("Only messages containing this text (case-insensitive).", max_length=200),
                "links_only": {"type": "boolean", "description": "Only messages containing links."},
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
        _object_schema(
            {
                "member_id": MEMBER_ID_PROPERTY,
                "delete_message_days": _integer_property("Also delete their messages from the last N days (0-7).", minimum=0, maximum=7),
                "reason": REASON_PROPERTY,
            },
            ["member_id"],
        ),
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
        "Create a text (or announcement) channel, optionally inside a category.",
        _object_schema(
            {
                "name": _name_property("New text channel name."),
                "category_id": _snowflake_property("Parent category ID."),
                "topic": _string_property("Channel topic.", min_length=0, max_length=1024),
                "slowmode_seconds": _integer_property("Slowmode delay in seconds (0-21600).", minimum=0, maximum=21600),
                "nsfw": {"type": "boolean", "description": "Age-restricted channel."},
                "announcement": {"type": "boolean", "description": "Create an announcement channel (Community servers)."},
                "position": _integer_property("Channel position.", minimum=0, maximum=500),
                "private_to_role_ids": PRIVATE_ROLE_IDS_PROPERTY,
                "reason": REASON_PROPERTY,
            },
            ["name"],
        ),
    ),
    "create_voice_channel": ToolDefinition(
        "create_voice_channel",
        "write",
        "normal",
        "Create a voice channel, optionally inside a category.",
        _object_schema(
            {
                "name": _name_property("New voice channel name."),
                "category_id": _snowflake_property("Parent category ID."),
                "user_limit": _integer_property("Max users (0 = unlimited).", minimum=0, maximum=99),
                "bitrate": _integer_property("Bitrate in bits/s (8000-384000, limited by server boost).", minimum=8000, maximum=384000),
                "position": _integer_property("Channel position.", minimum=0, maximum=500),
                "private_to_role_ids": PRIVATE_ROLE_IDS_PROPERTY,
                "reason": REASON_PROPERTY,
            },
            ["name"],
        ),
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
        "Create a role with optional color, display options and permissions.",
        _object_schema(
            {
                "name": _name_property("New role name."),
                "color": COLOR_PROPERTY,
                "hoist": {"type": "boolean", "description": "Show members separately in the member list."},
                "mentionable": {"type": "boolean", "description": "Anyone can mention the role."},
                "permissions": PERMISSION_LIST_PROPERTY,
                "reason": REASON_PROPERTY,
            },
            ["name"],
        ),
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

# /execute exposes a FIXED legacy action set (Discord allows at most 25 slash
# command choices). Tools added later are AI-only and never appear here.
EXECUTE_TOOL_NAMES = (
    "send_message",
    "purge_messages",
    "timeout_member",
    "clear_timeout",
    "kick_member",
    "ban_member",
    "unban_user",
    "add_role",
    "remove_role",
    "create_text_channel",
    "create_voice_channel",
    "rename_channel",
    "delete_channel",
    "create_role",
    "delete_role",
    "lock_channel",
    "unlock_channel",
)

# Capability groups shown to the AI planner. Every registered tool belongs to
# exactly one of them.
TOOL_CATEGORIES: dict[str, str] = {
    "core": "Read guild overview, channels, roles and members (always available).",
    "channels": "Create, edit, clone, delete channels and categories; channel permission overwrites.",
    "roles": "Create, edit, reorder, delete roles; role colors, display options and permissions.",
    "blueprint": "Build a whole server structure (roles, categories, channels, permissions) in one reviewed step; undo it.",
    "members": "Member roles, nicknames, bulk role changes, voice moves, server mute/deafen.",
    "moderation": "Purge, timeout, kick, ban/unban, ban list, audit log, channel lock, server lockdown.",
    "messages": "Send, edit, delete, pin messages; embeds; reactions; polls; announcement publishing.",
    "threads": "Threads and forum posts, forum tags.",
    "server": "Server settings: name, description, icon, banner, verification, system/rules/AFK channels, welcome screen, onboarding.",
    "automod": "Discord AutoMod rules (keyword, spam, mention spam, presets).",
    "webhooks": "List, create, use and delete webhooks (tokens are never revealed).",
    "invites": "List, create and revoke invites.",
    "expressions": "Custom emojis and stickers from attached images.",
    "events": "Scheduled server events.",
    "features": "Persistent bot features: role button menus, verification, welcome message and auto roles, scheduled messages.",
}

_LEGACY_CATEGORIES = {
    "get_recent_messages": "messages",
    "send_message": "messages",
    "purge_messages": "moderation",
    "timeout_member": "moderation",
    "clear_timeout": "moderation",
    "kick_member": "moderation",
    "ban_member": "moderation",
    "unban_user": "moderation",
    "lock_channel": "moderation",
    "unlock_channel": "moderation",
    "add_role": "members",
    "remove_role": "members",
    "create_text_channel": "channels",
    "create_voice_channel": "channels",
    "rename_channel": "channels",
    "delete_channel": "channels",
    "create_role": "roles",
    "delete_role": "roles",
}
for _tool_name, _category in _LEGACY_CATEGORIES.items():
    _old = TOOL_DEFINITIONS[_tool_name]
    TOOL_DEFINITIONS[_tool_name] = ToolDefinition(
        _old.name, _old.kind, _old.risk, _old.description, _old.arguments, _category
    )
del _tool_name, _category, _old


def tool_names_in_categories(categories: Any) -> tuple[str, ...]:
    wanted = set(categories)
    return tuple(name for name in sorted(TOOL_DEFINITIONS) if TOOL_DEFINITIONS[name].category in wanted)


def render_tool_catalog() -> str:
    """Compact capability catalog (names + one-line purpose) for the AI planner.

    Contains no argument schemas, so it stays small enough for low-TPM providers.
    """
    lines = []
    for category, summary in TOOL_CATEGORIES.items():
        names = tool_names_in_categories((category,))
        if not names:
            continue
        lines.append(f"[{category}] {summary}")
        for name in names:
            definition = TOOL_DEFINITIONS[name]
            marker = " (read)" if definition.kind == "read" else (" (destructive)" if definition.risk == "destructive" else "")
            lines.append(f"- {name}{marker}: {definition.description}")
    return "\n".join(lines)


def get_tool_definition(name: str) -> ToolDefinition:
    try:
        return TOOL_DEFINITIONS[name]
    except KeyError as exc:
        raise AdminToolError(f"Unknown admin tool: {name}") from exc


def list_tool_definitions() -> tuple[ToolDefinition, ...]:
    return tuple(TOOL_DEFINITIONS[name] for name in sorted(TOOL_DEFINITIONS))


def get_provider_tool_schema(name: str) -> dict[str, Any]:
    definition = get_tool_definition(name)
    return {
        "name": definition.name,
        "description": definition.description,
        "arguments": json.loads(json.dumps(definition.arguments)),
    }


def list_provider_tool_schemas(tool_names: tuple[str, ...] | None = None) -> tuple[dict[str, Any], ...]:
    names = tuple(sorted(TOOL_DEFINITIONS)) if tool_names is None else tuple(tool_names)
    return tuple(get_provider_tool_schema(name) for name in names)


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    return _validate_tool_arguments(get_tool_definition(tool_name), arguments)


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


_SNOWFLAKE_PATTERN = "^[1-9][0-9]*$"
MAX_SCHEMA_DEPTH = 8


def _validate_schema_property(field_name: str, value: Any, schema: dict[str, Any], depth: int = 0) -> None:
    """Validate one value against the small JSON-schema subset the tools use.

    Supported: type (single or list), enum, string min/maxLength + pattern,
    integer/number minimum/maximum, boolean, null, array (items, minItems,
    maxItems, uniqueItems) and nested objects (properties, required,
    additionalProperties false). Unknown constructs fail closed.
    """
    if depth > MAX_SCHEMA_DEPTH:
        raise AdminToolError(f"Schema nesting too deep for {field_name}.")
    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if not isinstance(expected_types, list):
        raise AdminToolError(f"Invalid internal schema type for {field_name}.")

    if value is None:
        if "null" not in expected_types:
            raise ValueError(f"{field_name} is required.")
        return

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise AdminToolError(f"Invalid internal enum for {field_name}.")
        if not any(type(item) is type(value) and item == value for item in enum):
            allowed = ", ".join(str(item) for item in enum if item is not None)
            raise ValueError(f"{field_name} must be one of: {allowed}.")

    if "string" in expected_types and isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ValueError(f"{field_name} must be at least {min_length} character(s).")
        if isinstance(max_length, int) and len(value) > max_length:
            raise ValueError(f"{field_name} must be {max_length} character(s) or fewer.")
        pattern = schema.get("pattern")
        if pattern == _SNOWFLAKE_PATTERN:
            parse_snowflake(value, field_name)
        elif isinstance(pattern, str):
            if re.fullmatch(pattern, value) is None:
                raise ValueError(f"{field_name} has invalid format.")
        return

    if "integer" in expected_types and isinstance(value, int) and not isinstance(value, bool):
        _check_numeric_bounds(field_name, value, schema)
        return

    if "number" in expected_types and isinstance(value, (int, float)) and not isinstance(value, bool):
        _check_numeric_bounds(field_name, value, schema)
        return

    if "boolean" in expected_types and isinstance(value, bool):
        return

    if "array" in expected_types and isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ValueError(f"{field_name} must contain at least {min_items} item(s).")
        if isinstance(max_items, int) and len(value) > max_items:
            raise ValueError(f"{field_name} must contain {max_items} item(s) or fewer.")
        if schema.get("uniqueItems") is True:
            seen = [json.dumps(item, sort_keys=True) for item in value]
            if len(seen) != len(set(seen)):
                raise ValueError(f"{field_name} must not contain duplicates.")
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, dict):
                raise AdminToolError(f"Invalid internal items schema for {field_name}.")
            for index, item in enumerate(value):
                _validate_schema_property(f"{field_name}[{index}]", item, items, depth + 1)
        return

    if "object" in expected_types and isinstance(value, dict):
        _validate_object(field_name, value, schema, depth + 1)
        return

    raise ValueError(f"{field_name} has invalid type.")


def _check_numeric_bounds(field_name: str, value: Any, schema: dict[str, Any]) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        raise ValueError(f"{field_name} must be {minimum} or greater.")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        raise ValueError(f"{field_name} must be {maximum} or lower.")


def _validate_object(field_name: str, value: dict[str, Any], schema: dict[str, Any], depth: int) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise AdminToolError(f"Invalid internal object schema for {field_name}.")
    if schema.get("additionalProperties", True) is False:
        unknown = sorted(str(key) for key in set(value) - set(properties))
        if unknown:
            raise ValueError(f"{field_name} has unknown field: {unknown[0]}")
    for key in required:
        if key not in value:
            raise ValueError(f"{field_name} is missing field: {key}")
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} field names must be strings.")
        if key in properties:
            _validate_schema_property(f"{field_name}.{key}", item, properties[key], depth)


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
    raw_type = getattr(value, "type", None)
    name = getattr(raw_type, "name", None)
    if isinstance(name, str):
        return name.lower()
    if isinstance(raw_type, str):
        return raw_type.lower()
    return type(value).__name__.lower()


def _is_text_channel(value: Any) -> bool:
    if isinstance(value, discord.TextChannel):
        return True
    return _normalized_channel_type(value) == "text"


def _is_voice_channel(value: Any) -> bool:
    if isinstance(value, discord.VoiceChannel):
        return True
    return _normalized_channel_type(value) == "voice"


def _is_category_channel(value: Any) -> bool:
    if isinstance(value, discord.CategoryChannel):
        return True
    return _normalized_channel_type(value) == "category"


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


def _snowflake_to_string(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _serialize_permission_overwrite(target: Any, overwrite: Any) -> dict[str, Any]:
    allow = None
    deny = None
    pair = getattr(overwrite, "pair", None)
    if callable(pair):
        allowed, denied = pair()
        allow = _permissions_value(allowed)
        deny = _permissions_value(denied)
    return {
        "target_id": _snowflake_to_string(getattr(target, "id", None)),
        "target_name": getattr(target, "name", None),
        "target_type": type(target).__name__,
        "allow": allow,
        "deny": deny,
    }


def _serialize_channel(channel: Any, include_overwrites: bool = False) -> dict[str, Any]:
    data = {
        "id": _snowflake_to_string(getattr(channel, "id", None)),
        "name": getattr(channel, "name", None),
        "type": str(_object_type_name(channel)),
        "position": getattr(channel, "position", None),
        "parent_id": _snowflake_to_string(
            getattr(getattr(channel, "category", None), "id", None)
            or getattr(getattr(channel, "parent", None), "id", None)
        ),
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
        "id": _snowflake_to_string(getattr(role, "id", None)),
        "name": getattr(role, "name", None),
        "position": getattr(role, "position", None),
        "managed": bool(getattr(role, "managed", False)),
        "color": str(getattr(role, "color", "")) if getattr(role, "color", None) is not None else None,
        "permissions": _permissions_value(getattr(role, "permissions", None)),
    }


def _serialize_member(member: Any) -> dict[str, Any]:
    return {
        "id": _snowflake_to_string(getattr(member, "id", None)),
        "display_name": getattr(member, "display_name", None) or getattr(member, "name", None),
        "role_ids": [_snowflake_to_string(getattr(role, "id", None)) for role in getattr(member, "roles", [])],
        "guild_permissions": _permissions_value(getattr(member, "guild_permissions", None)),
        "timed_out_until": _isoformat_or_none(getattr(member, "timed_out_until", None)),
    }


def _serialize_attachment(attachment: Any) -> dict[str, Any]:
    return {
        "id": _snowflake_to_string(getattr(attachment, "id", None)),
        "filename": getattr(attachment, "filename", None),
        "size": getattr(attachment, "size", None),
        "content_type": getattr(attachment, "content_type", None),
    }


def _serialize_message(message: Any) -> dict[str, Any]:
    author = getattr(message, "author", None)
    return {
        "id": _snowflake_to_string(getattr(message, "id", None)),
        "author_id": _snowflake_to_string(getattr(author, "id", None)),
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
        "id": _snowflake_to_string(getattr(guild, "id", None)),
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
    if context.suppress_mentions is True:
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
    else:
        await channel.send(content)
    return ToolResult(True, "send_message", f"Message sent to #{channel.name}.")


async def _purge_messages(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    channel = _resolve_text_channel(context, arguments.get("channel_id"), "purge_messages")
    count = _optional_positive_int(arguments.get("count"), "count", 10)
    if count > 100:
        raise ValueError("count must be 100 or lower.")
    check = _purge_filter(arguments)
    if check is None:
        deleted = await channel.purge(limit=count, reason=_reason(arguments))
    else:
        deleted = await channel.purge(limit=count, check=check, reason=_reason(arguments))
    return ToolResult(True, "purge_messages", f"Deleted {len(deleted)} messages from #{channel.name}.")


async def _timeout_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    ensure_member_actionable(context, member, action="time out")
    duration = _optional_positive_int(arguments.get("duration_minutes"), "duration_minutes", 10)
    until = datetime.now(timezone.utc) + timedelta(minutes=duration)
    await member.timeout(until, reason=_reason(arguments))
    return ToolResult(True, "timeout_member", f"Timed out {member} for {duration} minutes.")


async def _clear_timeout(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    ensure_member_actionable(context, member, action="change the timeout of")
    await member.timeout(None, reason=_reason(arguments))
    return ToolResult(True, "clear_timeout", f"Cleared timeout for {member}.")


async def _kick_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    ensure_member_actionable(context, member, action="kick")
    await member.kick(reason=_reason(arguments))
    return ToolResult(True, "kick_member", f"Kicked {member}.")


async def _ban_member(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    ensure_member_actionable(context, member, action="ban")
    days = arguments.get("delete_message_days")
    delete_seconds = 0 if days is None else int(days) * 86400
    await member.ban(reason=_reason(arguments), delete_message_seconds=delete_seconds)
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
    _ensure_assignable_role(context, role)
    await member.add_roles(role, reason=_reason(arguments))
    return ToolResult(True, "add_role", f"Added role {role.name} to {member}.")


async def _remove_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    member = await _resolve_member(context, arguments.get("member_id"))
    role = _resolve_role(context, arguments.get("role_id"))
    _ensure_assignable_role(context, role)
    await member.remove_roles(role, reason=_reason(arguments))
    return ToolResult(True, "remove_role", f"Removed role {role.name} from {member}.")


async def _create_text_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    name = _require_name(arguments.get("name"))
    options = _channel_create_options(context, arguments, text=True)
    created = await guild.create_text_channel(name=name, reason=_reason(arguments), **options)
    return ToolResult(
        True,
        "create_text_channel",
        f"Created text channel #{created.name}.",
        {"channel_id": _snowflake_to_string(getattr(created, "id", None))},
    )


async def _create_voice_channel(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    guild = _require_guild(context)
    name = _require_name(arguments.get("name"))
    options = _channel_create_options(context, arguments, text=False)
    created = await guild.create_voice_channel(name=name, reason=_reason(arguments), **options)
    return ToolResult(
        True,
        "create_voice_channel",
        f"Created voice channel {created.name}.",
        {"channel_id": _snowflake_to_string(getattr(created, "id", None))},
    )


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
    options: dict[str, Any] = {}
    if arguments.get("color") is not None:
        options["colour"] = parse_color(arguments["color"])
    for flag in ("hoist", "mentionable"):
        if arguments.get(flag) is not None:
            options[flag] = bool(arguments[flag])
    if arguments.get("permissions") is not None:
        permissions = permissions_from_names(parse_permission_names(arguments["permissions"], "permissions"))
        ensure_permissions_grantable(context, permissions)
        options["permissions"] = permissions
    created = await guild.create_role(name=name, reason=_reason(arguments), **options)
    return ToolResult(
        True,
        "create_role",
        f"Created role {created.name}.",
        {"role_id": _snowflake_to_string(getattr(created, "id", None))},
    )


async def _delete_role(context: AdminToolContext, arguments: dict[str, Any]) -> ToolResult:
    role = _resolve_role(context, arguments.get("role_id"))
    if callable(getattr(role, "is_default", None)) and role.is_default():
        raise AdminToolError("The @everyone role cannot be deleted.")
    ensure_role_manageable(context, role, action="delete")
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


def parse_color(value: Any) -> discord.Colour:
    if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
        raise ValueError("color must be a hex color like #FF8800.")
    return discord.Colour(int(value[1:], 16))


_LINK_RE = re.compile(r"https?://|discord\.gg/", re.IGNORECASE)


def _purge_filter(arguments: dict[str, Any]) -> Callable[[Any], bool] | None:
    author_id = arguments.get("author_id")
    author = parse_snowflake(author_id, "author_id") if author_id is not None else None
    bots_only = arguments.get("bots_only") is True
    links_only = arguments.get("links_only") is True
    text = arguments.get("contains_text")
    needle = text.lower() if isinstance(text, str) and text else None
    if author is None and not bots_only and not links_only and needle is None:
        return None

    def check(message: Any) -> bool:
        message_author = getattr(message, "author", None)
        content = str(getattr(message, "content", "") or "")
        if author is not None and getattr(message_author, "id", None) != author:
            return False
        if bots_only and getattr(message_author, "bot", False) is not True:
            return False
        if links_only and _LINK_RE.search(content) is None:
            return False
        if needle is not None and needle not in content.lower():
            return False
        return True

    return check


def resolve_category(context: AdminToolContext, category_id_value: Any) -> Any:
    channel = _resolve_channel(context, parse_snowflake(category_id_value, "category_id"))
    if not _is_category_channel(channel):
        raise ValueError("category_id must refer to a category.")
    return channel


def private_overwrites(context: AdminToolContext, role_ids: Any) -> dict[Any, Any]:
    """@everyone cannot see; listed roles and the bot can."""
    guild = _require_guild(context)
    overwrites: dict[Any, Any] = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for value in role_ids or []:
        role = _find_role(guild, parse_snowflake(value, "private_to_role_ids"))
        overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    bot = _bot_member(guild)
    if bot is not None:
        overwrites[bot] = discord.PermissionOverwrite(view_channel=True)
    return overwrites


def _channel_create_options(context: AdminToolContext, arguments: dict[str, Any], *, text: bool) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if arguments.get("category_id") is not None:
        options["category"] = resolve_category(context, arguments["category_id"])
    if arguments.get("position") is not None:
        options["position"] = arguments["position"]
    if arguments.get("private_to_role_ids"):
        options["overwrites"] = private_overwrites(context, arguments["private_to_role_ids"])
    if text:
        if arguments.get("topic") is not None:
            options["topic"] = arguments["topic"]
        if arguments.get("slowmode_seconds") is not None:
            options["slowmode_delay"] = arguments["slowmode_seconds"]
        if arguments.get("nsfw") is not None:
            options["nsfw"] = bool(arguments["nsfw"])
        if arguments.get("announcement") is True:
            options["news"] = True
    else:
        if arguments.get("user_limit") is not None:
            options["user_limit"] = arguments["user_limit"]
        if arguments.get("bitrate") is not None:
            options["bitrate"] = arguments["bitrate"]
    return options


# --------------------------------------------------------------------------
# AI-6 shared helpers: permission names, anti-escalation guards, attachments
# --------------------------------------------------------------------------

# Never granted through tools, whoever asks.
FORBIDDEN_GRANT_PERMISSIONS = frozenset({"administrator"})
PERMISSION_NAMES_HINT = (
    "Use Discord permission flag names such as view_channel, send_messages, read_message_history, "
    "connect, speak, manage_messages, manage_channels, manage_roles, kick_members, ban_members, "
    "moderate_members, mention_everyone, attach_files, embed_links, add_reactions."
)


def parse_permission_names(value: Any, field_name: str) -> list[str]:
    """Validate a list of discord.py permission flag names (deduplicated, order kept)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of permission names.")
    names: list[str] = []
    unknown: list[str] = []
    for raw in value:
        name = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if name not in discord.Permissions.VALID_FLAGS:
            unknown.append(raw)
        elif name not in names:
            names.append(name)
    if unknown:
        shown = ", ".join(repr(item) for item in unknown[:5])
        raise ValueError(f"{field_name} has unknown permission name(s): {shown}. {PERMISSION_NAMES_HINT}")
    return names


def permissions_from_names(names: Any) -> discord.Permissions:
    permissions = discord.Permissions.none()
    if names:
        permissions.update(**{name: True for name in names})
    return permissions


def permission_names(permissions: Any) -> list[str]:
    value = _permissions_value(permissions)
    if value is None:
        return []
    return [name for name, enabled in discord.Permissions(value) if enabled]


def _role_position(role: Any) -> int | None:
    position = getattr(role, "position", None)
    if isinstance(position, bool) or not isinstance(position, int):
        return None
    return position


def _top_role_position(member: Any) -> int | None:
    if member is None:
        return None
    top_role = getattr(member, "top_role", None)
    if top_role is not None:
        return _role_position(top_role)
    return None


def _is_guild_owner(guild: Any, member: Any) -> bool:
    owner_id = getattr(guild, "owner_id", None)
    return owner_id is not None and getattr(member, "id", None) == owner_id


def _bot_member(guild: Any) -> Any:
    return getattr(guild, "me", None)


def _ensure_assignable_role(context: AdminToolContext, role: Any) -> None:
    if callable(getattr(role, "is_default", None)) and role.is_default():
        raise AdminToolError("The @everyone role cannot be assigned or removed.")
    ensure_role_manageable(context, role, action="assign or remove")


def requester_member(context: AdminToolContext) -> Any:
    guild = _require_guild(context)
    member = None
    getter = getattr(guild, "get_member", None)
    if context.requesting_user_id is not None and callable(getter):
        member = getter(context.requesting_user_id)
    if member is None:
        raise AdminToolError("The requesting member could not be resolved; action refused.")
    return member


def ensure_role_manageable(context: AdminToolContext, role: Any, *, action: str = "change") -> None:
    """Bot hierarchy always; requester hierarchy for AI requests (owner exempt)."""
    guild = _require_guild(context)
    role_name = getattr(role, "name", "?")
    if getattr(role, "managed", False) is True:
        raise AdminToolError(f"Role {role_name} is managed by an integration or bot and cannot be changed.")
    is_default = callable(getattr(role, "is_default", None)) and role.is_default()
    position = _role_position(role)
    bot_top = _top_role_position(_bot_member(guild))
    if not is_default and bot_top is not None and position is not None and position >= bot_top:
        raise AdminToolError(
            f"Role {role_name} is at or above the bot's highest role; move the bot's role higher first."
        )
    if not context.enforce_hierarchy:
        return
    requester = requester_member(context)
    if _is_guild_owner(guild, requester):
        return
    requester_top = _top_role_position(requester)
    if requester_top is None or position is None or (not is_default and position >= requester_top):
        raise AdminToolError(f"Role {role_name} is at or above your highest role; the AI cannot {action} it for you.")


def ensure_member_actionable(context: AdminToolContext, member: Any, *, action: str, allow_self: bool = False) -> None:
    guild = _require_guild(context)
    member_name = str(member)
    if _is_guild_owner(guild, member):
        raise AdminToolError(f"Cannot {action} the server owner.")
    bot = _bot_member(guild)
    if bot is not None and getattr(bot, "id", None) == getattr(member, "id", None):
        raise AdminToolError(f"Cannot {action} the bot itself.")
    member_top = _top_role_position(member)
    bot_top = _top_role_position(bot)
    if bot_top is not None and member_top is not None and member_top >= bot_top:
        raise AdminToolError(f"{member_name} has a role at or above the bot's highest role.")
    if not context.enforce_hierarchy:
        return
    requester = requester_member(context)
    if getattr(requester, "id", None) == getattr(member, "id", None):
        if allow_self:
            return
        raise AdminToolError(f"The AI will not {action} you yourself.")
    if _is_guild_owner(guild, requester):
        return
    requester_top = _top_role_position(requester)
    if requester_top is None or member_top is None or member_top >= requester_top:
        raise AdminToolError(f"{member_name} has a role at or above your highest role; the AI cannot {action} them for you.")


def ensure_permissions_grantable(context: AdminToolContext, permissions: Any) -> None:
    """Never Administrator; for AI requests only permissions the requester holds."""
    value = _permissions_value(permissions) or 0
    granted = [name for name, enabled in discord.Permissions(value) if enabled]
    forbidden = sorted(set(granted) & FORBIDDEN_GRANT_PERMISSIONS)
    if forbidden:
        raise AdminToolError("Granting Administrator is not allowed through tools; do it manually in Discord.")
    if not context.enforce_hierarchy or not value:
        return
    guild = _require_guild(context)
    requester = requester_member(context)
    if _is_guild_owner(guild, requester):
        return
    requester_permissions = getattr(requester, "guild_permissions", None)
    if getattr(requester_permissions, "administrator", False) is True:
        return
    requester_value = _permissions_value(requester_permissions) or 0
    missing = value & ~requester_value
    if missing:
        names = ", ".join(name for name, enabled in discord.Permissions(missing) if enabled)
        raise AdminToolError(f"You do not have these permissions yourself, so the AI cannot grant them: {names}.")


IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(_IMAGE_SIGNATURES):
        return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


async def read_request_attachment(
    context: AdminToolContext,
    attachment_id_value: Any,
    *,
    max_bytes: int,
    label: str,
    allowed_types: frozenset[str] = IMAGE_CONTENT_TYPES,
) -> bytes:
    """Read one file attached to THIS request (Discord CDN only; no URLs, no host files)."""
    attachment_id = str(parse_snowflake(attachment_id_value, "attachment_id"))
    attachment = (context.attachments or {}).get(attachment_id)
    if attachment is None:
        raise AdminToolError("attachment_id must be one of the files attached to this request.")
    content_type = str(getattr(attachment, "content_type", "") or "").split(";")[0].strip().lower()
    if content_type not in allowed_types:
        raise AdminToolError(f"{label} must be one of: {', '.join(sorted(allowed_types))}.")
    size = getattr(attachment, "size", None)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > max_bytes:
        raise AdminToolError(f"{label} must be at most {max_bytes // 1024} KB.")
    data = await attachment.read()
    if not isinstance(data, (bytes, bytearray)) or len(data) > max_bytes or len(data) == 0:
        raise AdminToolError(f"{label} must be at most {max_bytes // 1024} KB.")
    if allowed_types <= IMAGE_CONTENT_TYPES and not _looks_like_image(bytes(data)):
        raise AdminToolError(f"{label} is not a valid image file.")
    return bytes(data)


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


def register_tool(definition: ToolDefinition, handler: Callable[[AdminToolContext, dict[str, Any]], Awaitable[Any]]) -> None:
    if definition.name in TOOL_DEFINITIONS or definition.name in _TOOL_HANDLERS:
        raise AdminToolError(f"Duplicate admin tool: {definition.name}")
    if definition.category not in TOOL_CATEGORIES:
        raise AdminToolError(f"Unknown tool category for {definition.name}: {definition.category}")
    if definition.kind not in ("read", "write") or definition.risk not in ("read", "normal", "destructive"):
        raise AdminToolError(f"Invalid kind/risk for {definition.name}.")
    if (definition.kind == "read") != (definition.risk == "read"):
        raise AdminToolError(f"Inconsistent kind/risk for {definition.name}.")
    TOOL_DEFINITIONS[definition.name] = definition
    _TOOL_HANDLERS[definition.name] = handler


EXTENSION_MODULES = ("admin_tools_server", "admin_tools_content", "admin_blueprint", "admin_features")


def _load_extension_tools() -> None:
    # Extensions receive this module explicitly (no import cycle) and return
    # (definition, handler) pairs. They are imported fresh for every admin_tools
    # module object so a re-imported admin_tools (tests) never shares extension
    # state with an older one. Import admin_tools before admin_features.
    import importlib

    core = sys.modules[__name__]
    for name in EXTENSION_MODULES:
        sys.modules.pop(name, None)
        module = importlib.import_module(name)
        for definition, handler in module.build_tools(core):
            register_tool(definition, handler)


_load_extension_tools()
