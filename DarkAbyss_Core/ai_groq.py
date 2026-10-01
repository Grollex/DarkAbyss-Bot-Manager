from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import ai_platform


GROQ_PROVIDER_ID = "groq"
GROQ_DISPLAY_NAME = "Groq"
GROQ_API_BASE = "https://api.groq.com/openai/v1"
GROQ_CHAT_COMPLETIONS_PATH = "/chat/completions"
GROQ_INITIAL_MODEL = "openai/gpt-oss-120b"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
ALLOWED_REASONING_EFFORT = {"low", "medium", "high"}
ALLOWED_OPTIONS = {"reasoning_effort", "max_completion_tokens", "temperature", "top_p"}


class GroqProviderError(ai_platform.AIPlatformError):
    """Contained Groq adapter failure."""


class GroqHTTPTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        ...


@dataclass(frozen=True)
class UrllibGroqHTTPTransport:
    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = _read_limited(response, max_response_bytes)
                return int(response.status), data
        except urllib.error.HTTPError as exc:
            data = _read_limited(exc, max_response_bytes)
            return int(exc.code), data
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GroqProviderError("Groq network request failed.") from exc


class GroqProvider:
    def __init__(
        self,
        credential_store: ai_platform.CredentialStore,
        *,
        transport: GroqHTTPTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlparse(GROQ_API_BASE)
        if parsed.scheme != "https":
            raise ValueError("Groq API endpoint must use HTTPS.")
        self._credential_store = credential_store
        self._api_base = GROQ_API_BASE
        self._transport = transport or UrllibGroqHTTPTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._metadata = ai_platform.ProviderMetadata(
            provider_id=GROQ_PROVIDER_ID,
            display_name=GROQ_DISPLAY_NAME,
            models=(
                ai_platform.ProviderModel(
                    model_id=GROQ_INITIAL_MODEL,
                    display_name="GPT-OSS 120B",
                    recommended_task_classes=(
                        ai_platform.TaskClass.ROUTINE,
                        ai_platform.TaskClass.PLANNER,
                    ),
                    supports_tool_calls=True,
                ),
            ),
            capabilities={
                "chat_completions": True,
                "tool_calls": True,
                "reasoning_effort": tuple(sorted(ALLOWED_REASONING_EFFORT)),
            },
        )

    @property
    def metadata(self) -> ai_platform.ProviderMetadata:
        return self._metadata

    def get_local_availability(
        self,
        *,
        credential_ref: str | None = None,
        credential_available: bool = False,
    ) -> ai_platform.Availability:
        if credential_ref is None:
            return ai_platform.Availability(
                ai_platform.AvailabilityState.CREDENTIAL_MISSING,
                "Groq credential is not configured.",
            )
        if not credential_available:
            return ai_platform.Availability(
                ai_platform.AvailabilityState.CREDENTIAL_MISSING,
                "Groq credential is missing on this device.",
            )
        return ai_platform.Availability(ai_platform.AvailabilityState.AVAILABLE, "Groq is locally configured.")

    async def test_connection(self, credential_ref: str | None = None) -> ai_platform.Availability:
        if credential_ref is None:
            return ai_platform.Availability(
                ai_platform.AvailabilityState.CREDENTIAL_MISSING,
                "Groq credential is not configured.",
            )
        try:
            request = build_groq_smoke_request()
            response = await self.generate(request, credential_ref)
        except GroqProviderError as exc:
            state = _availability_state_from_error(exc)
            return ai_platform.Availability(state, str(exc))
        if response.content.strip() == "KAIRO_GROQ_OK":
            return ai_platform.Availability(ai_platform.AvailabilityState.AVAILABLE, "Groq test connection succeeded.")
        return ai_platform.Availability(ai_platform.AvailabilityState.UNAVAILABLE, "Groq test response was unexpected.")

    async def generate(
        self,
        request: ai_platform.AIRequest,
        credential_ref: str | None = None,
    ) -> ai_platform.AIResponse:
        if credential_ref is None:
            raise GroqProviderError("Groq credential is not configured.")
        api_key = self._load_api_key(credential_ref)
        body = _map_request(request)
        url = urljoin(self._api_base + "/", GROQ_CHAT_COMPLETIONS_PATH.lstrip("/"))
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        status, response_bytes = await asyncio.to_thread(
            self._transport.post_json,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        if status < 200 or status >= 300:
            raise _error_for_status(status)
        return _parse_response(response_bytes)

    def _load_api_key(self, credential_ref: str) -> str:
        try:
            value = self._credential_store.read_secret(GROQ_PROVIDER_ID, credential_ref)
        except ai_platform.CredentialStoreError as exc:
            raise GroqProviderError("Groq credential is missing.") from exc
        api_key = value.strip()
        if not api_key:
            raise GroqProviderError("Groq credential is empty.")
        return api_key


def build_groq_smoke_request() -> ai_platform.AIRequest:
    return ai_platform.AIRequest(
        model_id=GROQ_INITIAL_MODEL,
        messages=(
            ai_platform.AIMessage(
                role=ai_platform.MessageRole.USER,
                content="Reply with exactly: KAIRO_GROQ_OK",
            ),
        ),
        options={"max_completion_tokens": 16},
    )


def _map_request(request: ai_platform.AIRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model_id,
        "messages": [_map_message(message) for message in request.messages],
    }
    body.update(_map_options(request.options))
    if request.tools:
        body["tools"] = [_map_tool(tool) for tool in request.tools]
        if request.model_id == GROQ_INITIAL_MODEL:
            body["include_reasoning"] = False
    return body


def _map_message(message: ai_platform.AIMessage) -> dict[str, Any]:
    mapped = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.name:
        mapped["name"] = message.name
    if message.tool_calls:
        mapped["tool_calls"] = [_map_outgoing_tool_call(tool_call) for tool_call in message.tool_calls]
    if message.tool_call_id:
        mapped["tool_call_id"] = message.tool_call_id
    return mapped


def _map_outgoing_tool_call(tool_call: ai_platform.AIToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {
            "name": tool_call.tool_name,
            "arguments": json.dumps(dict(tool_call.arguments)),
        },
    }


def _map_options(options: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in options.items():
        if key not in ALLOWED_OPTIONS:
            raise GroqProviderError(f"Unsupported Groq option: {key}")
        if key == "reasoning_effort" and value not in ALLOWED_REASONING_EFFORT:
            raise GroqProviderError("Unsupported Groq reasoning_effort.")
        if key == "max_completion_tokens" and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 8192
        ):
            raise GroqProviderError("Unsupported Groq max_completion_tokens.")
        if key == "temperature" and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 2
        ):
            raise GroqProviderError(f"Unsupported Groq {key}.")
        if key == "top_p" and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1
        ):
            raise GroqProviderError(f"Unsupported Groq {key}.")
        mapped[key] = value
    return mapped


def _map_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") == "function" and "function" in tool:
        return dict(tool)
    name = tool.get("name")
    description = tool.get("description", "")
    parameters = tool.get("parameters") or tool.get("arguments")
    if not isinstance(name, str) or not name:
        raise GroqProviderError("Tool schema requires a name.")
    if not isinstance(description, str):
        raise GroqProviderError("Tool schema description must be a string.")
    if not isinstance(parameters, dict):
        raise GroqProviderError("Tool schema parameters must be an object.")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _parse_response(response_bytes: bytes) -> ai_platform.AIResponse:
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GroqProviderError("Groq response was malformed.") from exc
    try:
        choice = payload["choices"][0]
        message = choice.get("message", {})
    except (KeyError, IndexError, TypeError) as exc:
        raise GroqProviderError("Groq response was malformed.") from exc
    content = message.get("content") or ""
    if not isinstance(content, str):
        raise GroqProviderError("Groq response content was malformed.")
    tool_calls = tuple(_parse_tool_call(item) for item in message.get("tool_calls") or ())
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        finish_reason = None
    return ai_platform.AIResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        metadata={"provider": GROQ_PROVIDER_ID, "model": payload.get("model")},
    )


def _parse_tool_call(item: dict[str, Any]) -> ai_platform.AIToolCall:
    if not isinstance(item, dict):
        raise GroqProviderError("Groq tool call was malformed.")
    function = item.get("function")
    if not isinstance(function, dict):
        raise GroqProviderError("Groq tool call was malformed.")
    tool_name = function.get("name")
    raw_arguments = function.get("arguments", "{}")
    if not isinstance(tool_name, str) or not tool_name:
        raise GroqProviderError("Groq tool call name was malformed.")
    if not isinstance(raw_arguments, str):
        raise GroqProviderError("Groq tool arguments were malformed.")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise GroqProviderError("Groq tool arguments were malformed.") from exc
    if not isinstance(arguments, dict):
        raise GroqProviderError("Groq tool arguments must be an object.")
    call_id = item.get("id")
    if call_id is not None and not isinstance(call_id, str):
        call_id = None
    return ai_platform.AIToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments)


def _error_for_status(status: int) -> GroqProviderError:
    if status in {401, 403}:
        return GroqProviderError("Groq credential is invalid.")
    if status == 429:
        return GroqProviderError("Groq rate limit or quota was reached.")
    if 400 <= status < 500:
        return GroqProviderError("Groq rejected the request.")
    if status >= 500:
        return GroqProviderError("Groq service is unavailable.")
    return GroqProviderError("Groq request failed.")


def _availability_state_from_error(error: GroqProviderError) -> ai_platform.AvailabilityState:
    message = str(error).lower()
    if "credential is invalid" in message:
        return ai_platform.AvailabilityState.CREDENTIAL_INVALID
    if "credential" in message:
        return ai_platform.AvailabilityState.CREDENTIAL_MISSING
    if "rate limit" in message or "quota" in message:
        return ai_platform.AvailabilityState.UNAVAILABLE
    return ai_platform.AvailabilityState.UNAVAILABLE


def _read_limited(response: Any, max_response_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if content_length is not None:
        try:
            if int(content_length) > max_response_bytes:
                raise GroqProviderError("Groq response was too large.")
        except ValueError:
            pass
    data = response.read(max_response_bytes + 1)
    if len(data) > max_response_bytes:
        raise GroqProviderError("Groq response was too large.")
    return data
