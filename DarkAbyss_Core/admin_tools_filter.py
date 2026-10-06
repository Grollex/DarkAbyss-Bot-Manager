"""AI tools for Kairo's content filter ("Кайро, начни фильтровать @Вася").

Loaded by admin_tools like the other extensions. The filter list itself is
``content_filter.FilterStore`` (context.content_filter); naming a member here
is the same as ``/filter add`` or the Manager's Content Filter page. Adding and
removing are normal-risk writes (the AI plan is approved first) and follow the
usual anti-escalation rules: nobody can put the owner, the bot, or someone at
or above their own highest role on the list.
"""

from __future__ import annotations

from typing import Any

from bot_i18n import t

C: Any = None


def _store(context: Any) -> Any:
    store = getattr(context, "content_filter", None)
    if store is None:
        raise C.AdminToolError(t("The content filter is not available on this bot."))
    return store


async def _watch(context: Any, arguments: dict[str, Any]) -> Any:
    store = _store(context)
    member = await C._resolve_member(context, arguments.get("member_id"))
    if getattr(member, "bot", False):
        raise C.AdminToolError(t("Bots are not filtered."))
    C.ensure_member_actionable(context, member, action="filter")
    added = store.watch(
        context.guild.id,
        member.id,
        name=getattr(member, "display_name", None) or str(member),
        added_by=str(context.requesting_user_id or "manager"),
        note=str(arguments.get("note") or ""),
    )
    message = (
        t("Now filtering {member}: insults and hostility get a mute of 30 minutes to 3 hours.", member=member)
        if added
        else t("{member} is already filtered.", member=member)
    )
    return C.ToolResult(True, "content_filter_watch", message)


async def _unwatch(context: Any, arguments: dict[str, Any]) -> Any:
    store = _store(context)
    member_id = C.parse_snowflake(arguments.get("member_id"), "member_id")
    removed = store.unwatch(context.guild.id, member_id)
    message = t("Stopped filtering <@{member_id}>.", member_id=member_id) if removed else t("<@{member_id}> was not filtered.", member_id=member_id)
    return C.ToolResult(True, "content_filter_unwatch", message)


async def _list(context: Any, arguments: dict[str, Any]) -> Any:
    store = _store(context)
    guild = store.guild(context.guild.id)
    if not guild.watched:
        return C.ToolResult(True, "content_filter_list", t("Nobody is filtered on this server."), {"members": []})
    members = [
        {"member_id": str(member.user_id), "name": member.name, "note": member.note, "mutes": sum(1 for action in guild.actions if action.user_id == member.user_id and action.result == "muted")}
        for member in guild.watched.values()
    ]
    lines = [t("Filtered members:")] + [
        f"- <@{item['member_id']}>" + (f" ({item['note']})" if item["note"] else "") + t(" — mutes: {count}", count=item["mutes"]) for item in members
    ]
    return C.ToolResult(True, "content_filter_list", "\n".join(lines), {"members": members})


def build_tools(core: Any) -> list[tuple[Any, Any]]:
    global C
    C = core
    T = core.ToolDefinition
    S = core._object_schema
    note = core._string_property("Optional note: why this member is filtered.", max_length=120)
    return [
        (
            T(
                "content_filter_watch",
                "write",
                "normal",
                "Start filtering a member: insults and hostility in their messages get a 30 min to 3 h mute (Kairo decides how long).",
                S({"member_id": core.MEMBER_ID_PROPERTY, "note": note}, ["member_id"]),
                "moderation",
            ),
            _watch,
        ),
        (
            T("content_filter_unwatch", "write", "normal", "Stop filtering a member.", S({"member_id": core.MEMBER_ID_PROPERTY}, ["member_id"]), "moderation"),
            _unwatch,
        ),
        (T("content_filter_list", "read", "read", "Show the members whose messages are filtered on this server.", S(), "moderation"), _list),
    ]
