"""Discord AI transport for the Admin bot (AI-4 /ai, AI-5 control channel).

This module adapts the provider-neutral AI-3A orchestrator to Discord without
coupling ``ai_orchestrator`` to Discord. Slash /ai (ephemeral) and natural
messages in one configured control channel (public) share one code path. It
owns everything transport-specific:

* explicit AI whitelist authorization (separate from /execute),
* confirmation ownership (requesting user + guild + channel),
* safe ephemeral rendering with mentions suppressed and Discord size limits,
* the injected Admin Tool executor with execution-time re-authorization.

AI is an accessory: this module imports no AI/provider module at import time.
The orchestrator is created lazily on the first authorized /ai request, and any
AI construction/import failure produces a contained "unavailable" reply.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

import discord

import admin_terminal
import admin_tools
import ai_context
import ai_memory
import bot_i18n
from bot_i18n import t


AI_SOURCE = "/ai"
AI_PROMPT_MAX_CHARS = 2000
DISCORD_MESSAGE_LIMIT = 2000
AI_MESSAGE_CHUNK_CHARS = 1900
AI_MAX_RESPONSE_CHUNKS = 6
# Confirmation preview: exact, never truncated, split across bounded pages.
AI_PREVIEW_PAGE_CHARS = 1900
AI_PREVIEW_HEADER_RESERVE = 80
AI_PREVIEW_MIN_PIECE_CHARS = 200
# Hard bound on preview pages. The current Admin Tool contract worst realistic
# case (8 calls x send_message 2000-char content + 512-char reason) fits well
# below it; anything larger fails closed (no Approve, confirmation cancelled).
AI_MAX_PREVIEW_PAGES = 20
AI_MAX_TOOL_SUMMARY_CHARS = 200
# Must not exceed ai_orchestrator.CONFIRMATION_TTL_SECONDS (900 s).
CONFIRMATION_VIEW_TIMEOUT_SECONDS = 600.0
TASK_MODES: dict[str, str] = {
    "routine": "ROUTINE",
    "planner": "PLANNER",
    "creative": "CREATIVE",
}
DEFAULT_TASK_MODE = "routine"

# Discord member type used for AI authorization. Overridable in tests only.
MEMBER_TYPES: tuple[type, ...] = (discord.Member,)

ACCESS_DENIED_MESSAGE = "Access denied. /ai requires an explicit AI user or role whitelist entry."
UNAVAILABLE_MESSAGE = "AI is currently unavailable. No action was executed."
CONFIG_UNAVAILABLE_MESSAGE = "Admin configuration is unavailable. No action was executed."
NOT_OWNER_MESSAGE = "Only the user who started this /ai request can respond to it, in the same server and channel."
INACTIVE_MESSAGE = "This confirmation is no longer active. Nothing from it was executed by this click."
EXPIRED_MESSAGE = "This confirmation expired or was already used. Nothing from it was executed."
# Used when the orchestrator raised after it may already have run tools: never
# claim that nothing ran, never imply rollback.
UNEXPECTED_FAILURE_MESSAGE = (
    "The AI request stopped unexpectedly. Any actions that already ran were not undone; "
    "check the server and the audit log."
)
# Public message of a successful AI-3A rejection (OrchestratorResult.message).
CORE_REJECTED_MESSAGE = "Confirmation rejected."
EARLIER_ACTIONS_NOTE = (
    "Earlier actions from this request may already have executed and are listed above; "
    "they were not undone."
)


def unreviewable_plan_message(*, too_large: bool, cancelled: bool, earlier_actions: bool) -> str:
    """Honest fail-closed wording about the CURRENT plan only.

    Never claims that nothing at all was executed when earlier actions exist,
    never implies rollback, and only says "cancelled" when the core rejection
    was confirmed.
    """
    subject = t("The new AI action plan") if earlier_actions else t("The AI action plan")
    reason = t("was too large to review safely") if too_large else t("could not be displayed safely")
    if cancelled:
        text = t("{subject} {reason}. It was cancelled, and no action from this plan was executed.", subject=subject, reason=reason)
    else:
        text = t(
            "{subject} {reason}. No approval control was created and no action from this plan "
            "was executed. The pending request will expire automatically.",
            subject=subject,
            reason=reason,
        )
    if earlier_actions:
        text += "\n" + t(EARLIER_ACTIONS_NOTE)
    return text


AUTH_REVOKED_TOOL_MESSAGE = "AI access is no longer authorized for this user; the action was not executed."


def no_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def actor_has_ai_access(actor: Any, guild: Any, config: dict[str, Any]) -> bool:
    """Explicit AI whitelist only.

    Allowed iff the actor is a guild Member whose ID is in ``ai_allowed_user_ids``
    or who currently has a role in ``ai_allowed_role_ids``. Nothing else grants
    access: no Administrator shortcut, no owner shortcut, no /execute whitelist.
    """
    if guild is None or not isinstance(actor, MEMBER_TYPES):
        return False
    actor_guild = getattr(actor, "guild", None)
    if actor_guild is not None and getattr(actor_guild, "id", None) != getattr(guild, "id", None):
        return False
    try:
        user_ids = {int(value) for value in config.get("ai_allowed_user_ids") or []}
        role_ids = {int(value) for value in config.get("ai_allowed_role_ids") or []}
    except (TypeError, ValueError):
        return False
    if not user_ids and not role_ids:
        return False
    if getattr(actor, "id", None) in user_ids:
        return True
    return any(getattr(role, "id", None) in role_ids for role in getattr(actor, "roles", None) or ())


# --------------------------------------------------------------------------
# Rendering helpers (transport-specific; never inside ai_orchestrator)
# --------------------------------------------------------------------------


def clip_text(text: str, limit: int) -> str:
    text = text if isinstance(text, str) else ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def chunk_text(text: str, *, chunk_chars: int = AI_MESSAGE_CHUNK_CHARS, max_chunks: int = AI_MAX_RESPONSE_CHUNKS) -> list[str]:
    text = text if isinstance(text, str) else ""
    if not text:
        return []
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_chars:
            chunks.append(remaining)
            break
        # Prefer to split at a paragraph/line boundary in the last third of the chunk.
        cut = remaining.rfind("\n", chunk_chars * 2 // 3, chunk_chars)
        cut = cut + 1 if cut != -1 else chunk_chars
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        marker = "\n" + t("... (response truncated)")
        chunks[-1] = chunks[-1][: chunk_chars - len(marker)] + marker
    return chunks


def _code_safe(text: str) -> str:
    # Prevent breaking out of an inline code block.
    return text.replace("`", "'")


class PlanPreviewError(Exception):
    """The exact plan could not be rendered within the safe preview bound."""


class PlanPreviewTooLarge(PlanPreviewError):
    """The exact plan needs more preview pages than the hard bound allows."""


# Characters escaped (as JSON \\uXXXX escapes, which keep the value exact) so
# executable values can neither break the code-block display nor hide content:
# backticks plus control/format/invisible/separator/private/unassigned chars.
_ESCAPED_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def _escape_display_char(char: str) -> str:
    code = ord(char)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


def exact_json_display(value: Any) -> str:
    """Exact, reviewable JSON text for one validated value.

    json.loads(result) == value always holds; display-hostile characters are
    expressed as JSON escapes instead of being dropped or replaced.
    """
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PlanPreviewError("Plan value is not exact JSON.") from exc
    out = []
    for char in raw:
        if char == "`" or unicodedata.category(char) in _ESCAPED_CATEGORIES:
            out.append(_escape_display_char(char))
        else:
            out.append(char)
    text = "".join(out)
    if json.loads(text) != value:
        raise PlanPreviewError("Plan value could not be displayed exactly.")
    return text


_JSON_ATOM_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\.|.", re.DOTALL)


def _plan_items(tool_plan: Any) -> list[tuple[str, str, str]]:
    """Flatten the plan into ("text", block, "") and ("value", label, json) items."""
    calls = tuple(tool_plan)
    if not calls:
        raise PlanPreviewError("Empty plan.")
    items: list[tuple[str, str, str]] = []
    for index, call in enumerate(calls, start=1):
        public = call.public_dict()
        tool_name = public.get("tool_name") if isinstance(public, dict) else None
        risk = public.get("risk") if isinstance(public, dict) else None
        arguments = public.get("arguments") if isinstance(public, dict) else None
        if not isinstance(tool_name, str) or not tool_name or not isinstance(risk, str) or not risk:
            raise PlanPreviewError("Plan call is missing its tool name or risk.")
        if not isinstance(arguments, dict):
            raise PlanPreviewError("Plan call arguments are not a mapping.")
        items.append(
            (
                "text",
                t("**Action {index} of {count}:** tool `{tool}` | risk `{risk}`", index=index, count=len(calls), tool=exact_json_display(tool_name), risk=exact_json_display(risk)),
                "",
            )
        )
        if not arguments:
            items.append(("text", t("(no arguments)"), ""))
        for key in sorted(arguments):
            items.append(("value", t("argument `{name}`", name=exact_json_display(key)), exact_json_display(arguments[key])))
    return items


def _value_block(label: str, part: int, final: bool, piece: str) -> str:
    if part == 1 and final:
        suffix = " " + t("(exact JSON value)")
    elif final:
        suffix = " " + t("(exact JSON value, part {part}, final; parts join without separators)", part=part)
    else:
        suffix = " " + t("(exact JSON value, part {part}, continues)", part=part)
    return f"{label}{suffix}:\n```json\n{piece}\n```"


def render_tool_plan_pages(tool_plan: Any) -> list[str]:
    """Render EVERY tool and EVERY validated argument value, never truncated.

    The plan may span several bounded pages; long values are split only at
    JSON-atom boundaries and labelled as consecutive parts. Raises
    PlanPreviewError if the exact plan cannot be rendered and
    PlanPreviewTooLarge if it needs more than AI_MAX_PREVIEW_PAGES pages;
    callers must then fail closed.
    """
    items = _plan_items(tool_plan)
    body_limit = AI_PREVIEW_PAGE_CHARS - AI_PREVIEW_HEADER_RESERVE
    bodies: list[str] = []
    current = ""

    def push() -> None:
        nonlocal current
        bodies.append(current)
        current = ""
        if len(bodies) > AI_MAX_PREVIEW_PAGES:
            raise PlanPreviewTooLarge("Plan preview exceeds the page bound.")

    def append(block: str) -> None:
        nonlocal current
        if len(block) > body_limit:
            raise PlanPreviewError("Plan block exceeds the page size.")
        if current and len(current) + 1 + len(block) > body_limit:
            push()
        current = f"{current}\n{block}" if current else block

    for kind, label, display in items:
        if kind == "text":
            append(label)
            continue
        atoms = _JSON_ATOM_RE.findall(display)
        if "".join(atoms) != display:
            raise PlanPreviewError("Plan value could not be split exactly.")
        position = 0
        part = 1
        overhead = len(_value_block(label, 99, True, "")) + 1
        while position < len(atoms) or (position == 0 and not atoms):
            available = body_limit - (len(current) + 1 if current else 0) - overhead
            if available < AI_PREVIEW_MIN_PIECE_CHARS and current:
                push()
                continue
            piece = ""
            end = position
            while end < len(atoms) and len(piece) + len(atoms[end]) <= available:
                piece += atoms[end]
                end += 1
            if end == position and atoms:
                raise PlanPreviewError("Plan value could not be split.")
            final = end >= len(atoms)
            append(_value_block(label, part, final, piece))
            position = end
            part += 1
            if not atoms:
                break
    if current:
        push()
    total = len(bodies)
    pages = [t("**AI action plan - review page {number} of {total}**", number=number, total=total) + f"\n{body}" for number, body in enumerate(bodies, start=1)]
    if any(len(page) > AI_PREVIEW_PAGE_CHARS for page in pages):
        raise PlanPreviewError("Plan page exceeds the page size.")
    return pages


def _outline_name(value: Any) -> str:
    return _code_safe(_printable_text(value, 60))


def _printable_text(value: Any, limit: int) -> str:
    return "".join(char for char in str(value or "") if char.isprintable())[:limit]


def render_plan_outline(tool_plan: Any) -> str:
    """Readable outline of blueprint actions, shown IN ADDITION to the exact pages.

    Derived only from the validated arguments; the exact JSON pages remain the
    authoritative review. Empty when the plan has no blueprint call.
    """
    lines: list[str] = []
    for call in tuple(tool_plan or ()):
        public = call.public_dict() if hasattr(call, "public_dict") else {}
        if public.get("tool_name") != "apply_server_blueprint":
            continue
        blueprint = (public.get("arguments") or {}).get("blueprint") or {}
        lines.append(t("**Blueprint outline** (existing objects with the same name are reused, not changed):"))
        roles = [_outline_name(role.get("name")) for role in blueprint.get("roles") or [] if isinstance(role, dict)]
        if roles:
            lines.append(t("Roles: {names}", names=", ".join(roles)))
        for category in blueprint.get("categories") or []:
            if not isinstance(category, dict):
                continue
            private = category.get("private_to_roles") or []
            suffix = " " + t("(private: {roles})", roles=", ".join(_outline_name(name) for name in private)) if private else ""
            channels = ", ".join(
                f"{_outline_name(item.get('name'))} [{_outline_name(item.get('type'))}]"
                for item in category.get("channels") or []
                if isinstance(item, dict)
            )
            lines.append(t("Category {name}{suffix}: {channels}", name=_outline_name(category.get("name")), suffix=suffix, channels=channels or "-"))
        loose = [
            f"{_outline_name(item.get('name'))} [{_outline_name(item.get('type'))}]"
            for item in blueprint.get("channels") or []
            if isinstance(item, dict)
        ]
        if loose:
            lines.append(t("Without category: {channels}", channels=", ".join(loose)))
    return clip_text("\n".join(lines), AI_MESSAGE_CHUNK_CHARS) if lines else ""


def render_confirmation_control(tool_plan: Any, tool_risk: Any, page_count: int) -> str:
    risk_value = getattr(tool_risk, "value", None)
    if not isinstance(risk_value, str):
        raise PlanPreviewError("Plan risk is unknown.")
    count = len(tuple(tool_plan))
    return t(
        "**Confirm AI action plan:** {count} action(s), overall risk {risk}.\n"
        "Review all {pages} plan page(s) above. Approve executes exactly that plan; "
        "Cancel executes nothing.",
        count=count,
        risk=risk_value,
        pages=page_count,
    )


def render_executed_tools(executed_tools: Any) -> str:
    if not executed_tools:
        return ""
    lines = [t("Actions already executed:")]
    for tool in executed_tools:
        status = t("ok") if getattr(tool, "ok", False) is True else t("FAILED")
        name = clip_text(str(getattr(tool, "tool_name", "?")), 64)
        message = clip_text(str(getattr(tool, "message", "")), AI_MAX_TOOL_SUMMARY_CHARS)
        lines.append(f"- `{_code_safe(name)}` {status}: {message}")
    return clip_text("\n".join(lines), 900)


PROVIDER_DISPLAY_NAMES = {"groq": "Groq", "gemini": "Gemini"}
AI_ENGINE_FOOTER_CHARS = 90


def render_engine_footer(result: Any, planner_label: str = "") -> str:
    """Compact "which engine answered" line from public OrchestratorResult fields.

    Uses only provider_id / profile_id / model_id / fallback_used; never
    credential refs, attempts, errors or provider payloads. Empty if absent.
    With ``planner_label`` (two-stage requests) the planning engine is shown too.
    """
    label = engine_label(result)
    if planner_label and label:
        text = "".join(char for char in t("plan: {planner} | run: {engine}", planner=planner_label, engine=label) if char.isprintable())
        return "-# " + clip_text(text, AI_ENGINE_FOOTER_CHARS * 2)
    if not label:
        return ""
    return "-# " + clip_text(label, AI_ENGINE_FOOTER_CHARS)


def engine_label(result: Any) -> str:
    provider_id = getattr(result, "provider_id", None)
    profile_id = getattr(result, "profile_id", None)
    model_id = getattr(result, "model_id", None)
    parts = []
    if isinstance(provider_id, str) and provider_id:
        parts.append(PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id))
    for value in (profile_id, model_id):
        if isinstance(value, str) and value:
            parts.append(value)
    if not parts:
        return ""
    if getattr(result, "fallback_used", False) is True:
        parts.append(t("fallback"))
    text = " · ".join(_code_safe(part) for part in parts)
    return "".join(char for char in text if char.isprintable())


def with_footer(text: str, footer: str) -> str:
    if not footer:
        return text
    return f"{clip_text(text, AI_MESSAGE_CHUNK_CHARS)}\n{footer}"


def render_result_messages(result: Any) -> list[str]:
    """Render a non-confirmation orchestrator result into safe Discord texts."""
    status = getattr(getattr(result, "status", None), "value", None)
    message = clip_text(str(getattr(result, "message", "") or ""), 400)
    executed = render_executed_tools(getattr(result, "executed_tools", ()))
    if status == "COMPLETED":
        chunks = chunk_text(str(getattr(result, "content", "") or "")) or [t("(The AI returned no text.)")]
        if executed:
            chunks.append(executed)
        return chunks
    if status == "CANCELLED":
        if message == CORE_REJECTED_MESSAGE:
            text = t("Cancelled. Nothing from this plan was executed.")
        else:
            text = t(EXPIRED_MESSAGE)
    elif status == "UNAVAILABLE":
        text = t("AI is currently unavailable.") if executed else t(UNAVAILABLE_MESSAGE)
        if message.startswith("Provider failed."):
            text += "\n" + t("Reason: {reason}", reason=failure_reason(result))
    elif status == "INVALID_TOOL_PLAN":
        text = t("The AI proposed an invalid action plan; nothing from that plan was executed.")
        if message:
            text += "\n" + t("Reason: {reason}", reason=message)
    elif status == "TOOL_EXECUTION_FAILED":
        text = t("An AI action failed; remaining actions were not executed.")
        if message:
            text += "\n" + t("Reason: {reason}", reason=message)
    elif status == "LIMIT_REACHED":
        text = t("The AI request stopped because a safety limit was reached.")
        if message:
            text += "\n" + t("Reason: {reason}", reason=message)
    else:
        text = t("The AI request ended in an unexpected state. Nothing further was executed.")
    parts = [clip_text(text, AI_MESSAGE_CHUNK_CHARS)]
    if executed:
        parts.append(executed)
    return parts


# --------------------------------------------------------------------------
# AI-6: agent instructions, two-stage planning, tool routing, attachments
# --------------------------------------------------------------------------

MAX_REQUEST_ATTACHMENTS = 5
MAX_EXECUTOR_TOOLS = 30
MAX_PLAN_STEPS = 20
MAX_PLAN_TEXT_CHARS = 2400

# Fallback routing when no plan is available: lowercase substrings (ru/en).
KEYWORD_ROUTES: dict[str, tuple[str, ...]] = {
    "channels": ("канал", "категор", "войс", "голосов", "трибун", "форум", "channel", "category", "voice", "stage", "forum", "приват", "private"),
    "roles": ("рол", "role", "цвет", "color", "colour", "иерарх", "прав", "permission", "доступ"),
    "blueprint": ("сервер", "структур", "с нуля", "шаблон", "blueprint", "server", "structure", "template", "setup", "настрой", "спроектир", "построй", "откат", "undo"),
    "members": ("участник", "member", "ник", "nick", "перемест", "move", "заглуш", "deafen", "выдай", "give", "забер", "сними", "всем"),
    "moderation": ("бан", "ban", "кик", "kick", "мут", "mute", "тайм", "timeout", "очист", "удали сообщ", "purge", "аудит", "audit", "журнал", "локдаун", "lockdown", "заблок", "lock", "разблок"),
    "messages": ("сообщ", "напиш", "отправ", "embed", "эмбед", "опрос", "poll", "закреп", "pin", "реакц", "react", "анонс", "announce", "message", "send", "say", "скажи", "опублик"),
    "threads": ("ветк", "тред", "thread", "форум", "forum", "пост", "post", "тег", "tag"),
    "server": ("сервер", "иконк", "аватар", "баннер", "icon", "avatar", "banner", "название", "верификац", "verification", "онбординг", "onboarding", "welcome screen", "правил", "rules", "afk", "описание"),
    "automod": ("автомод", "automod", "фильтр", "filter", "запрещ", "мат", "ссылк", "link", "спам", "spam", "слов"),
    "webhooks": ("вебхук", "webhook", "хук"),
    "invites": ("инвайт", "приглаш", "invite"),
    "expressions": ("эмодзи", "эмоджи", "смайл", "emoji", "стикер", "sticker"),
    "events": ("событ", "ивент", "event", "мероприят", "турнир", "tournament"),
    "features": ("меню рол", "role menu", "кнопк", "button", "верифик", "verify", "приветств", "welcome", "автороль", "auto role", "каждые", "расписан", "schedule", "регулярн", "по таймер", "глюк"),
}
DEFAULT_FALLBACK_CATEGORIES = ("channels", "roles", "messages")


def route_tools_by_keywords(prompt: str) -> tuple[str, ...]:
    text = (prompt or "").lower()
    categories = [name for name, words in KEYWORD_ROUTES.items() if any(word in text for word in words)]
    if not categories:
        categories = list(DEFAULT_FALLBACK_CATEGORIES)
    names = list(admin_tools.tool_names_in_categories(("core",)))
    for name in admin_tools.tool_names_in_categories(categories):
        if name not in names:
            names.append(name)
    return tuple(names[: MAX_EXECUTOR_TOOLS + 10])


def _now_utc_text() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _guild_line(guild: Any) -> str:
    name = "".join(char for char in str(getattr(guild, "name", "") or "") if char.isprintable())[:100]
    return f'Discord server "{name}" (id {getattr(guild, "id", "?")})'


def planner_instruction(guild: Any, context_text: str = "") -> str:
    context = f"Request context:\n{context_text}\n" if context_text else ""
    return (
        f"You are the PLANNING stage of Kairo, the admin assistant bot of the {_guild_line(guild)}. "
        f"Current UTC time: {_now_utc_text()}.\n"
        + context
        + "Earlier user/assistant messages, if any, are the recent conversation; use them to resolve "
        "references such as 'it', 'him', 'that channel'.\n"
        "You cannot call tools. Decide how the request is handled and reply with ONE JSON object only "
        "(no markdown, no code fence):\n"
        '{"mode":"answer","answer":"<complete reply to the user>"} - only for questions or chat that need neither server data nor changes;\n'
        '{"mode":"act","tools":["tool_name",...],"steps":["short concrete step",...],"notes":"<optional: what is impossible or must be asked>"} '
        "- whenever the request needs server data or wants something done on the server.\n"
        "Rules: the user wants actions performed, not advice or tutorials. If the catalog can do it (even partly), use mode act. "
        f"List every tool the executor will need, including read tools to look up IDs (max {MAX_EXECUTOR_TOOLS}). "
        "To create several roles/categories/channels at once, plan apply_server_blueprint (one call) instead of many single creates. "
        "Steps must be concrete (names, colors, which roles see which channels). Never invent tool names. "
        "Write the answer and notes in the language required by the LANGUAGE RULE.\n"
        "Tool catalog:\n" + admin_tools.render_tool_catalog()
    )


def executor_instruction(
    guild: Any,
    plan: dict[str, Any] | None,
    context_text: str = "",
    message_content: bool | None = None,
) -> str:
    text = (
        f"You are Kairo, the admin assistant bot of the {_guild_line(guild)}. Current UTC time: {_now_utc_text()}.\n"
        "You act through the provided tools. The bot asks the user for approval where needed (buttons), "
        "so do not ask for confirmation in text - call the tools.\n"
        "- Perform the task with tool calls instead of describing what the user could do.\n"
        "- Use IDs from the request context or from read tools; never guess IDs. Find members by name with list_members.\n"
        "- Independent calls can go in one batch; calls that need an earlier result go in a later batch.\n"
        "- For several new roles/categories/channels use apply_server_blueprint with one complete blueprint.\n"
        "- If a tool result has ok=false, read its message: fix the arguments and retry, or explain the problem. "
        "Never repeat the same failing call unchanged.\n"
        "- Only claim what tool results confirm. If something is impossible with the available tools, say so briefly.\n"
        "- The AI cannot grant Administrator or act on roles/members at or above the requester's highest role.\n"
        "- Earlier user/assistant messages, if any, are the recent conversation.\n"
        "- Final reply: in the language required by the LANGUAGE RULE, short, plain Discord markdown, no tables."
    )
    if message_content is False:
        text += (
            "\n- Message text is NOT readable (Message Content Intent is off): message contents arrive empty "
            "and text-based purge filters are refused. Tell the user how to enable it if they need it."
        )
    if context_text:
        text += "\nRequest context:\n" + context_text
    if plan:
        steps = plan.get("steps") or []
        lines = [f"{index}. {step}" for index, step in enumerate(steps, start=1)]
        if plan.get("notes"):
            lines.append(f"Notes: {plan['notes']}")
        if lines:
            text += "\nPlan from the planning stage (follow it; adapt if tool results require):\n" + clip_text("\n".join(lines), MAX_PLAN_TEXT_CHARS)
    return text


def parse_plan(content: Any) -> dict[str, Any] | None:
    """Extract the planner JSON object. Returns None if it is not a usable plan."""
    if not isinstance(content, str):
        return None
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        raw = json.loads(content[start : end + 1])
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode == "answer" and isinstance(raw.get("answer"), str) and raw["answer"].strip():
        return {"mode": "answer", "answer": raw["answer"]}
    if mode != "act":
        return None
    tools = [name for name in raw.get("tools") or [] if isinstance(name, str) and name in admin_tools.TOOL_DEFINITIONS]
    steps = [str(step)[:300] for step in raw.get("steps") or [] if isinstance(step, (str, int, float))][:MAX_PLAN_STEPS]
    notes = raw.get("notes") if isinstance(raw.get("notes"), str) else ""
    return {"mode": "act", "tools": tools, "steps": steps, "notes": notes[:600]}


def executor_tool_names(plan: dict[str, Any] | None, prompt: str) -> tuple[str, ...]:
    """Tools for the executor: planned tools (+ core reads), or keyword routing as fallback."""
    if not plan or not plan.get("tools"):
        return route_tools_by_keywords(prompt)
    names = list(admin_tools.tool_names_in_categories(("core",)))
    for name in plan["tools"]:
        if name not in names:
            names.append(name)
    return tuple(names[: MAX_EXECUTOR_TOOLS + 8])


def _printable(text: Any, limit: int) -> str:
    return "".join(char for char in str(text or "") if char.isprintable())[:limit]


def collect_attachments(items: Any) -> dict[str, Any]:
    """Map attachment ID -> discord.Attachment for at most MAX_REQUEST_ATTACHMENTS files."""
    result: dict[str, Any] = {}
    for item in list(items or [])[:MAX_REQUEST_ATTACHMENTS]:
        attachment_id = getattr(item, "id", None)
        if isinstance(attachment_id, int) and not isinstance(attachment_id, bool) and attachment_id > 0:
            result[str(attachment_id)] = item
    return result


MENTION_DENIED_MESSAGE = "Sorry, you don't have permission to give me commands."
MENTION_EMPTY_MESSAGE = "I'm listening! Write the task right after the mention."
# One denial reply per member per window, so pinging the bot cannot spam the chat.
MENTION_DENIAL_COOLDOWN_SECONDS = 60.0
CONFIRMATION_MODES = ("plan", "strict")
RESET_WORDS = frozenset({"reset", "/reset", "!reset", "сброс", "/сброс", "забудь"})
MEMORY_CLEARED_MESSAGE = "Conversation memory for this channel was cleared."
MAX_APPROVAL_STEPS = 12
MAX_APPROVAL_STEP_CHARS = 200


def confirmation_mode(config: dict[str, Any]) -> str:
    """"plan" or "strict"; anything missing/unknown is the safe "strict"."""
    value = config.get("ai_confirmation_mode") if isinstance(config, dict) else None
    return value if value in CONFIRMATION_MODES else "strict"


def plan_write_tools(plan: dict[str, Any] | None) -> list[str]:
    if not plan:
        return []
    return [
        name
        for name in plan.get("tools") or []
        if name in admin_tools.TOOL_DEFINITIONS and admin_tools.TOOL_DEFINITIONS[name].kind == "write"
    ]


def render_plan_approval(plan: dict[str, Any]) -> str:
    """One readable approval message for a planner plan.

    Steps are model text (mentions are suppressed on send); the enforced
    boundary is the tool list: the executor only gets these tools, and every
    destructive one is still confirmed separately with its exact arguments.
    """
    steps = [_printable(step, MAX_APPROVAL_STEP_CHARS) for step in plan.get("steps") or [] if _printable(step, 1)]
    lines = [t("**AI plan - approve to run it**")]
    for index, step in enumerate(steps[:MAX_APPROVAL_STEPS], start=1):
        lines.append(f"{index}. {step}")
    if len(steps) > MAX_APPROVAL_STEPS:
        lines.append(t("... and {count} more step(s).", count=len(steps) - MAX_APPROVAL_STEPS))
    if not steps:
        lines.append(t("(The planner gave no step list.)"))
    writes = plan_write_tools(plan)
    destructive = [name for name in writes if admin_tools.TOOL_DEFINITIONS[name].risk == "destructive"]
    lines.append(t("Changes allowed: {tools}", tools=", ".join(f"`{_code_safe(name)}`" for name in writes)))
    if destructive:
        lines.append(
            t("Destructive actions ({tools}) will still ask for confirmation with the exact data.", tools=", ".join(f"`{_code_safe(name)}`" for name in destructive))
        )
    if plan.get("notes"):
        lines.append(t("Notes: {notes}", notes=_printable(plan["notes"], 300)))
    lines.append(t("Approve runs the normal actions of this plan without further prompts. Cancel changes nothing."))
    return clip_text("\n".join(lines), AI_MESSAGE_CHUNK_CHARS - AI_ENGINE_FOOTER_CHARS * 2)


MAX_PRIOR_TOOL_LINES = 20


def engine_name(provider_id: Any, model_id: Any = None) -> str:
    name = PROVIDER_DISPLAY_NAMES.get(str(provider_id or ""), str(provider_id or "AI"))
    return _code_safe(_printable(f"{name} ({model_id})" if model_id else name, 80))


def failure_reason(result: Any) -> str:
    """Sanitized provider failure text ("Gemini rate limit or quota was reached.")."""
    message = str(getattr(result, "message", "") or "")
    if message.startswith("Provider failed."):
        message = message[len("Provider failed.") :].strip()
    return _printable(message, 200) or t("unknown error")


def render_switch_offer(stage: str, failed: Any, alternative: dict[str, str], executed: Any) -> str:
    failed_name = engine_name(getattr(failed, "provider_id", None), getattr(failed, "model_id", None))
    alt_name = engine_name(alternative.get("provider_id"), alternative.get("model_id"))
    if stage == "plan":
        lines = [t("⚠️ **{engine}** stopped during planning: {reason}", engine=failed_name, reason=failure_reason(failed))]
    else:
        lines = [t("⚠️ **{engine}** stopped during execution: {reason}", engine=failed_name, reason=failure_reason(failed))]
    done = render_executed_tools(executed)
    if done:
        lines.append(done)
    if stage == "plan":
        lines.append(t("Continue **planning** with **{engine}**? Nothing has been changed yet.", engine=alt_name))
    else:
        lines.append(
            t(
                "Continue **the task** with **{engine}**? It gets the same plan and the list of actions "
                "already done, so it does not repeat them.",
                engine=alt_name,
            )
        )
    lines.append(t("Cancel stops here; actions that already ran are not undone."))
    return clip_text("\n".join(lines), AI_MESSAGE_CHUNK_CHARS)


def prior_tools_note(prior_tools: Any) -> str:
    """Executor note: what another engine already did in this request."""
    tools = tuple(prior_tools or ())
    if not tools:
        return ""
    lines = [
        "Another AI engine already worked on this request and stopped. These actions ALREADY RAN - do not "
        "repeat them; read current state with read tools if needed and continue with the remaining steps:"
    ]
    for tool in tools[:MAX_PRIOR_TOOL_LINES]:
        status = "ok" if getattr(tool, "ok", False) is True else "FAILED"
        lines.append(f"- {getattr(tool, 'tool_name', '?')} {status}: {_printable(getattr(tool, 'message', ''), 160)}")
    if len(tools) > MAX_PRIOR_TOOL_LINES:
        lines.append(f"- ... and {len(tools) - MAX_PRIOR_TOOL_LINES} more.")
    return "\n".join(lines)


def memory_text_tools(tools: Any) -> str:
    executed = [
        f"{getattr(tool, 'tool_name', '?')} {'ok' if getattr(tool, 'ok', False) is True else 'FAILED'}"
        for tool in tools or ()
    ]
    return f"[actions: {', '.join(executed[:10])}]" if executed else ""


def memory_text(result: Any) -> str:
    """What the conversation memory keeps from one finished request."""
    status = getattr(getattr(result, "status", None), "value", None)
    tools_text = memory_text_tools(getattr(result, "executed_tools", ()))
    actions = f" {tools_text}" if tools_text else ""
    if status == "COMPLETED":
        return f"{getattr(result, 'content', '') or ''}{actions}".strip()
    message = getattr(result, "message", "") or ""
    return f"(Request ended: {status or 'unknown'}. {message}){actions}".strip()


def describe_attachments(attachments: dict[str, Any]) -> str:
    if not attachments:
        return ""
    lines = ["[Files attached to this request; tools take them by attachment_id]"]
    for attachment_id, item in attachments.items():
        lines.append(
            f'- attachment_id={attachment_id} name="{_printable(getattr(item, "filename", ""), 80)}" '
            f'type={_printable(getattr(item, "content_type", ""), 60) or "unknown"} size={getattr(item, "size", "?")} bytes'
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Confirmation state and view
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestBinding:
    user_id: int
    guild_id: int
    channel_id: int | None


@dataclass
class PendingConfirmation:
    """Transport-side owner of one opaque core confirmation ID (never shown)."""

    confirmation_id: str = field(repr=False)
    binding: RequestBinding
    summary: str
    resolved: bool = False
    # "ephemeral" (slash /ai) or "public" (control channel); kept across approval.
    delivery_mode: Any = "ephemeral"
    # Files attached to the original request (attachment ID -> Attachment).
    attachments: dict[str, Any] = field(default_factory=dict, repr=False)
    # Planning engine label for the footer of continuations (two-stage).
    planner_label: str = ""
    # (memory_key, prompt) recorded when the request reaches a final state.
    memory: Any = field(default=None, repr=False)
    # Request run + plan, so an engine failure after Approve can offer a switch.
    run: Any = field(default=None, repr=False)
    plan: Any = field(default=None, repr=False)


@dataclass
class RequestRun:
    """Everything needed to (re)start the planning or executor stage of one request."""

    binding: RequestBinding
    prompt: str = field(repr=False)
    user_text: str = field(repr=False)
    task_mode: str
    attachments: dict[str, Any] = field(default_factory=dict, repr=False)
    planner_label: str = ""
    # Policy of the executor stage (False only after an approved plan).
    confirm_normal: bool = True
    # Engines chosen by the user after a failure (manual profile override).
    planner_profile_id: str | None = None
    executor_profile_id: str | None = None
    # Profiles that failed during this request; never offered again.
    failed_profile_ids: list[str] = field(default_factory=list)
    # Actions a failed executor engine already ran (told to the next engine).
    prior_tools: tuple[Any, ...] = ()


@dataclass
class PendingSwitch:
    """An engine failed; the user decides whether another engine continues."""

    run: RequestRun
    stage: str  # "plan" or "execute"
    alternative: dict[str, str]
    plan: Any = field(default=None, repr=False)
    executed: tuple[Any, ...] = ()
    delivery_mode: Any = "ephemeral"
    summary: str = ""
    resolved: bool = False


class SwitchEngineView(discord.ui.View):
    def __init__(self, transport: "AITransport", state: PendingSwitch) -> None:
        super().__init__(timeout=CONFIRMATION_VIEW_TIMEOUT_SECONDS)
        self.transport = transport
        self.state = state
        self.message: Any = None
        name = PROVIDER_DISPLAY_NAMES.get(state.alternative.get("provider_id", ""), state.alternative.get("provider_id", "?"))
        self.continue_button.label = clip_text(t("Continue with {engine}", engine=name), 80)
        self.cancel_button.label = t("Cancel")

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_switch_decision(interaction, self.state, approved=True, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_switch_decision(interaction, self.state, approved=False, view=self)

    async def on_timeout(self) -> None:
        self.state.resolved = True
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


@dataclass
class PendingPlanApproval:
    """A planner plan waiting for one Approve/Cancel (plan confirmation mode)."""

    run: RequestRun
    plan: dict[str, Any] = field(repr=False)
    delivery_mode: Any = "ephemeral"
    summary: str = ""
    resolved: bool = False


class PlanApprovalView(discord.ui.View):
    def __init__(self, transport: "AITransport", state: PendingPlanApproval) -> None:
        super().__init__(timeout=CONFIRMATION_VIEW_TIMEOUT_SECONDS)
        self.transport = transport
        self.state = state
        self.message: Any = None
        self.approve_button.label = t("Approve plan")
        self.cancel_button.label = t("Cancel")

    @discord.ui.button(label="Approve plan", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_plan_decision(interaction, self.state, approved=True, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_plan_decision(interaction, self.state, approved=False, view=self)

    async def on_timeout(self) -> None:
        # Nothing executes on timeout.
        self.state.resolved = True
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


class ConfirmationView(discord.ui.View):
    def __init__(self, transport: "AITransport", state: PendingConfirmation) -> None:
        super().__init__(timeout=CONFIRMATION_VIEW_TIMEOUT_SECONDS)
        self.transport = transport
        self.state = state
        self.message: Any = None
        self.approve_button.label = t("Approve")
        self.cancel_button.label = t("Cancel")

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_decision(interaction, self.state, approved=True, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.transport.handle_decision(interaction, self.state, approved=False, view=self)

    async def on_timeout(self) -> None:
        # Nothing executes on timeout; the core confirmation expires by its own TTL.
        self.state.resolved = True
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def _import_ai_modules() -> tuple[Any, Any]:
    import ai_orchestrator
    import ai_platform

    return ai_platform, ai_orchestrator


def _default_orchestrator_factory(stores: Any = None) -> Any:
    """Orchestrator over ONE bot instance's AI settings and keys (ai_storage)."""
    _ai_platform, ai_orchestrator = _import_ai_modules()
    if stores is None:
        raise ValueError("This bot instance has no AI storage configured.")
    return ai_orchestrator.AIOrchestrator(stores=stores)


AuditFn = Callable[[Any, dict, str, str], Awaitable[str | None]]


class DeliveryMode(str, Enum):
    """Where the AI control plane of one request is shown."""

    EPHEMERAL = "ephemeral"  # slash /ai
    PUBLIC = "public"  # natural messages in the configured control channel


class InteractionDelivery:
    """Sends through an interaction's follow-up webhook (ephemeral or public)."""

    def __init__(self, interaction: Any, mode: Any) -> None:
        self.interaction = interaction
        self.mode = DeliveryMode(mode)

    async def send(self, content: str, *, view: Any = None) -> Any:
        kwargs: dict[str, Any] = {
            "ephemeral": self.mode is DeliveryMode.EPHEMERAL,
            "allowed_mentions": no_mentions(),
        }
        if view is not None:
            kwargs["view"] = view
            kwargs["wait"] = True
        return await self.interaction.followup.send(clip_text(content, DISCORD_MESSAGE_LIMIT), **kwargs)


class ChannelDelivery:
    """Public delivery into the control channel; first message replies to the request."""

    mode = DeliveryMode.PUBLIC

    def __init__(self, channel: Any, reply_to: Any = None) -> None:
        self.channel = channel
        self.reply_to = reply_to

    async def send(self, content: str, *, view: Any = None) -> Any:
        kwargs: dict[str, Any] = {"allowed_mentions": no_mentions()}
        if view is not None:
            kwargs["view"] = view
        content = clip_text(content, DISCORD_MESSAGE_LIMIT)
        reply_to, self.reply_to = self.reply_to, None
        if reply_to is not None:
            try:
                return await reply_to.reply(content, mention_author=False, **kwargs)
            except Exception:
                pass
        return await self.channel.send(content, **kwargs)


@dataclass(frozen=True)
class RequestContext:
    """Minimal guild + user pair for executor/audit when there is no interaction."""

    guild: Any
    user: Any


@asynccontextmanager
async def _maybe_typing(channel: Any):
    manager = None
    try:
        manager = channel.typing()
        await manager.__aenter__()
    except Exception:
        manager = None
    try:
        yield
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


class AITransport:
    def __init__(
        self,
        *,
        load_config: Callable[[], dict],
        fetch_user: Callable[[int], Awaitable[Any]] | None = None,
        audit: AuditFn | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        view_factory: Callable[["AITransport", PendingConfirmation], Any] | None = None,
        feature_store: Any = None,
        planning: bool = False,
        ai_stores: Any = None,
    ) -> None:
        self._load_config = load_config
        self._fetch_user = fetch_user
        self._audit = audit
        self.feature_store = feature_store
        # True: a planning call (PLANNER profile, no tool schemas) picks the
        # tools, then the executor (ROUTINE/chosen mode) runs with only those.
        self.planning = planning is True
        # ai_storage.InstanceAIStores of THIS bot instance (set by Admin.main).
        self.ai_stores = ai_stores
        self._orchestrator_factory = orchestrator_factory or (lambda: _default_orchestrator_factory(self.ai_stores))
        self._view_factory = view_factory or ConfirmationView
        self._orchestrator: Any = None
        # Short per (guild, channel, user) conversation memory, RAM only.
        self.memory = ai_memory.ConversationMemory()
        # Set by Admin.main(): whether the bot requested the Message Content
        # Intent. None = unknown (tests, older wiring).
        self.message_content_enabled: bool | None = None
        # @mention requests (gate fixed at startup like the control channel).
        self.mention_enabled = False
        self._mention_denials: dict[int, float] = {}
        self._mention_clock: Callable[[], float] = time.monotonic
        # Natural-message listener gate, fixed at startup together with the
        # Message Content Intent decision. None = natural AI disabled.
        self.control_channel_id: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def get_orchestrator(self) -> Any:
        """Lazy per-process orchestrator shared by /ai and the control channel."""
        if self._orchestrator is None:
            try:
                self._orchestrator = self._orchestrator_factory()
            except Exception as exc:
                print(f"AI orchestrator unavailable: {type(exc).__name__}")
                return None
        return self._orchestrator

    def set_control_channel(self, channel_id: Any) -> None:
        self.control_channel_id = channel_id if isinstance(channel_id, int) and not isinstance(channel_id, bool) else None

    def _try_load_config(self) -> dict | None:
        try:
            config = self._load_config()
        except Exception:
            return None
        if not isinstance(config, dict):
            return None
        # One process = one bot instance: its language applies to everything it says.
        bot_i18n.set_bot_language(config.get("language"))
        return config

    # -- sending -----------------------------------------------------------

    @staticmethod
    async def _respond(interaction: Any, content: str) -> None:
        """Ephemeral reply to one interaction (denials, validation errors)."""
        content = clip_text(t(content), AI_MESSAGE_CHUNK_CHARS)
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=no_mentions())
        else:
            await interaction.response.send_message(content, ephemeral=True, allowed_mentions=no_mentions())

    async def _send_result(
        self,
        delivery: Any,
        result: Any,
        binding: RequestBinding,
        attachments: dict[str, Any] | None = None,
        planner_label: str = "",
        memory: Any = None,
        run: Any = None,
        plan: Any = None,
    ) -> None:
        """Render a result. ``memory`` = (memory_key, prompt): recorded once the
        request reaches a final state, carried along while a confirmation is pending.
        With ``run`` an executor engine failure offers another engine instead."""
        status = getattr(getattr(result, "status", None), "value", None)
        footer = render_engine_footer(result, planner_label)
        if status == "UNAVAILABLE" and run is not None:
            task_class = TASK_MODES.get(getattr(run, "task_mode", ""), "ROUTINE")
            if await self._offer_switch(delivery, run, "execute", result, plan, task_class):
                return
        if status != "NEEDS_CONFIRMATION" or not getattr(result, "confirmation_id", None):
            self._remember(memory, result)
        if status == "NEEDS_CONFIRMATION" and getattr(result, "confirmation_id", None):
            executed = render_executed_tools(getattr(result, "executed_tools", ()))
            if executed:
                await delivery.send(executed)
            tool_plan = getattr(result, "tool_plan", ())
            try:
                pages = render_tool_plan_pages(tool_plan)
                summary = render_confirmation_control(tool_plan, getattr(result, "tool_risk", None), len(pages))
            except Exception as exc:
                # Fail closed: no Approve button for a plan that cannot be shown exactly.
                too_large = isinstance(exc, PlanPreviewTooLarge)
                if not too_large:
                    print(f"/ai plan preview failed: {type(exc).__name__}")
                cancelled = await self._cancel_unreviewable(result.confirmation_id)
                self._remember(memory, result)
                await delivery.send(
                    with_footer(
                        unreviewable_plan_message(too_large=too_large, cancelled=cancelled, earlier_actions=bool(executed)),
                        footer,
                    )
                )
                return
            outline = ""
            try:
                outline = render_plan_outline(tool_plan)
            except Exception:
                outline = ""
            if outline:
                await delivery.send(outline)
            for page in pages:
                await delivery.send(page)
            state = PendingConfirmation(
                confirmation_id=result.confirmation_id,
                binding=binding,
                summary=with_footer(summary, footer),
                delivery_mode=delivery.mode,
                attachments=dict(attachments or {}),
                planner_label=planner_label,
                memory=memory,
                run=run,
                plan=plan,
            )
            view = self._view_factory(self, state)
            message = await delivery.send(state.summary, view=view)
            try:
                view.message = message
            except Exception:
                pass
            return
        texts = render_result_messages(result)
        if footer and texts:
            # Attach the engine indicator to the primary answer message.
            texts[0] = with_footer(texts[0], footer)
        for text in texts:
            await delivery.send(text)

    async def _cancel_unreviewable(self, confirmation_id: str) -> bool:
        """Reject a pending core confirmation whose plan cannot be reviewed.

        Returns True only when the public core result confirms the rejection
        (status CANCELLED with the rejection message). Anything else - missing
        orchestrator, an exception, an unknown/expired ID - returns False and the
        caller must not claim the plan was cancelled.
        """
        orchestrator = self._orchestrator
        if orchestrator is None:
            return False

        async def refuse(tool_name: str, arguments: dict) -> admin_tools.ToolResult:
            return admin_tools.ToolResult(False, tool_name, "Unreviewable plan; not executed.")

        try:
            result = await orchestrator.approve_confirmation(confirmation_id, approved=False, executor=refuse)
        except Exception as exc:
            print(f"/ai could not cancel unreviewable plan: {type(exc).__name__}")
            return False
        status = getattr(getattr(result, "status", None), "value", None)
        return status == "CANCELLED" and getattr(result, "message", None) == CORE_REJECTED_MESSAGE

    # -- executor ----------------------------------------------------------

    def build_executor(
        self, source: Any, binding: RequestBinding, attachments: dict[str, Any] | None = None
    ) -> Callable[[str, dict], Awaitable[Any]]:
        """Executor bound to ONE requester context (interaction or control message).

        ``source`` provides ``.guild`` and ``.user``; for approvals it is the fresh
        button interaction.
        """

        # data={"fatal": True}: the orchestrator ends the run instead of letting
        # the model retry (recover_errors) - authorization will not come back.
        revoked = {"fatal": True}

        async def executor(tool_name: str, arguments: dict) -> admin_tools.ToolResult:
            config = self._try_load_config()
            guild = getattr(source, "guild", None)
            if config is None or guild is None or getattr(guild, "id", None) != binding.guild_id:
                return admin_tools.ToolResult(False, tool_name, t(AUTH_REVOKED_TOOL_MESSAGE), dict(revoked))
            if getattr(getattr(source, "user", None), "id", None) != binding.user_id:
                return admin_tools.ToolResult(False, tool_name, t(AUTH_REVOKED_TOOL_MESSAGE), dict(revoked))
            operator = binding.user_id == admin_terminal.OPERATOR_USER_ID
            if operator:
                # Local Manager operator: owns the bot token, so the Discord AI
                # whitelist and requester hierarchy do not apply (bot hierarchy
                # and every confirmation still do). Only ManagerInteraction
                # sources qualify; Discord IDs are never 0.
                if not admin_terminal.is_manager_operator(source):
                    return admin_tools.ToolResult(False, tool_name, t(AUTH_REVOKED_TOOL_MESSAGE), dict(revoked))
                member = source.user
            else:
                # Fresh member state (current roles), fail closed if not resolvable.
                member = guild.get_member(binding.user_id) if hasattr(guild, "get_member") else None
                if member is None or not actor_has_ai_access(member, guild, config):
                    return admin_tools.ToolResult(False, tool_name, t(AUTH_REVOKED_TOOL_MESSAGE), dict(revoked))
            context = admin_tools.AdminToolContext(
                guild=guild,
                fetch_user=self._fetch_user,
                source="manager" if operator else AI_SOURCE,
                requesting_user_id=None if operator else binding.user_id,
                requesting_user_name=str(member),
                suppress_mentions=True,
                enforce_hierarchy=not operator,
                attachments=dict(attachments or {}),
                feature_store=self.feature_store,
                message_content=self.message_content_enabled,
            )
            result = await admin_tools.execute_tool(context, tool_name, arguments)
            audit_failure = await self._safe_audit(source, config, tool_name, result.message)
            if result.ok and audit_failure:
                result = admin_tools.ToolResult(
                    True,
                    result.tool_name,
                    f"{result.message} " + t("(Action completed, but audit logging failed.)"),
                    result.data,
                )
            return result

        return executor

    async def _safe_audit(self, source: Any, config: dict, tool_name: str, message: str) -> str | None:
        if self._audit is None:
            return None
        try:
            return await self._audit(source, config, f"{AI_SOURCE} {tool_name}", clip_text(message, 1000))
        except Exception as exc:
            return f"Audit logging failed: {type(exc).__name__}"

    # -- request context and memory ----------------------------------------

    def _context_text(self, source: Any, binding: RequestBinding, *, include_ids: bool) -> str:
        """Requester/channel/server snapshot for the prompts; never fails the request."""
        try:
            guild = getattr(source, "guild", None)
            if guild is None:
                return ""
            operator = binding.user_id == admin_terminal.OPERATOR_USER_ID
            getter = getattr(guild, "get_member", None)
            member = None if operator or not callable(getter) else getter(binding.user_id)
            member = None if operator else (member or getattr(source, "user", None))
            channel = getattr(source, "channel", None)
            if binding.channel_id is not None:
                for name in ("get_channel_or_thread", "get_channel"):
                    lookup = getattr(guild, name, None)
                    found = lookup(binding.channel_id) if callable(lookup) else None
                    if found is not None:
                        channel = found
                        break
            budget = ai_context.EXECUTOR_CONTEXT_CHARS if include_ids else ai_context.PLANNER_CONTEXT_CHARS
            text = ai_context.describe_request(guild, member, channel, include_ids=include_ids, budget=budget)
            if operator:
                text = (
                    "Requester: the bot's operator, typing in the local Manager app (not a Discord member; "
                    "full bot permissions; requester role limits do not apply).\n" + text
                )
            return text
        except Exception as exc:
            print(f"/ai context unavailable: {type(exc).__name__}")
            return ""

    @staticmethod
    def memory_key(binding: RequestBinding) -> tuple[int, int, int]:
        return (binding.guild_id, binding.channel_id or 0, binding.user_id)

    def _memory_messages(self, ai_platform: Any, key: tuple[int, int, int]) -> tuple[Any, ...]:
        messages = []
        for user_text, assistant_text in self.memory.history(key):
            messages.append(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content=user_text))
            messages.append(ai_platform.AIMessage(role=ai_platform.MessageRole.ASSISTANT, content=assistant_text))
        return tuple(messages)

    def _remember(self, memory: Any, result: Any = None, text: str | None = None) -> None:
        """Record one finished exchange: (memory_key, prompt) plus the final answer."""
        if not memory:
            return
        key, prompt = memory
        try:
            self.memory.record(key, prompt, text if text is not None else memory_text(result))
        except Exception as exc:  # pragma: no cover - memory must never break a reply
            print(f"/ai memory failed: {type(exc).__name__}")

    # -- shared orchestration ---------------------------------------------

    async def _run_request(
        self,
        *,
        delivery: Any,
        source: Any,
        binding: RequestBinding,
        prompt: str,
        task_mode: str,
        attachments: dict[str, Any] | None = None,
    ) -> None:
        """One request: optional planning, optional plan approval, then execution.

        Planner and executor both see the bounded request context and the
        short conversation memory of this user in this channel. With planning
        enabled a tool-less planning call runs first and selects the executor's
        tools (or answers directly when no action is needed). In "plan"
        confirmation mode a plan that changes the server is shown once for
        approval; after Approve its normal actions run without further prompts
        and destructive ones are still confirmed with exact arguments.
        """
        attachments = dict(attachments or {})
        note = describe_attachments(attachments)
        run = RequestRun(
            binding=binding,
            prompt=prompt,
            user_text=f"{prompt}\n\n{note}" if note else prompt,
            task_mode=task_mode,
            attachments=attachments,
        )
        await self._planning_stage(delivery, source, run)

    async def _planning_stage(self, delivery: Any, source: Any, run: "RequestRun") -> None:
        """Plan (or answer), then plan approval or execution. Restartable with another engine."""
        guild = getattr(source, "guild", None)
        memory_key = self.memory_key(run.binding)
        config = self._try_load_config() or {}
        try:
            ai_platform, ai_orchestrator = _import_ai_modules()
            orchestrator = self.get_orchestrator()
            if orchestrator is None:
                await delivery.send(t(UNAVAILABLE_MESSAGE))
                return
            policy = ai_orchestrator.ConfirmationPolicy(confirm_normal=True)
            history = self._memory_messages(ai_platform, memory_key)
            user_message = ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content=run.user_text)
        except Exception as exc:
            print(f"/ai request failed: {type(exc).__name__}")
            await delivery.send(t(UNAVAILABLE_MESSAGE))
            return

        plan: dict[str, Any] | None = None
        if self.planning:
            planned, failed = await self._plan(
                orchestrator,
                ai_platform,
                ai_orchestrator,
                guild,
                (*history, user_message),
                policy,
                self._context_text(source, run.binding, include_ids=False),
                manual_profile_id=run.planner_profile_id,
            )
            if failed is not None and await self._offer_switch(delivery, run, "plan", failed, None, "PLANNER"):
                return
            if planned is not None:
                plan, planner_result = planned
                run.planner_label = engine_label(planner_result)
                if plan["mode"] == "answer":
                    texts = chunk_text(plan["answer"]) or [t("(The AI returned no text.)")]
                    footer = render_engine_footer(planner_result)
                    if footer:
                        texts[0] = with_footer(texts[0], footer)
                    for text in texts:
                        await delivery.send(text)
                    self._remember((memory_key, run.prompt), text=plan["answer"])
                    return
                if confirmation_mode(config) == "plan" and plan_write_tools(plan):
                    await self._offer_plan(delivery, run, plan, render_engine_footer(planner_result))
                    return
        await self._execute(delivery, source, run, plan, confirm_normal=True)

    async def _execute(
        self,
        delivery: Any,
        source: Any,
        run: "RequestRun",
        plan: dict[str, Any] | None,
        *,
        confirm_normal: bool,
    ) -> None:
        """Executor stage. ``confirm_normal=False`` only after the user approved ``plan``.

        Engines never switch silently: routing fallbacks are disabled here and
        a provider failure offers the next engine to the user (_offer_switch).
        """
        guild = getattr(source, "guild", None)
        memory_key = self.memory_key(run.binding)
        run.confirm_normal = confirm_normal
        try:
            ai_platform, ai_orchestrator = _import_ai_modules()
            orchestrator = self.get_orchestrator()
            if orchestrator is None:
                await delivery.send(t(UNAVAILABLE_MESSAGE))
                return
            instruction = executor_instruction(
                guild,
                plan,
                context_text=self._context_text(source, run.binding, include_ids=True),
                message_content=self.message_content_enabled,
            )
            note = prior_tools_note(run.prior_tools)
            if note:
                instruction += "\n" + note
            request = ai_orchestrator.OrchestratorRequest(
                messages=(
                    ai_platform.AIMessage(role=ai_platform.MessageRole.SYSTEM, content=instruction),
                    *self._memory_messages(ai_platform, memory_key),
                    ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content=run.user_text),
                ),
                task_class=ai_platform.TaskClass(TASK_MODES[run.task_mode]),
                manual_profile_id=run.executor_profile_id,
                allowed_tool_names=executor_tool_names(plan, run.prompt) if self.planning else None,
                recover_errors=True,
                auto_fallback=False,
                response_language=bot_i18n.bot_language(),
            )
            policy = ai_orchestrator.ConfirmationPolicy(confirm_normal=confirm_normal)
        except Exception as exc:
            print(f"/ai request failed: {type(exc).__name__}")
            await delivery.send(t(UNAVAILABLE_MESSAGE))
            return
        try:
            result = await orchestrator.orchestrate(
                request,
                executor=self.build_executor(source, run.binding, run.attachments),
                confirmation_policy=policy,
            )
        except Exception as exc:
            # The core contains provider/executor failures; reaching this means an
            # unexpected error after tools may have run. Do not claim nothing ran.
            print(f"/ai request failed: {type(exc).__name__}")
            await delivery.send(t(UNEXPECTED_FAILURE_MESSAGE))
            return
        await self._send_result(
            delivery,
            result,
            run.binding,
            run.attachments,
            run.planner_label,
            memory=(memory_key, run.prompt),
            run=run,
            plan=plan,
        )

    async def _offer_switch(
        self,
        delivery: Any,
        run: "RequestRun",
        stage: str,
        failed: Any,
        plan: Any,
        task_class: str,
    ) -> bool:
        """Offer another engine after a provider failure. False if none is usable.

        Asks instead of switching silently: the user sees which engine failed,
        why, what already ran, and which engine would continue.
        """
        finder = getattr(self._orchestrator, "alternative_profile", None)
        failed_id = getattr(failed, "profile_id", None)
        if not callable(finder) or not failed_id:
            return False
        if failed_id not in run.failed_profile_ids:
            run.failed_profile_ids.append(failed_id)
        try:
            alternative = finder(task_class, tuple(run.failed_profile_ids))
        except Exception as exc:
            print(f"/ai engine switch lookup failed: {type(exc).__name__}")
            return False
        if not alternative:
            return False
        executed = tuple(getattr(failed, "executed_tools", ()) or ())
        state = PendingSwitch(
            run=run,
            stage=stage,
            alternative=dict(alternative),
            plan=plan,
            executed=executed,
            delivery_mode=delivery.mode,
        )
        state.summary = render_switch_offer(stage, failed, state.alternative, executed)
        view = SwitchEngineView(self, state)
        message = await delivery.send(state.summary, view=view)
        try:
            view.message = message
        except Exception:
            pass
        return True

    async def handle_switch_decision(
        self,
        interaction: Any,
        state: "PendingSwitch",
        *,
        approved: bool,
        view: Any = None,
    ) -> None:
        """Continue with the offered engine, or stop. Same ownership checks as other buttons."""
        if type(approved) is not bool:
            raise ValueError("approved must be exactly True or False.")
        if state.resolved:
            await self._respond(interaction, INACTIVE_MESSAGE)
            return
        if not await self._check_button_owner(interaction, state.run.binding):
            return
        state.resolved = True
        if view is not None:
            try:
                view.stop()
            except Exception:
                pass
        alt_name = engine_name(state.alternative.get("provider_id"), state.alternative.get("model_id"))
        decision = t("Continuing with {engine}...", engine=alt_name) if approved else t("Stopped. Nothing else was executed.")
        try:
            await interaction.response.edit_message(
                content=clip_text(f"{state.summary}\n\n{decision}", AI_MESSAGE_CHUNK_CHARS),
                view=None,
                allowed_mentions=no_mentions(),
            )
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.defer(
                    ephemeral=DeliveryMode(state.delivery_mode) == DeliveryMode.EPHEMERAL, thinking=True
                )
        run = state.run
        if not approved:
            self._remember(
                (self.memory_key(run.binding), run.prompt),
                text=f"(Stopped: the AI engine failed and the user declined switching.) {memory_text_tools(state.executed)}".strip(),
            )
            return
        delivery = InteractionDelivery(interaction, state.delivery_mode)
        if state.stage == "plan":
            run.planner_profile_id = state.alternative["profile_id"]
            await self._planning_stage(delivery, interaction, run)
            return
        run.executor_profile_id = state.alternative["profile_id"]
        run.prior_tools = (*run.prior_tools, *state.executed)
        await self._execute(delivery, interaction, run, state.plan, confirm_normal=run.confirm_normal)

    async def _offer_plan(self, delivery: Any, run: "RequestRun", plan: dict[str, Any], footer: str) -> None:
        """Show the planner's plan once with Approve/Cancel (plan confirmation mode)."""
        state = PendingPlanApproval(run=run, plan=plan, delivery_mode=delivery.mode)
        state.summary = with_footer(render_plan_approval(plan), footer)
        view = PlanApprovalView(self, state)
        message = await delivery.send(state.summary, view=view)
        try:
            view.message = message
        except Exception:
            pass

    async def _plan(
        self,
        orchestrator: Any,
        ai_platform: Any,
        ai_orchestrator: Any,
        guild: Any,
        conversation: Any,
        policy: Any,
        context_text: str = "",
        *,
        manual_profile_id: str | None = None,
    ) -> tuple[tuple[dict[str, Any], Any] | None, Any]:
        """Tool-less planning call. PLANNER profile first, ROUTINE if no planner is configured.

        ``conversation`` is the memory plus the current USER message. Returns
        ((plan, result) or None, failed_result). ``failed_result`` is set when
        a configured planning engine failed and another engine could take over
        (the caller asks the user). ``(None, None)`` means no usable plan: the
        caller falls back to keyword tool routing. Never executes tools.
        """

        async def refuse(tool_name: str, arguments: dict) -> admin_tools.ToolResult:
            return admin_tools.ToolResult(False, tool_name, "The planning stage cannot run tools.")

        if not isinstance(conversation, tuple):
            conversation = (conversation,)
        messages = (
            ai_platform.AIMessage(role=ai_platform.MessageRole.SYSTEM, content=planner_instruction(guild, context_text)),
            *conversation,
        )
        switchable = callable(getattr(orchestrator, "alternative_profile", None))
        for task_class in ("PLANNER", "ROUTINE"):
            try:
                request = ai_orchestrator.OrchestratorRequest(
                    messages=messages,
                    task_class=ai_platform.TaskClass(task_class),
                    manual_profile_id=manual_profile_id,
                    allowed_tool_names=(),
                    auto_fallback=False,
                    response_language=bot_i18n.bot_language(),
                )
                result = await orchestrator.orchestrate(request, executor=refuse, confirmation_policy=policy)
            except Exception as exc:
                print(f"/ai planning failed: {type(exc).__name__}")
                return None, None
            status = getattr(getattr(result, "status", None), "value", None)
            if status == "UNAVAILABLE":
                # A configured engine that actually failed (limit, outage): ask
                # before another engine plans. No engine configured for
                # planning: plan with the ROUTINE engine as before.
                if switchable and getattr(result, "profile_id", None):
                    return None, result
                if task_class == "PLANNER" and manual_profile_id is None:
                    continue
            if status != "COMPLETED":
                return None, None
            plan = parse_plan(getattr(result, "content", None))
            return ((plan, result) if plan is not None else None), None
        return None, None

    # -- /ai ---------------------------------------------------------------

    async def handle_ai_command(
        self, interaction: Any, prompt: Any, mode: str | None = None, attachments: Any = None
    ) -> None:
        config = self._try_load_config()
        if config is None:
            await self._respond(interaction, CONFIG_UNAVAILABLE_MESSAGE)
            return
        if not actor_has_ai_access(interaction.user, interaction.guild, config):
            await self._respond(interaction, ACCESS_DENIED_MESSAGE)
            return
        if not isinstance(prompt, str) or not prompt.strip():
            await self._respond(interaction, t("Prompt must not be empty."))
            return
        if len(prompt) > AI_PROMPT_MAX_CHARS:
            await self._respond(interaction, t("Prompt must be {limit} characters or fewer.", limit=AI_PROMPT_MAX_CHARS))
            return
        selected_mode = (mode or DEFAULT_TASK_MODE).lower()
        if selected_mode not in TASK_MODES:
            await self._respond(interaction, t("Unknown AI mode."))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        binding = RequestBinding(
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            channel_id=getattr(interaction, "channel_id", None),
        )
        await self._run_request(
            delivery=InteractionDelivery(interaction, DeliveryMode.EPHEMERAL),
            source=interaction,
            binding=binding,
            prompt=prompt,
            task_mode=selected_mode,
            attachments=collect_attachments(attachments),
        )

    # -- Manager terminal (local operator) ----------------------------------

    async def handle_manager_request(self, interaction: Any, prompt: str, mode: str | None = None) -> None:
        """A prompt typed in the Manager terminal (admin_terminal mailbox).

        Runs the same pipeline as /ai for the local operator; replies,
        plans and confirmations go back to the Manager as events.
        """
        if not admin_terminal.is_manager_operator(interaction):
            raise ValueError("handle_manager_request accepts only Manager operator interactions.")
        selected_mode = (mode or DEFAULT_TASK_MODE).lower()
        if selected_mode not in TASK_MODES:
            selected_mode = DEFAULT_TASK_MODE
        binding = RequestBinding(
            user_id=admin_terminal.OPERATOR_USER_ID,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
        )
        if prompt.strip().lower() in RESET_WORDS:
            self.memory.clear(self.memory_key(binding))
            await interaction.followup.send(t(MEMORY_CLEARED_MESSAGE))
            return
        await self._run_request(
            delivery=InteractionDelivery(interaction, DeliveryMode.EPHEMERAL),
            source=interaction,
            binding=binding,
            prompt=prompt,
            task_mode=selected_mode,
        )

    async def dispatch_view_decision(self, interaction: Any, view: Any, approved: bool) -> None:
        """Route a Manager button click to the handler of the view that asked."""
        state = getattr(view, "state", None)
        if isinstance(view, PlanApprovalView):
            await self.handle_plan_decision(interaction, state, approved=approved, view=view)
        elif isinstance(view, SwitchEngineView):
            await self.handle_switch_decision(interaction, state, approved=approved, view=view)
        else:
            await self.handle_decision(interaction, state, approved=approved, view=view)

    # -- @mention requests ---------------------------------------------------

    def mention_prompt(self, message: Any) -> str | None:
        """Prompt text if the message pings the bot user or its own role, else None."""
        guild = getattr(message, "guild", None)
        me = getattr(guild, "me", None)
        bot_id = getattr(me, "id", None)
        if bot_id is None:
            return None
        self_role = getattr(guild, "self_role", None)
        role_id = getattr(self_role, "id", None)
        user_hit = bot_id in (getattr(message, "raw_mentions", None) or [])
        role_hit = role_id is not None and role_id in (getattr(message, "raw_role_mentions", None) or [])
        if not user_hit and not role_hit:
            return None
        text = str(getattr(message, "content", "") or "")
        text = re.sub(rf"<@!?{bot_id}>", " ", text)
        if role_id is not None:
            text = re.sub(rf"<@&{role_id}>", " ", text)
        return " ".join(text.split())

    def _mention_channel_allowed(self, message: Any, config: dict[str, Any]) -> bool:
        allowed = {int(value) for value in config.get("ai_mention_channel_ids") or []}
        if not allowed:
            return True  # empty list = every channel the bot can read
        channel = getattr(message, "channel", None)
        ids = {getattr(channel, "id", None), getattr(channel, "parent_id", None)}
        return bool(ids & allowed)

    async def handle_mention_message(self, message: Any) -> None:
        """@Kairo (bot user or its role) in an allowed channel starts an AI request."""
        if not self.mention_enabled:
            return
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        if guild is None or channel is None or author is None:
            return
        if getattr(message, "webhook_id", None) is not None or getattr(author, "bot", False) is not False:
            return
        if not isinstance(author, MEMBER_TYPES):
            return
        prompt = self.mention_prompt(message)
        if prompt is None:
            return
        config = self._try_load_config()
        if config is None or config.get("ai_mention_enabled") is not True:
            return
        if config.get("ai_control_channel_id") == getattr(channel, "id", None):
            return  # the control channel handles every message itself
        if not self._mention_channel_allowed(message, config):
            return
        delivery = ChannelDelivery(channel, reply_to=message)
        if not actor_has_ai_access(author, guild, config):
            now = self._mention_clock()
            last = self._mention_denials.get(author.id, -MENTION_DENIAL_COOLDOWN_SECONDS)
            if now - last >= MENTION_DENIAL_COOLDOWN_SECONDS:
                self._mention_denials[author.id] = now
                await delivery.send(t(MENTION_DENIED_MESSAGE))
            return
        binding = RequestBinding(user_id=author.id, guild_id=guild.id, channel_id=channel.id)
        if not prompt:
            await delivery.send(t(MENTION_EMPTY_MESSAGE))
            return
        if len(prompt) > AI_PROMPT_MAX_CHARS:
            await delivery.send(t("Message must be {limit} characters or fewer for AI requests.", limit=AI_PROMPT_MAX_CHARS))
            return
        if prompt.lower() in RESET_WORDS:
            self.memory.clear(self.memory_key(binding))
            await delivery.send(t(MEMORY_CLEARED_MESSAGE))
            return
        async with _maybe_typing(channel):
            await self._run_request(
                delivery=delivery,
                source=RequestContext(guild=guild, user=author),
                binding=binding,
                prompt=prompt,
                task_mode=DEFAULT_TASK_MODE,
                attachments=collect_attachments(getattr(message, "attachments", None)),
            )

    # -- natural control channel (AI-5) -------------------------------------

    def is_control_message_candidate(self, message: Any) -> bool:
        """Cheap pre-filter before any config load, provider or reply.

        Only real member messages with text, in a guild, in exactly the control
        channel configured at startup, are candidates. Bots (including this
        bot), webhooks, DMs and every other channel are ignored completely.
        """
        channel_id = self.control_channel_id
        if channel_id is None:
            return False
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        if guild is None or channel is None or author is None:
            return False
        if getattr(channel, "id", None) != channel_id:
            return False
        if getattr(message, "webhook_id", None) is not None:
            return False
        if getattr(author, "bot", False) is not False:
            return False
        if not isinstance(author, MEMBER_TYPES):
            return False
        content = getattr(message, "content", None)
        return isinstance(content, str) and bool(content.strip())

    async def handle_control_message(self, message: Any) -> None:
        if not self.is_control_message_candidate(message):
            return
        config = self._try_load_config()
        if config is None:
            return
        # The live config must still name this channel (no restart needed to
        # switch the feature off) and the author must be AI-whitelisted now.
        if config.get("ai_control_channel_id") != message.channel.id:
            return
        if not actor_has_ai_access(message.author, message.guild, config):
            return  # silently ignored: no provider call, no public reply
        delivery = ChannelDelivery(message.channel, reply_to=message)
        prompt = message.content
        if len(prompt) > AI_PROMPT_MAX_CHARS:
            await delivery.send(t("Message must be {limit} characters or fewer for AI requests.", limit=AI_PROMPT_MAX_CHARS))
            return
        binding = RequestBinding(user_id=message.author.id, guild_id=message.guild.id, channel_id=message.channel.id)
        if prompt.strip().lower() in RESET_WORDS:
            self.memory.clear(self.memory_key(binding))
            await delivery.send(t(MEMORY_CLEARED_MESSAGE))
            return
        async with _maybe_typing(message.channel):
            await self._run_request(
                delivery=delivery,
                source=RequestContext(guild=message.guild, user=message.author),
                binding=binding,
                prompt=prompt,
                task_mode=DEFAULT_TASK_MODE,
                attachments=collect_attachments(getattr(message, "attachments", None)),
            )

    # -- confirmation buttons ---------------------------------------------

    def _binding_matches(self, interaction: Any, binding: RequestBinding) -> bool:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        guild = getattr(interaction, "guild", None)
        guild_id = getattr(interaction, "guild_id", None) or getattr(guild, "id", None)
        return (
            user_id == binding.user_id
            and guild is not None
            and guild_id == binding.guild_id
            and getattr(interaction, "channel_id", None) == binding.channel_id
        )

    async def handle_decision(
        self,
        interaction: Any,
        state: PendingConfirmation,
        *,
        approved: bool,
        view: Any = None,
    ) -> None:
        # Same strict contract as the core: never interpret truthiness.
        if type(approved) is not bool:
            raise ValueError("approved must be exactly True or False.")
        if state.resolved:
            await self._respond(interaction, INACTIVE_MESSAGE)
            return
        # Ownership and fresh authorization are checked BEFORE the core is
        # called, so a failed check never consumes the pending confirmation.
        # Denials are always ephemeral, even under a public control-channel plan.
        if not await self._check_button_owner(interaction, state.binding):
            return
        orchestrator = self._orchestrator
        if orchestrator is None:
            state.resolved = True
            await self._respond(interaction, EXPIRED_MESSAGE)
            return

        # Single use from here on (no await between the check above and this).
        state.resolved = True
        if view is not None:
            try:
                view.stop()
            except Exception:
                pass
        decision = t("Approved. Executing the plan...") if approved else t("Cancelled.")
        try:
            await interaction.response.edit_message(
                content=clip_text(f"{state.summary}\n\n{decision}", AI_MESSAGE_CHUNK_CHARS),
                view=None,
                allowed_mentions=no_mentions(),
            )
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=DeliveryMode(state.delivery_mode) == DeliveryMode.EPHEMERAL, thinking=True)

        # The continuation keeps the delivery mode of the original request:
        # slash /ai stays ephemeral, the control channel stays public.
        delivery = InteractionDelivery(interaction, state.delivery_mode)
        try:
            result = await orchestrator.approve_confirmation(
                state.confirmation_id,
                approved=approved,
                executor=self.build_executor(interaction, state.binding, state.attachments),
            )
        except Exception as exc:
            print(f"/ai confirmation failed: {type(exc).__name__}")
            await delivery.send(t(UNEXPECTED_FAILURE_MESSAGE))
            return
        await self._send_result(
            delivery,
            result,
            state.binding,
            state.attachments,
            state.planner_label,
            memory=state.memory,
            run=state.run,
            plan=state.plan,
        )

    async def _check_button_owner(self, interaction: Any, binding: RequestBinding) -> bool:
        """Ownership + fresh AI authorization for a button click; replies on denial."""
        if not self._binding_matches(interaction, binding):
            await self._respond(interaction, NOT_OWNER_MESSAGE)
            return False
        if binding.user_id == admin_terminal.OPERATOR_USER_ID:
            # Operator requests are decided only from the local Manager.
            return admin_terminal.is_manager_operator(interaction)
        config = self._try_load_config()
        if config is None:
            await self._respond(interaction, CONFIG_UNAVAILABLE_MESSAGE)
            return False
        if not actor_has_ai_access(interaction.user, interaction.guild, config):
            await self._respond(interaction, ACCESS_DENIED_MESSAGE)
            return False
        return True

    async def handle_plan_decision(
        self,
        interaction: Any,
        state: "PendingPlanApproval",
        *,
        approved: bool,
        view: Any = None,
    ) -> None:
        """Approve or cancel a planner plan (plan confirmation mode).

        Approve runs the executor restricted to the plan's tools with normal
        actions unconfirmed; destructive actions still need their own exact
        confirmation. Checks mirror handle_decision: owner, guild, channel and
        fresh AI authorization, all before the single-use state is consumed.
        """
        if type(approved) is not bool:
            raise ValueError("approved must be exactly True or False.")
        if state.resolved:
            await self._respond(interaction, INACTIVE_MESSAGE)
            return
        if not await self._check_button_owner(interaction, state.run.binding):
            return
        state.resolved = True
        if view is not None:
            try:
                view.stop()
            except Exception:
                pass
        decision = t("Approved. Working on it...") if approved else t("Cancelled. Nothing was changed.")
        try:
            await interaction.response.edit_message(
                content=clip_text(f"{state.summary}\n\n{decision}", AI_MESSAGE_CHUNK_CHARS),
                view=None,
                allowed_mentions=no_mentions(),
            )
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.defer(
                    ephemeral=DeliveryMode(state.delivery_mode) == DeliveryMode.EPHEMERAL, thinking=True
                )
        if not approved:
            self._remember((self.memory_key(state.run.binding), state.run.prompt), text="(The user cancelled the proposed plan.)")
            return
        delivery = InteractionDelivery(interaction, state.delivery_mode)
        await self._execute(delivery, interaction, state.run, state.plan, confirm_normal=False)

    # -- memory reset ----------------------------------------------------------

    async def handle_reset_command(self, interaction: Any) -> None:
        config = self._try_load_config()
        if config is None:
            await self._respond(interaction, CONFIG_UNAVAILABLE_MESSAGE)
            return
        if not actor_has_ai_access(interaction.user, interaction.guild, config):
            await self._respond(interaction, ACCESS_DENIED_MESSAGE)
            return
        binding = RequestBinding(
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            channel_id=getattr(interaction, "channel_id", None),
        )
        self.memory.clear(self.memory_key(binding))
        await self._respond(interaction, MEMORY_CLEARED_MESSAGE)
