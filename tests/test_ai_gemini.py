import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_modules():
    sys.path.insert(0, str(CORE_ROOT))
    for name in ("ai_gemini", "ai_platform"):
        sys.modules.pop(name, None)
    import ai_platform
    import ai_gemini

    return ai_platform, ai_gemini


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
                raise sys.modules["ai_gemini"].GeminiProviderError("Gemini response was too large.")
            return status, payload
        data = json.dumps(payload).encode("utf-8")
        if len(data) > max_response_bytes:
            raise sys.modules["ai_gemini"].GeminiProviderError("Gemini response was too large.")
        return status, data


class GeminiAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, ai_platform, secret="GEMINI_SECRET"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ai_platform.CredentialStore(Path(temp_dir.name) / "secrets" / "ai")
        if secret is not None:
            store.write_secret("gemini", "gemini-default", secret)
        return store

    def success_payload(self, *, text="hello", parts=None, finish_reason="STOP"):
        return {
            "modelVersion": "gemini-3.8-flash",
            "candidates": [
                {
                    "content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]},
                    "finishReason": finish_reason,
                }
            ],
        }

    async def test_provider_metadata_local_availability_and_no_network_on_construction(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport()
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)

        self.assertEqual(provider.metadata.provider_id, "gemini")
        self.assertEqual(provider.metadata.display_name, "Google Gemini")
        model_ids = {model.model_id for model in provider.metadata.models}
        self.assertIn("gemini-3.8-flash", model_ids)
        self.assertIn("gemini-3.5-flash-lite", model_ids)
        self.assertEqual(transport.calls, [])
        self.assertEqual(
            provider.get_local_availability(credential_ref=None, credential_available=False).state,
            ai_platform.AvailabilityState.CREDENTIAL_MISSING,
        )
        self.assertTrue(provider.get_local_availability(credential_ref="gemini-default", credential_available=True).ok)

    async def test_missing_credential_is_contained_and_network_not_called(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport([(200, self.success_payload(text="KAIRO_GEMINI_OK"))])
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform, secret=None), transport=transport)

        availability = await provider.test_connection("gemini-default")

        self.assertEqual(availability.state, ai_platform.AvailabilityState.CREDENTIAL_MISSING)
        self.assertEqual(transport.calls, [])

    async def test_auth_headers_endpoint_timeout_and_size_limit(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport([(200, self.success_payload(text="ok"))])
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform, secret="  SECRET_KEY\n"),
            transport=transport,
            timeout_seconds=7,
            max_response_bytes=4096,
        )
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )

        await provider.generate(request, "gemini-default")

        call = transport.calls[0]
        self.assertEqual(call["headers"]["x-goog-api-key"], "SECRET_KEY")
        self.assertNotIn("Authorization", call["headers"])
        self.assertEqual(call["headers"]["User-Agent"], "DarkAbyssBotManager/AI-2C")
        self.assertEqual(call["headers"]["Accept"], "application/json")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["timeout_seconds"], 7)
        self.assertEqual(call["max_response_bytes"], 4096)
        self.assertEqual(call["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent")

    async def test_message_mapping_system_user_assistant_and_options(self):
        ai_platform, ai_gemini = load_modules()
        for effort in ("low", "medium", "high"):
            transport = FakeTransport([(200, self.success_payload(text=effort))])
            provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)
            request = ai_platform.AIRequest(
                model_id="gemini-3.8-flash",
                messages=(
                    ai_platform.AIMessage(role="system", content="first"),
                    ai_platform.AIMessage(role="system", content="second"),
                    ai_platform.AIMessage(role="user", content="hello"),
                    ai_platform.AIMessage(role="assistant", content="hi"),
                ),
                options={"reasoning_effort": effort, "max_output_tokens": 32},
            )
            await provider.generate(request, "gemini-default")
            body = transport.calls[0]["body"]
            self.assertEqual(body["systemInstruction"]["parts"], [{"text": "first"}, {"text": "second"}])
            self.assertEqual(body["contents"][0], {"role": "user", "parts": [{"text": "hello"}]})
            self.assertEqual(body["contents"][1], {"role": "model", "parts": [{"text": "hi"}]})
            self.assertEqual(body["generationConfig"]["thinkingConfig"]["thinkingLevel"], effort)
            self.assertEqual(body["generationConfig"]["thinkingConfig"]["includeThoughts"], False)
            self.assertEqual(body["generationConfig"]["maxOutputTokens"], 32)
            self.assertNotIn("temperature", body["generationConfig"])
            self.assertNotIn("topP", body["generationConfig"])

    async def test_option_validation_rejects_unknown_bool_nan_inf_and_out_of_range(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport())
        base_messages = (ai_platform.AIMessage(role="user", content="hello"),)
        bad_options = [
            {"unknown": True},
            {"reasoning_effort": "extreme"},
            {"max_output_tokens": True},
            {"max_output_tokens": 0},
            {"max_output_tokens": 8193},
            {"temperature": 0.5},
            {"top_p": 1},
            {"top_k": 1},
        ]
        for options in bad_options:
            request = ai_platform.AIRequest(model_id="gemini-3.8-flash", messages=base_messages, options=options)
            with self.subTest(options=options):
                with self.assertRaises(ai_gemini.GeminiProviderError):
                    await provider.generate(request, "gemini-default")

    async def test_function_declarations_real_admin_schema_are_data_only(self):
        ai_platform, ai_gemini = load_modules()
        sys.path.insert(0, str(CORE_ROOT))
        sys.modules.pop("admin_tools", None)
        import admin_tools

        definition = admin_tools.get_tool_definition("send_message")
        transport = FakeTransport([(200, self.success_payload(text="ok"))])
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
            tools=({"name": definition.name, "description": definition.description, "arguments": definition.arguments},),
        )

        await provider.generate(request, "gemini-default")

        declaration = transport.calls[0]["body"]["tools"][0]["functionDeclarations"][0]
        json.dumps(declaration)
        self.assertEqual(declaration["name"], "send_message")
        self.assertIn("channel_id", declaration["parametersJsonSchema"]["properties"])
        self.assertNotIn("parameters", declaration)
        self.assertIs(declaration["parametersJsonSchema"]["additionalProperties"], False)
        serialized = json.dumps(declaration)
        self.assertNotIn("TextChannel", serialized)
        self.assertNotIn("SECRET", serialized)
        source = (CORE_ROOT / "ai_gemini.py").read_text(encoding="utf-8")
        self.assertNotIn("admin_tools.execute_tool", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)

    async def test_function_call_parsing_thought_signature_and_thought_text_not_exposed(self):
        ai_platform, ai_gemini = load_modules()
        parts = [
            {"text": "hidden thought", "thought": True},
            {"text": "visible"},
            {
                "functionCall": {
                    "id": "call-1",
                    "name": "send_message",
                    "args": {"channel_id": "123", "content": "hi"},
                },
                "thoughtSignature": "opaque-signature",
            },
            {"functionCall": {"id": "call-2", "name": "lock_channel", "args": {"channel_id": "123"}}},
        ]
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, self.success_payload(parts=parts, finish_reason="FUNCTION_CALL"))]),
        )
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )

        response = await provider.generate(request, "gemini-default")

        self.assertEqual(response.content, "visible")
        self.assertEqual(response.finish_reason, "FUNCTION_CALL")
        self.assertEqual(len(response.tool_calls), 2)
        self.assertEqual(response.tool_calls[0].call_id, "call-1")
        self.assertEqual(response.tool_calls[0].metadata["gemini"]["thought_signature"], "opaque-signature")
        self.assertNotIn("hidden thought", response.content)

    async def test_thought_signature_replay_and_function_response_mapping(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport([(200, self.success_payload(text="ok"))])
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={"channel_id": "123"},
                            metadata={"gemini": {"thought_signature": "opaque-signature"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
            ),
        )

        await provider.generate(request, "gemini-default")

        contents = transport.calls[0]["body"]["contents"]
        self.assertEqual(contents[0]["parts"][0]["thoughtSignature"], "opaque-signature")
        self.assertNotIn("thoughtSignature", contents[0]["parts"][0]["functionCall"])
        response_part = contents[1]["parts"][0]["functionResponse"]
        self.assertEqual(response_part["id"], "call-1")
        self.assertEqual(response_part["name"], "send_message")
        self.assertEqual(response_part["response"], {"ok": True})

    async def test_parallel_function_call_signature_rule_and_response_batching(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport([(200, self.success_payload(text="ok"))])
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={"channel_id": "123"},
                            metadata={"gemini": {"thought_signature": "sig-first"}},
                        ),
                        ai_platform.AIToolCall(call_id="call-2", tool_name="lock_channel", arguments={"channel_id": "123"}),
                        ai_platform.AIToolCall(call_id="call-3", tool_name="unlock_channel", arguments={"channel_id": "123"}),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
                ai_platform.AIMessage(role="tool", content="[1,2]", tool_call_id="call-2"),
                ai_platform.AIMessage(role="tool", content="true", tool_call_id="call-3"),
            ),
        )

        await provider.generate(request, "gemini-default")

        contents = transport.calls[0]["body"]["contents"]
        self.assertEqual(len(contents), 2)
        model_parts = contents[0]["parts"]
        self.assertEqual([part["functionCall"]["id"] for part in model_parts], ["call-1", "call-2", "call-3"])
        self.assertEqual(model_parts[0]["thoughtSignature"], "sig-first")
        self.assertNotIn("thoughtSignature", model_parts[1])
        self.assertNotIn("thoughtSignature", model_parts[2])
        response_parts = contents[1]["parts"]
        self.assertEqual([part["functionResponse"]["id"] for part in response_parts], ["call-1", "call-2", "call-3"])
        self.assertEqual(response_parts[0]["functionResponse"]["response"], {"ok": True})
        self.assertEqual(response_parts[1]["functionResponse"]["response"], {"result": "[1,2]"})
        self.assertEqual(response_parts[2]["functionResponse"]["response"], {"result": "true"})

    async def test_sequential_function_call_steps_preserve_each_first_signature(self):
        ai_platform, ai_gemini = load_modules()
        transport = FakeTransport([(200, self.success_payload(text="ok"))])
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=transport)
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "sigA"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content="done", tool_call_id="call-1"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-2",
                            tool_name="lock_channel",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "sigB"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content="5", tool_call_id="call-2"),
            ),
        )

        await provider.generate(request, "gemini-default")

        contents = transport.calls[0]["body"]["contents"]
        self.assertEqual(contents[0]["parts"][0]["thoughtSignature"], "sigA")
        self.assertEqual(contents[2]["parts"][0]["thoughtSignature"], "sigB")
        self.assertEqual(contents[1]["parts"][0]["functionResponse"]["response"], {"result": "done"})
        self.assertEqual(contents[3]["parts"][0]["functionResponse"]["response"], {"result": "5"})

    async def test_visible_text_part_boundaries_and_signatures_round_trip(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport(
                [
                    (
                        200,
                        self.success_payload(
                            parts=[
                                {"text": "A"},
                                {"text": "hidden", "thought": True},
                                {"text": "B", "thoughtSignature": "sigB"},
                                {"text": "C"},
                            ]
                        ),
                    ),
                    (200, self.success_payload(text="ok")),
                ]
            ),
        )
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )

        response = await provider.generate(request, "gemini-default")
        self.assertEqual(response.content, "ABC")
        self.assertEqual(
            response.metadata["gemini"]["visible_text_parts"],
            (
                {"text": "A", "thought_signature": None},
                {"text": "B", "thought_signature": "sigB"},
                {"text": "C", "thought_signature": None},
            ),
        )
        self.assertEqual(response.metadata["gemini"]["thought_signature"], "sigB")
        self.assertNotIn("hidden", response.content)

        replay = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="assistant", content=response.content, metadata=response.metadata),),
        )
        await provider.generate(replay, "gemini-default")
        text_parts = provider._transport.calls[1]["body"]["contents"][0]["parts"]
        self.assertEqual(text_parts, [{"text": "A"}, {"text": "B", "thoughtSignature": "sigB"}, {"text": "C"}])

        inconsistent = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="assistant", content="ABX", metadata=response.metadata),),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "metadata"):
            await provider.generate(inconsistent, "gemini-default")

    async def test_empty_signed_visible_text_part_round_trips(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport(
                [
                    (200, self.success_payload(parts=[{"text": "", "thoughtSignature": "empty-signature"}])),
                    (200, self.success_payload(text="ok")),
                ]
            ),
        )

        response = await provider.generate(
            ai_platform.AIRequest(
                model_id="gemini-3.8-flash",
                messages=(ai_platform.AIMessage(role="user", content="hello"),),
            ),
            "gemini-default",
        )

        self.assertEqual(response.content, "")
        self.assertEqual(
            response.metadata["gemini"]["visible_text_parts"],
            ({"text": "", "thought_signature": "empty-signature"},),
        )

        await provider.generate(
            ai_platform.AIRequest(
                model_id="gemini-3.8-flash",
                messages=(ai_platform.AIMessage(role="assistant", content="", metadata=response.metadata),),
            ),
            "gemini-default",
        )
        self.assertEqual(
            provider._transport.calls[1]["body"]["contents"][0]["parts"],
            [{"text": "", "thoughtSignature": "empty-signature"}],
        )

    async def test_previous_turn_missing_signature_is_allowed_but_current_turn_is_strict(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, self.success_payload(text="ok"))]),
        )
        previous_turn = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="old"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(ai_platform.AIToolCall(call_id="old-call", tool_name="send_message", arguments={}),),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="old-call"),
                ai_platform.AIMessage(role="user", content="new"),
                ai_platform.AIMessage(role="assistant", content="plain"),
            ),
        )

        await provider.generate(previous_turn, "gemini-default")

        contents = provider._transport.calls[0]["body"]["contents"]
        self.assertEqual(contents[1]["parts"][0]["functionCall"]["id"], "old-call")
        self.assertNotIn("thoughtSignature", contents[1]["parts"][0])
        self.assertEqual(contents[4], {"role": "model", "parts": [{"text": "plain"}]})

        current_turn = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="current"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="send_message", arguments={}),),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "thoughtSignature"):
            await provider.generate(current_turn, "gemini-default")

    async def test_tool_responses_match_only_immediate_assistant_step(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport())
        stale_tool_result = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="old"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="old-call",
                            tool_name="send_message",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "old-signature"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="old-call"),
                ai_platform.AIMessage(role="user", content="new"),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="old-call"),
            ),
        )

        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "matching"):
            await provider.generate(stale_tool_result, "gemini-default")

    async def test_parallel_tool_results_must_be_complete_unique_and_reordered(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, self.success_payload(text="ok"))]),
        )
        reordered = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="current"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "first-signature"}},
                        ),
                        ai_platform.AIToolCall(call_id="call-2", tool_name="lock_channel", arguments={}),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content="[1,2]", tool_call_id="call-2"),
                ai_platform.AIMessage(role="tool", content="null", tool_call_id="call-1"),
            ),
        )

        await provider.generate(reordered, "gemini-default")

        response_parts = provider._transport.calls[0]["body"]["contents"][2]["parts"]
        self.assertEqual([part["functionResponse"]["id"] for part in response_parts], ["call-1", "call-2"])
        self.assertEqual(response_parts[0]["functionResponse"]["response"], {"result": "null"})
        self.assertEqual(response_parts[1]["functionResponse"]["response"], {"result": "[1,2]"})

        incomplete = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="current"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "first-signature"}},
                        ),
                        ai_platform.AIToolCall(call_id="call-2", tool_name="lock_channel", arguments={}),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "incomplete"):
            await provider.generate(incomplete, "gemini-default")

        duplicate = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(role="user", content="current"),
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="send_message",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "first-signature"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
                ai_platform.AIMessage(role="tool", content='{"again":true}', tool_call_id="call-1"),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "duplicate"):
            await provider.generate(duplicate, "gemini-default")

    async def test_missing_signature_and_tool_match_are_rejected_locally(self):
        ai_platform, ai_gemini = load_modules()
        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport())
        no_signature = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="send_message", arguments={}),),
                ),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "thoughtSignature"):
            await provider.generate(no_signature, "gemini-default")

        parallel_missing_first = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(call_id="call-1", tool_name="send_message", arguments={}),
                        ai_platform.AIToolCall(call_id="call-2", tool_name="lock_channel", arguments={}),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "thoughtSignature"):
            await provider.generate(parallel_missing_first, "gemini-default")

        missing_match = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="tool", content="ok", tool_call_id="missing"),),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "matching"):
            await provider.generate(missing_match, "gemini-default")

        duplicate = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(
                ai_platform.AIMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="one",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "sig1"}},
                        ),
                        ai_platform.AIToolCall(
                            call_id="call-1",
                            tool_name="two",
                            arguments={},
                            metadata={"gemini": {"thought_signature": "sig2"}},
                        ),
                    ),
                ),
                ai_platform.AIMessage(role="tool", content="ok", tool_call_id="call-1"),
            ),
        )
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "ambiguous"):
            await provider.generate(duplicate, "gemini-default")

    async def test_malformed_responses_and_arguments_are_rejected(self):
        ai_platform, ai_gemini = load_modules()
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        malformed_payloads = [
            b"{bad json",
            {"candidates": []},
            {"candidates": [{"content": {"parts": "bad"}}]},
            self.success_payload(parts=[{"functionCall": {"name": "send_message", "args": []}}]),
            self.success_payload(parts=[{"functionCall": {"id": "", "name": "send_message", "args": {}}}]),
            self.success_payload(parts=[{"functionCall": {"id": 123, "name": "send_message", "args": {}}}]),
        ]
        for payload in malformed_payloads:
            provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport([(200, payload)]))
            with self.subTest(payload=payload):
                with self.assertRaises(ai_gemini.GeminiProviderError):
                    await provider.generate(request, "gemini-default")

    async def test_http_status_network_timeout_and_oversize_mapping(self):
        ai_platform, ai_gemini = load_modules()
        request = ai_platform.AIRequest(
            model_id="gemini-3.8-flash",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        cases = [
            (401, {"error": {"status": "UNAUTHENTICATED", "message": "API_KEY_INVALID SECRET"}}, "invalid"),
            (403, {"error": {"status": "PERMISSION_DENIED", "message": "SECRET forbidden"}}, "forbidden"),
            (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "SECRET quota"}}, "quota"),
            (402, {"error": {"status": "FAILED_PRECONDITION", "message": "billing credit required SECRET"}}, "billing"),
            (404, {"error": {"status": "NOT_FOUND", "message": "model not found SECRET"}}, "model"),
            (503, {"error": {"status": "UNAVAILABLE", "message": "SECRET unavailable"}}, "unavailable"),
            (400, {"error": {"status": "INVALID_ARGUMENT", "message": "SECRET bad"}}, "rejected"),
        ]
        for status, payload, expected in cases:
            provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport([(status, payload)]))
            with self.subTest(status=status):
                with self.assertRaises(ai_gemini.GeminiProviderError) as error:
                    await provider.generate(request, "gemini-default")
                self.assertIn(expected, str(error.exception).lower())
                self.assertNotIn("SECRET", str(error.exception))

        provider = ai_gemini.GeminiProvider(self.make_store(ai_platform), transport=FakeTransport(error=socket.timeout()))
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "network"):
            await provider.generate(request, "gemini-default")

        oversized = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, b"x" * 10)]),
            max_response_bytes=5,
        )
        with self.assertRaises(ai_gemini.GeminiProviderError):
            await oversized.generate(request, "gemini-default")

    async def test_test_connection_smoke_shape_and_status_mapping(self):
        ai_platform, ai_gemini = load_modules()
        request = ai_gemini.build_gemini_smoke_request()
        self.assertEqual(request.model_id, "gemini-3.8-flash")
        self.assertIn("KAIRO_GEMINI_OK", request.messages[0].content)
        self.assertEqual(request.options["reasoning_effort"], "low")
        self.assertNotIn("max_output_tokens", request.options)

        provider = ai_gemini.GeminiProvider(
            self.make_store(ai_platform),
            transport=FakeTransport([(200, self.success_payload(text="KAIRO_GEMINI_OK"))]),
        )
        availability = await provider.test_connection("gemini-default")
        self.assertEqual(availability.state, ai_platform.AvailabilityState.AVAILABLE)

        cases = [
            (401, ai_platform.AvailabilityState.CREDENTIAL_INVALID),
            (403, ai_platform.AvailabilityState.ACCESS_FORBIDDEN),
            (429, ai_platform.AvailabilityState.UNAVAILABLE),
        ]
        for status, state in cases:
            provider = ai_gemini.GeminiProvider(
                self.make_store(ai_platform),
                transport=FakeTransport([(status, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "raw SECRET"}})]),
            )
            with self.subTest(status=status):
                availability = await provider.test_connection("gemini-default")
                self.assertEqual(availability.state, state)
                self.assertNotIn("SECRET", availability.message)


class GeminiRetryTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, transport, retry_delays=(3.0, 8.0)):
        ai_platform, ai_gemini = load_modules()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = ai_platform.CredentialStore(Path(temp_dir.name) / "secrets" / "ai")
        store.write_secret("gemini", "gemini-default", "GEMINI_SECRET")
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        provider = ai_gemini.GeminiProvider(store, transport=transport, retry_delays=retry_delays, sleep=fake_sleep)
        request = ai_platform.AIRequest(
            model_id="gemini-3.5-flash-lite",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        return ai_gemini, provider, request, sleeps

    @staticmethod
    def ok_payload():
        return {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}

    @staticmethod
    def quota(delay):
        return {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}],
            }
        }

    async def test_retry_info_delay_is_honoured_then_request_succeeds(self):
        transport = FakeTransport([(429, self.quota("4s")), (200, self.ok_payload())])
        _, provider, request, sleeps = self.make_provider(transport)

        response = await provider.generate(request, "gemini-default")

        self.assertEqual(response.content, "done")
        self.assertEqual(sleeps, [4.5])

    async def test_long_retry_delay_and_client_errors_are_not_retried(self):
        for status, payload in ((429, self.quota("3600s")), (400, {"error": {"status": "INVALID_ARGUMENT"}})):
            transport = FakeTransport([(status, payload)])
            ai_gemini, provider, request, sleeps = self.make_provider(transport)
            with self.subTest(status=status):
                with self.assertRaises(ai_gemini.GeminiProviderError):
                    await provider.generate(request, "gemini-default")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(sleeps, [])

    async def test_server_and_network_errors_retry_with_configured_delays(self):
        transport = FakeTransport([(503, {}), (500, {}), (200, self.ok_payload())])
        _, provider, request, sleeps = self.make_provider(transport)
        response = await provider.generate(request, "gemini-default")
        self.assertEqual(response.content, "done")
        self.assertEqual(sleeps, [3.0, 8.0])

        network = FakeTransport(error=socket.timeout())
        ai_gemini, provider, request, sleeps = self.make_provider(network)
        with self.assertRaisesRegex(ai_gemini.GeminiProviderError, "network"):
            await provider.generate(request, "gemini-default")
        self.assertEqual(len(network.calls), 3)


class DependencyTests(unittest.TestCase):
    def test_no_provider_sdk_dependency_added(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("google-genai", "google-generativeai", "httpx", "requests"):
            with self.subTest(package=package):
                self.assertNotIn(package, requirements)


if __name__ == "__main__":
    unittest.main()
