"""AI-6 server blueprint: build a whole server structure in ONE reviewed step.

The model describes the wanted roles, categories and channels (with permission
overwrites by role NAME) as one JSON object. ``check_server_blueprint`` (read)
reports what would be created or reused; ``apply_server_blueprint`` (one
confirmation for the exact JSON) creates only what is missing, never edits or
deletes existing objects, and records the created IDs so that
``undo_last_blueprint`` can delete exactly those objects again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

C: Any = None  # admin_tools core module, set by build_tools()

MAX_ROLES = 25
MAX_CATEGORIES = 15
MAX_CHANNELS = 60
MAX_OVERWRITES = 10
CHANNEL_TYPES = ("text", "voice", "announcement", "forum", "stage")
EVERYONE = "@everyone"
STORE_KEY = "last_blueprint"


class BlueprintError(Exception):
    pass


def _norm(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def _norm_channel(name: str, kind: str) -> str:
    base = _norm(name)
    if kind in ("text", "announcement", "forum"):
        # Discord stores text-like channel names lowercase with dashes.
        return base.replace(" ", "-")
    return base


def _family(kind: str) -> str:
    return {
        "text": "text",
        "news": "text",
        "announcement": "text",
        "voice": "voice",
        "stage_voice": "stage",
        "stage": "stage",
        "forum": "forum",
        "media": "forum",
    }.get(kind, kind)


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def blueprint_schema(core: Any) -> dict[str, Any]:
    perms = dict(core.PERMISSION_LIST_PROPERTY)
    role_names = {
        "type": "array",
        "maxItems": 10,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 100},
        "description": "Names of roles (from this blueprint or existing).",
    }
    overwrite = {
        "type": "object",
        "additionalProperties": False,
        "required": ["target"],
        "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 100, "description": "@everyone or a role name."},
            "allow": perms,
            "deny": perms,
        },
    }
    overwrites = {"type": "array", "maxItems": MAX_OVERWRITES, "items": overwrite, "description": "Permission overwrites."}
    channel = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "type"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "type": {"type": "string", "enum": list(CHANNEL_TYPES)},
            "topic": {"type": "string", "minLength": 0, "maxLength": 1024},
            "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
            "nsfw": {"type": "boolean"},
            "user_limit": {"type": "integer", "minimum": 0, "maximum": 99},
            "private_to_roles": role_names,
            "overwrites": overwrites,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "Wanted server structure. Existing objects with the same name are reused, never changed.",
        "properties": {
            "roles": {"type": "array", "maxItems": MAX_ROLES, "description": "The roles to have, highest first.", "items": {
                "type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "color": core.COLOR_PROPERTY,
                    "hoist": {"type": "boolean"},
                    "mentionable": {"type": "boolean"},
                    "permissions": perms,
                },
            }},
            "categories": {"type": "array", "maxItems": MAX_CATEGORIES, "description": "Categories with their channels.", "items": {
                "type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "private_to_roles": role_names,
                    "overwrites": overwrites,
                    "channels": {"type": "array", "maxItems": 30, "items": channel},
                },
            }},
            "channels": {"type": "array", "maxItems": 30, "description": "Channels without a category.", "items": channel},
        },
    }


# --------------------------------------------------------------------------
# planning (pure: no Discord writes)
# --------------------------------------------------------------------------


def _existing_roles(guild: Any) -> dict[str, Any]:
    return {_norm(getattr(role, "name", "")): role for role in getattr(guild, "roles", []) or []}


def _existing_categories(guild: Any) -> dict[str, Any]:
    return {
        _norm(getattr(channel, "name", "")): channel
        for channel in getattr(guild, "channels", []) or []
        if C._is_category_channel(channel)
    }


def _existing_channel(guild: Any, category: Any, name: str, kind: str) -> Any:
    wanted = _norm_channel(name, kind)
    family = _family(kind)
    parent_id = getattr(category, "id", None) if category is not None else None
    for channel in getattr(guild, "channels", []) or []:
        if C._is_category_channel(channel):
            continue
        channel_parent = getattr(getattr(channel, "category", None), "id", None)
        if channel_parent != parent_id:
            continue
        existing_kind = C._normalized_channel_type(channel)
        if _family(existing_kind) == family and _norm_channel(getattr(channel, "name", ""), kind) == wanted:
            return channel
    return None


def build_plan(context: Any, blueprint: dict[str, Any]) -> dict[str, Any]:
    """Validate references/permissions and compute create-vs-reuse. Raises on errors."""
    guild = C._require_guild(context)
    roles = blueprint.get("roles") or []
    categories = blueprint.get("categories") or []
    loose = blueprint.get("channels") or []
    if not roles and not categories and not loose:
        raise BlueprintError("The blueprint is empty.")
    channel_count = len(loose) + sum(len(item.get("channels") or []) for item in categories)
    if channel_count > MAX_CHANNELS:
        raise BlueprintError(f"At most {MAX_CHANNELS} channels per blueprint (got {channel_count}).")

    existing_roles = _existing_roles(guild)
    planned_roles: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for role in roles:
        key = _norm(role["name"])
        if key in seen_roles or key == EVERYONE:
            raise BlueprintError(f"Role {role['name']!r} is listed twice or reserved.")
        seen_roles.add(key)
        permissions = C.permissions_from_names(C.parse_permission_names(role.get("permissions"), f"role {role['name']} permissions"))
        if key in existing_roles:
            planned_roles.append({"name": role["name"], "action": "reuse", "id": C._snowflake_to_string(existing_roles[key].id)})
            continue
        C.ensure_permissions_grantable(context, permissions)
        if role.get("color"):
            C.parse_color(role["color"])
        planned_roles.append({"name": role["name"], "action": "create", "spec": role, "permissions": permissions.value})
    known_roles = set(existing_roles) | seen_roles

    def check_targets(names: Any, label: str) -> None:
        for name in names or []:
            if _norm(name) != EVERYONE and _norm(name) not in known_roles:
                raise BlueprintError(f"{label}: role {name!r} is neither in the blueprint nor on the server.")

    def check_overwrites(items: Any, label: str) -> None:
        for item in items or []:
            check_targets([item["target"]], label)
            allow = C.parse_permission_names(item.get("allow"), f"{label} allow")
            deny = C.parse_permission_names(item.get("deny"), f"{label} deny")
            if set(allow) & set(deny):
                raise BlueprintError(f"{label}: a permission is both allowed and denied.")
            C.ensure_permissions_grantable(context, C.permissions_from_names(allow))

    def plan_channel(item: dict[str, Any], category: Any, category_label: str) -> dict[str, Any]:
        label = f"channel {item['name']!r}"
        check_targets(item.get("private_to_roles"), label)
        check_overwrites(item.get("overwrites"), label)
        existing = _existing_channel(guild, category, item["name"], item["type"]) if category is not False else None
        if existing is not None:
            return {"name": item["name"], "type": item["type"], "category": category_label, "action": "reuse", "id": C._snowflake_to_string(existing.id)}
        return {"name": item["name"], "type": item["type"], "category": category_label, "action": "create", "spec": item}

    existing_categories = _existing_categories(guild)
    planned_categories = []
    seen_categories: set[str] = set()
    for category in categories:
        key = _norm(category["name"])
        if key in seen_categories:
            raise BlueprintError(f"Category {category['name']!r} is listed twice.")
        seen_categories.add(key)
        label = f"category {category['name']!r}"
        check_targets(category.get("private_to_roles"), label)
        check_overwrites(category.get("overwrites"), label)
        existing = existing_categories.get(key)
        # A new category has no existing channels: use False as "nothing to match".
        channels = [plan_channel(item, existing if existing is not None else False, category["name"]) for item in category.get("channels") or []]
        planned_categories.append(
            {
                "name": category["name"],
                "action": "reuse" if existing is not None else "create",
                "id": C._snowflake_to_string(existing.id) if existing is not None else None,
                "spec": category,
                "channels": channels,
            }
        )
    planned_loose = [plan_channel(item, None, "") for item in loose]
    return {"roles": planned_roles, "categories": planned_categories, "channels": planned_loose}


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    def strip(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key not in ("spec", "permissions", "channels")}

    channels = list(plan["channels"]) + [channel for category in plan["categories"] for channel in category["channels"]]
    counts = {
        "roles_to_create": sum(1 for item in plan["roles"] if item["action"] == "create"),
        "categories_to_create": sum(1 for item in plan["categories"] if item["action"] == "create"),
        "channels_to_create": sum(1 for item in channels if item["action"] == "create"),
        "reused_existing": sum(1 for item in [*plan["roles"], *plan["categories"], *channels] if item["action"] == "reuse"),
    }
    return {
        "counts": counts,
        "roles": [strip(item) for item in plan["roles"]],
        "categories": [strip(item) for item in plan["categories"]],
        "channels": [strip(item) for item in channels],
    }


# --------------------------------------------------------------------------
# apply / undo
# --------------------------------------------------------------------------


def _overwrite_map(guild: Any, role_lookup: dict[str, Any], spec: dict[str, Any]) -> dict[Any, Any] | None:
    result: dict[Any, Any] = {}

    def target(name: str) -> Any:
        if _norm(name) == EVERYONE:
            return guild.default_role
        role = role_lookup.get(_norm(name))
        if role is None:
            raise BlueprintError(f"Role {name!r} is not available.")
        return role

    private = spec.get("private_to_roles") or []
    if private:
        result[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        for name in private:
            result[target(name)] = discord.PermissionOverwrite(view_channel=True)
        bot = C._bot_member(guild)
        if bot is not None:
            result[bot] = discord.PermissionOverwrite(view_channel=True)
    for item in spec.get("overwrites") or []:
        key = target(item["target"])
        overwrite = result.get(key) or discord.PermissionOverwrite()
        overwrite.update(**{name: True for name in C.parse_permission_names(item.get("allow"), "allow")})
        overwrite.update(**{name: False for name in C.parse_permission_names(item.get("deny"), "deny")})
        result[key] = overwrite
    return result or None


async def _create_channel(guild: Any, category: Any, spec: dict[str, Any], overwrites: Any, reason: str) -> Any:
    kind = spec["type"]
    options: dict[str, Any] = {"reason": reason}
    if category is not None:
        options["category"] = category
    if overwrites:
        options["overwrites"] = overwrites
    if kind in ("text", "announcement", "forum"):
        if spec.get("topic"):
            options["topic"] = spec["topic"]
        if spec.get("slowmode_seconds") is not None:
            options["slowmode_delay"] = spec["slowmode_seconds"]
        if spec.get("nsfw") is not None:
            options["nsfw"] = bool(spec["nsfw"])
    if kind in ("voice", "stage") and spec.get("user_limit") is not None:
        options["user_limit"] = spec["user_limit"]
    if kind == "text":
        return await guild.create_text_channel(spec["name"], **options)
    if kind == "announcement":
        return await guild.create_text_channel(spec["name"], news=True, **options)
    if kind == "forum":
        return await guild.create_forum(spec["name"], **options)
    if kind == "voice":
        return await guild.create_voice_channel(spec["name"], **options)
    return await guild.create_stage_channel(spec["name"], **options)


async def _check_server_blueprint(context: Any, arguments: dict[str, Any]) -> Any:
    try:
        plan = build_plan(context, arguments["blueprint"])
    except (BlueprintError, C.AdminToolError, ValueError) as exc:
        return C.ToolResult(False, "check_server_blueprint", f"Blueprint problem: {exc}")
    return C.ToolResult(True, "check_server_blueprint", "Blueprint is valid.", summarize_plan(plan))


async def _apply_server_blueprint(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = context.feature_store
    try:
        plan = build_plan(context, arguments["blueprint"])
    except BlueprintError as exc:
        raise C.AdminToolError(f"Blueprint problem: {exc}") from exc
    reason = C._reason(arguments) or "Server blueprint via Admin bot"
    created: dict[str, list[str]] = {"roles": [], "categories": [], "channels": []}
    created_names: dict[str, str] = {}
    role_lookup = _existing_roles(guild)

    def record() -> None:
        if store is not None and any(created.values()):
            store.set(
                guild.id,
                STORE_KEY,
                {
                    "created": created,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "by": context.requesting_user_id,
                },
            )

    try:
        for item in plan["roles"]:
            if item["action"] != "create":
                continue
            spec = item["spec"]
            options: dict[str, Any] = {"name": spec["name"], "permissions": discord.Permissions(item["permissions"]), "reason": reason}
            if spec.get("color"):
                options["colour"] = C.parse_color(spec["color"])
            for flag in ("hoist", "mentionable"):
                if spec.get(flag) is not None:
                    options[flag] = bool(spec[flag])
            role = await guild.create_role(**options)
            created["roles"].append(str(role.id))
            created_names[f"role:{spec['name']}"] = str(role.id)
            role_lookup[_norm(spec["name"])] = role

        async def make_channel(item: dict[str, Any], category: Any) -> None:
            if item["action"] != "create":
                return
            spec = item["spec"]
            channel = await _create_channel(guild, category, spec, _overwrite_map(guild, role_lookup, spec), reason)
            created["channels"].append(str(channel.id))
            created_names[f"channel:{spec['name']}"] = str(channel.id)

        for category_plan in plan["categories"]:
            spec = category_plan["spec"]
            if category_plan["action"] == "create":
                options = {"reason": reason}
                overwrites = _overwrite_map(guild, role_lookup, spec)
                if overwrites:
                    options["overwrites"] = overwrites
                category = await guild.create_category(spec["name"], **options)
                created["categories"].append(str(category.id))
                created_names[f"category:{spec['name']}"] = str(category.id)
            else:
                category = C._find_channel(guild, int(category_plan["id"]))
            for item in category_plan["channels"]:
                await make_channel(item, category)
        for item in plan["channels"]:
            await make_channel(item, None)
    except Exception as exc:
        record()
        total = sum(len(values) for values in created.values())
        detail = "Discord refused an action (check the bot's role position and permissions)" if isinstance(exc, discord.Forbidden) else f"{type(exc).__name__}: {exc}"
        return C.ToolResult(
            False,
            "apply_server_blueprint",
            f"Blueprint stopped after creating {total} object(s): {detail}. "
            "Created objects were kept; undo_last_blueprint removes them.",
            {"created": created_names},
        )
    record()
    counts = {key: len(values) for key, values in created.items()}
    summary = summarize_plan(plan)["counts"]
    undo = " Undo with undo_last_blueprint." if store is not None and any(created.values()) else ""
    return C.ToolResult(
        True,
        "apply_server_blueprint",
        f"Blueprint applied: created {counts['roles']} role(s), {counts['categories']} categor(ies), "
        f"{counts['channels']} channel(s); reused {summary['reused_existing']} existing.{undo}",
        {"created": created_names},
    )


async def _undo_last_blueprint(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    store = context.feature_store
    if store is None:
        raise C.AdminToolError("The bot feature store is unavailable; nothing to undo.")
    record = store.get(guild.id, STORE_KEY)
    if not isinstance(record, dict) or not isinstance(record.get("created"), dict):
        raise C.AdminToolError("There is no recorded blueprint to undo.")
    reason = C._reason(arguments) or "Undo server blueprint via Admin bot"
    created = record["created"]
    deleted = missing = failed = 0
    for key in ("channels", "categories"):
        for value in created.get(key) or []:
            channel = getattr(guild, "get_channel", lambda _id: None)(int(value))
            if channel is None:
                missing += 1
                continue
            try:
                await channel.delete(reason=reason)
                deleted += 1
            except discord.HTTPException:
                failed += 1
    for value in created.get("roles") or []:
        role = getattr(guild, "get_role", lambda _id: None)(int(value))
        if role is None:
            missing += 1
            continue
        try:
            C.ensure_role_manageable(context, role, action="delete")
            await role.delete(reason=reason)
            deleted += 1
        except (discord.HTTPException, C.AdminToolError):
            failed += 1
    store.set(guild.id, STORE_KEY, None)
    message = f"Blueprint undone: deleted {deleted} object(s)"
    if missing:
        message += f", {missing} were already gone"
    if failed:
        message += f", {failed} could not be deleted"
    return C.ToolResult(failed == 0, "undo_last_blueprint", message + ".")


def build_tools(core: Any) -> list[tuple[Any, Any]]:
    global C
    C = core
    T = core.ToolDefinition
    schema = blueprint_schema(core)
    return [
        (
            T(
                "check_server_blueprint",
                "read",
                "read",
                "Validate a server blueprint and show what would be created or reused (no changes).",
                core._object_schema({"blueprint": schema}, ["blueprint"]),
                "blueprint",
            ),
            _check_server_blueprint,
        ),
        (
            T(
                "apply_server_blueprint",
                "write",
                "normal",
                f"Create a whole structure in one step: roles, categories, channels (max {MAX_CHANNELS}) and permission overwrites by role name. Only missing objects are created.",
                core._object_schema({"blueprint": schema, "reason": core.REASON_PROPERTY}, ["blueprint"]),
                "blueprint",
            ),
            _apply_server_blueprint,
        ),
        (
            T(
                "undo_last_blueprint",
                "write",
                "destructive",
                "Delete everything the last applied blueprint created.",
                core._object_schema({"reason": core.REASON_PROPERTY}),
                "blueprint",
            ),
            _undo_last_blueprint,
        ),
    ]
