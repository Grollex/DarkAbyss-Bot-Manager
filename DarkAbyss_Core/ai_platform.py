from __future__ import annotations

import os
import re
import secrets
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

import app_paths


SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_CREDENTIALS_ROOT = app_paths.DATA_ROOT / "secrets" / "ai"


class AIPlatformError(Exception):
    """Base error for contained optional-AI platform failures."""


class CredentialStoreError(AIPlatformError):
    """Raised when device-local AI credential storage cannot be used safely."""


class TaskClass(str, Enum):
    DIRECT = "DIRECT"
    ROUTINE = "ROUTINE"
    PLANNER = "PLANNER"
    CREATIVE = "CREATIVE"


class AvailabilityState(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PROVIDER_MISSING = "PROVIDER_MISSING"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"


class ToolRisk(str, Enum):
    READ = "READ"
    NORMAL = "NORMAL"
    DESTRUCTIVE = "DESTRUCTIVE"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Availability:
    state: AvailabilityState
    message: str

    @property
    def ok(self) -> bool:
        return self.state is AvailabilityState.AVAILABLE


@dataclass(frozen=True)
class ProviderModel:
    model_id: str
    display_name: str
    recommended_task_classes: tuple[TaskClass, ...] = ()
    supports_tool_calls: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.model_id, "model_id")


@dataclass(frozen=True)
class ProviderMetadata:
    provider_id: str
    display_name: str
    models: tuple[ProviderModel, ...] = ()
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_identifier(self.provider_id, "provider_id")
        if not _is_json_compatible(self.capabilities):
            raise ValueError("capabilities must be JSON-compatible.")
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))


@dataclass(frozen=True)
class AIMessage:
    role: MessageRole | str
    content: str = field(repr=False)
    name: str | None = None
    tool_calls: tuple["AIToolCall", ...] = ()
    tool_call_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _coerce_message_role(self.role))
        if not isinstance(self.content, str):
            raise ValueError("content must be a string.")
        if self.name is not None:
            _validate_identifier(self.name, "name")
        tool_calls = tuple(self.tool_calls)
        if not all(isinstance(tool_call, AIToolCall) for tool_call in tool_calls):
            raise ValueError("tool_calls must contain AIToolCall objects.")
        if self.tool_call_id is not None:
            _validate_identifier(self.tool_call_id, "tool_call_id")
        if self.role is MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id.")
        if self.role is not MessageRole.TOOL and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool messages.")
        if self.role is not MessageRole.ASSISTANT and tool_calls:
            raise ValueError("tool_calls are only valid for assistant messages.")
        if self.role is MessageRole.ASSISTANT:
            for tool_call in tool_calls:
                if not tool_call.call_id:
                    raise ValueError("assistant tool_calls require call_id.")
        if not _is_json_compatible(self.metadata):
            raise ValueError("metadata must be JSON-compatible.")
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "name": self.name,
            "tool_calls": [tool_call.public_dict() for tool_call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AIToolCall:
    tool_name: str
    arguments: Mapping[str, Any]
    call_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.tool_name, "tool_name")
        if self.call_id is not None:
            _validate_identifier(self.call_id, "call_id")
        if not _is_json_compatible(self.arguments):
            raise ValueError("tool-call arguments must be JSON-compatible.")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class AIRequest:
    model_id: str
    messages: tuple[AIMessage, ...]
    tools: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty string.")
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("messages must not be empty.")
        if not all(isinstance(message, AIMessage) for message in messages):
            raise ValueError("messages must contain AIMessage objects.")
        tools = tuple(dict(tool) for tool in self.tools)
        if not _is_json_compatible(list(tools)):
            raise ValueError("tools must be JSON-compatible.")
        if not _is_json_compatible(self.options):
            raise ValueError("options must be JSON-compatible.")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "messages": [message.public_dict() for message in self.messages],
            "tools": [dict(tool) for tool in self.tools],
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class AIResponse:
    content: str = field(default="", repr=False)
    tool_calls: tuple[AIToolCall, ...] = ()
    finish_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("content must be a string.")
        tool_calls = tuple(self.tool_calls)
        if not all(isinstance(tool_call, AIToolCall) for tool_call in tool_calls):
            raise ValueError("tool_calls must contain AIToolCall objects.")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise ValueError("finish_reason must be a string.")
        if not _is_json_compatible(self.metadata):
            raise ValueError("metadata must be JSON-compatible.")
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [tool_call.public_dict() for tool_call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "metadata": dict(self.metadata),
        }


class AIProvider(Protocol):
    @property
    def metadata(self) -> ProviderMetadata:
        ...

    def get_local_availability(
        self,
        *,
        credential_ref: str | None = None,
        credential_available: bool = False,
    ) -> Availability:
        ...

    async def test_connection(self, credential_ref: str | None = None) -> Availability:
        ...

    async def generate(self, request: AIRequest, credential_ref: str | None = None) -> AIResponse:
        ...


@dataclass(frozen=True)
class AIProfile:
    profile_id: str
    provider_id: str
    model_id: str
    credential_ref: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.profile_id, "profile_id")
        _validate_identifier(self.provider_id, "provider_id")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty string.")
        if self.credential_ref is not None:
            _validate_identifier(self.credential_ref, "credential_ref")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean.")
        if not _is_json_compatible(self.options):
            raise ValueError("options must be JSON-compatible.")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "credential_ref": self.credential_ref,
            "options": dict(self.options),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class CredentialReference:
    credential_ref: str
    provider_id: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        _validate_safe_component(self.credential_ref, "credential_ref")
        _validate_safe_component(self.provider_id, "provider_id")

    def public_dict(self) -> dict[str, str | None]:
        return {
            "credential_ref": self.credential_ref,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
        }


class CredentialStore:
    def __init__(self, root: Path | str = DEFAULT_CREDENTIALS_ROOT) -> None:
        self.root = Path(root).expanduser().resolve()

    def path_for(self, provider_id: str, credential_ref: str) -> Path:
        _validate_safe_component(provider_id, "provider_id")
        _validate_safe_component(credential_ref, "credential_ref")
        path = (self.root / provider_id / f"{credential_ref}.secret").resolve()
        if not path.is_relative_to(self.root):
            raise CredentialStoreError("Credential path escapes credential store root.")
        return path

    def exists(self, provider_id: str, credential_ref: str) -> bool:
        return self.path_for(provider_id, credential_ref).is_file()

    def write_secret(self, provider_id: str, credential_ref: str, secret_value: str) -> None:
        if not isinstance(secret_value, str) or not secret_value:
            raise CredentialStoreError("Secret value must be a non-empty string.")
        path = self.path_for(provider_id, credential_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            temp_path.write_text(secret_value, encoding="utf-8")
            os.replace(temp_path, path)
        except OSError as exc:
            raise CredentialStoreError("Failed to write AI credential.") from exc
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    def read_secret(self, provider_id: str, credential_ref: str) -> str:
        path = self.path_for(provider_id, credential_ref)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CredentialStoreError("AI credential is missing.") from exc
        except OSError as exc:
            raise CredentialStoreError("Failed to read AI credential.") from exc

    def delete_secret(self, provider_id: str, credential_ref: str) -> None:
        path = self.path_for(provider_id, credential_ref)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("Failed to delete AI credential.") from exc


@dataclass(frozen=True)
class AISettings:
    schema_version: int = 1
    profiles: tuple[AIProfile, ...] = ()
    routing: RoutingConfig = field(default_factory=lambda: RoutingConfig())

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profiles": [profile.public_dict() for profile in self.profiles],
            "routing": {
                "routine_profile_id": self.routing.routine_profile_id,
                "planner_profile_id": self.routing.planner_profile_id,
                "creative_profile_id": self.routing.creative_profile_id,
            },
        }


class AISettingsStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or (app_paths.CONFIG_DIR / "ai.json")).resolve()

    def load(self) -> AISettings:
        if not self.path.exists():
            return AISettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings must be an object")
            if raw.get("schema_version") != 1:
                raise ValueError("unsupported schema_version")
            profiles = tuple(_profile_from_json(item) for item in raw.get("profiles", []))
            routing_raw = raw.get("routing", {})
            if not isinstance(routing_raw, dict):
                raise ValueError("routing must be an object")
            routing = RoutingConfig(
                routine_profile_id=_optional_identifier(routing_raw.get("routine_profile_id"), "routine_profile_id"),
                planner_profile_id=_optional_identifier(routing_raw.get("planner_profile_id"), "planner_profile_id"),
                creative_profile_id=_optional_identifier(routing_raw.get("creative_profile_id"), "creative_profile_id"),
            )
            return AISettings(schema_version=1, profiles=profiles, routing=routing)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise AIPlatformError("AI settings are invalid.") from exc

    def save(self, settings: AISettings) -> None:
        data = settings.public_dict()
        serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        try:
            temp_path.write_text(serialized, encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise AIPlatformError("Failed to write AI settings.") from exc
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass


def default_groq_settings() -> AISettings:
    profile = AIProfile(
        profile_id="groq-default",
        provider_id="groq",
        model_id="openai/gpt-oss-120b",
        credential_ref="groq-default",
        options={"reasoning_effort": "medium"},
    )
    return AISettings(
        profiles=(profile,),
        routing=RoutingConfig(
            routine_profile_id="groq-default",
            planner_profile_id="groq-default",
            creative_profile_id="groq-default",
        ),
    )


@dataclass(frozen=True)
class RoutingConfig:
    routine_profile_id: str | None = None
    planner_profile_id: str | None = None
    creative_profile_id: str | None = None

    def profile_for(self, task_class: TaskClass) -> str | None:
        selected = _coerce_task_class(task_class)
        if selected is TaskClass.DIRECT:
            return None
        if selected is TaskClass.ROUTINE:
            return self.routine_profile_id
        if selected is TaskClass.PLANNER:
            return self.planner_profile_id
        if selected is TaskClass.CREATIVE:
            return self.creative_profile_id
        raise AIPlatformError(f"Unsupported task class: {task_class}")


@dataclass(frozen=True)
class RoutingDecision:
    task_class: TaskClass
    profile: AIProfile | None
    provider: ProviderMetadata | None
    availability: Availability
    manual_override: bool = False

    @property
    def uses_ai(self) -> bool:
        return self.task_class is not TaskClass.DIRECT and self.profile is not None


@dataclass(frozen=True)
class ComparePlanRequest:
    profile_ids: tuple[str, ...]
    tool_risk: ToolRisk

    def __post_init__(self) -> None:
        if len(self.profile_ids) < 2:
            raise ValueError("profile_ids must contain at least two profiles.")
        if len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError("profile_ids must be distinct.")
        for profile_id in self.profile_ids:
            _validate_identifier(profile_id, "profile_id")


class ProviderRegistry:
    def __init__(self, providers: Mapping[str, AIProvider] | None = None) -> None:
        self._providers: dict[str, AIProvider] = {}
        for provider in (providers or {}).values():
            self.register(provider)

    def register(self, provider: AIProvider) -> None:
        provider_id = provider.metadata.provider_id
        _validate_identifier(provider_id, "provider_id")
        if provider_id in self._providers:
            raise AIPlatformError(f"AI provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> AIProvider | None:
        _validate_identifier(provider_id, "provider_id")
        return self._providers.get(provider_id)

    def metadata(self) -> tuple[ProviderMetadata, ...]:
        return tuple(provider.metadata for provider in self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)


class LazyProviderRegistry(ProviderRegistry):
    def __init__(self) -> None:
        super().__init__()
        self._factories: dict[str, Any] = {}
        self._failed_provider_ids: set[str] = set()

    def register_factory(self, provider_id: str, factory: Any) -> None:
        _validate_identifier(provider_id, "provider_id")
        if not callable(factory):
            raise AIPlatformError("Provider factory must be callable.")
        if provider_id in self._providers or provider_id in self._factories:
            raise AIPlatformError(f"AI provider already registered: {provider_id}")
        self._factories[provider_id] = factory

    def get(self, provider_id: str) -> AIProvider | None:
        _validate_identifier(provider_id, "provider_id")
        if provider_id in self._failed_provider_ids:
            return None
        provider = self._providers.get(provider_id)
        if provider is not None:
            return provider
        factory = self._factories.get(provider_id)
        if factory is None:
            return None
        try:
            provider = factory()
            if provider.metadata.provider_id != provider_id:
                self._failed_provider_ids.add(provider_id)
                return None
            self.register(provider)
        except Exception:
            self._failed_provider_ids.add(provider_id)
            return None
        self._factories.pop(provider_id, None)
        return provider


class AIProfileStore:
    def __init__(self, profiles: Mapping[str, AIProfile] | None = None) -> None:
        self._profiles: dict[str, AIProfile] = {}
        for profile in (profiles or {}).values():
            self.add(profile)

    def add(self, profile: AIProfile) -> None:
        if profile.profile_id in self._profiles:
            raise AIPlatformError(f"AI profile already exists: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> AIProfile | None:
        _validate_identifier(profile_id, "profile_id")
        return self._profiles.get(profile_id)

    def disabled_copy(self, profile_id: str) -> AIProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise AIPlatformError(f"AI profile not found: {profile_id}")
        disabled = replace(profile, enabled=False)
        self._profiles[profile_id] = disabled
        return disabled

    def public_profiles(self) -> tuple[dict[str, Any], ...]:
        return tuple(profile.public_dict() for profile in self._profiles.values())


def route_request(
    *,
    task_class: TaskClass | str,
    routing: RoutingConfig,
    profiles: AIProfileStore,
    providers: ProviderRegistry,
    manual_profile_id: str | None = None,
    credential_store: CredentialStore | None = None,
) -> RoutingDecision:
    selected_task_class = _coerce_task_class(task_class)
    if selected_task_class is TaskClass.DIRECT:
        return RoutingDecision(
            task_class=selected_task_class,
            profile=None,
            provider=None,
            availability=Availability(AvailabilityState.AVAILABLE, "DIRECT task does not require AI."),
            manual_override=manual_profile_id is not None,
        )

    selected_profile_id = manual_profile_id or routing.profile_for(selected_task_class)
    manual_override = manual_profile_id is not None
    if selected_profile_id is None:
        return RoutingDecision(
            task_class=selected_task_class,
            profile=None,
            provider=None,
            availability=Availability(AvailabilityState.NOT_CONFIGURED, "No AI profile configured for task class."),
            manual_override=manual_override,
        )

    profile = profiles.get(selected_profile_id)
    if profile is None:
        return RoutingDecision(
            task_class=selected_task_class,
            profile=None,
            provider=None,
            availability=Availability(AvailabilityState.NOT_CONFIGURED, "Selected AI profile does not exist."),
            manual_override=manual_override,
        )

    if not profile.enabled:
        return RoutingDecision(
            task_class=selected_task_class,
            profile=profile,
            provider=None,
            availability=Availability(AvailabilityState.DISABLED, "Selected AI profile is disabled."),
            manual_override=manual_override,
        )

    try:
        provider = providers.get(profile.provider_id)
    except Exception:
        provider = None
    if provider is None:
        return RoutingDecision(
            task_class=selected_task_class,
            profile=profile,
            provider=None,
            availability=Availability(AvailabilityState.PROVIDER_MISSING, "Selected AI provider is not registered."),
            manual_override=manual_override,
        )

    credential_available = False
    if profile.credential_ref is not None:
        if credential_store is None or not credential_store.exists(profile.provider_id, profile.credential_ref):
            return RoutingDecision(
                task_class=selected_task_class,
                profile=profile,
                provider=_safe_provider_metadata(provider),
                availability=Availability(AvailabilityState.CREDENTIAL_MISSING, "Selected AI credential is missing."),
                manual_override=manual_override,
            )
        credential_available = True

    provider_metadata = _safe_provider_metadata(provider)
    try:
        availability = provider.get_local_availability(
            credential_ref=profile.credential_ref,
            credential_available=credential_available,
        )
    except Exception:
        availability = Availability(AvailabilityState.UNAVAILABLE, "Selected AI provider is unavailable.")
    return RoutingDecision(
        task_class=selected_task_class,
        profile=profile,
        provider=provider_metadata,
        availability=availability,
        manual_override=manual_override,
    )


def _safe_provider_metadata(provider: AIProvider) -> ProviderMetadata | None:
    try:
        return provider.metadata
    except Exception:
        return None


def _coerce_task_class(task_class: TaskClass | str) -> TaskClass:
    if isinstance(task_class, TaskClass):
        return task_class
    try:
        return TaskClass(str(task_class))
    except ValueError as exc:
        raise AIPlatformError(f"Unsupported task class: {task_class}") from exc


def _coerce_message_role(role: MessageRole | str) -> MessageRole:
    if isinstance(role, MessageRole):
        return role
    try:
        return MessageRole(str(role))
    except ValueError as exc:
        raise ValueError(f"Unsupported message role: {role}") from exc


def _profile_from_json(raw: Any) -> AIProfile:
    if not isinstance(raw, dict):
        raise ValueError("profile must be an object")
    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("profile options must be an object")
    return AIProfile(
        profile_id=_required_identifier(raw.get("profile_id"), "profile_id"),
        provider_id=_required_identifier(raw.get("provider_id"), "provider_id"),
        model_id=_required_string(raw.get("model_id"), "model_id"),
        credential_ref=_optional_identifier(raw.get("credential_ref"), "credential_ref"),
        options=options,
        enabled=raw.get("enabled", True),
    )


def _required_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    _validate_identifier(value, field_name)
    return value


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, field_name)


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must not contain whitespace.")


def _validate_safe_component(value: str, field_name: str) -> None:
    _validate_identifier(value, field_name)
    if value in {".", ".."} or not SAFE_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe path component.")
    if Path(value).is_absolute() or Path(value).name != value or ":" in value:
        raise ValueError(f"{field_name} must be a safe path component.")


def _is_json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_compatible(item) for key, item in value.items())
    return False
