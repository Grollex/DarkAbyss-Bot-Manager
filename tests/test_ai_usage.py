"""AI usage statistics: only provider-reported data, stored per bot instance."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

PATH_MODULES = (
    "ai_usage",
    "ai_storage",
    "ai_connections",
    "ai_providers",
    "ai_orchestrator",
    "ai_platform",
    "ai_groq",
    "ai_gemini",
    "config_store",
    "instance_store",
    "bot_registry",
    "app_paths",
)
KEY_A = "test-key-usage-a"
KEY_B = "test-key-usage-b"
PROMPT = "SECRET_PROMPT_TEXT_must_not_be_stored"
ANSWER = "SECRET_ANSWER_TEXT_must_not_be_stored"
GROQ_HEADERS = {
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "6100",
    "x-ratelimit-reset-tokens": "7.66s",
}


def groq_body(prompt_tokens=34000, completion_tokens=6800, total_tokens=40800):
    return json.dumps(
        {
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens},
        }
    ).encode("utf-8")


def gemini_body():
    return json.dumps(
        {
            "candidates": [{"content": {"parts": [{"text": ANSWER}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 19000, "candidatesTokenCount": 5000, "thoughtsTokenCount": 3000, "totalTokenCount": 27000},
            "modelVersion": "gemini-3.8-flash",
        }
    ).encode("utf-8")


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers_seen = []

    def post_json(self, *, url, headers, body, timeout_seconds, max_response_bytes):
        self.headers_seen.append(dict(headers))
        return self.responses.pop(0)


class UsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        os.environ["DARKABYSS_DATA_DIR"] = str(self.data_root)
        for name in PATH_MODULES:
            sys.modules.pop(name, None)
        self.ai_usage = importlib.import_module("ai_usage")
        self.ai_platform = importlib.import_module("ai_platform")
        self.ai_groq = importlib.import_module("ai_groq")
        self.ai_gemini = importlib.import_module("ai_gemini")
        self.ai_storage = importlib.import_module("ai_storage")
        self.ai_orchestrator = importlib.import_module("ai_orchestrator")
        self.instance_store = importlib.import_module("instance_store")
        self.admin = self.instance_store.create_instance("admin", "admin-main")
        self.presence = self.instance_store.create_instance("game_presence", "gp-main")
        self.names = {"openai/gpt-oss-120b": "GPT-OSS 120B", "gemini-3.8-flash": "Gemini 3.8 Flash"}

    def stores(self, instance_id):
        return self.ai_storage.for_instance_id(instance_id)

    def request(self, model_id):
        return self.ai_platform.AIRequest(
            model_id=model_id,
            messages=(self.ai_platform.AIMessage(role=self.ai_platform.MessageRole.USER, content=PROMPT),),
        )

    def provider_from_registry(self, stores, provider_id, transport):
        """The production path: AIOrchestrator(stores=...) -> shared adapter bound to this instance."""
        orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores)
        provider = orchestrator._providers.get(provider_id)
        provider._transport = transport
        provider._retry_delays = ()
        return provider

    # -- what is recorded ------------------------------------------------------------

    def test_groq_usage_and_rate_limit_headers_are_recorded_per_instance(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        transport = FakeTransport((200, groq_body(), GROQ_HEADERS))
        provider = self.provider_from_registry(admin, "groq", transport)
        asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        self.assertIn(f"Bearer {KEY_A}", transport.headers_seen[0]["Authorization"])

        text = self.ai_usage.provider_usage_text(admin.usage, "groq", self.names)
        self.assertEqual(text, "GPT-OSS 120B · 1 req · 41k tok\n34k in / 6.8k out · TPM 6.1k left")
        self.assertEqual(admin.usage.path, (self.admin.paths.data_dir / "ai_usage.json").resolve())
        self.assertFalse(self.stores("gp-main").usage.path.exists())  # another bot's statistics untouched

        stored = admin.usage.path.read_text(encoding="utf-8")
        for secret in (PROMPT, ANSWER, KEY_A):
            self.assertNotIn(secret, stored)

    def test_gemini_usage_metadata_with_thinking_and_no_invented_limits(self):
        presence = self.stores("gp-main")
        presence.credentials.write_secret("gemini", "gemini-default", KEY_B)
        transport = FakeTransport((200, gemini_body()))
        provider = self.provider_from_registry(presence, "gemini", transport)
        asyncio.run(provider.generate(self.request("gemini-3.8-flash"), "gemini-default"))
        self.assertEqual(transport.headers_seen[0]["x-goog-api-key"], KEY_B)
        text = self.ai_usage.provider_usage_text(presence.usage, "gemini", self.names)
        self.assertEqual(text, "Gemini 3.8 Flash · 1 req · 27k tok\n19k in / 5k out / 3k think")
        self.assertNotIn("left", text)
        self.assertNotIn("%", text)
        self.assertEqual(self.ai_usage.provider_usage_text(self.stores("admin-main").usage, "gemini"), "No usage yet")

    def test_requests_aggregate_per_provider_model_and_day(self):
        store = self.stores("admin-main").usage
        now = time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))
        for _ in range(3):
            store.record(self.ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", input_tokens=1000, output_tokens=200, total_tokens=1200), now)
        store.record(self.ai_usage.UsageEvent("groq", "other-model", input_tokens=5, output_tokens=5, total_tokens=10), now)
        rows = store.day_usage("groq", now)
        self.assertEqual([(row.model_id, row.requests, row.total_tokens) for row in rows], [("openai/gpt-oss-120b", 3, 3600), ("other-model", 1, 10)])
        self.assertEqual(store.day_usage("groq", now + 86400), [])  # a new day starts at zero
        data = store.load()
        self.assertEqual(list(data["days"]), ["2026-10-02"])

    def test_old_days_are_pruned(self):
        store = self.stores("admin-main").usage
        start = time.mktime((2026, 8, 1, 12, 0, 0, 0, 0, -1))
        store.record(self.ai_usage.UsageEvent("groq", "m", input_tokens=1, output_tokens=1, total_tokens=2), start)
        store.record(self.ai_usage.UsageEvent("groq", "m", input_tokens=1, output_tokens=1, total_tokens=2), start + 40 * 86400)
        self.assertEqual(len(store.load()["days"]), 1)

    def test_final_rate_limit_error_is_a_failed_request_without_tokens(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        headers = {**GROQ_HEADERS, "x-ratelimit-remaining-tokens": "0"}
        provider = self.provider_from_registry(admin, "groq", FakeTransport((429, b'{"error": {"message": "slow down"}}', headers)))
        with self.assertRaises(self.ai_groq.GroqProviderError):
            asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        row = admin.usage.day_usage("groq")[0]
        self.assertEqual((row.requests, row.failed_requests, row.total_tokens), (1, 1, 0))
        self.assertEqual(
            self.ai_usage.provider_usage_text(admin.usage, "groq", self.names),
            "GPT-OSS 120B · 1 req (1 failed) · 0 tok\nTPM 0 left",
        )

    def retrying(self, provider):
        async def no_sleep(_seconds):
            return None

        provider._retry_delays = (0.0, 0.0, 0.0)
        provider._sleep = no_sleep
        return provider

    def test_every_retry_attempt_counts_and_intermediate_429_limits_are_kept(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        throttled = {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "45s", "x-ratelimit-remaining-requests": "500"}
        transport = FakeTransport(
            (429, b'{"error": {"message": "Please try again in 1.5s"}}', throttled),
            (503, b"upstream down", {}),
            (200, groq_body(prompt_tokens=900, completion_tokens=100, total_tokens=1000)),  # no headers
        )
        provider = self.retrying(self.provider_from_registry(admin, "groq", transport))
        asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        row = admin.usage.day_usage("groq")[0]
        self.assertEqual((row.requests, row.failed_requests), (3, 2))
        self.assertEqual((row.input_tokens, row.output_tokens, row.total_tokens), (900, 100, 1000))  # only reported tokens
        self.assertEqual(row.rate_limits["remaining_tokens"], 0)  # snapshot from the intermediate 429
        text = self.ai_usage.provider_usage_text(admin.usage, "groq", self.names)
        self.assertTrue(text.startswith("GPT-OSS 120B · 3 req (2 failed) · 1k tok\n900 in / 100 out · TPM 0 left · RPD 500 left"))

    def test_latest_rate_limit_snapshot_wins(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        transport = FakeTransport(
            (429, b"{}", {"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "45s"}),
            (200, groq_body(), {"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "45s"}),
        )
        provider = self.retrying(self.provider_from_registry(admin, "groq", transport))
        asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        self.assertEqual(admin.usage.day_usage("groq")[0].rate_limits["remaining_tokens"], 7000)

    def test_tool_use_failed_retry_and_network_failures(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)

        class Flaky(FakeTransport):
            def post_json(self, **kwargs):
                item = self.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        transport = Flaky(
            self.ai_groq.GroqNetworkError("no route"),  # nothing received: not a counted request
            (400, b'{"error": {"code": "tool_use_failed"}}', {}),
            (200, groq_body(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        )
        provider = self.retrying(self.provider_from_registry(admin, "groq", transport))
        asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        row = admin.usage.day_usage("groq")[0]
        self.assertEqual((row.requests, row.failed_requests, row.total_tokens), (2, 1, 15))

    def test_tokens_reported_on_a_failed_response_are_counted_none_are_invented(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        reported = json.dumps({"error": {"message": "length"}, "usage": {"prompt_tokens": 40, "completion_tokens": 0, "total_tokens": 40}}).encode("utf-8")
        provider = self.provider_from_registry(admin, "groq", FakeTransport((400, reported, {})))
        with self.assertRaises(self.ai_groq.GroqProviderError):
            asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        row = admin.usage.day_usage("groq")[0]
        self.assertEqual((row.requests, row.failed_requests, row.input_tokens, row.total_tokens), (1, 1, 40, 40))

    def test_gemini_retries_count_and_failed_answers_add_no_tokens(self):
        presence = self.stores("gp-main")
        presence.credentials.write_secret("gemini", "gemini-default", KEY_B)
        transport = FakeTransport(
            (503, b'{"error": {"code": 503, "message": "high demand"}}'),
            (429, b'{"error": {"code": 429}}'),
            (200, gemini_body()),
        )
        provider = self.retrying(self.provider_from_registry(presence, "gemini", transport))
        asyncio.run(provider.generate(self.request("gemini-3.8-flash"), "gemini-default"))
        row = presence.usage.day_usage("gemini")[0]
        self.assertEqual((row.requests, row.failed_requests), (3, 2))
        self.assertEqual((row.input_tokens, row.output_tokens, row.thinking_tokens, row.total_tokens), (19000, 5000, 3000, 27000))
        self.assertEqual(row.rate_limits, {})
        self.assertEqual(
            self.ai_usage.provider_usage_text(presence.usage, "gemini", self.names),
            "Gemini 3.8 Flash · 3 req (2 failed) · 27k tok\n19k in / 5k out / 3k think",
        )

    def test_test_connection_is_a_counted_request(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        smoke = json.dumps(
            {"choices": [{"message": {"content": "KAIRO_GROQ_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}
        ).encode("utf-8")
        provider = self.ai_groq.GroqProvider(admin.credentials, transport=FakeTransport((200, smoke, {})), usage_recorder=admin.usage.recorder())
        availability = asyncio.run(provider.test_connection("groq-default"))
        self.assertTrue(availability.ok)
        row = admin.usage.day_usage("groq")[0]
        self.assertEqual((row.model_id, row.requests, row.total_tokens), ("openai/gpt-oss-120b", 1, 100))

    def test_concurrent_writers_lose_no_request(self):
        import threading

        path = self.stores("admin-main").usage.path
        stores = [self.ai_usage.AIUsageStore(path), self.ai_usage.AIUsageStore(path)]  # like bot + Manager

        def write(store):
            for _ in range(25):
                store.record(self.ai_usage.UsageEvent("groq", "m", total_tokens=2))

        threads = [threading.Thread(target=write, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        row = stores[0].day_usage("groq")[0]
        self.assertEqual((row.requests, row.total_tokens), (50, 100))

    def test_missing_usage_or_headers_are_not_invented(self):
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        body = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode("utf-8")
        provider = self.provider_from_registry(admin, "groq", FakeTransport((200, body)))  # old-style 2-tuple transport
        asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        text = self.ai_usage.provider_usage_text(admin.usage, "groq", self.names)
        self.assertEqual(text, "GPT-OSS 120B · 1 req · 0 tok")
        self.assertNotIn("left", text)

    def test_expired_rate_limit_window_is_hidden_and_reset_is_shown(self):
        row = self.ai_usage.ModelUsage(
            "groq", "openai/gpt-oss-120b", 2, 100, 50, 0, 150,
            {"captured_at": 1000.0, "remaining_tokens": 500, "reset_tokens_seconds": 10.0,
             "remaining_requests": 980, "reset_requests_seconds": 3600.0},
        )
        lines = self.ai_usage.usage_lines(row, "GPT-OSS 120B", now=1005.0)
        self.assertIn("TPM 500 left", lines[1])
        self.assertIn("RPD 980 left (resets", lines[1])
        lines = self.ai_usage.usage_lines(row, "GPT-OSS 120B", now=1020.0)
        self.assertNotIn("TPM", lines[1])  # the minute window already reset: old number hidden
        self.assertIn("RPD 980 left", lines[1])
        lines = self.ai_usage.usage_lines(row, "GPT-OSS 120B", now=1000.0 + 7200)
        self.assertEqual(lines, ["GPT-OSS 120B · 2 req · 150 tok", "100 in / 50 out"])

    def test_header_and_number_parsing(self):
        parse = self.ai_usage.parse_duration_seconds
        self.assertAlmostEqual(parse("7.66s"), 7.66)
        self.assertAlmostEqual(parse("750ms"), 0.75)
        self.assertAlmostEqual(parse("2m59.56s"), 179.56)
        self.assertAlmostEqual(parse("1h2m3s"), 3723.0)
        for bad in ("", "soon", "5", "-1s", None, "1s; drop table"):
            self.assertIsNone(parse(bad))
        limits = self.ai_usage.groq_rate_limits({"X-RateLimit-Remaining-Tokens": "12", "x-ratelimit-reset-tokens": "nope", "other": "1"})
        self.assertEqual(limits, {"remaining_tokens": 12})
        number = self.ai_usage.compact_number
        self.assertEqual([number(v) for v in (0, 980, 1000, 6100, 6800, 34000, 40800, 1_250_000)], ["0", "980", "1k", "6.1k", "6.8k", "34k", "41k", "1.2M"])

    def test_urllib_transport_keeps_only_rate_limit_headers(self):
        response = SimpleNamespace(headers={"x-ratelimit-remaining-tokens": "5", "Set-Cookie": "secret", "Authorization": "x"})
        self.assertEqual(self.ai_groq._rate_limit_headers(response), {"x-ratelimit-remaining-tokens": "5"})

    def test_corrupted_usage_file_is_reported_then_set_aside(self):
        store = self.stores("admin-main").usage
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.ai_usage.provider_usage_text(store, "groq"), "Usage data unreadable")
        store.record(self.ai_usage.UsageEvent("groq", "m", input_tokens=1, output_tokens=1, total_tokens=2))
        self.assertEqual(store.day_usage("groq")[0].requests, 1)
        self.assertEqual(len(list(store.path.parent.glob("ai_usage.corrupt-*.json"))), 1)

    def test_no_usage_without_store_or_data(self):
        self.assertEqual(self.ai_usage.provider_usage_text(None, "groq"), "No usage yet")
        self.assertEqual(self.ai_usage.provider_usage_text(self.stores("admin-main").usage, "groq"), "No usage yet")

    def test_recorder_never_breaks_a_request(self):
        def broken(_event):
            raise RuntimeError("disk full")

        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        provider = self.ai_groq.GroqProvider(admin.credentials, transport=FakeTransport((200, groq_body(), GROQ_HEADERS)), usage_recorder=broken)
        response = asyncio.run(provider.generate(self.request("openai/gpt-oss-120b"), "groq-default"))
        self.assertEqual(response.content, ANSWER)


if __name__ == "__main__":
    unittest.main()
