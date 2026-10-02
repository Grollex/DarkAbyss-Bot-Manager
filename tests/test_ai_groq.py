import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_modules():
    sys.path.insert(0, str(CORE_ROOT))
    for name in ("ai_groq", "ai_platform"):
        sys.modules.pop(name, None)
    import ai_platform
    import ai_groq

    return ai_platform, ai_groq


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post_json(self, *, url, headers, body, timeout_seconds, max_response_bytes):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.error is not None:
            raise self.error
        status, payload = self.responses.pop(0)
        if isinstance(payload, bytes):
            if len(payload) > max_response_bytes:
                raise sys.modules["ai_groq"].GroqProviderError("Groq response was too large.")
            return status, payload
        data = json.dumps(payload).encode("utf-8")
        if len(data) > max_response_bytes:
            raise sys.modules["ai_groq"].GroqProviderError("Groq response was too large.")
        return status, data


class GroqAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, ai_platform, secret="GROQ_SECRET"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ai_platform.CredentialStore(Path(temp_dir.name) / "secrets" / "ai")
        if secret is not None:
            store.write_secret("groq", "groq-default", secret)
        return store

    def success_payload(self, *, content="hello", tool_calls=None):
        message = {"role": "assistant", "content": content}
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        return {
            "id": "chatcmpl-test",
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": message, "finish_reason": "stop"}],
        }

    async def test_provider_metadata_and_local_availability(self):
        ai_platform, ai_groq = load_modules()
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=FakeTransport())
        self.assertEqual(provider.metadata.provider_id, "groq")
        self.assertEqual(provider.metadata.display_name, "Groq")
        self.assertEqual(provider.metadata.models[0].model_id, "openai/gpt-oss-120b")

        missing = provider.get_local_availability(credential_ref=None, credential_available=False)
        self.assertEqual(missing.state, ai_platform.AvailabilityState.CREDENTIAL_MISSING)
        available = provider.get_local_availability(credential_ref="groq-default", credential_available=True)
        self.assertTrue(available.ok)

    async def test_missing_credential_is_contained_and_secret_loaded_only_for_network(self):
        ai_platform, ai_groq = load_modules()
        store = self.make_store(ai_platform, secret=None)
        transport = FakeTransport([self.success_payload()])
        provider = ai_groq.GroqProvider(store, transport=transport)

        local = provider.get_local_availability(credential_ref="groq-default", credential_available=False)
        self.assertEqual(local.state, ai_platform.AvailabilityState.CREDENTIAL_MISSING)
        self.assertEqual(transport.calls, [])

        connection = await provider.test_connection("groq-default")
        self.assertEqual(connection.state, ai_platform.AvailabilityState.CREDENTIAL_MISSING)
        self.assertEqual(transport.calls, [])

    async def test_authorization_header_is_internal_and_secret_is_sanitized(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform, secret="  SECRET_KEY\n"), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        response = await provider.generate(request, "groq-default")
        self.assertEqual(response.content, "ok")
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer SECRET_KEY")
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], "DarkAbyssBotManager/AI-2B")
        self.assertEqual(transport.calls[0]["headers"]["Accept"], "application/json")
        self.assertEqual(transport.calls[0]["headers"]["Content-Type"], "application/json")
        self.assertNotIn("SECRET_KEY", repr(response))
        self.assertNotIn("SECRET_KEY", str(response.public_dict()))

        reject = ai_groq.GroqProvider(
            self.make_store(ai_platform, secret="SECRET_KEY"),
            transport=FakeTransport([(401, {"error": {"message": "bad key SECRET_KEY"}})]),
        )
        with self.assertRaises(ai_groq.GroqProviderError) as error:
            await reject.generate(request, "groq-default")
        self.assertIn("invalid", str(error.exception).lower())
        self.assertNotIn("SECRET_KEY", str(error.exception))

    async def test_every_groq_request_carries_stable_darkabyss_user_agent(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        await provider.generate(request, "groq-default")

        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], ai_groq.GROQ_USER_AGENT)
        self.assertTrue(transport.calls[0]["url"].startswith("https://api.groq.com/openai/v1/"))

    async def test_request_mapping_messages_options_and_tools(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(
                ai_platform.AIMessage(role="system", content="system"),
                ai_platform.AIMessage(role="user", content="user"),
                ai_platform.AIMessage(role="assistant", content="assistant"),
                ai_platform.AIMessage(role="tool", content="tool", name="tool_name", tool_call_id="call-1"),
            ),
            tools=(
                {
                    "name": "send_message",
                    "description": "Send message",
                    "arguments": {
                        "type": "object",
                        "properties": {"channel_id": {"type": "string"}},
                        "required": ["channel_id"],
                        "additionalProperties": False,
                    },
                },
            ),
            options={"reasoning_effort": "high", "max_completion_tokens": 32, "temperature": 0, "top_p": 1},
        )
        await provider.generate(request, "groq-default")
        body = transport.calls[0]["body"]
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["max_completion_tokens"], 32)
        self.assertEqual(body["include_reasoning"], False)
        self.assertNotIn("reasoning_format", body)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["function"]["name"], "send_message")

    async def test_gpt_oss_text_request_disables_reasoning_output(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )

        await provider.generate(request, "groq-default")

        body = transport.calls[0]["body"]
        self.assertEqual(body["include_reasoning"], False)
        self.assertNotIn("reasoning_format", body)

    async def test_assistant_tool_call_and_tool_result_message_mapping(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={"channel_id": "123", "content": "hi"},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
            ),
        )
        await provider.generate(request, "groq-default")
        messages = transport.calls[0]["body"]["messages"]
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["name"], "send_message")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[1]["tool_call_id"], "call-1")

    async def test_reasoning_effort_values_and_unknown_option_rejected(self):
        ai_platform, ai_groq = load_modules()
        for effort in ("low", "medium", "high"):
            transport = FakeTransport([(200, self.success_payload(content=effort))])
            provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
            request = ai_platform.AIRequest(
                model_id="openai/gpt-oss-120b",
                messages=(ai_platform.AIMessage(role="user", content="hello"),),
                options={"reasoning_effort": effort},
            )
            await provider.generate(request, "groq-default")
            self.assertEqual(transport.calls[0]["body"]["reasoning_effort"], effort)

        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=FakeTransport())
        bad_request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            options={"browser_search": True},
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "Unsupported Groq option"):
            await provider.generate(bad_request, "groq-default")

        deprecated_request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            options={"max_tokens": 16},
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "Unsupported Groq option"):
            await provider.generate(deprecated_request, "groq-default")

        bad_top_p = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            options={"top_p": 1.5},
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "Unsupported Groq top_p"):
            await provider.generate(bad_top_p, "groq-default")

        bool_temperature = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            options={"temperature": True},
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "Unsupported Groq temperature"):
            await provider.generate(bool_temperature, "groq-default")

    async def test_real_admin_tool_schema_maps_to_groq_function_schema(self):
        ai_platform, ai_groq = load_modules()
        sys.path.insert(0, str(CORE_ROOT))
        sys.modules.pop("admin_tools", None)
        import admin_tools

        definition = admin_tools.get_tool_definition("send_message")
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            tools=(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "arguments": definition.arguments,
                },
            ),
        )
        await provider.generate(request, "groq-default")
        function = transport.calls[0]["body"]["tools"][0]["function"]
        json.dumps(function)
        self.assertEqual(function["name"], "send_message")
        self.assertIn("channel_id", function["parameters"]["properties"])
        serialized = json.dumps(function)
        self.assertNotIn("TextChannel", serialized)
        self.assertNotIn("SECRET", serialized)

    async def test_plain_text_and_multiple_tool_call_response_parsing(self):
        ai_platform, ai_groq = load_modules()
        tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "send_message", "arguments": '{"channel_id":"123","content":"hi"}'},
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {"name": "lock_channel", "arguments": '{"channel_id":"123"}'},
            },
        ]
        transport = FakeTransport([(200, self.success_payload(content="plan", tool_calls=tool_calls))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        response = await provider.generate(request, "groq-default")
        self.assertEqual(response.content, "plan")
        self.assertEqual(len(response.tool_calls), 2)
        self.assertEqual(response.tool_calls[0].tool_name, "send_message")
        self.assertEqual(response.tool_calls[0].arguments["channel_id"], "123")

    async def test_malformed_tool_arguments_rejected_without_eval(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport(
            [
                (
                    200,
                    self.success_payload(
                        tool_calls=[
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "send_message", "arguments": "__import__('os').system('x')"},
                            }
                        ]
                    ),
                )
            ]
        )
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "tool arguments"):
            await provider.generate(request, "groq-default")

    async def test_http_status_network_timeout_and_oversize_mapping(self):
        ai_platform, ai_groq = load_modules()
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        cases = [
            (401, "invalid"),
            (403, "forbidden"),
            (429, "rate limit"),
            (500, "unavailable"),
        ]
        for status, expected in cases:
            provider = ai_groq.GroqProvider(
                self.make_store(ai_platform),
                transport=FakeTransport([(status, {"error": {"message": "raw provider detail"}})]),
            )
            with self.subTest(status=status):
                with self.assertRaises(ai_groq.GroqProviderError) as error:
                    await provider.generate(request, "groq-default")
                self.assertIn(expected, str(error.exception).lower())
                self.assertNotIn("raw provider detail", str(error.exception))

        cloudflare = ai_groq.GroqProvider(
            self.make_store(ai_platform),
            transport=FakeTransport(
                [
                    (
                        403,
                        {
                            "error_code": 1010,
                            "error_name": "browser_signature_banned",
                            "detail": "SECRET raw provider detail",
                        },
                    )
                ]
            ),
        )
        with self.assertRaises(ai_groq.GroqProviderError) as cloudflare_error:
            await cloudflare.generate(request, "groq-default")
        self.assertIn("security gateway", str(cloudflare_error.exception))
        self.assertNotIn("SECRET", str(cloudflare_error.exception))

        provider = ai_groq.GroqProvider(
            self.make_store(ai_platform),
            transport=FakeTransport(error=ai_groq.GroqProviderError("Groq network request failed.")),
        )
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "network"):
            await provider.generate(request, "groq-default")

        oversized = ai_groq.GroqProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, b"x" * 10)]),
            max_response_bytes=5,
        )
        with self.assertRaises(ai_groq.GroqProviderError):
            await oversized.generate(request, "groq-default")

    async def test_endpoint_is_pinned_and_no_admin_tool_execution(self):
        ai_platform, ai_groq = load_modules()
        transport = FakeTransport([(200, self.success_payload(content="ok"))])
        provider = ai_groq.GroqProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        await provider.generate(request, "groq-default")
        self.assertTrue(transport.calls[0]["url"].startswith("https://api.groq.com/openai/v1/"))
        source = (CORE_ROOT / "ai_groq.py").read_text(encoding="utf-8")
        self.assertNotIn("admin_tools.execute_tool", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)

    async def test_explicit_smoke_request_shape(self):
        ai_platform, ai_groq = load_modules()
        request = ai_groq.build_groq_smoke_request()
        self.assertEqual(request.model_id, "openai/gpt-oss-120b")
        self.assertIn("KAIRO_GROQ_OK", request.messages[0].content)
        self.assertEqual(request.options["max_completion_tokens"], 256)
        self.assertEqual(request.options["reasoning_effort"], "low")

    async def test_test_connection_maps_401_403_and_429(self):
        ai_platform, ai_groq = load_modules()
        cases = [
            (401, ai_platform.AvailabilityState.CREDENTIAL_INVALID),
            (403, ai_platform.AvailabilityState.ACCESS_FORBIDDEN),
            (429, ai_platform.AvailabilityState.UNAVAILABLE),
        ]
        for status, state in cases:
            provider = ai_groq.GroqProvider(
                self.make_store(ai_platform),
                transport=FakeTransport([(status, {"error": {"message": "raw SECRET provider detail"}})]),
            )
            with self.subTest(status=status):
                availability = await provider.test_connection("groq-default")
                self.assertEqual(availability.state, state)
                self.assertNotIn("SECRET", availability.message)


class GroqRetryTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, transport, retry_delays=(3.0, 8.0)):
        ai_platform, ai_groq = load_modules()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ai_platform.CredentialStore(Path(temp_dir.name) / "secrets" / "ai")
        store.write_secret("groq", "groq-default", "GROQ_SECRET")
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        provider = ai_groq.GroqProvider(store, transport=transport, retry_delays=retry_delays, sleep=fake_sleep)
        request = ai_platform.AIRequest(
            model_id="openai/gpt-oss-120b",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        return ai_groq, provider, request, sleeps

    @staticmethod
    def ok_payload():
        return {"model": "openai/gpt-oss-120b", "choices": [{"message": {"role": "assistant", "content": "done"}}]}

    async def test_rate_limit_hint_is_honoured_then_request_succeeds(self):
        hint = {"error": {"message": "Rate limit reached on tokens per minute (TPM). Please try again in 7.5s."}}
        transport = FakeTransport([(429, hint), (200, self.ok_payload())])
        _, provider, request, sleeps = self.make_provider(transport)

        response = await provider.generate(request, "groq-default")

        self.assertEqual(response.content, "done")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [8.0])

    async def test_server_errors_use_configured_delays_and_give_up(self):
        transport = FakeTransport([(503, {}), (502, {}), (500, {})])
        ai_groq, provider, request, sleeps = self.make_provider(transport)

        with self.assertRaisesRegex(ai_groq.GroqProviderError, "unavailable"):
            await provider.generate(request, "groq-default")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [3.0, 8.0])

    async def test_tool_use_failed_is_resampled_once_per_retry_slot(self):
        failed = {"error": {"message": "Failed to call a function.", "code": "tool_use_failed"}}
        transport = FakeTransport([(400, failed), (200, self.ok_payload())])
        _, provider, request, sleeps = self.make_provider(transport)

        response = await provider.generate(request, "groq-default")

        self.assertEqual(response.content, "done")
        self.assertEqual(sleeps, [0.5])

    async def test_long_waits_and_client_errors_are_not_retried(self):
        daily = {"error": {"message": "Rate limit reached on requests per day. Please try again in 12m30s."}}
        for status, payload in ((429, daily), (401, {}), (400, {})):
            transport = FakeTransport([(status, payload)])
            ai_groq, provider, request, sleeps = self.make_provider(transport)
            with self.subTest(status=status):
                with self.assertRaises(ai_groq.GroqProviderError):
                    await provider.generate(request, "groq-default")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(sleeps, [])

    async def test_network_errors_are_retried_only_when_enabled(self):
        transport = FakeTransport()
        ai_groq, provider, request, sleeps = self.make_provider(transport)
        # Raise the class of the freshly loaded module (load_modules re-imports it).
        transport.error = ai_groq.GroqNetworkError("Groq network request failed.")
        with self.assertRaisesRegex(ai_groq.GroqProviderError, "network"):
            await provider.generate(request, "groq-default")
        self.assertEqual(len(transport.calls), 3)

        no_retry_transport = FakeTransport([(429, {})])
        ai_groq, no_retry, request, no_sleeps = self.make_provider(no_retry_transport, retry_delays=())
        with self.assertRaises(ai_groq.GroqProviderError):
            await no_retry.generate(request, "groq-default")
        self.assertEqual(len(no_retry_transport.calls), 1)
        self.assertEqual(no_sleeps, [])


class DependencyTests(unittest.TestCase):
    def test_no_provider_sdk_dependency_added(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("groq", "openai", "httpx", "requests"):
            with self.subTest(package=package):
                self.assertNotIn(package, requirements)


if __name__ == "__main__":
    unittest.main()
