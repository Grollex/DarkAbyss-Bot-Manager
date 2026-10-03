"""Catalog of AI provider adapters (the extension point for new providers).

Every provider is one adapter module plus one ``ProviderSpec`` entry below.
The Manager (connection editor, model lists, logos), the provider registry of
the bots and the usage display all read this catalog, so nothing else needs to
change when a provider is added.

Adapter contract (see ai_groq.GroqProvider / ai_gemini.GeminiProvider):

    class SomeProvider:
        def __init__(self, credential_store, *, usage_recorder=None,
                     retry_delays=(), ...): ...
        metadata -> ai_platform.ProviderMetadata      # models with display names
        get_local_availability(credential_ref=..., credential_available=...)
        async test_connection(credential_ref) -> ai_platform.Availability
        async generate(request, credential_ref) -> ai_platform.AIResponse

    module constants: DEFAULT_RETRY_DELAYS

* Keys are read only through the given CredentialStore with the
  ``credential_ref`` of the connection being used (never from anywhere else).
* ``usage_recorder`` receives one ``ai_usage.UsageEvent`` per received HTTP
  response (provider-reported numbers only).
* Options a provider accepts are listed in ``ProviderSpec.options``; the
  adapter must reject anything else.

Only Groq and Gemini exist today.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import ai_platform


class UnknownProviderError(ai_platform.AIPlatformError):
    """The provider ID is not in the catalog."""


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    logo: str  # short text shown as the provider logo in the Manager
    module: str  # adapter module, imported lazily
    class_name: str
    default_model: str
    key_placeholder: str
    key_help: str
    options: tuple[str, ...] = ("reasoning_effort",)
    reasoning_levels: tuple[str, ...] = ("low", "medium", "high")


PROVIDERS: dict[str, ProviderSpec] = {
    spec.provider_id: spec
    for spec in (
        ProviderSpec(
            provider_id="groq",
            display_name="Groq",
            logo="groq",
            module="ai_groq",
            class_name="GroqProvider",
            default_model="openai/gpt-oss-120b",
            key_placeholder="Paste Groq API key (gsk_...)",
            key_help="console.groq.com -> API Keys -> Create API Key.",
        ),
        ProviderSpec(
            provider_id="gemini",
            display_name="Gemini",
            logo="✦",
            module="ai_gemini",
            class_name="GeminiProvider",
            default_model="gemini-3.8-flash",
            key_placeholder="Paste Gemini API key",
            key_help="aistudio.google.com -> Get API key.",
        ),
    )
}


def provider_ids() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def get_spec(provider_id: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError) as exc:
        raise UnknownProviderError(f"Unknown AI provider: {provider_id!r}") from exc


def adapter_module(provider_id: str) -> Any:
    spec = get_spec(provider_id)
    try:
        return importlib.import_module(spec.module)
    except Exception as exc:
        raise ai_platform.AIPlatformError(f"{spec.display_name} adapter is unavailable.") from exc


def create_provider(
    provider_id: str,
    credential_store: ai_platform.CredentialStore,
    *,
    usage_recorder: Callable[[Any], None] | None = None,
    retry: bool = False,
) -> Any:
    """Adapter instance bound to ``credential_store``.

    ``retry`` enables the adapter's transient-failure retries (bot requests);
    Manager Test Connection calls fail fast.
    """
    if not isinstance(credential_store, ai_platform.CredentialStore):
        raise ValueError("A CredentialStore is required.")
    spec = get_spec(provider_id)
    module = adapter_module(provider_id)
    kwargs: dict[str, Any] = {"usage_recorder": usage_recorder}
    if retry:
        kwargs["retry_delays"] = getattr(module, "DEFAULT_RETRY_DELAYS", ())
    return getattr(module, spec.class_name)(credential_store, **kwargs)


_MODEL_CACHE: dict[str, tuple[tuple[str, str], ...]] = {}


def model_choices(provider_id: str) -> tuple[tuple[str, str], ...]:
    """(model_id, display name) pairs from the adapter metadata (no network)."""
    if provider_id not in _MODEL_CACHE:
        # Metadata needs an instance; this store is never read (no key access).
        probe = create_provider(provider_id, ai_platform.CredentialStore(Path(__file__).resolve().parent / ".unused-credentials"))
        _MODEL_CACHE[provider_id] = tuple((model.model_id, model.display_name) for model in probe.metadata.models)
    return _MODEL_CACHE[provider_id]


def model_display_name(provider_id: str, model_id: str) -> str:
    try:
        return dict(model_choices(provider_id)).get(model_id, model_id)
    except ai_platform.AIPlatformError:
        return model_id
