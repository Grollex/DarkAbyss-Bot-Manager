from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urljoin, urlparse

import ai_platform


GEMINI_PROVIDER_ID = "gemini"
GEMINI_DISPLAY_NAME = "Google Gemini"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_LITE_MODEL = "gemini-3.5-flash-lite"
GEMINI_USER_AGENT = "DarkAbyssBotManager/AI-2C"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OUTPUT_TOKENS_LIMIT = 8192
ALLOWED_REASONING_EFFORT = {"low", "medium", "high"}
ALLOWED_OPTIONS = {"reasoning_effort", "max_output_tokens"}
# Same retry contract as the Groq adapter: off by default, enabled by the bot's
# provider registry; a RetryInfo.retryDelay hint is honoured up to the cap.
DEFAULT_RETRY_DELAYS = (3.0, 8.0)
MAX_RETRY_WAIT_SECONDS = 30.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class GeminiProviderError(ai_platform.AIPlatformError):
    """Contained Gemini adapter failure."""


class GeminiNetworkError(GeminiProviderError):
    """Network/timeout failure before any HTTP status was received (retryable)."""


class GeminiHTTPTransport(Protocol):
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
class UrllibGeminiHTTPTransport:
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
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = _read_limited(response, max_response_bytes)
                return int(response.status), data
        except urllib.error.HTTPError as exc:
            data = _read_limited(exc, max_response_bytes)
            return int(exc.code), data
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GeminiNetworkError("Gemini network request failed.") from exc


class GeminiProvider:
    def __init__(
        self,
        credential_store: ai_platform.CredentialStore,
        *,
        transport: GeminiHTTPTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        retry_delays: tuple[float, ...] = (),
        sleep: Any = None,
    ) -> None:
        parsed = urlparse(GEMINI_API_BASE)
        if parsed.scheme != "https":
            raise ValueError("Gemini API endpoint must use HTTPS.")
        self._credential_store = credential_store
        self._api_base = GEMINI_API_BASE
        self._transport = transport or UrllibGeminiHTTPTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._retry_delays = tuple(float(delay) for delay in retry_delays)
        self._sleep = sleep or asyncio.sleep
        self._metadata = ai_platform.ProviderMetadata(
            provider_id=GEMINI_PROVIDER_ID,
            display_name=GEMINI_DISPLAY_NAME,
            models=(
                ai_platform.ProviderModel(
                    model_id=GEMINI_DEFAULT_MODEL,
                    display_name="Gemini 3.8 Flash",
                    recommended_task_classes=(ai_platform.TaskClass.ROUTINE, ai_platform.TaskClass.PLANNER),
                    supports_tool_calls=True,
                ),
                ai_platform.ProviderModel(
                    model_id=GEMINI_LITE_MODEL,
                    display_name="Gemini 3.5 Flash Lite",
                    recommended_task_classes=(ai_platform.TaskClass.ROUTINE,),
                    supports_tool_calls=True,
                ),
            ),
            capabilities={
                "generate_content": True,
                "function_calling": True,
                "thinking_level": tuple(sorted(ALLOWED_REASONING_EFFORT)),
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
                "Gemini credential is not configured.",
            )
        if not credential_available:
            return ai_platform.Availability(
                ai_platform.AvailabilityState.CREDENTIAL_MISSING,
                "Gemini credential is missing on this device.",
            )
        return ai_platform.Availability(ai_platform.AvailabilityState.AVAILABLE, "Gemini is locally configured.")

    async def test_connection(self, credential_ref: str | None = None) -> ai_platform.Availability:
        if credential_ref is None:
            return ai_platform.Availability(
                ai_platform.AvailabilityState.CREDENTIAL_MISSING,
                "Gemini credential is not configured.",
            )
        try:
            response = await self.generate(build_gemini_smoke_request(), credential_ref)
        except GeminiProviderError as exc:
            return ai_platform.Availability(_availability_state_from_error(exc), str(exc))
        if response.content.strip() == "KAIRO_GEMINI_OK":
            return ai_platform.Availability(ai_platform.AvailabilityState.AVAILABLE, "Gemini test connection succeeded.")
        return ai_platform.Availability(ai_platform.AvailabilityState.UNAVAILABLE, "Gemini test response was unexpected.")

    async def generate(
        self,
        request: ai_platform.AIRequest,
        credential_ref: str | None = None,
    ) -> ai_platform.AIResponse:
        if credential_ref is None:
            raise GeminiProviderError("Gemini credential is not configured.")
        api_key = self._load_api_key(credential_ref)
        body = _map_request(request)
        url = _generate_url(self._api_base, request.model_id)
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": GEMINI_USER_AGENT,
        }
        attempt = 0
        while True:
            try:
                try:
                    status, response_bytes = await asyncio.to_thread(
                        self._transport.post_json,
                        url=url,
                        headers=headers,
                        body=body,
                        timeout_seconds=self._timeout_seconds,
                        max_response_bytes=self._max_response_bytes,
                    )
                except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                    raise GeminiNetworkError("Gemini network request failed.") from exc
            except GeminiNetworkError:
                if attempt >= len(self._retry_delays):
                    raise
                await self._sleep(self._retry_delays[attempt])
                attempt += 1
                continue
            if status in RETRYABLE_STATUSES and attempt < len(self._retry_delays):
                wait = _retry_wait_seconds(response_bytes, self._retry_delays[attempt])
                if wait is not None:
                    await self._sleep(wait)
                    attempt += 1
                    continue
            break
        if status < 200 or status >= 300:
            raise _error_for_status(status, response_bytes)
        return _parse_response(response_bytes, model_id=request.model_id)

    def _load_api_key(self, credential_ref: str) -> str:
        try:
            value = self._credential_store.read_secret(GEMINI_PROVIDER_ID, credential_ref)
        except ai_platform.CredentialStoreError as exc:
            raise GeminiProviderError("Gemini credential is missing.") from exc
        api_key = value.strip()
        if not api_key:
            raise GeminiProviderError("Gemini credential is empty.")
        return api_key


def build_gemini_smoke_request() -> ai_platform.AIRequest:
    return ai_platform.AIRequest(
        model_id=GEMINI_DEFAULT_MODEL,
        messages=(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content="Reply with exactly: KAIRO_GEMINI_OK"),),
        options={"reasoning_effort": "low"},
    )


def _generate_url(api_base: str, model_id: str) -> str:
    return urljoin(api_base + "/", f"models/{quote(model_id, safe='')}:generateContent")


def _map_request(request: ai_platform.AIRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contents": _map_contents(request),
        "generationConfig": _map_generation_config(request.options),
    }
    system_parts = [{"text": message.content} for message in request.messages if message.role is ai_platform.MessageRole.SYSTEM]
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    if request.tools:
        body["tools"] = [{"functionDeclarations": [_map_tool(tool) for tool in request.tools]}]
    return body


def _map_contents(request: ai_platform.AIRequest) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    index = 0
    messages = request.messages
    current_turn_start = _current_turn_start(messages)
    pending_calls: tuple[ai_platform.AIToolCall, ...] = ()
    while index < len(messages):
        message = messages[index]
        if message.role is ai_platform.MessageRole.SYSTEM:
            index += 1
            continue
        if message.role is ai_platform.MessageRole.USER:
            contents.append({"role": "user", "parts": [{"text": message.content}]})
            pending_calls = ()
            index += 1
        elif message.role is ai_platform.MessageRole.ASSISTANT:
            parts: list[dict[str, Any]] = []
            visible_parts = _gemini_visible_text_parts(message.metadata)
            if message.content or visible_parts is not None:
                parts.extend(_map_assistant_text_parts(message))
            if _requires_tool_signature(request.model_id) and message.tool_calls and index >= current_turn_start:
                first_signature = _gemini_thought_signature(message.tool_calls[0].metadata)
                if not first_signature:
                    raise GeminiProviderError("Gemini 3 function call continuation requires first thoughtSignature.")
            for tool_call in message.tool_calls:
                parts.append(_map_function_call_part(tool_call))
            if parts:
                contents.append({"role": "model", "parts": parts})
            pending_calls = tuple(message.tool_calls)
            index += 1
        elif message.role is ai_platform.MessageRole.TOOL:
            if not pending_calls:
                raise GeminiProviderError("Gemini tool response has no matching function call step.")
            tool_messages: dict[str, ai_platform.AIMessage] = {}
            while index < len(messages) and messages[index].role is ai_platform.MessageRole.TOOL:
                tool_message = messages[index]
                if not tool_message.tool_call_id:
                    raise GeminiProviderError("Gemini tool response requires tool_call_id.")
                if tool_message.tool_call_id in tool_messages:
                    raise GeminiProviderError("Gemini tool response duplicates a function call result.")
                tool_messages[tool_message.tool_call_id] = tool_message
                index += 1
            calls_by_id = _calls_by_id_for_step(pending_calls)
            missing = [call.call_id for call in pending_calls if call.call_id not in tool_messages]
            unknown = [call_id for call_id in tool_messages if call_id not in calls_by_id]
            if missing:
                raise GeminiProviderError("Gemini tool response set is incomplete.")
            if unknown:
                raise GeminiProviderError("Gemini tool response has no matching function call.")
            response_parts = [
                _map_function_response_part(tool_messages[call.call_id], call)
                for call in pending_calls
                if call.call_id is not None
            ]
            contents.append({"role": "user", "parts": response_parts})
            pending_calls = ()
    return contents


def _current_turn_start(messages: tuple[ai_platform.AIMessage, ...]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role is ai_platform.MessageRole.USER:
            return index
    return 0


def _calls_by_id_for_step(tool_calls: tuple[ai_platform.AIToolCall, ...]) -> dict[str, ai_platform.AIToolCall]:
    calls: dict[str, ai_platform.AIToolCall] = {}
    duplicates: set[str] = set()
    for tool_call in tool_calls:
        if not tool_call.call_id:
            continue
        if tool_call.call_id in calls:
            duplicates.add(tool_call.call_id)
        calls[tool_call.call_id] = tool_call
    if duplicates:
        raise GeminiProviderError("Gemini tool response has ambiguous function call IDs.")
    return calls


def _map_assistant_text_parts(message: ai_platform.AIMessage) -> list[dict[str, Any]]:
    visible_parts = _gemini_visible_text_parts(message.metadata)
    if visible_parts is None:
        text_part: dict[str, Any] = {"text": message.content}
        signature = _gemini_thought_signature(message.metadata)
        if signature:
            text_part["thoughtSignature"] = signature
        return [text_part]
    combined = "".join(part["text"] for part in visible_parts)
    if combined != message.content:
        raise GeminiProviderError("Gemini visible text part metadata does not match assistant content.")
    mapped: list[dict[str, Any]] = []
    for part in visible_parts:
        mapped_part: dict[str, Any] = {"text": part["text"]}
        signature = part.get("thought_signature")
        if signature:
            mapped_part["thoughtSignature"] = signature
        mapped.append(mapped_part)
    return mapped


def _map_function_call_part(tool_call: ai_platform.AIToolCall) -> dict[str, Any]:
    function_call = {
        "id": tool_call.call_id,
        "name": tool_call.tool_name,
        "args": dict(tool_call.arguments),
    }
    part = {"functionCall": function_call}
    signature = _gemini_thought_signature(tool_call.metadata)
    if signature:
        part["thoughtSignature"] = signature
    return part


def _map_function_response_part(
    message: ai_platform.AIMessage,
    tool_call: ai_platform.AIToolCall,
) -> dict[str, Any]:
    return {
        "functionResponse": {
            "id": message.tool_call_id,
            "name": tool_call.tool_name,
            "response": _tool_response_payload(message.content),
        }
    }


def _tool_response_payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"result": content}
    if isinstance(parsed, dict):
        return parsed
    return {"result": content}


def _map_generation_config(options: ai_platform.Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "thinkingConfig": {
            "thinkingLevel": str(options.get("reasoning_effort", "medium")),
            "includeThoughts": False,
        }
    }
    for key, value in options.items():
        if key not in ALLOWED_OPTIONS:
            raise GeminiProviderError(f"Unsupported Gemini option: {key}")
        if key == "reasoning_effort":
            if value not in ALLOWED_REASONING_EFFORT:
                raise GeminiProviderError("Unsupported Gemini reasoning_effort.")
            config["thinkingConfig"]["thinkingLevel"] = value
        elif key == "max_output_tokens":
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > MAX_OUTPUT_TOKENS_LIMIT:
                raise GeminiProviderError("Unsupported Gemini max_output_tokens.")
            config["maxOutputTokens"] = value
    return config


def _map_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        function = tool["function"]
        name = function.get("name")
        description = function.get("description", "")
        parameters = function.get("parameters")
    else:
        name = tool.get("name")
        description = tool.get("description", "")
        parameters = tool.get("parameters") or tool.get("arguments")
    if not isinstance(name, str) or not name:
        raise GeminiProviderError("Tool schema requires a name.")
    if not isinstance(description, str):
        raise GeminiProviderError("Tool schema description must be a string.")
    if not isinstance(parameters, dict):
        raise GeminiProviderError("Tool schema parameters must be an object.")
    # parametersJsonSchema accepts standard JSON Schema (additionalProperties,
    # ["string", "null"] type unions, nested arrays/objects). The legacy
    # OpenAPI-subset "parameters" field rejects those constructs.
    declaration = {"name": name, "description": description, "parametersJsonSchema": parameters}
    _json_roundtrip(declaration, "Tool schema must be JSON-safe.")
    return declaration


def _parse_response(response_bytes: bytes, *, model_id: str) -> ai_platform.AIResponse:
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeminiProviderError("Gemini response was malformed.") from exc
    try:
        candidate = payload["candidates"][0]
        content = candidate.get("content", {})
        parts = content.get("parts") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiProviderError("Gemini response was malformed.") from exc
    if not isinstance(parts, list):
        raise GeminiProviderError("Gemini response was malformed.")
    visible_text: list[str] = []
    visible_text_parts: list[dict[str, str | None]] = []
    tool_calls: list[ai_platform.AIToolCall] = []
    metadata: dict[str, Any] = {"provider": GEMINI_PROVIDER_ID, "model": payload.get("modelVersion") or model_id}
    for part in parts:
        if not isinstance(part, dict):
            raise GeminiProviderError("Gemini response part was malformed.")
        if part.get("thought") is True:
            continue
        if isinstance(part.get("text"), str):
            text = part["text"]
            visible_text.append(text)
            signature = part.get("thoughtSignature")
            visible_text_parts.append(
                {
                    "text": text,
                    "thought_signature": signature if isinstance(signature, str) and signature else None,
                }
            )
            if isinstance(signature, str) and signature:
                metadata["gemini"] = {"thought_signature": signature}
        if "functionCall" in part:
            tool_calls.append(_parse_function_call(part["functionCall"], part, model_id))
    finish_reason = candidate.get("finishReason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        finish_reason = None
    if visible_text_parts:
        gemini_metadata = dict(metadata.get("gemini") or {})
        gemini_metadata["visible_text_parts"] = visible_text_parts
        metadata["gemini"] = gemini_metadata
    return ai_platform.AIResponse(
        content="".join(visible_text),
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        metadata=metadata,
    )


def _parse_function_call(raw_call: Any, raw_part: dict[str, Any], model_id: str) -> ai_platform.AIToolCall:
    if not isinstance(raw_call, dict):
        raise GeminiProviderError("Gemini function call was malformed.")
    name = raw_call.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiProviderError("Gemini function call name was malformed.")
    args = raw_call.get("args", {})
    if not isinstance(args, dict):
        raise GeminiProviderError("Gemini function call arguments must be an object.")
    call_id = raw_call.get("id")
    if _requires_tool_signature(model_id) and (not isinstance(call_id, str) or not call_id):
        raise GeminiProviderError("Gemini function call id was malformed.")
    if call_id is not None and not isinstance(call_id, str):
        raise GeminiProviderError("Gemini function call id was malformed.")
    signature = raw_part.get("thoughtSignature") or raw_call.get("thoughtSignature")
    metadata = {"gemini": {"thought_signature": signature}} if isinstance(signature, str) and signature else {}
    return ai_platform.AIToolCall(call_id=call_id, tool_name=name, arguments=args, metadata=metadata)


def _error_for_status(status: int, response_bytes: bytes = b"") -> GeminiProviderError:
    error = _parse_error_body(response_bytes)
    status_text = str(error.get("status", "")).upper()
    message = str(error.get("message", "")).upper()
    if status == 401 or "API_KEY_INVALID" in message or "API_KEY_INVALID" in status_text or "UNAUTHENTICATED" in status_text:
        return GeminiProviderError("Gemini credential is invalid.")
    if status == 403 or "PERMISSION_DENIED" in status_text:
        return GeminiProviderError("Gemini API access is forbidden.")
    if status == 429 or "RESOURCE_EXHAUSTED" in status_text:
        return GeminiProviderError("Gemini rate limit or quota was reached.")
    if status == 402 or "BILLING" in message or "CREDIT" in message:
        return GeminiProviderError("Gemini billing or credit prerequisite is not satisfied.")
    if status == 404 or "NOT_FOUND" in status_text or "MODEL" in message and "FOUND" in message:
        return GeminiProviderError("Gemini model is unavailable.")
    if status == 400:
        return GeminiProviderError("Gemini rejected the request.")
    if status >= 500 or status == 503:
        return GeminiProviderError("Gemini service is unavailable.")
    return GeminiProviderError("Gemini request failed.")


def _parse_error_body(response_bytes: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def _retry_wait_seconds(response_bytes: bytes, default: float) -> float | None:
    """Wait before retrying: google.rpc.RetryInfo.retryDelay ("17s") or ``default``.

    None means "do not retry" (the provider asks for a longer wait than we accept).
    """
    error = _parse_error_body(response_bytes)
    for detail in error.get("details") or []:
        if not isinstance(detail, dict) or not str(detail.get("@type", "")).endswith("RetryInfo"):
            continue
        raw = str(detail.get("retryDelay", "")).strip()
        if not raw.endswith("s"):
            continue
        try:
            seconds = float(raw[:-1])
        except ValueError:
            continue
        if seconds > MAX_RETRY_WAIT_SECONDS:
            return None
        return max(seconds + 0.5, 0.5)
    return default


def _availability_state_from_error(error: GeminiProviderError) -> ai_platform.AvailabilityState:
    message = str(error).lower()
    if "credential is invalid" in message:
        return ai_platform.AvailabilityState.CREDENTIAL_INVALID
    if "credential" in message:
        return ai_platform.AvailabilityState.CREDENTIAL_MISSING
    if "forbidden" in message:
        return ai_platform.AvailabilityState.ACCESS_FORBIDDEN
    return ai_platform.AvailabilityState.UNAVAILABLE


def _gemini_thought_signature(metadata: Mapping[str, Any] | dict[str, Any]) -> str | None:
    gemini = metadata.get("gemini") if isinstance(metadata, Mapping) else None
    if isinstance(gemini, Mapping):
        signature = gemini.get("thought_signature")
        if isinstance(signature, str) and signature:
            return signature
    return None


def _gemini_visible_text_parts(metadata: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, str | None]] | None:
    gemini = metadata.get("gemini") if isinstance(metadata, Mapping) else None
    if not isinstance(gemini, Mapping) or "visible_text_parts" not in gemini:
        return None
    raw_parts = gemini.get("visible_text_parts")
    if not isinstance(raw_parts, (list, tuple)):
        raise GeminiProviderError("Gemini visible text part metadata is malformed.")
    parts: list[dict[str, str | None]] = []
    for item in raw_parts:
        if not isinstance(item, Mapping):
            raise GeminiProviderError("Gemini visible text part metadata is malformed.")
        text = item.get("text")
        signature = item.get("thought_signature")
        if not isinstance(text, str):
            raise GeminiProviderError("Gemini visible text part metadata is malformed.")
        if signature is not None and not isinstance(signature, str):
            raise GeminiProviderError("Gemini visible text part metadata is malformed.")
        parts.append({"text": text, "thought_signature": signature if signature else None})
    return parts


def _requires_tool_signature(model_id: str) -> bool:
    return model_id.startswith("gemini-3")


def _json_roundtrip(value: Any, message: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise GeminiProviderError(message) from exc


def _read_limited(response: Any, max_response_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if content_length is not None:
        try:
            if int(content_length) > max_response_bytes:
                raise GeminiProviderError("Gemini response was too large.")
        except ValueError:
            pass
    data = response.read(max_response_bytes + 1)
    if len(data) > max_response_bytes:
        raise GeminiProviderError("Gemini response was too large.")
    return data
