"""Discord /ai transport for the Admin bot (AI-4).

This module adapts the provider-neutral AI-3A orchestrator to Discord without
coupling ``ai_orchestrator`` to Discord. It owns everything transport-specific:

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
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import discord

import admin_tools


AI_SOURCE = "/ai"
AI_PROMPT_MAX_CHARS = 2000
DISCORD_MESSAGE_LIMIT = 2000
AI_MESSAGE_CHUNK_CHARS = 1900
AI_MAX_RESPONSE_CHUNKS = 3
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
    subject = "The new AI action plan" if earlier_actions else "The AI action plan"
    reason = "was too large to review safely" if too_large else "could not be displayed safely"
    if cancelled:
        text = f"{subject} {reason}. It was cancelled, and no action from this plan was executed."
    else:
        text = (
            f"{subject} {reason}. No approval control was created and no action from this plan "
            "was executed. The pending request will expire automatically."
        )
    if earlier_actions:
        text += "\n" + EARLIER_ACTIONS_NOTE
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
    chunks = [text[index : index + chunk_chars] for index in range(0, len(text), chunk_chars)]
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        marker = "\n... (response truncated)"
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
                f"**Action {index} of {len(calls)}:** tool `{exact_json_display(tool_name)}` | risk `{exact_json_display(risk)}`",
                "",
            )
        )
        if not arguments:
            items.append(("text", "(no arguments)", ""))
        for key in sorted(arguments):
            items.append(("value", f"argument `{exact_json_display(key)}`", exact_json_display(arguments[key])))
    return items


def _value_block(label: str, part: int, final: bool, piece: str) -> str:
    if part == 1 and final:
        suffix = " (exact JSON value)"
    elif final:
        suffix = f" (exact JSON value, part {part}, final; parts join without separators)"
    else:
        suffix = f" (exact JSON value, part {part}, continues)"
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
    pages = [f"**AI action plan - review page {number} of {total}**\n{body}" for number, body in enumerate(bodies, start=1)]
    if any(len(page) > AI_PREVIEW_PAGE_CHARS for page in pages):
        raise PlanPreviewError("Plan page exceeds the page size.")
    return pages


def render_confirmation_control(tool_plan: Any, tool_risk: Any, page_count: int) -> str:
    risk_value = getattr(tool_risk, "value", None)
    if not isinstance(risk_value, str):
        raise PlanPreviewError("Plan risk is unknown.")
    count = len(tuple(tool_plan))
    return (
        f"**Confirm AI action plan:** {count} action(s), overall risk {risk_value}.\n"
        f"Review all {page_count} plan page(s) above. Approve executes exactly that plan; "
        "Cancel executes nothing."
    )


def render_executed_tools(executed_tools: Any) -> str:
    if not executed_tools:
        return ""
    lines = ["Actions already executed:"]
    for tool in executed_tools:
        status = "ok" if getattr(tool, "ok", False) is True else "FAILED"
        name = clip_text(str(getattr(tool, "tool_name", "?")), 64)
        message = clip_text(str(getattr(tool, "message", "")), AI_MAX_TOOL_SUMMARY_CHARS)
        lines.append(f"- `{_code_safe(name)}` {status}: {message}")
    return clip_text("\n".join(lines), 900)


def render_result_messages(result: Any) -> list[str]:
    """Render a non-confirmation orchestrator result into safe Discord texts."""
    status = getattr(getattr(result, "status", None), "value", None)
    message = clip_text(str(getattr(result, "message", "") or ""), 400)
    executed = render_executed_tools(getattr(result, "executed_tools", ()))
    if status == "COMPLETED":
        chunks = chunk_text(str(getattr(result, "content", "") or "")) or ["(The AI returned no text.)"]
        if executed:
            chunks.append(executed)
        return chunks
    if status == "CANCELLED":
        if message == CORE_REJECTED_MESSAGE:
            text = "Cancelled. Nothing from this plan was executed."
        else:
            text = EXPIRED_MESSAGE
    elif status == "UNAVAILABLE":
        text = "AI is currently unavailable." if executed else UNAVAILABLE_MESSAGE
    elif status == "INVALID_TOOL_PLAN":
        text = "The AI proposed an invalid action plan; nothing from that plan was executed."
        if message:
            text += f"\nReason: {message}"
    elif status == "TOOL_EXECUTION_FAILED":
        text = "An AI action failed; remaining actions were not executed."
        if message:
            text += f"\nReason: {message}"
    elif status == "LIMIT_REACHED":
        text = "The AI request stopped because a safety limit was reached."
        if message:
            text += f"\nReason: {message}"
    else:
        text = "The AI request ended in an unexpected state. Nothing further was executed."
    parts = [clip_text(text, AI_MESSAGE_CHUNK_CHARS)]
    if executed:
        parts.append(executed)
    return parts


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


class ConfirmationView(discord.ui.View):
    def __init__(self, transport: "AITransport", state: PendingConfirmation) -> None:
        super().__init__(timeout=CONFIRMATION_VIEW_TIMEOUT_SECONDS)
        self.transport = transport
        self.state = state
        self.message: Any = None

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


def _default_orchestrator_factory() -> Any:
    _ai_platform, ai_orchestrator = _import_ai_modules()
    return ai_orchestrator.AIOrchestrator()


AuditFn = Callable[[Any, dict, str, str], Awaitable[str | None]]


class AITransport:
    def __init__(
        self,
        *,
        load_config: Callable[[], dict],
        fetch_user: Callable[[int], Awaitable[Any]] | None = None,
        audit: AuditFn | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        view_factory: Callable[["AITransport", PendingConfirmation], Any] | None = None,
    ) -> None:
        self._load_config = load_config
        self._fetch_user = fetch_user
        self._audit = audit
        self._orchestrator_factory = orchestrator_factory or _default_orchestrator_factory
        self._view_factory = view_factory or ConfirmationView
        self._orchestrator: Any = None

    # -- lifecycle ---------------------------------------------------------

    def get_orchestrator(self) -> Any:
        """Lazy per-process orchestrator; a failure is not cached (retry later)."""
        if self._orchestrator is None:
            try:
                self._orchestrator = self._orchestrator_factory()
            except Exception as exc:
                print(f"AI orchestrator unavailable: {type(exc).__name__}")
                return None
        return self._orchestrator

    def _try_load_config(self) -> dict | None:
        try:
            config = self._load_config()
        except Exception:
            return None
        return config if isinstance(config, dict) else None

    # -- sending -----------------------------------------------------------

    @staticmethod
    async def _respond(interaction: Any, content: str) -> None:
        content = clip_text(content, AI_MESSAGE_CHUNK_CHARS)
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=no_mentions())
        else:
            await interaction.response.send_message(content, ephemeral=True, allowed_mentions=no_mentions())

    async def _send_result(self, interaction: Any, result: Any, binding: RequestBinding) -> None:
        status = getattr(getattr(result, "status", None), "value", None)
        if status == "NEEDS_CONFIRMATION" and getattr(result, "confirmation_id", None):
            executed = render_executed_tools(getattr(result, "executed_tools", ()))
            if executed:
                await interaction.followup.send(executed, ephemeral=True, allowed_mentions=no_mentions())
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
                await interaction.followup.send(
                    unreviewable_plan_message(too_large=too_large, cancelled=cancelled, earlier_actions=bool(executed)),
                    ephemeral=True,
                    allowed_mentions=no_mentions(),
                )
                return
            for page in pages:
                await interaction.followup.send(page, ephemeral=True, allowed_mentions=no_mentions())
            state = PendingConfirmation(confirmation_id=result.confirmation_id, binding=binding, summary=summary)
            view = self._view_factory(self, state)
            message = await interaction.followup.send(
                summary,
                ephemeral=True,
                allowed_mentions=no_mentions(),
                view=view,
                wait=True,
            )
            try:
                view.message = message
            except Exception:
                pass
            return
        for text in render_result_messages(result):
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=no_mentions())

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

    def build_executor(self, interaction: Any, binding: RequestBinding) -> Callable[[str, dict], Awaitable[Any]]:
        """Executor bound to ONE Discord interaction (fresh per approval)."""

        async def executor(tool_name: str, arguments: dict) -> admin_tools.ToolResult:
            config = self._try_load_config()
            guild = getattr(interaction, "guild", None)
            if config is None or guild is None or getattr(guild, "id", None) != binding.guild_id:
                return admin_tools.ToolResult(False, tool_name, AUTH_REVOKED_TOOL_MESSAGE)
            if getattr(getattr(interaction, "user", None), "id", None) != binding.user_id:
                return admin_tools.ToolResult(False, tool_name, AUTH_REVOKED_TOOL_MESSAGE)
            # Fresh member state (current roles), fail closed if not resolvable.
            member = guild.get_member(binding.user_id) if hasattr(guild, "get_member") else None
            if member is None or not actor_has_ai_access(member, guild, config):
                return admin_tools.ToolResult(False, tool_name, AUTH_REVOKED_TOOL_MESSAGE)
            context = admin_tools.AdminToolContext(
                guild=guild,
                fetch_user=self._fetch_user,
                source=AI_SOURCE,
                requesting_user_id=binding.user_id,
                requesting_user_name=str(member),
                suppress_mentions=True,
            )
            result = await admin_tools.execute_tool(context, tool_name, arguments)
            audit_failure = await self._safe_audit(interaction, config, tool_name, result.message)
            if result.ok and audit_failure:
                result = admin_tools.ToolResult(
                    True,
                    result.tool_name,
                    f"{result.message} (Action completed, but audit logging failed.)",
                    result.data,
                )
            return result

        return executor

    async def _safe_audit(self, interaction: Any, config: dict, tool_name: str, message: str) -> str | None:
        if self._audit is None:
            return None
        try:
            return await self._audit(interaction, config, f"{AI_SOURCE} {tool_name}", clip_text(message, 1000))
        except Exception as exc:
            return f"Audit logging failed: {type(exc).__name__}"

    # -- /ai ---------------------------------------------------------------

    async def handle_ai_command(self, interaction: Any, prompt: Any, mode: str | None = None) -> None:
        config = self._try_load_config()
        if config is None:
            await self._respond(interaction, CONFIG_UNAVAILABLE_MESSAGE)
            return
        if not actor_has_ai_access(interaction.user, interaction.guild, config):
            await self._respond(interaction, ACCESS_DENIED_MESSAGE)
            return
        if not isinstance(prompt, str) or not prompt.strip():
            await self._respond(interaction, "Prompt must not be empty.")
            return
        if len(prompt) > AI_PROMPT_MAX_CHARS:
            await self._respond(interaction, f"Prompt must be {AI_PROMPT_MAX_CHARS} characters or fewer.")
            return
        selected_mode = (mode or DEFAULT_TASK_MODE).lower()
        if selected_mode not in TASK_MODES:
            await self._respond(interaction, "Unknown AI mode.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        binding = RequestBinding(
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            channel_id=getattr(interaction, "channel_id", None),
        )
        try:
            ai_platform, ai_orchestrator = _import_ai_modules()
            orchestrator = self.get_orchestrator()
            if orchestrator is None:
                await self._respond(interaction, UNAVAILABLE_MESSAGE)
                return
            request = ai_orchestrator.OrchestratorRequest(
                messages=(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content=prompt),),
                task_class=ai_platform.TaskClass(TASK_MODES[selected_mode]),
            )
            policy = ai_orchestrator.ConfirmationPolicy(confirm_normal=True)
        except Exception as exc:
            print(f"/ai request failed: {type(exc).__name__}")
            await self._respond(interaction, UNAVAILABLE_MESSAGE)
            return
        try:
            result = await orchestrator.orchestrate(
                request,
                executor=self.build_executor(interaction, binding),
                confirmation_policy=policy,
            )
        except Exception as exc:
            # The core contains provider/executor failures; reaching this means an
            # unexpected error after tools may have run. Do not claim nothing ran.
            print(f"/ai request failed: {type(exc).__name__}")
            await self._respond(interaction, UNEXPECTED_FAILURE_MESSAGE)
            return
        await self._send_result(interaction, result, binding)

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
        if not self._binding_matches(interaction, state.binding):
            await self._respond(interaction, NOT_OWNER_MESSAGE)
            return
        config = self._try_load_config()
        if config is None:
            await self._respond(interaction, CONFIG_UNAVAILABLE_MESSAGE)
            return
        if not actor_has_ai_access(interaction.user, interaction.guild, config):
            await self._respond(interaction, ACCESS_DENIED_MESSAGE)
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
        decision = "Approved. Executing the plan..." if approved else "Cancelled."
        try:
            await interaction.response.edit_message(
                content=clip_text(f"{state.summary}\n\n{decision}", AI_MESSAGE_CHUNK_CHARS),
                view=None,
                allowed_mentions=no_mentions(),
            )
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result = await orchestrator.approve_confirmation(
                state.confirmation_id,
                approved=approved,
                executor=self.build_executor(interaction, state.binding),
            )
        except Exception as exc:
            print(f"/ai confirmation failed: {type(exc).__name__}")
            await self._respond(interaction, UNEXPECTED_FAILURE_MESSAGE)
            return
        await self._send_result(interaction, result, state.binding)
