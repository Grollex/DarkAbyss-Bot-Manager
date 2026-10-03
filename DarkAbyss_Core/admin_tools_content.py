"""AI-6 Admin Tools: messages, embeds, polls, threads/forums, webhooks, invites,
emojis/stickers and scheduled events.

Discord-only, like every Admin Tool. Webhook tokens/URLs are never returned to
the caller (the model) or written to results; webhooks are referenced by ID
and used internally. Images come only from files attached to the request.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from bot_i18n import t

C: Any = None  # admin_tools core module, set by build_tools()

MAX_EMBED_TOTAL_CHARS = 6000
MAX_EMOJI_BYTES = 256 * 1024
MAX_STICKER_BYTES = 512 * 1024
STICKER_CONTENT_TYPES = frozenset({"image/png", "image/gif"})
MESSAGEABLE_TYPES = ("text", "news", "voice", "stage_voice", "public_thread", "private_thread", "news_thread")
URL_PATTERN = r"^https://[^\s<>\"']{1,500}$"
EMOJI_NAME_PATTERN = r"^[A-Za-z0-9_]{2,32}$"
INVITE_CODE_PATTERN = r"^(https://(discord\.gg|discord\.com/invite)/)?[A-Za-z0-9-]{2,32}$"
ARCHIVE_MINUTES = (60, 1440, 4320, 10080)


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


def _url(description: str) -> dict[str, Any]:
    return {"type": "string", "pattern": URL_PATTERN, "description": description}


def _ok(tool: str, message: str, data: Any = None) -> Any:
    return C.ToolResult(True, tool, message, data)


def _sid(value: Any) -> str | None:
    return C._snowflake_to_string(getattr(value, "id", None))


def _reason(arguments: dict[str, Any]) -> str | None:
    return C._reason(arguments)


def _mentions(context: Any) -> Any:
    return discord.AllowedMentions.none() if context.suppress_mentions is True else None


def _send_kwargs(context: Any) -> dict[str, Any]:
    mentions = _mentions(context)
    return {"allowed_mentions": mentions} if mentions is not None else {}


def resolve_any_channel(context: Any, value: Any, field: str = "channel_id") -> Any:
    guild = C._require_guild(context)
    channel_id = C.parse_snowflake(value, field)
    getter = getattr(guild, "get_channel_or_thread", None)
    if callable(getter):
        channel = getter(channel_id)
        if channel is not None:
            return channel
    return C._find_channel(guild, channel_id)


def resolve_messageable(context: Any, value: Any, tool: str) -> Any:
    channel = resolve_any_channel(context, value)
    if C._normalized_channel_type(channel) not in MESSAGEABLE_TYPES:
        raise ValueError(t("{tool} needs a text, announcement, voice-chat or thread channel.", tool=tool))
    return channel


async def fetch_channel_message(context: Any, arguments: dict[str, Any], tool: str) -> tuple[Any, Any]:
    channel = resolve_messageable(context, arguments.get("channel_id"), tool)
    message_id = C.parse_snowflake(arguments.get("message_id"), "message_id")
    message = await channel.fetch_message(message_id)
    return channel, message


# --------------------------------------------------------------------------
# embeds
# --------------------------------------------------------------------------


def embed_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "Rich embed.",
        "properties": {
            "title": _str("Title.", max_length=256),
            "description": _str("Main text (Discord markdown).", max_length=4096),
            "color": C.COLOR_PROPERTY,
            "url": _url("Title link (https)."),
            "fields": {"type": "array", "maxItems": 25, "description": "Fields.", "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "value"],
                "properties": {
                    "name": _str("Field title.", max_length=256),
                    "value": _str("Field text.", max_length=1024),
                    "inline": _bool("Show side by side."),
                },
            }},
            "footer": _str("Footer text.", max_length=2048),
            "image_url": _url("Large image (https)."),
            "thumbnail_url": _url("Small image (https)."),
        },
    }


def build_embed(spec: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=spec.get("title") or None,
        description=spec.get("description") or None,
        url=spec.get("url") or None,
        colour=C.parse_color(spec["color"]) if spec.get("color") else None,
    )
    for field in spec.get("fields") or []:
        embed.add_field(name=field["name"], value=field["value"], inline=bool(field.get("inline", False)))
    if spec.get("footer"):
        embed.set_footer(text=spec["footer"])
    if spec.get("image_url"):
        embed.set_image(url=spec["image_url"])
    if spec.get("thumbnail_url"):
        embed.set_thumbnail(url=spec["thumbnail_url"])
    if len(embed) == 0 and not spec.get("image_url") and not spec.get("thumbnail_url"):
        raise ValueError(t("The embed is empty."))
    if len(embed) > MAX_EMBED_TOTAL_CHARS:
        raise ValueError(t("The embed text exceeds Discord's {max_embed_total_chars}-character limit.", max_embed_total_chars=MAX_EMBED_TOTAL_CHARS))
    return embed


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


async def _send_embed(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_messageable(context, arguments.get("channel_id"), "send_embed")
    embed = build_embed(arguments["embed"])
    content = arguments.get("content") or None
    message = await channel.send(content=content, embed=embed, **_send_kwargs(context))
    return _ok("send_embed", t("Embed sent to {name}.", name=channel.name), {"message_id": _sid(message)})


async def _edit_bot_message(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    channel, message = await fetch_channel_message(context, arguments, "edit_bot_message")
    bot = C._bot_member(guild)
    if bot is None or getattr(getattr(message, "author", None), "id", None) != getattr(bot, "id", None):
        raise C.AdminToolError(t("Only messages written by this bot can be edited."))
    options: dict[str, Any] = {}
    if arguments.get("content") is not None:
        options["content"] = arguments["content"] or None
    if arguments.get("embed") is not None:
        options["embed"] = build_embed(arguments["embed"])
    if arguments.get("remove_embeds") is True:
        if "embed" in options:
            raise ValueError(t("Use either embed or remove_embeds."))
        options["embeds"] = []
    if not options:
        raise ValueError(t("Nothing to change."))
    await message.edit(**options, **_send_kwargs(context))
    return _ok("edit_bot_message", t("Edited the bot message in {name}.", name=channel.name))


async def _delete_message(context: Any, arguments: dict[str, Any]) -> Any:
    channel, message = await fetch_channel_message(context, arguments, "delete_message")
    await message.delete()
    return _ok("delete_message", t("Deleted a message in {name}.", name=channel.name))


async def _pin_message(context: Any, arguments: dict[str, Any]) -> Any:
    channel, message = await fetch_channel_message(context, arguments, "pin_message")
    if arguments.get("unpin") is True:
        await message.unpin(reason=_reason(arguments))
        return _ok("pin_message", t("Unpinned a message in {name}.", name=channel.name))
    await message.pin(reason=_reason(arguments))
    return _ok("pin_message", t("Pinned a message in {name}.", name=channel.name))


def _emoji_input(context: Any, value: str) -> Any:
    value = value.strip()
    guild = C._require_guild(context)
    for emoji in getattr(guild, "emojis", None) or []:
        if value in (getattr(emoji, "name", None), f":{getattr(emoji, 'name', '')}:"):
            return emoji
    return value


async def _add_reaction(context: Any, arguments: dict[str, Any]) -> Any:
    channel, message = await fetch_channel_message(context, arguments, "add_reaction")
    added = 0
    for value in arguments["emojis"]:
        await message.add_reaction(_emoji_input(context, value))
        added += 1
    return _ok("add_reaction", t("Added {added} reaction(s) in {name}.", added=added, name=channel.name))


async def _create_poll(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_messageable(context, arguments.get("channel_id"), "create_poll")
    poll = discord.Poll(
        question=arguments["question"],
        duration=timedelta(hours=arguments.get("duration_hours") or 24),
        multiple=arguments.get("multiple") is True,
    )
    for answer in arguments["answers"]:
        poll.add_answer(text=answer["text"], emoji=answer.get("emoji") or None)
    message = await channel.send(content=arguments.get("content") or None, poll=poll, **_send_kwargs(context))
    return _ok("create_poll", t("Poll posted in {name}.", name=channel.name), {"message_id": _sid(message)})


async def _publish_message(context: Any, arguments: dict[str, Any]) -> Any:
    channel, message = await fetch_channel_message(context, arguments, "publish_message")
    if C._normalized_channel_type(channel) != "news":
        raise ValueError(t("Only messages in announcement channels can be published."))
    await message.publish()
    return _ok("publish_message", t("Published a message from {name} to following servers.", name=channel.name))


# --------------------------------------------------------------------------
# threads and forums
# --------------------------------------------------------------------------


def _serialize_thread(thread: Any) -> dict[str, Any]:
    return {
        "id": _sid(thread),
        "name": getattr(thread, "name", None),
        "parent_id": C._snowflake_to_string(getattr(thread, "parent_id", None)),
        "archived": getattr(thread, "archived", None),
        "locked": getattr(thread, "locked", None),
        "message_count": getattr(thread, "message_count", None),
    }


async def _list_active_threads(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    threads = await guild.active_threads()
    return {"threads": [_serialize_thread(thread) for thread in list(threads)[:100]]}


async def _create_thread(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if C._normalized_channel_type(channel) not in ("text", "news"):
        raise ValueError(t("create_thread needs a text or announcement channel; use create_forum_post for forums."))
    options: dict[str, Any] = {"name": C._require_name(arguments.get("name"))}
    if arguments.get("auto_archive_minutes") is not None:
        options["auto_archive_duration"] = arguments["auto_archive_minutes"]
    if arguments.get("slowmode_seconds") is not None:
        options["slowmode_delay"] = arguments["slowmode_seconds"]
    if arguments.get("message_id"):
        options["message"] = discord.Object(id=C.parse_snowflake(arguments["message_id"], "message_id"))
    elif arguments.get("private") is True:
        options["type"] = discord.ChannelType.private_thread
    else:
        options["type"] = discord.ChannelType.public_thread
    thread = await channel.create_thread(reason=_reason(arguments), **options)
    return _ok("create_thread", t("Created thread {name}.", name=thread.name), {"thread_id": _sid(thread)})


async def _create_forum_post(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if C._normalized_channel_type(channel) not in ("forum", "media"):
        raise ValueError(t("create_forum_post needs a forum channel."))
    tags = []
    wanted = [name.lower() for name in arguments.get("tag_names") or []]
    available = {str(getattr(tag, "name", "")).lower(): tag for tag in getattr(channel, "available_tags", None) or []}
    for name in wanted:
        if name not in available:
            raise ValueError(t("Forum tag {name} does not exist in {name2}.", name=repr(name), name2=channel.name))
        tags.append(available[name])
    options: dict[str, Any] = {"name": C._require_name(arguments.get("title"), "title"), "content": arguments["content"]}
    if arguments.get("embed") is not None:
        options["embed"] = build_embed(arguments["embed"])
    if tags:
        options["applied_tags"] = tags
    created = await channel.create_thread(reason=_reason(arguments), **options, **_send_kwargs(context))
    thread = getattr(created, "thread", created)
    return _ok("create_forum_post", t("Created forum post {name}.", name=thread.name), {"thread_id": _sid(thread)})


async def _edit_thread(context: Any, arguments: dict[str, Any]) -> Any:
    thread = resolve_any_channel(context, arguments.get("thread_id"), "thread_id")
    if not C._normalized_channel_type(thread).endswith("thread"):
        raise ValueError(t("thread_id must refer to a thread or forum post."))
    options: dict[str, Any] = {}
    if arguments.get("name") is not None:
        options["name"] = C._require_name(arguments["name"])
    for field, target in (
        ("archived", "archived"),
        ("locked", "locked"),
        ("pinned", "pinned"),
        ("slowmode_seconds", "slowmode_delay"),
        ("auto_archive_minutes", "auto_archive_duration"),
    ):
        if arguments.get(field) is not None:
            options[target] = arguments[field]
    if not options:
        raise ValueError(t("Nothing to change."))
    await thread.edit(reason=_reason(arguments), **options)
    return _ok("edit_thread", t("Updated thread {name}.", name=thread.name))


async def _set_forum_tags(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if C._normalized_channel_type(channel) not in ("forum", "media"):
        raise ValueError(t("set_forum_tags needs a forum channel."))
    existing = {str(getattr(tag, "name", "")).lower(): tag for tag in getattr(channel, "available_tags", None) or []}
    tags = []
    for item in arguments["tags"]:
        old = existing.get(item["name"].lower())
        tag = discord.ForumTag(name=item["name"], emoji=item.get("emoji") or None, moderated=bool(item.get("moderated", False)))
        if old is not None:
            tag.id = old.id  # keep the tag (and posts using it) when only its options change
        tags.append(tag)
    await channel.edit(available_tags=tags, reason=_reason(arguments))
    return _ok("set_forum_tags", t("Forum {name} now has {count} tag(s).", name=channel.name, count=len(tags)))


# --------------------------------------------------------------------------
# webhooks (IDs only; tokens never leave this module)
# --------------------------------------------------------------------------


async def _list_webhooks(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    bot = C._bot_member(guild)
    hooks = await guild.webhooks()
    return {
        "webhooks": [
            {
                "id": _sid(hook),
                "name": getattr(hook, "name", None),
                "channel_id": C._snowflake_to_string(getattr(hook, "channel_id", None)),
                "created_by_this_bot": bot is not None and getattr(getattr(hook, "user", None), "id", None) == getattr(bot, "id", None),
                "usable_by_bot": bool(getattr(hook, "token", None)),
            }
            for hook in hooks
        ]
    }


async def _find_webhook(context: Any, value: Any) -> Any:
    guild = C._require_guild(context)
    webhook_id = C.parse_snowflake(value, "webhook_id")
    for hook in await guild.webhooks():
        if getattr(hook, "id", None) == webhook_id:
            return hook
    raise C.AdminToolError(t("Webhook {webhook_id} was not found.", webhook_id=webhook_id))


async def _create_webhook(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if C._normalized_channel_type(channel) not in ("text", "news", "forum", "voice", "stage_voice"):
        raise ValueError(t("Webhooks can be created in text, announcement, forum, voice or stage channels."))
    hook = await channel.create_webhook(name=arguments["name"], reason=_reason(arguments))
    # Only the ID is returned; the token/URL stays private (visible to admins in Discord settings).
    return _ok("create_webhook", t("Created webhook {name} in {name2}.", name=hook.name, name2=channel.name), {"webhook_id": _sid(hook)})


async def _send_webhook_message(context: Any, arguments: dict[str, Any]) -> Any:
    hook = await _find_webhook(context, arguments.get("webhook_id"))
    if not getattr(hook, "token", None):
        raise C.AdminToolError(t("This webhook cannot be used by the bot (no token available)."))
    if not arguments.get("content") and arguments.get("embed") is None:
        raise ValueError(t("Provide content and/or embed."))
    options: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none(), "wait": True}
    if arguments.get("content"):
        options["content"] = arguments["content"]
    if arguments.get("embed") is not None:
        options["embed"] = build_embed(arguments["embed"])
    if arguments.get("username"):
        options["username"] = arguments["username"]
    if arguments.get("avatar_url"):
        options["avatar_url"] = arguments["avatar_url"]
    await hook.send(**options)
    return _ok("send_webhook_message", t("Sent a message through webhook {name}.", name=hook.name))


async def _delete_webhook(context: Any, arguments: dict[str, Any]) -> Any:
    hook = await _find_webhook(context, arguments.get("webhook_id"))
    name = hook.name
    await hook.delete(reason=_reason(arguments))
    return _ok("delete_webhook", t("Deleted webhook {name}.", name=name))


# --------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------


async def _list_invites(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    invites = await guild.invites()
    return {
        "invites": [
            {
                "code": getattr(invite, "code", None),
                "url": getattr(invite, "url", None),
                "channel_id": _sid(getattr(invite, "channel", None)),
                "inviter_id": _sid(getattr(invite, "inviter", None)),
                "uses": getattr(invite, "uses", None),
                "max_uses": getattr(invite, "max_uses", None),
                "max_age": getattr(invite, "max_age", None),
                "temporary": getattr(invite, "temporary", None),
                "expires_at": C._isoformat_or_none(getattr(invite, "expires_at", None)),
            }
            for invite in list(invites)[:100]
        ]
    }


async def _create_invite(context: Any, arguments: dict[str, Any]) -> Any:
    channel = resolve_any_channel(context, arguments.get("channel_id"))
    if C._normalized_channel_type(channel) in ("category",) or C._normalized_channel_type(channel).endswith("thread"):
        raise ValueError(t("Invites need a text, voice, stage or forum channel."))
    invite = await channel.create_invite(
        max_age=arguments.get("max_age_seconds", 86400),
        max_uses=arguments.get("max_uses", 0),
        temporary=arguments.get("temporary") is True,
        unique=arguments.get("unique", True) is not False,
        reason=_reason(arguments),
    )
    return _ok("create_invite", t("Invite created: {url}", url=invite.url), {"url": invite.url, "code": invite.code})


async def _revoke_invite(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    code = arguments["code"].rstrip("/").rsplit("/", 1)[-1]
    for invite in await guild.invites():
        if getattr(invite, "code", None) == code:
            await invite.delete(reason=_reason(arguments))
            return _ok("revoke_invite", t("Revoked invite {code}.", code=code))
    raise C.AdminToolError(t("Invite {code} was not found on this server.", code=code))


# --------------------------------------------------------------------------
# emojis and stickers
# --------------------------------------------------------------------------


async def _list_emojis_and_stickers(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    return {
        "emoji_limit": getattr(guild, "emoji_limit", None),
        "sticker_limit": getattr(guild, "sticker_limit", None),
        "emojis": [
            {"id": _sid(emoji), "name": getattr(emoji, "name", None), "animated": getattr(emoji, "animated", None)}
            for emoji in getattr(guild, "emojis", None) or []
        ],
        "stickers": [
            {"id": _sid(sticker), "name": getattr(sticker, "name", None), "emoji": getattr(sticker, "emoji", None)}
            for sticker in getattr(guild, "stickers", None) or []
        ],
    }


def _find_emoji(context: Any, value: Any) -> Any:
    guild = C._require_guild(context)
    return C._find_by_id(getattr(guild, "emojis", None) or [], C.parse_snowflake(value, "emoji_id"), "Emoji")


async def _create_emoji(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    data = await C.read_request_attachment(context, arguments.get("attachment_id"), max_bytes=MAX_EMOJI_BYTES, label="Emoji image")
    roles = [C._resolve_role(context, value) for value in arguments.get("role_ids") or []]
    options: dict[str, Any] = {"name": arguments["name"], "image": data, "reason": _reason(arguments)}
    if roles:
        options["roles"] = roles
    emoji = await guild.create_custom_emoji(**options)
    return _ok("create_emoji", t("Created emoji :{name}:.", name=emoji.name), {"emoji_id": _sid(emoji)})


async def _rename_emoji(context: Any, arguments: dict[str, Any]) -> Any:
    emoji = _find_emoji(context, arguments.get("emoji_id"))
    old = emoji.name
    await emoji.edit(name=arguments["name"], reason=_reason(arguments))
    return _ok("rename_emoji", t("Renamed emoji :{old}: to :{name}:.", old=old, name=arguments['name']))


async def _delete_emoji(context: Any, arguments: dict[str, Any]) -> Any:
    emoji = _find_emoji(context, arguments.get("emoji_id"))
    name = emoji.name
    await emoji.delete(reason=_reason(arguments))
    return _ok("delete_emoji", t("Deleted emoji :{name}:.", name=name))


async def _create_sticker(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    data = await C.read_request_attachment(
        context,
        arguments.get("attachment_id"),
        max_bytes=MAX_STICKER_BYTES,
        label="Sticker image",
        allowed_types=STICKER_CONTENT_TYPES,
    )
    extension = "gif" if data.startswith((b"GIF87a", b"GIF89a")) else "png"
    sticker = await guild.create_sticker(
        name=arguments["name"],
        description=arguments.get("description") or "",
        emoji=arguments["related_emoji"],
        file=discord.File(io.BytesIO(data), filename=f"sticker.{extension}"),
        reason=_reason(arguments),
    )
    return _ok("create_sticker", t("Created sticker {name}.", name=sticker.name), {"sticker_id": _sid(sticker)})


async def _delete_sticker(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    sticker = C._find_by_id(getattr(guild, "stickers", None) or [], C.parse_snowflake(arguments.get("sticker_id"), "sticker_id"), "Sticker")
    name = sticker.name
    await sticker.delete(reason=_reason(arguments))
    return _ok("delete_sticker", t("Deleted sticker {name}.", name=name))


# --------------------------------------------------------------------------
# scheduled events
# --------------------------------------------------------------------------


def parse_future_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(t("{field} must be an ISO 8601 date-time string.", field=field))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(t("{field} must be ISO 8601, e.g. 2026-10-05T18:00:00+03:00.", field=field)) from exc
    if parsed.tzinfo is None:
        raise ValueError(t("{field} must include a UTC offset, e.g. 2026-10-05T18:00:00+03:00.", field=field))
    if parsed <= datetime.now(timezone.utc):
        raise ValueError(t("{field} must be in the future.", field=field))
    return parsed


def _serialize_event(event: Any) -> dict[str, Any]:
    return {
        "id": _sid(event),
        "name": getattr(event, "name", None),
        "status": getattr(getattr(event, "status", None), "name", None),
        "type": getattr(getattr(event, "entity_type", None), "name", None),
        "channel_id": C._snowflake_to_string(getattr(event, "channel_id", None)),
        "location": getattr(event, "location", None),
        "start_time": C._isoformat_or_none(getattr(event, "start_time", None)),
        "end_time": C._isoformat_or_none(getattr(event, "end_time", None)),
        "interested": getattr(event, "user_count", None),
    }


async def _list_scheduled_events(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    guild = C._require_guild(context)
    events = await guild.fetch_scheduled_events()
    return {"events": [_serialize_event(event) for event in events]}


def _event_location(context: Any, arguments: dict[str, Any], *, required: bool) -> dict[str, Any]:
    kind = arguments.get("location_type")
    options: dict[str, Any] = {}
    if kind is None:
        if required:
            raise ValueError(t("location_type is required."))
        return options
    if kind == "external":
        if not arguments.get("location"):
            raise ValueError(t("External events need location."))
        options["entity_type"] = discord.EntityType.external
        options["location"] = arguments["location"]
        options["channel"] = None
    else:
        if not arguments.get("channel_id"):
            raise ValueError(t("Voice/stage events need channel_id."))
        channel = resolve_any_channel(context, arguments["channel_id"])
        expected = "voice" if kind == "voice" else "stage_voice"
        if C._normalized_channel_type(channel) != expected:
            raise ValueError(t("channel_id must be a {kind} channel.", kind=kind))
        options["entity_type"] = discord.EntityType.voice if kind == "voice" else discord.EntityType.stage_instance
        options["channel"] = channel
    return options


async def _create_scheduled_event(context: Any, arguments: dict[str, Any]) -> Any:
    guild = C._require_guild(context)
    start = parse_future_time(arguments.get("start_time"), "start_time")
    options: dict[str, Any] = {
        "name": arguments["name"],
        "start_time": start,
        "privacy_level": discord.PrivacyLevel.guild_only,
        "reason": _reason(arguments),
    }
    options.update(_event_location(context, arguments, required=True))
    if options.get("channel") is None:
        options.pop("channel", None)
    if arguments.get("end_time"):
        end = parse_future_time(arguments["end_time"], "end_time")
        if end <= start:
            raise ValueError(t("end_time must be after start_time."))
        options["end_time"] = end
    elif options.get("entity_type") == discord.EntityType.external:
        raise ValueError(t("External events need end_time."))
    if arguments.get("description"):
        options["description"] = arguments["description"]
    event = await guild.create_scheduled_event(**options)
    return _ok("create_scheduled_event", t("Created event {name}.", name=event.name), {"event_id": _sid(event)})


async def _find_event(context: Any, value: Any) -> Any:
    guild = C._require_guild(context)
    event_id = C.parse_snowflake(value, "event_id")
    getter = getattr(guild, "get_scheduled_event", None)
    event = getter(event_id) if callable(getter) else None
    if event is None:
        for item in await guild.fetch_scheduled_events():
            if getattr(item, "id", None) == event_id:
                return item
        raise C.AdminToolError(t("Event {event_id} was not found.", event_id=event_id))
    return event


async def _edit_scheduled_event(context: Any, arguments: dict[str, Any]) -> Any:
    event = await _find_event(context, arguments.get("event_id"))
    options: dict[str, Any] = {}
    for field in ("name", "description"):
        if arguments.get(field) is not None:
            options[field] = arguments[field]
    if arguments.get("start_time"):
        options["start_time"] = parse_future_time(arguments["start_time"], "start_time")
    if arguments.get("end_time"):
        options["end_time"] = parse_future_time(arguments["end_time"], "end_time")
    options.update(_event_location(context, arguments, required=False))
    if not options:
        raise ValueError(t("Nothing to change."))
    await event.edit(reason=_reason(arguments), **options)
    return _ok("edit_scheduled_event", t("Updated event {name}.", name=event.name))


async def _cancel_scheduled_event(context: Any, arguments: dict[str, Any]) -> Any:
    event = await _find_event(context, arguments.get("event_id"))
    name = event.name
    if arguments.get("delete") is True:
        await event.delete(reason=_reason(arguments))
        return _ok("cancel_scheduled_event", t("Deleted event {name}.", name=name))
    await event.cancel(reason=_reason(arguments))
    return _ok("cancel_scheduled_event", t("Cancelled event {name}.", name=name))


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def build_tools(core: Any) -> list[tuple[Any, Any]]:
    global C
    C = core
    T = core.ToolDefinition
    R = core.REASON_PROPERTY
    channel_id = core.CHANNEL_ID_PROPERTY
    message_id = _sf("Message ID.")
    embed = embed_schema()
    archive = {"type": "integer", "enum": list(ARCHIVE_MINUTES), "description": "Auto-archive after N minutes of inactivity."}
    event_location = {
        "location_type": _enum(("voice", "stage", "external"), "Where the event happens."),
        "channel_id": _sf("Voice or stage channel (voice/stage events)."),
        "location": _str("Place or link (external events).", max_length=100),
    }
    return [
        # messages
        (T("send_embed", "write", "normal", "Send a rich embed (title, text, color, fields, images) with optional plain text.", _schema({
            "channel_id": channel_id, "embed": embed, "content": _str("Plain text above the embed.", max_length=2000), "reason": R,
        }, ["channel_id", "embed"]), "messages"), _send_embed),
        (T("edit_bot_message", "write", "normal", "Edit a message previously sent by this bot (text and/or embed).", _schema({
            "channel_id": channel_id, "message_id": message_id,
            "content": _str("New text (empty string removes it).", min_length=0, max_length=2000),
            "embed": embed, "remove_embeds": _bool("Remove embeds."), "reason": R,
        }, ["channel_id", "message_id"]), "messages"), _edit_bot_message),
        (T("delete_message", "write", "destructive", "Delete one message.", _schema({
            "channel_id": channel_id, "message_id": message_id, "reason": R,
        }, ["channel_id", "message_id"]), "messages"), _delete_message),
        (T("pin_message", "write", "normal", "Pin (or unpin) a message.", _schema({
            "channel_id": channel_id, "message_id": message_id, "unpin": _bool("Unpin instead of pin."), "reason": R,
        }, ["channel_id", "message_id"]), "messages"), _pin_message),
        (T("add_reaction", "write", "normal", "Add reactions (unicode emoji or this server's custom emoji names) to a message.", _schema({
            "channel_id": channel_id, "message_id": message_id,
            "emojis": {"type": "array", "minItems": 1, "maxItems": 20, "items": _str("Emoji.", max_length=64), "description": "Emojis to add in order."},
            "reason": R,
        }, ["channel_id", "message_id", "emojis"]), "messages"), _add_reaction),
        (T("create_poll", "write", "normal", "Post a native Discord poll.", _schema({
            "channel_id": channel_id,
            "question": _str("Question.", max_length=300),
            "answers": {"type": "array", "minItems": 1, "maxItems": 10, "description": "Answers.", "items": {
                "type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": _str("Answer text.", max_length=55), "emoji": _str("Optional emoji.", max_length=64)},
            }},
            "duration_hours": _int("Poll duration in hours (default 24).", maximum=768),
            "multiple": _bool("Allow multiple answers."),
            "content": _str("Optional text above the poll.", max_length=2000),
            "reason": R,
        }, ["channel_id", "question", "answers"]), "messages"), _create_poll),
        (T("publish_message", "write", "normal", "Publish (crosspost) a message from an announcement channel.", _schema({
            "channel_id": channel_id, "message_id": message_id, "reason": R,
        }, ["channel_id", "message_id"]), "messages"), _publish_message),
        # threads
        (T("list_active_threads", "read", "read", "List active threads and forum posts.", _schema(), "threads"), _list_active_threads),
        (T("create_thread", "write", "normal", "Create a thread in a text channel (from a message or standalone, public or private).", _schema({
            "channel_id": channel_id, "name": core._name_property("Thread name."),
            "message_id": _sf("Start the thread from this message."),
            "private": _bool("Private thread (standalone only)."),
            "auto_archive_minutes": archive,
            "slowmode_seconds": _int("Slowmode in seconds.", minimum=0, maximum=21600),
            "reason": R,
        }, ["channel_id", "name"]), "threads"), _create_thread),
        (T("create_forum_post", "write", "normal", "Create a post in a forum channel.", _schema({
            "channel_id": channel_id, "title": core._name_property("Post title."),
            "content": _str("Post text.", max_length=2000), "embed": embed,
            "tag_names": {"type": "array", "maxItems": 5, "items": _str("Tag name.", max_length=20), "description": "Existing forum tags to apply."},
            "reason": R,
        }, ["channel_id", "title", "content"]), "threads"), _create_forum_post),
        (T("edit_thread", "write", "normal", "Rename, archive, lock, pin or change slowmode of a thread/forum post.", _schema({
            "thread_id": _sf("Thread ID."), "name": core._name_property("New name."),
            "archived": _bool("Archive or unarchive."), "locked": _bool("Lock or unlock."), "pinned": _bool("Pin in forum."),
            "slowmode_seconds": _int("Slowmode in seconds.", minimum=0, maximum=21600), "auto_archive_minutes": archive, "reason": R,
        }, ["thread_id"]), "threads"), _edit_thread),
        (T("set_forum_tags", "write", "normal", "Replace a forum channel's available tags.", _schema({
            "channel_id": channel_id,
            "tags": {"type": "array", "maxItems": 20, "description": "Full new tag list.", "items": {
                "type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {"name": _str("Tag name.", max_length=20), "emoji": _str("Optional unicode emoji.", max_length=32), "moderated": _bool("Moderators only.")},
            }},
            "reason": R,
        }, ["channel_id", "tags"]), "threads"), _set_forum_tags),
        # webhooks
        (T("list_webhooks", "read", "read", "List webhooks (IDs and names only; no tokens).", _schema(), "webhooks"), _list_webhooks),
        (T("create_webhook", "write", "normal", "Create a webhook in a channel (returns its ID; the URL stays private).", _schema({
            "channel_id": channel_id, "name": _str("Webhook name.", max_length=80), "reason": R,
        }, ["channel_id", "name"]), "webhooks"), _create_webhook),
        (T("send_webhook_message", "write", "normal", "Send a message through a webhook with a custom name/avatar.", _schema({
            "webhook_id": _sf("Webhook ID."), "content": _str("Text.", max_length=2000), "embed": embed,
            "username": _str("Display name for this message.", max_length=80), "avatar_url": _url("Avatar image (https)."),
        }, ["webhook_id"]), "webhooks"), _send_webhook_message),
        (T("delete_webhook", "write", "destructive", "Delete a webhook.", _schema({"webhook_id": _sf("Webhook ID."), "reason": R}, ["webhook_id"]), "webhooks"), _delete_webhook),
        # invites
        (T("list_invites", "read", "read", "List active invites.", _schema(), "invites"), _list_invites),
        (T("create_invite", "write", "normal", "Create an invite link for a channel.", _schema({
            "channel_id": channel_id,
            "max_age_seconds": _int("Lifetime in seconds, 0 = never expires (default 86400).", minimum=0, maximum=604800),
            "max_uses": _int("Max uses, 0 = unlimited.", minimum=0, maximum=100),
            "temporary": _bool("Joined users are kicked when they go offline unless given a role."),
            "unique": _bool("Always create a new code (default true)."),
            "reason": R,
        }, ["channel_id"]), "invites"), _create_invite),
        (T("revoke_invite", "write", "destructive", "Revoke an invite by code or link.", _schema({
            "code": {"type": "string", "pattern": INVITE_CODE_PATTERN, "description": "Invite code or discord.gg link."}, "reason": R,
        }, ["code"]), "invites"), _revoke_invite),
        # expressions
        (T("list_emojis_and_stickers", "read", "read", "List custom emojis and stickers with limits.", _schema(), "expressions"), _list_emojis_and_stickers),
        (T("create_emoji", "write", "normal", "Create a custom emoji from an image attached to the request (max 256 KB).", _schema({
            "name": {"type": "string", "pattern": EMOJI_NAME_PATTERN, "description": "Emoji name (letters, digits, underscore)."},
            "attachment_id": _sf("ID of an image attached to this request."),
            "role_ids": {"type": "array", "items": dict(core.SNOWFLAKE_ITEM), "maxItems": 10, "uniqueItems": True, "description": "Only these roles may use it."},
            "reason": R,
        }, ["name", "attachment_id"]), "expressions"), _create_emoji),
        (T("rename_emoji", "write", "normal", "Rename a custom emoji.", _schema({
            "emoji_id": _sf("Emoji ID."), "name": {"type": "string", "pattern": EMOJI_NAME_PATTERN, "description": "New name."}, "reason": R,
        }, ["emoji_id", "name"]), "expressions"), _rename_emoji),
        (T("delete_emoji", "write", "destructive", "Delete a custom emoji.", _schema({"emoji_id": _sf("Emoji ID."), "reason": R}, ["emoji_id"]), "expressions"), _delete_emoji),
        (T("create_sticker", "write", "normal", "Create a sticker from an attached PNG/APNG/GIF (max 512 KB, 320x320).", _schema({
            "name": _str("Sticker name.", min_length=2, max_length=30),
            "description": _str("Description.", min_length=0, max_length=100),
            "related_emoji": _str("A unicode emoji describing it.", max_length=32),
            "attachment_id": _sf("ID of an image attached to this request."),
            "reason": R,
        }, ["name", "related_emoji", "attachment_id"]), "expressions"), _create_sticker),
        (T("delete_sticker", "write", "destructive", "Delete a sticker.", _schema({"sticker_id": _sf("Sticker ID."), "reason": R}, ["sticker_id"]), "expressions"), _delete_sticker),
        # events
        (T("list_scheduled_events", "read", "read", "List scheduled events.", _schema(), "events"), _list_scheduled_events),
        (T("create_scheduled_event", "write", "normal", "Create a scheduled event (voice, stage or external).", _schema({
            "name": _str("Event name.", max_length=100),
            "start_time": _str("ISO 8601 start with UTC offset, e.g. 2026-10-05T18:00:00+03:00.", max_length=40),
            "end_time": _str("ISO 8601 end with offset (required for external).", max_length=40),
            "description": _str("Description.", min_length=0, max_length=1000),
            **event_location,
            "reason": R,
        }, ["name", "start_time", "location_type"]), "events"), _create_scheduled_event),
        (T("edit_scheduled_event", "write", "normal", "Edit a scheduled event.", _schema({
            "event_id": _sf("Event ID."),
            "name": _str("Event name.", max_length=100),
            "description": _str("Description.", min_length=0, max_length=1000),
            "start_time": _str("ISO 8601 start with offset.", max_length=40),
            "end_time": _str("ISO 8601 end with offset.", max_length=40),
            **event_location,
            "reason": R,
        }, ["event_id"]), "events"), _edit_scheduled_event),
        (T("cancel_scheduled_event", "write", "destructive", "Cancel (or delete) a scheduled event.", _schema({
            "event_id": _sf("Event ID."), "delete": _bool("Delete instead of cancel."), "reason": R,
        }, ["event_id"]), "events"), _cancel_scheduled_event),
    ]
