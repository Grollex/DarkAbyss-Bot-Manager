from __future__ import annotations

import json
import math
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

import admin_tools
import ai_platform


MAX_PROVIDER_ATTEMPTS = 4
MAX_TOOL_ROUNDS = 15
# Bounded by the exact confirmation preview (admin_ai.AI_MAX_PREVIEW_PAGES):
# 8 maximal send_message calls are the worst case that still fits.
MAX_TOOL_CALLS_PER_ROUND = 8
# With OrchestratorRequest.recover_errors, invalid tool calls and failed tool
# results are returned to the model (instead of ending the run) at most this
# many times per orchestration.
MAX_RECOVERED_ERRORS = 3
MAX_PENDING_CONFIRMATIONS = 64
MAX_PENDING_COMPARES = 32
MAX_COMPARE_PROFILES = ai_platform.MAX_COMPARE_PROFILES
CONFIRMATION_TTL_SECONDS = 900.0
COMPARE_TTL_SECONDS = 900.0
MAX_TOOL_RESULT_BYTES = 65536
MAX_PUBLIC_MESSAGE_CHARS = 500
MAX_TOOL_CALL_ID_CHARS = 128


SYSTEM_INSTRUCTION = (
    "Answer the user's request. Use only the supplied local tools when a structured action is needed. "
    "Do not invent tool names. Do not claim a tool succeeded before receiving its tool result. "
    "Ask for clarification in visible text when required data is missing."
)


class OrchestratorError(ai_platform.AIPlatformError):
    """Contained optional-AI orchestrator failure."""


class OrchestratorStatus(str, Enum):
    DIRECT = "DIRECT"
    COMPLETED = "COMPLETED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_TOOL_PLAN = "INVALID_TOOL_PLAN"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    LIMIT_REACHED = "LIMIT_REACHED"


class CompareStatus(str, Enum):
    COMPLETED = "COMPLETED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class ToolExecutor(Protocol):
    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> Awaitable[admin_tools.ToolResult]:
        ...


@dataclass(frozen=True)
class ConfirmationPolicy:
    confirm_normal: bool = False

    """Fixed-semantics value object (not an extension hook).

    READ never requires confirmation, NORMAL requires it iff ``confirm_normal``
    is True, DESTRUCTIVE always requires it. Only the exact ConfirmationPolicy
    type is accepted by the orchestrator; subclasses are rejected.
    """

    def __post_init__(self) -> None:
        if type(self.confirm_normal) is not bool:
            raise ValueError("confirm_normal must be boolean.")

    def requires_confirmation(self, risk: ai_platform.ToolRisk) -> bool:
        return _requires_confirmation(self, risk)


@dataclass(frozen=True)
class OrchestratorRequest:
    messages: tuple[ai_platform.AIMessage, ...]
    task_class: ai_platform.TaskClass | str
    manual_profile_id: str | None = None
    allow_fallback_on_manual_override: bool = False
    allowed_tool_names: tuple[str, ...] | None = None
    # True: invalid tool calls and failed tool results go back to the model as
    # tool results (bounded by MAX_RECOVERED_ERRORS) so it can correct itself.
    # False keeps the strict fail-fast contract.
    recover_errors: bool = False
    # False: never switch to a routing fallback profile automatically; the
    # caller asks the user first (see AIOrchestrator.alternative_profile).
    auto_fallback: bool = True

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("messages must not be empty.")
        if not all(isinstance(message, ai_platform.AIMessage) for message in messages):
            raise ValueError("messages must contain AIMessage objects.")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "task_class", ai_platform._coerce_task_class(self.task_class))
        if self.manual_profile_id is not None:
            ai_platform._validate_identifier(self.manual_profile_id, "manual_profile_id")
        if not isinstance(self.allow_fallback_on_manual_override, bool):
            raise ValueError("allow_fallback_on_manual_override must be boolean.")
        if type(self.recover_errors) is not bool:
            raise ValueError("recover_errors must be boolean.")
        if type(self.auto_fallback) is not bool:
            raise ValueError("auto_fallback must be boolean.")
        if self.allowed_tool_names is not None:
            object.__setattr__(self, "allowed_tool_names", _coerce_allowed_tool_names(self.allowed_tool_names))


@dataclass(frozen=True)
class AttemptRecord:
    profile_id: str | None
    provider_id: str | None
    model_id: str | None
    category: str
    fallback: bool = False
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _bounded_text(self.message))

    def public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "category": self.category,
            "fallback": self.fallback,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidatedToolCall:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    risk: ai_platform.ToolRisk

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not isinstance(self.tool_name, str):
            raise ValueError("call_id and tool_name must be strings.")
        if not isinstance(self.risk, ai_platform.ToolRisk):
            raise ValueError("risk must be a ToolRisk.")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments must be a mapping.")
        # Recursive copy-and-freeze: builds brand-new immutable containers, so
        # the stored plan shares no mutable object with the caller/provider.
        object.__setattr__(self, "arguments", _json_freeze(self.arguments))

    def executor_arguments(self) -> dict[str, Any]:
        """Fresh, ordinary JSON-compatible dict for one executor invocation."""
        return _json_thaw(self.arguments)

    def public_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": _json_thaw(self.arguments),
            "risk": self.risk.value,
        }


@dataclass(frozen=True)
class ExecutedToolSummary:
    call_id: str
    tool_name: str
    ok: bool
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ok", self.ok is True)
        object.__setattr__(self, "message", _bounded_text(self.message))

    def public_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "message": self.message,
        }


@dataclass(frozen=True)
class OrchestratorResult:
    status: OrchestratorStatus
    content: str = ""
    profile_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    task_class: ai_platform.TaskClass | None = None
    manual_override: bool = False
    fallback_used: bool = False
    attempts: tuple[AttemptRecord, ...] = ()
    executed_tools: tuple[ExecutedToolSummary, ...] = ()
    confirmation_id: str | None = None
    tool_risk: ai_platform.ToolRisk | None = None
    tool_plan: tuple[ValidatedToolCall, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _bounded_text(self.message))

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "content": self.content,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "task_class": self.task_class.value if self.task_class else None,
            "manual_override": self.manual_override,
            "fallback_used": self.fallback_used,
            "attempts": [attempt.public_dict() for attempt in self.attempts],
            "executed_tools": [tool.public_dict() for tool in self.executed_tools],
            "confirmation_id": self.confirmation_id,
            "tool_risk": self.tool_risk.value if self.tool_risk else None,
            "tool_plan": [call.public_dict() for call in self.tool_plan],
            "message": self.message,
        }


@dataclass(frozen=True)
class CompareCandidate:
    profile_id: str
    provider_id: str | None
    model_id: str | None
    status: CompareStatus
    content: str = ""
    tool_plan: tuple[ValidatedToolCall, ...] = ()
    tool_risk: ai_platform.ToolRisk | None = None
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _bounded_text(self.message))

    def public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": self.status.value,
            "content": self.content,
            "tool_plan": [call.public_dict() for call in self.tool_plan],
            "tool_risk": self.tool_risk.value if self.tool_risk else None,
            "message": self.message,
        }


@dataclass(frozen=True)
class CompareResult:
    compare_id: str | None
    candidates: tuple[CompareCandidate, ...]
    status: CompareStatus
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _bounded_text(self.message))

    def public_dict(self) -> dict[str, Any]:
        return {
            "compare_id": self.compare_id,
            "status": self.status.value,
            "message": self.message,
            "candidates": [candidate.public_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class _SelectedProfile:
    profile: ai_platform.AIProfile
    provider: ai_platform.AIProvider
    fallback: bool
    attempt: AttemptRecord


@dataclass(frozen=True)
class _RunContext:
    """Fixed per-orchestration state shared by every tool round and resume.

    ``allowed_tool_names`` and ``tools`` always come from the ORIGINAL
    orchestration request (or compare request) and never from a previous
    tool batch. ``confirmation_policy`` is fixed when the run begins
    (orchestrate() or select_compare_candidate()) and is reused for every
    later round and confirmation resume; approval cannot replace it.
    """

    profile: ai_platform.AIProfile
    provider: ai_platform.AIProvider
    task_class: ai_platform.TaskClass
    manual_override: bool
    fallback_used: bool
    allowed_tool_names: tuple[str, ...] | None
    tools: tuple[Mapping[str, Any], ...]
    confirmation_policy: ConfirmationPolicy
    recover_errors: bool = False


@dataclass(frozen=True)
class _PendingPlan:
    context: _RunContext
    # Already-initialized provider history: contains the orchestrator SYSTEM
    # instruction exactly once. Never pass it through _initial_history again.
    history_before_response: tuple[ai_platform.AIMessage, ...]
    response: ai_platform.AIResponse
    tool_plan: tuple[ValidatedToolCall, ...]
    attempts: tuple[AttemptRecord, ...]
    executed_tools: tuple[ExecutedToolSummary, ...]
    # Total tool rounds consumed by this orchestration, INCLUDING tool_plan.
    tool_rounds_used: int
    # Errors already returned to the model (recover_errors runs only).
    tool_errors_used: int = 0


@dataclass(frozen=True)
class _StoredCompareCandidate:
    profile: ai_platform.AIProfile
    provider: ai_platform.AIProvider
    response: ai_platform.AIResponse
    # Already-initialized provider history (SYSTEM instruction included once).
    history_before_response: tuple[ai_platform.AIMessage, ...]
    tool_plan: tuple[ValidatedToolCall, ...]
    allowed_tool_names: tuple[str, ...] | None


@dataclass(frozen=True)
class _StoredCompare:
    candidates: Mapping[str, _StoredCompareCandidate]


def build_default_provider_registry(
    credential_store: ai_platform.CredentialStore,
) -> ai_platform.LazyProviderRegistry:
    """Shared Groq/Gemini adapters bound to ONE bot instance's credentials."""
    if not isinstance(credential_store, ai_platform.CredentialStore):
        raise ValueError("A bot instance's CredentialStore is required.")
    credentials = credential_store
    registry = ai_platform.LazyProviderRegistry()

    # Bot requests retry transient provider failures (rate limit, 5xx, network);
    # explicit Manager "Test Connection" calls build their own providers and
    # fail fast.
    def create_groq() -> ai_platform.AIProvider:
        import ai_groq

        return ai_groq.GroqProvider(credentials, retry_delays=ai_groq.DEFAULT_RETRY_DELAYS)

    def create_gemini() -> ai_platform.AIProvider:
        import ai_gemini

        return ai_gemini.GeminiProvider(credentials, retry_delays=ai_gemini.DEFAULT_RETRY_DELAYS)

    registry.register_factory("groq", create_groq)
    registry.register_factory("gemini", create_gemini)
    return registry


class AIOrchestrator:
    def __init__(
        self,
        *,
        settings_store: ai_platform.AISettingsStore | None = None,
        provider_registry: ai_platform.ProviderRegistry | None = None,
        credential_store: ai_platform.CredentialStore | None = None,
        clock: Callable[[], float] | None = None,
        stores: Any = None,
    ) -> None:
        if stores is not None:
            # ai_storage.InstanceAIStores of one bot instance.
            settings_store = settings_store or stores.settings
            credential_store = credential_store or stores.credentials
        # AI settings and credentials belong to one bot instance (ai_storage);
        # there is no global fallback, so a bot can never pick up another
        # bot's keys or routing by accident.
        if settings_store is None or credential_store is None:
            raise ValueError("AIOrchestrator needs the bot instance's settings_store and credential_store.")
        self._settings_store = settings_store
        self._credential_store = credential_store
        # Use explicit None checks: an injected empty ProviderRegistry has len()==0
        # and is falsy, but it is a valid caller choice (zero providers) and must
        # never be silently replaced by the default Groq/Gemini registry.
        self._providers = (
            provider_registry
            if provider_registry is not None
            else build_default_provider_registry(self._credential_store)
        )
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable.")
        # Monotonic clock for pending-state expiry; injectable for tests.
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        # Pending state is in-memory only, bounded in count and expiring by TTL.
        # Values are (expires_at, state). Expired entries are purged lazily on
        # every create/lookup; no background thread, no persistence.
        self._pending_confirmations: dict[str, tuple[float, _PendingPlan]] = {}
        self._pending_compares: dict[str, tuple[float, _StoredCompare]] = {}

    def _now(self) -> float:
        now = self._clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise OrchestratorError("Orchestrator clock returned an invalid value.")
        return float(now)

    def _purge_expired(self) -> None:
        now = self._now()
        for store in (self._pending_confirmations, self._pending_compares):
            for key in [key for key, (expires_at, _state) in store.items() if expires_at <= now]:
                store.pop(key, None)

    def _take_pending(self, store: dict[str, tuple[float, Any]], key: str) -> Any:
        """Single-use removal; expired entries are dropped and never returned."""
        entry = store.pop(key, None)
        self._purge_expired()
        if entry is None:
            return None
        expires_at, state = entry
        if expires_at <= self._now():
            return None
        return state

    def _peek_pending(self, store: dict[str, tuple[float, Any]], key: str) -> Any:
        self._purge_expired()
        entry = store.get(key)
        return None if entry is None else entry[1]

    async def orchestrate(
        self,
        request: OrchestratorRequest,
        *,
        executor: ToolExecutor | None = None,
        confirmation_policy: ConfirmationPolicy = ConfirmationPolicy(),
    ) -> OrchestratorResult:
        if not isinstance(request, OrchestratorRequest):
            raise ValueError("request must be an OrchestratorRequest.")
        if request.task_class is ai_platform.TaskClass.DIRECT:
            return OrchestratorResult(
                status=OrchestratorStatus.DIRECT,
                content=request.messages[-1].content,
                task_class=request.task_class,
                manual_override=request.manual_profile_id is not None,
                message="DIRECT task bypassed AI provider routing.",
            )

        _validate_confirmation_policy(confirmation_policy)
        manual_override = request.manual_profile_id is not None
        try:
            settings = self._settings_store.load()
            profiles = ai_platform.AIProfileStore({profile.profile_id: profile for profile in settings.profiles})
            tools = _provider_tool_schemas(request.allowed_tool_names)
        except Exception as exc:
            return OrchestratorResult(
                status=OrchestratorStatus.UNAVAILABLE,
                task_class=request.task_class,
                manual_override=manual_override,
                message=_safe_exception_message("AI settings are unavailable.", exc),
            )

        entries = self._select_profiles(
            request=request,
            settings=settings,
            profiles=profiles,
            allow_fallback=request.auto_fallback
            and (not manual_override or request.allow_fallback_on_manual_override),
        )

        # Ordered, accumulating attempt history across the whole routing pass:
        # unavailable/skipped profiles, selections, and provider failures.
        attempts: tuple[AttemptRecord, ...] = ()
        seen_profiles: set[str] = set()
        last_result: OrchestratorResult | None = None
        for entry in entries:
            if isinstance(entry, AttemptRecord):
                attempts = (*attempts, entry)
                continue
            if entry.profile.profile_id in seen_profiles:
                continue
            seen_profiles.add(entry.profile.profile_id)
            attempts = (*attempts, entry.attempt)
            context = _RunContext(
                profile=entry.profile,
                provider=entry.provider,
                task_class=request.task_class,
                manual_override=manual_override,
                fallback_used=entry.fallback,
                allowed_tool_names=request.allowed_tool_names,
                tools=tools,
                confirmation_policy=confirmation_policy,
                recover_errors=request.recover_errors,
            )
            result = await self._run_loop(
                context=context,
                history=_initial_history(request.messages),
                executor=executor,
                attempts=attempts,
                executed_tools=(),
                tool_rounds_used=0,
            )
            if result.status is not OrchestratorStatus.UNAVAILABLE:
                return result
            if _has_side_effects(result.executed_tools):
                # Never fall back after any side effect. Read-only tools change
                # nothing, so a fallback profile may start the request over.
                return result
            attempts = result.attempts
            last_result = result

        if last_result is not None:
            return replace(last_result, attempts=attempts)
        return OrchestratorResult(
            status=OrchestratorStatus.UNAVAILABLE,
            task_class=request.task_class,
            manual_override=manual_override,
            attempts=attempts,
            message="No usable AI profile is available.",
        )

    async def approve_confirmation(
        self,
        confirmation_id: str,
        *,
        approved: bool,
        executor: ToolExecutor,
    ) -> OrchestratorResult:
        """Approve or reject a stored plan.

        The caller controls only the confirmation ID, the yes/no decision and the
        executor. Profile, provider, model, tool plan, arguments, order,
        allowlist and confirmation policy all come from the stored run state.
        """
        # Validate ALL caller input before touching pending state, so invalid API
        # input neither authorizes, rejects, nor consumes the stored plan.
        if type(approved) is not bool:
            raise ValueError("approved must be exactly True or False.")
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ValueError("confirmation_id must be a non-empty string.")
        if approved and not callable(executor):
            raise ValueError("executor is required to approve a confirmation.")
        pending = self._take_pending(self._pending_confirmations, confirmation_id)
        if pending is None:
            return OrchestratorResult(status=OrchestratorStatus.CANCELLED, message="Unknown or expired confirmation.")
        if not approved:
            return _context_result(
                pending.context,
                OrchestratorStatus.CANCELLED,
                attempts=pending.attempts,
                executed_tools=pending.executed_tools,
                tool_plan=pending.tool_plan,
                tool_risk=_batch_risk(pending.tool_plan),
                message="Confirmation rejected.",
            )
        return await self._execute_and_continue(pending=pending, executor=executor)

    async def compare_plans(
        self,
        compare_request: ai_platform.ComparePlanRequest,
        *,
        messages: tuple[ai_platform.AIMessage, ...],
        allowed_tool_names: tuple[str, ...] | None = None,
    ) -> CompareResult:
        if not isinstance(compare_request, ai_platform.ComparePlanRequest):
            raise ValueError("compare_request must be a ComparePlanRequest.")
        # Defensive resource bound at the provider-spending boundary: re-check the
        # (normally immutable) request so even a tampered instance cannot fan out
        # into an unbounded number of provider requests. Snapshot once and iterate
        # only that snapshot.
        profile_ids = _bounded_compare_profile_ids(compare_request.profile_ids)
        messages = tuple(messages)
        if not messages:
            raise ValueError("messages must not be empty.")
        if not all(isinstance(message, ai_platform.AIMessage) for message in messages):
            raise ValueError("messages must contain AIMessage objects.")
        if allowed_tool_names is not None:
            allowed_tool_names = _coerce_allowed_tool_names(allowed_tool_names)
        # Check the bound BEFORE spending any provider request.
        self._purge_expired()
        if len(self._pending_compares) >= MAX_PENDING_COMPARES:
            return CompareResult(None, (), CompareStatus.UNAVAILABLE, "Pending compare limit exceeded.")
        try:
            settings = self._settings_store.load()
            profiles = ai_platform.AIProfileStore({profile.profile_id: profile for profile in settings.profiles})
            tools = _provider_tool_schemas(allowed_tool_names)
        except Exception as exc:
            return CompareResult(None, (), CompareStatus.UNAVAILABLE, _safe_exception_message("AI settings are unavailable.", exc))

        candidates: list[CompareCandidate] = []
        stored: dict[str, _StoredCompareCandidate] = {}
        for profile_id in profile_ids:
            selected = self._select_one_profile(profile_id, profiles, fallback=False)
            if selected is None:
                candidates.append(CompareCandidate(profile_id, None, None, CompareStatus.UNAVAILABLE, message="Profile is unavailable."))
                continue
            history = _initial_history(messages)
            try:
                response = await selected.provider.generate(
                    ai_platform.AIRequest(
                        model_id=selected.profile.model_id,
                        messages=history,
                        tools=tools,
                        options=selected.profile.options,
                    ),
                    selected.profile.credential_ref,
                )
                if not isinstance(response, ai_platform.AIResponse):
                    raise OrchestratorError("Provider returned an invalid response.")
                plan = _validate_tool_plan(response.tool_calls, allowed_tool_names)
                candidates.append(
                    CompareCandidate(
                        profile_id=selected.profile.profile_id,
                        provider_id=selected.profile.provider_id,
                        model_id=selected.profile.model_id,
                        status=CompareStatus.COMPLETED,
                        content=response.content,
                        tool_plan=plan,
                        tool_risk=_batch_risk(plan) if plan else None,
                    )
                )
                stored[selected.profile.profile_id] = _StoredCompareCandidate(
                    profile=selected.profile,
                    provider=selected.provider,
                    response=response,
                    history_before_response=history,
                    tool_plan=plan,
                    allowed_tool_names=allowed_tool_names,
                )
            except Exception as exc:
                candidates.append(
                    CompareCandidate(
                        profile_id=selected.profile.profile_id,
                        provider_id=selected.profile.provider_id,
                        model_id=selected.profile.model_id,
                        status=CompareStatus.UNAVAILABLE,
                        message=_safe_exception_message("Provider candidate failed.", exc),
                    )
                )

        if not stored:
            return CompareResult(None, tuple(candidates), CompareStatus.UNAVAILABLE, "All compare candidates failed.")
        # Re-check after the awaits: concurrent compares may have filled the store.
        self._purge_expired()
        if len(self._pending_compares) >= MAX_PENDING_COMPARES:
            return CompareResult(None, tuple(candidates), CompareStatus.UNAVAILABLE, "Pending compare limit exceeded.")
        compare_id = _opaque_id("cmp")
        while compare_id in self._pending_compares:  # pragma: no cover - 192-bit collision
            compare_id = _opaque_id("cmp")
        self._pending_compares[compare_id] = (self._now() + COMPARE_TTL_SECONDS, _StoredCompare(stored))
        return CompareResult(compare_id, tuple(candidates), CompareStatus.COMPLETED)

    async def select_compare_candidate(
        self,
        compare_id: str,
        profile_id: str,
        *,
        executor: ToolExecutor | None = None,
        confirmation_policy: ConfirmationPolicy = ConfirmationPolicy(),
    ) -> OrchestratorResult:
        # Validate caller input before touching pending state.
        _validate_confirmation_policy(confirmation_policy)
        if not isinstance(compare_id, str) or not compare_id:
            raise ValueError("compare_id must be a non-empty string.")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("profile_id must be a non-empty string.")
        if executor is not None and not callable(executor):
            raise ValueError("executor must be callable.")
        peeked = self._peek_pending(self._pending_compares, compare_id)
        if peeked is not None:
            peeked_candidate = peeked.candidates.get(profile_id)
            if (
                peeked_candidate is not None
                and peeked_candidate.tool_plan
                and executor is None
                and not _requires_confirmation(confirmation_policy, _batch_risk(peeked_candidate.tool_plan))
            ):
                # Misuse: an ungated plan needs an executor. Do not consume the compare.
                raise ValueError("executor is required to run the selected candidate.")
        # Single selection: the compare is consumed by any valid selection attempt,
        # including one naming a profile that was not a stored candidate.
        stored_compare = self._take_pending(self._pending_compares, compare_id)
        if stored_compare is None:
            return OrchestratorResult(status=OrchestratorStatus.CANCELLED, message="Unknown or expired compare selection.")
        candidate = stored_compare.candidates.get(profile_id)
        if candidate is None:
            return OrchestratorResult(status=OrchestratorStatus.CANCELLED, message="Selected profile was not part of this compare result.")
        if not candidate.tool_plan:
            return OrchestratorResult(
                status=OrchestratorStatus.COMPLETED,
                content=candidate.response.content,
                profile_id=candidate.profile.profile_id,
                provider_id=candidate.profile.provider_id,
                model_id=candidate.profile.model_id,
            )
        context = _RunContext(
            profile=candidate.profile,
            provider=candidate.provider,
            task_class=ai_platform.TaskClass.ROUTINE,
            manual_override=True,
            fallback_used=False,
            allowed_tool_names=candidate.allowed_tool_names,
            tools=_provider_tool_schemas(candidate.allowed_tool_names),
            confirmation_policy=confirmation_policy,
        )
        if len(candidate.tool_plan) > MAX_TOOL_CALLS_PER_ROUND:
            return _context_result(context, OrchestratorStatus.LIMIT_REACHED, message="Tool call limit exceeded.")
        if MAX_TOOL_ROUNDS < 1:
            return _context_result(context, OrchestratorStatus.LIMIT_REACHED, message="Tool round limit exceeded.")
        pending = _PendingPlan(
            context=context,
            history_before_response=candidate.history_before_response,
            response=candidate.response,
            tool_plan=candidate.tool_plan,
            attempts=(),
            executed_tools=(),
            tool_rounds_used=1,
        )
        if _requires_confirmation(context.confirmation_policy, _batch_risk(candidate.tool_plan)):
            return self._store_confirmation(pending)
        if executor is None:
            return _context_result(context, OrchestratorStatus.INVALID_TOOL_PLAN, message="Tool executor is required.")
        return await self._execute_and_continue(pending=pending, executor=executor)

    def _select_profiles(
        self,
        *,
        request: OrchestratorRequest,
        settings: ai_platform.AISettings,
        profiles: ai_platform.AIProfileStore,
        allow_fallback: bool,
    ) -> tuple[_SelectedProfile | AttemptRecord, ...]:
        """Return routing entries in configured order.

        Each entry is either a usable selection or a sanitized UNAVAILABLE
        attempt record for a profile that was skipped locally.
        """
        primary_id = request.manual_profile_id or settings.routing.profile_for(request.task_class)
        fallback_ids = settings.routing.fallback_profiles_for(request.task_class) if allow_fallback else ()
        profile_ids = tuple(profile_id for profile_id in (primary_id, *fallback_ids) if profile_id)
        entries: list[_SelectedProfile | AttemptRecord] = []
        for index, profile_id in enumerate(profile_ids[:MAX_PROVIDER_ATTEMPTS]):
            selected = self._select_one_profile(profile_id, profiles, fallback=index > 0)
            if selected is None:
                entries.append(AttemptRecord(profile_id, None, None, "UNAVAILABLE", index > 0, "Profile/provider unavailable."))
            else:
                entries.append(selected)
        return tuple(entries)

    def _select_one_profile(
        self,
        profile_id: str,
        profiles: ai_platform.AIProfileStore,
        *,
        fallback: bool,
    ) -> _SelectedProfile | None:
        profile = profiles.get(profile_id)
        if profile is None or not profile.enabled:
            return None
        # Every local lookup is contained: a broken registry, credential store
        # or provider availability check makes the profile unavailable instead
        # of crashing the caller. No network access happens here.
        try:
            provider = self._providers.get(profile.provider_id)
            if provider is None:
                return None
            if profile.credential_ref and not self._credential_store.exists(profile.provider_id, profile.credential_ref):
                return None
            availability = provider.get_local_availability(
                credential_ref=profile.credential_ref,
                credential_available=bool(profile.credential_ref),
            )
            if not availability.ok:
                return None
        except Exception:
            return None
        return _SelectedProfile(
            profile=profile,
            provider=provider,
            fallback=fallback,
            attempt=AttemptRecord(profile.profile_id, profile.provider_id, profile.model_id, "SELECTED", fallback, availability.message),
        )

    def alternative_profile(
        self,
        task_class: ai_platform.TaskClass | str,
        exclude_profile_ids: Sequence[str] = (),
    ) -> dict[str, str] | None:
        """Another locally usable profile for ``task_class`` (no network).

        Order: the task class's fallback list, then the other routed engines
        (planner / routine / creative). Used by transports that ask the user
        before switching engines (``OrchestratorRequest.auto_fallback=False``).
        """
        try:
            task = ai_platform._coerce_task_class(task_class)
            settings = self._settings_store.load()
            profiles = ai_platform.AIProfileStore({profile.profile_id: profile for profile in settings.profiles})
            routing = settings.routing
            candidates = [
                *routing.fallback_profiles_for(task),
                routing.planner_profile_id,
                routing.routine_profile_id,
                routing.creative_profile_id,
            ]
        except Exception:
            return None
        excluded = set(exclude_profile_ids)
        for profile_id in candidates:
            if not profile_id or profile_id in excluded:
                continue
            selected = self._select_one_profile(profile_id, profiles, fallback=True)
            if selected is not None:
                return {
                    "profile_id": selected.profile.profile_id,
                    "provider_id": selected.profile.provider_id,
                    "model_id": selected.profile.model_id,
                }
        return None

    async def _run_loop(
        self,
        *,
        context: _RunContext,
        history: tuple[ai_platform.AIMessage, ...],
        executor: ToolExecutor | None,
        attempts: tuple[AttemptRecord, ...],
        executed_tools: tuple[ExecutedToolSummary, ...],
        tool_rounds_used: int,
        tool_errors_used: int = 0,
    ) -> OrchestratorResult:
        """Run the bounded tool loop on ALREADY-INITIALIZED provider history.

        ``tool_rounds_used`` is the total consumed by this orchestration so far
        (including earlier runs before a confirmation pause), so
        MAX_TOOL_ROUNDS is a hard cap across resumes. Every iteration either
        returns or consumes one tool round, so the loop is bounded.
        ``tool_errors_used`` likewise bounds recovered errors across resumes.
        """
        while True:
            try:
                response = await context.provider.generate(
                    ai_platform.AIRequest(
                        model_id=context.profile.model_id,
                        messages=history,
                        tools=context.tools,
                        options=context.profile.options,
                    ),
                    context.profile.credential_ref,
                )
                if not isinstance(response, ai_platform.AIResponse):
                    raise OrchestratorError("Provider returned an invalid response.")
            except Exception as exc:
                if isinstance(exc, ai_platform.AIPlatformError) and not isinstance(exc, OrchestratorError):
                    # Provider adapters raise fixed, sanitized texts ("Groq rate
                    # limit or quota was reached.") - safe and useful to show.
                    safe_message = _bounded_text(f"Provider failed. {exc}")
                else:
                    safe_message = _safe_exception_message("Provider failed.", exc)
                failed = AttemptRecord(
                    context.profile.profile_id,
                    context.profile.provider_id,
                    context.profile.model_id,
                    "FAILED",
                    context.fallback_used,
                    safe_message,
                )
                return _context_result(
                    context,
                    OrchestratorStatus.UNAVAILABLE,
                    attempts=(*attempts, failed),
                    executed_tools=executed_tools,
                    message=safe_message,
                )
            try:
                if context.recover_errors:
                    tool_plan, call_errors = _validate_tool_plan(
                        response.tool_calls, context.allowed_tool_names, collect_errors=True
                    )
                else:
                    tool_plan, call_errors = _validate_tool_plan(response.tool_calls, context.allowed_tool_names), {}
            except Exception as exc:
                # Model-controlled plans can never crash the caller.
                message = str(exc) if isinstance(exc, OrchestratorError) else _safe_exception_message(
                    "Provider tool plan could not be validated.", exc
                )
                return _context_result(
                    context,
                    OrchestratorStatus.INVALID_TOOL_PLAN,
                    content=response.content,
                    attempts=attempts,
                    executed_tools=executed_tools,
                    message=message,
                )
            if call_errors:
                # Nothing from this batch runs; every call gets a result so the
                # model can correct the arguments and try again.
                first_error = next(iter(call_errors.values()))
                if tool_errors_used >= MAX_RECOVERED_ERRORS or tool_rounds_used >= MAX_TOOL_ROUNDS:
                    return _context_result(
                        context,
                        OrchestratorStatus.INVALID_TOOL_PLAN,
                        content=response.content,
                        attempts=attempts,
                        executed_tools=executed_tools,
                        message=first_error,
                    )
                tool_errors_used += 1
                tool_rounds_used += 1
                history = _append_tool_round(history, response, _rejected_call_messages(response.tool_calls, call_errors))
                continue
            if not tool_plan:
                return _context_result(
                    context,
                    OrchestratorStatus.COMPLETED,
                    content=response.content,
                    attempts=attempts,
                    executed_tools=executed_tools,
                )
            if tool_rounds_used >= MAX_TOOL_ROUNDS:
                return _context_result(
                    context,
                    OrchestratorStatus.LIMIT_REACHED,
                    attempts=attempts,
                    executed_tools=executed_tools,
                    message="Tool round limit exceeded.",
                )
            if len(tool_plan) > MAX_TOOL_CALLS_PER_ROUND:
                return _context_result(
                    context,
                    OrchestratorStatus.LIMIT_REACHED,
                    attempts=attempts,
                    executed_tools=executed_tools,
                    message="Tool call limit exceeded.",
                )
            tool_rounds_used += 1
            pending = _PendingPlan(
                context=context,
                history_before_response=history,
                response=response,
                tool_plan=tool_plan,
                attempts=attempts,
                executed_tools=executed_tools,
                tool_rounds_used=tool_rounds_used,
                tool_errors_used=tool_errors_used,
            )
            if _requires_confirmation(context.confirmation_policy, _batch_risk(tool_plan)):
                return self._store_confirmation(pending)
            if executor is None:
                return _context_result(
                    context,
                    OrchestratorStatus.INVALID_TOOL_PLAN,
                    attempts=attempts,
                    executed_tools=executed_tools,
                    message="Tool executor is required.",
                )
            executed = await _execute_tool_batch(tool_plan, executor)
            executed_tools = (*executed_tools, *executed.summaries)
            if executed.failure:
                if not _can_recover(context, executed, tool_errors_used):
                    return _context_result(
                        context,
                        OrchestratorStatus.TOOL_EXECUTION_FAILED,
                        content=response.content,
                        attempts=attempts,
                        executed_tools=executed_tools,
                        message=executed.failure,
                    )
                tool_errors_used += 1
            history = _append_tool_round(history, response, _completed_tool_messages(tool_plan, executed))

    def _store_confirmation(self, pending: _PendingPlan) -> OrchestratorResult:
        self._purge_expired()
        if len(self._pending_confirmations) >= MAX_PENDING_CONFIRMATIONS:
            # Keep run context and already-executed tool summaries visible; the
            # new plan is NOT stored and nothing from it executes.
            return _context_result(
                pending.context,
                OrchestratorStatus.LIMIT_REACHED,
                attempts=pending.attempts,
                executed_tools=pending.executed_tools,
                message="Pending confirmation limit exceeded.",
            )
        confirmation_id = _opaque_id("confirm")
        while confirmation_id in self._pending_confirmations:  # pragma: no cover - 192-bit collision
            confirmation_id = _opaque_id("confirm")
        self._pending_confirmations[confirmation_id] = (self._now() + CONFIRMATION_TTL_SECONDS, pending)
        return _context_result(
            pending.context,
            OrchestratorStatus.NEEDS_CONFIRMATION,
            content=pending.response.content,
            attempts=pending.attempts,
            executed_tools=pending.executed_tools,
            confirmation_id=confirmation_id,
            tool_risk=_batch_risk(pending.tool_plan),
            tool_plan=pending.tool_plan,
            message="Tool plan requires confirmation.",
        )

    async def _execute_and_continue(
        self,
        *,
        pending: _PendingPlan,
        executor: ToolExecutor,
    ) -> OrchestratorResult:
        executed = await _execute_tool_batch(pending.tool_plan, executor)
        executed_tools = (*pending.executed_tools, *executed.summaries)
        tool_errors_used = pending.tool_errors_used
        if executed.failure:
            if not _can_recover(pending.context, executed, tool_errors_used):
                return _context_result(
                    pending.context,
                    OrchestratorStatus.TOOL_EXECUTION_FAILED,
                    content=pending.response.content,
                    attempts=pending.attempts,
                    executed_tools=executed_tools,
                    message=executed.failure,
                )
            tool_errors_used += 1
        # pending.history_before_response is already initialized; reuse it as-is.
        history = _append_tool_round(
            pending.history_before_response,
            pending.response,
            _completed_tool_messages(pending.tool_plan, executed),
        )
        return await self._run_loop(
            context=pending.context,
            history=history,
            executor=executor,
            attempts=pending.attempts,
            executed_tools=executed_tools,
            tool_rounds_used=pending.tool_rounds_used,
            tool_errors_used=tool_errors_used,
        )


def _context_result(
    context: _RunContext,
    status: OrchestratorStatus,
    *,
    content: str = "",
    attempts: tuple[AttemptRecord, ...] = (),
    executed_tools: tuple[ExecutedToolSummary, ...] = (),
    confirmation_id: str | None = None,
    tool_risk: ai_platform.ToolRisk | None = None,
    tool_plan: tuple[ValidatedToolCall, ...] = (),
    message: str = "",
) -> OrchestratorResult:
    return OrchestratorResult(
        status=status,
        content=content,
        profile_id=context.profile.profile_id,
        provider_id=context.profile.provider_id,
        model_id=context.profile.model_id,
        task_class=context.task_class,
        manual_override=context.manual_override,
        fallback_used=context.fallback_used,
        attempts=attempts,
        executed_tools=executed_tools,
        confirmation_id=confirmation_id,
        tool_risk=tool_risk,
        tool_plan=tool_plan,
        message=message,
    )


@dataclass(frozen=True)
class _ExecutionBatch:
    summaries: tuple[ExecutedToolSummary, ...]
    tool_messages: tuple[ai_platform.AIMessage, ...]
    failure: str | None = None
    # Fatal failures (executor crash, a result flagged data={"fatal": True},
    # e.g. revoked authorization) always end the run, even with recover_errors.
    fatal: bool = False


EXECUTOR_FAILURE_MESSAGE = "Tool executor failed unexpectedly."


async def _execute_tool_batch(tool_plan: tuple[ValidatedToolCall, ...], executor: ToolExecutor) -> _ExecutionBatch:
    summaries: list[ExecutedToolSummary] = []
    tool_messages: list[ai_platform.AIMessage] = []
    for call in tool_plan:
        try:
            # Fresh, ordinary dict/list structure thawed from the immutable plan.
            result = await executor(call.tool_name, call.executor_arguments())
            # Strict success: only a real True counts; never truthiness.
            ok = result.ok is True
            result_message = str(result.message)
            safe_content = _serialize_tool_result(result)
        except Exception as exc:
            # Contain unexpected executor failures: stop the batch, never
            # fabricate success, expose only a safe message (type name only).
            summaries.append(ExecutedToolSummary(call.call_id, call.tool_name, False, EXECUTOR_FAILURE_MESSAGE))
            return _ExecutionBatch(
                tuple(summaries),
                tuple(tool_messages),
                _safe_exception_message(EXECUTOR_FAILURE_MESSAGE, exc),
                fatal=True,
            )
        summaries.append(ExecutedToolSummary(call.call_id, call.tool_name, ok, result_message))
        tool_messages.append(ai_platform.AIMessage(role=ai_platform.MessageRole.TOOL, content=safe_content, tool_call_id=call.call_id))
        if not ok:
            data = getattr(result, "data", None)
            fatal = isinstance(data, dict) and data.get("fatal") is True
            return _ExecutionBatch(tuple(summaries), tuple(tool_messages), f"Tool failed: {call.tool_name}", fatal=fatal)
    return _ExecutionBatch(tuple(summaries), tuple(tool_messages))


def _has_side_effects(executed_tools: tuple[ExecutedToolSummary, ...]) -> bool:
    """True if any executed tool is not a registered READ tool (unknown = side effect)."""
    for tool in executed_tools:
        try:
            if admin_tools.get_tool_definition(tool.tool_name).kind != "read":
                return True
        except admin_tools.AdminToolError:
            return True
    return False


def _can_recover(context: _RunContext, executed: _ExecutionBatch, tool_errors_used: int) -> bool:
    return context.recover_errors and not executed.fatal and tool_errors_used < MAX_RECOVERED_ERRORS


def _tool_error_message(call_id: str, tool_name: str, message: str) -> ai_platform.AIMessage:
    payload = {"ok": False, "tool_name": tool_name, "message": _bounded_text(message), "data": None}
    return ai_platform.AIMessage(
        role=ai_platform.MessageRole.TOOL,
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        tool_call_id=call_id,
    )


def _completed_tool_messages(
    tool_plan: tuple[ValidatedToolCall, ...], executed: _ExecutionBatch
) -> tuple[ai_platform.AIMessage, ...]:
    """Executed results plus a "skipped" result for every call after a failure.

    Providers require exactly one result per tool call of the replayed step.
    """
    if not executed.failure:
        return executed.tool_messages
    answered = {message.tool_call_id for message in executed.tool_messages}
    skipped = tuple(
        _tool_error_message(call.call_id, call.tool_name, "Not executed: an earlier action in this batch failed.")
        for call in tool_plan
        if call.call_id not in answered
    )
    return (*executed.tool_messages, *skipped)


def _rejected_call_messages(
    tool_calls: tuple[ai_platform.AIToolCall, ...], call_errors: Mapping[str, str]
) -> tuple[ai_platform.AIMessage, ...]:
    """One result per call of a batch that was rejected before execution."""
    messages = []
    for tool_call in tool_calls:
        error = call_errors.get(tool_call.call_id)
        text = (
            f"Not executed: {error} Fix the call and try again."
            if error
            else "Not executed: another call in the same batch was invalid; call it again if still needed."
        )
        messages.append(_tool_error_message(tool_call.call_id, tool_call.tool_name, text))
    return tuple(messages)


def _append_tool_round(
    history: tuple[ai_platform.AIMessage, ...],
    response: ai_platform.AIResponse,
    tool_messages: tuple[ai_platform.AIMessage, ...],
) -> tuple[ai_platform.AIMessage, ...]:
    assistant = ai_platform.AIMessage(
        role=ai_platform.MessageRole.ASSISTANT,
        content=response.content,
        tool_calls=response.tool_calls,
        metadata=response.metadata,
    )
    return (*history, assistant, *tool_messages)


def _initial_history(messages: tuple[ai_platform.AIMessage, ...]) -> tuple[ai_platform.AIMessage, ...]:
    return (ai_platform.AIMessage(role=ai_platform.MessageRole.SYSTEM, content=SYSTEM_INSTRUCTION), *messages)


def _provider_tool_schemas(allowed_tool_names: tuple[str, ...] | None) -> tuple[Mapping[str, Any], ...]:
    if allowed_tool_names is None:
        return admin_tools.list_provider_tool_schemas()
    for tool_name in allowed_tool_names:
        admin_tools.get_tool_definition(tool_name)
    return admin_tools.list_provider_tool_schemas(tuple(allowed_tool_names))


def _validate_tool_plan(
    tool_calls: tuple[ai_platform.AIToolCall, ...],
    allowed_tool_names: tuple[str, ...] | None,
    *,
    collect_errors: bool = False,
) -> Any:
    """Validate a provider tool batch.

    Default: return the validated plan or raise OrchestratorError on the first
    problem. ``collect_errors=True``: return ``(plan, {call_id: error})`` so
    per-call problems can be reported back to the model; structural problems
    (missing/duplicate/over-long call IDs) still raise because results could
    not be matched to calls.
    """
    if not tool_calls:
        return ((), {}) if collect_errors else ()
    allowed = None if allowed_tool_names is None else set(allowed_tool_names)
    seen_call_ids: set[str] = set()
    validated: list[ValidatedToolCall] = []
    errors: dict[str, str] = {}
    for tool_call in tool_calls:
        if not tool_call.call_id:
            raise OrchestratorError("Provider tool call is missing a call ID.")
        if len(tool_call.call_id) > MAX_TOOL_CALL_ID_CHARS:
            raise OrchestratorError("Provider tool call ID is too long.")
        if tool_call.call_id in seen_call_ids:
            raise OrchestratorError("Provider tool call IDs must be unique.")
        seen_call_ids.add(tool_call.call_id)
        try:
            validated.append(_validate_tool_call(tool_call, allowed))
        except OrchestratorError as exc:
            if not collect_errors:
                raise
            errors[tool_call.call_id] = str(exc)
    if collect_errors:
        return tuple(validated), errors
    return tuple(validated)


def _validate_tool_call(tool_call: ai_platform.AIToolCall, allowed: set[str] | None) -> ValidatedToolCall:
    if allowed is not None and tool_call.tool_name not in allowed:
        raise OrchestratorError(f"Tool is not allowed: {tool_call.tool_name}")
    try:
        definition = admin_tools.get_tool_definition(tool_call.tool_name)
        arguments = admin_tools.validate_tool_arguments(tool_call.tool_name, dict(tool_call.arguments))
        return ValidatedToolCall(
            call_id=tool_call.call_id,
            tool_name=tool_call.tool_name,
            arguments=arguments,
            risk=_risk_from_admin_tool(definition.risk),
        )
    except (admin_tools.AdminToolError, ValueError) as exc:
        raise OrchestratorError(str(exc)) from exc


def _validate_confirmation_policy(policy: ConfirmationPolicy) -> None:
    # Exact type only: ConfirmationPolicy is a value object, not a hook.
    if type(policy) is not ConfirmationPolicy:
        raise ValueError("confirmation_policy must be exactly a ConfirmationPolicy.")
    if type(policy.confirm_normal) is not bool:
        raise ValueError("confirm_normal must be boolean.")


def _requires_confirmation(policy: ConfirmationPolicy, risk: ai_platform.ToolRisk) -> bool:
    """Fixed safety semantics; never dispatches to an overridable method."""
    if risk is ai_platform.ToolRisk.DESTRUCTIVE:
        return True
    if risk is ai_platform.ToolRisk.NORMAL:
        return policy.confirm_normal is True
    if risk is ai_platform.ToolRisk.READ:
        return False
    # Unknown risk value: fail closed.
    return True


def _risk_from_admin_tool(risk: str) -> ai_platform.ToolRisk:
    if risk == "read":
        return ai_platform.ToolRisk.READ
    if risk == "normal":
        return ai_platform.ToolRisk.NORMAL
    if risk == "destructive":
        return ai_platform.ToolRisk.DESTRUCTIVE
    raise OrchestratorError("Unknown Admin Tool risk.")


def _batch_risk(tool_plan: tuple[ValidatedToolCall, ...]) -> ai_platform.ToolRisk:
    if any(call.risk is ai_platform.ToolRisk.DESTRUCTIVE for call in tool_plan):
        return ai_platform.ToolRisk.DESTRUCTIVE
    if any(call.risk is ai_platform.ToolRisk.NORMAL for call in tool_plan):
        return ai_platform.ToolRisk.NORMAL
    return ai_platform.ToolRisk.READ


def _serialize_tool_result(result: admin_tools.ToolResult) -> str:
    payload = {
        "ok": result.ok is True,
        "tool_name": result.tool_name,
        "message": result.message,
        "data": result.data,
    }
    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload["data"] = None
        payload["ok"] = False
        payload["message"] = "Tool result was not JSON-serializable."
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES:
        return serialized
    bounded = {
        "ok": False,
        "tool_name": result.tool_name,
        "message": "Tool result exceeded the orchestrator size limit.",
        "data": None,
    }
    return json.dumps(bounded, ensure_ascii=False, sort_keys=True)


def _bounded_compare_profile_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("compare profile_ids must be a tuple of non-empty strings.")
    limit = min(MAX_COMPARE_PROFILES, ai_platform.MAX_COMPARE_PROFILES)
    if not 2 <= len(value) <= limit:
        raise ValueError(f"compare requires between 2 and {limit} profiles.")
    if len(set(value)) != len(value):
        raise ValueError("compare profile_ids must be distinct.")
    return tuple(value)


def _coerce_allowed_tool_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("allowed_tool_names must be a sequence of tool names, not a string.")
    names = tuple(value)
    if not all(isinstance(name, str) and name for name in names):
        raise ValueError("allowed_tool_names must contain non-empty strings.")
    return names


def _json_freeze(value: Any) -> Any:
    """Recursively copy JSON data into new immutable containers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Tool arguments must not contain non-finite numbers.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Tool argument keys must be strings.")
            frozen[key] = _json_freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_json_freeze(item) for item in value)
    raise ValueError("Tool arguments must be JSON-compatible.")


def _json_thaw(value: Any) -> Any:
    """Fresh ordinary dict/list copy of frozen JSON data."""
    if isinstance(value, Mapping):
        return {key: _json_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_thaw(item) for item in value]
    return value


def _bounded_text(value: Any, limit: int = MAX_PUBLIC_MESSAGE_CHARS) -> str:
    """Bound public diagnostic text; never raises."""
    text = value if isinstance(value, str) else ""
    text = text.replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def _safe_exception_message(prefix: str, exc: Exception) -> str:
    return f"{prefix} {type(exc).__name__}"
