import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_ai_platform():
    sys.path.insert(0, str(CORE_ROOT))
    sys.modules.pop("ai_platform", None)
    return importlib.import_module("ai_platform")


class FakeProvider:
    def __init__(self, ai_platform, provider_id="fake", state=None, fail_local=False):
        self.ai_platform = ai_platform
        self.local_availability_calls = []
        self.connection_tests = []
        self.generate_calls = []
        self.fail_local = fail_local
        self._metadata = ai_platform.ProviderMetadata(
            provider_id=provider_id,
            display_name="Fake Provider",
            models=(
                ai_platform.ProviderModel(
                    model_id="fake-model",
                    display_name="Fake Model",
                    recommended_task_classes=(ai_platform.TaskClass.ROUTINE,),
                    supports_tool_calls=True,
                ),
            ),
        )
        self._state = state or ai_platform.AvailabilityState.AVAILABLE

    @property
    def metadata(self):
        return self._metadata

    def get_local_availability(self, *, credential_ref=None, credential_available=False):
        self.local_availability_calls.append((credential_ref, credential_available))
        if self.fail_local:
            raise RuntimeError("secret value should not leak")
        return self.ai_platform.Availability(self._state, self._state.value)

    async def test_connection(self, credential_ref=None):
        self.connection_tests.append(credential_ref)
        return self.ai_platform.Availability(self.ai_platform.AvailabilityState.AVAILABLE, "explicit check only")

    async def generate(self, request, credential_ref=None):
        self.generate_calls.append((request, credential_ref))
        return self.ai_platform.AIResponse(content="not called by routing")


class AIPlatformFoundationTests(unittest.TestCase):
    def credential_store(self, ai_platform, temp_dir):
        return ai_platform.CredentialStore(Path(temp_dir) / "data" / "secrets" / "ai")

    def test_zero_providers_and_empty_registry_are_valid(self):
        ai_platform = load_ai_platform()
        registry = ai_platform.ProviderRegistry()
        profiles = ai_platform.AIProfileStore()
        routing = ai_platform.RoutingConfig()

        self.assertEqual(len(registry), 0)
        decision = ai_platform.route_request(
            task_class=ai_platform.TaskClass.ROUTINE,
            routing=routing,
            profiles=profiles,
            providers=registry,
        )
        self.assertEqual(decision.availability.state, ai_platform.AvailabilityState.NOT_CONFIGURED)

    def test_direct_requires_no_provider(self):
        ai_platform = load_ai_platform()
        decision = ai_platform.route_request(
            task_class=ai_platform.TaskClass.DIRECT,
            routing=ai_platform.RoutingConfig(routine_profile_id="missing"),
            profiles=ai_platform.AIProfileStore(),
            providers=ai_platform.ProviderRegistry(),
        )
        self.assertTrue(decision.availability.ok)
        self.assertFalse(decision.uses_ai)
        self.assertIsNone(decision.profile)

    def test_routing_config_maps_task_classes_without_provider_hardcoding(self):
        ai_platform = load_ai_platform()
        routing = ai_platform.RoutingConfig(
            routine_profile_id="profile-routine",
            planner_profile_id="profile-planner",
            creative_profile_id="profile-creative",
        )
        self.assertEqual(routing.profile_for(ai_platform.TaskClass.ROUTINE), "profile-routine")
        self.assertEqual(routing.profile_for(ai_platform.TaskClass.PLANNER), "profile-planner")
        self.assertEqual(routing.profile_for(ai_platform.TaskClass.CREATIVE), "profile-creative")
        self.assertIsNone(routing.profile_for(ai_platform.TaskClass.DIRECT))

    def test_manual_profile_override_wins_and_uses_local_availability_only(self):
        ai_platform = load_ai_platform()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.credential_store(ai_platform, temp_dir)
            store.write_secret("provider-b", "cred-b", "LOCAL_SECRET")
            provider = FakeProvider(ai_platform, provider_id="provider-b")
            registry = ai_platform.ProviderRegistry({"provider-b": provider})
            profiles = ai_platform.AIProfileStore(
                {
                    "routine-default": ai_platform.AIProfile(
                        profile_id="routine-default",
                        provider_id="missing-provider",
                        model_id="model-a",
                        credential_ref="cred-a",
                    ),
                    "manual": ai_platform.AIProfile(
                        profile_id="manual",
                        provider_id="provider-b",
                        model_id="model-b",
                        credential_ref="cred-b",
                    ),
                }
            )
            decision = ai_platform.route_request(
                task_class=ai_platform.TaskClass.ROUTINE,
                routing=ai_platform.RoutingConfig(routine_profile_id="routine-default"),
                profiles=profiles,
                providers=registry,
                manual_profile_id="manual",
                credential_store=store,
            )
            self.assertTrue(decision.availability.ok)
            self.assertTrue(decision.manual_override)
            self.assertEqual(decision.profile.profile_id, "manual")
            self.assertEqual(provider.local_availability_calls, [("cred-b", True)])
            self.assertEqual(provider.connection_tests, [])
            self.assertEqual(provider.generate_calls, [])

    def test_missing_profile_provider_disabled_and_credential_are_contained(self):
        ai_platform = load_ai_platform()
        disabled = ai_platform.AIProfile(
            profile_id="disabled",
            provider_id="fake",
            model_id="fake-model",
            enabled=False,
        )
        missing_provider = ai_platform.AIProfile(
            profile_id="missing-provider",
            provider_id="missing",
            model_id="fake-model",
        )
        missing_credential = ai_platform.AIProfile(
            profile_id="missing-credential",
            provider_id="fake",
            model_id="fake-model",
            credential_ref="local-ref",
        )
        profiles = ai_platform.AIProfileStore(
            {
                "disabled": disabled,
                "missing-provider": missing_provider,
                "missing-credential": missing_credential,
            }
        )
        registry = ai_platform.ProviderRegistry({"fake": FakeProvider(ai_platform)})

        missing_profile_decision = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="missing-profile"),
            profiles=profiles,
            providers=registry,
        )
        self.assertEqual(missing_profile_decision.availability.state, ai_platform.AvailabilityState.NOT_CONFIGURED)

        missing_provider_decision = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="missing-provider"),
            profiles=profiles,
            providers=registry,
        )
        self.assertEqual(missing_provider_decision.availability.state, ai_platform.AvailabilityState.PROVIDER_MISSING)

        disabled_decision = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="disabled"),
            profiles=profiles,
            providers=registry,
        )
        self.assertEqual(disabled_decision.availability.state, ai_platform.AvailabilityState.DISABLED)

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_credential_decision = ai_platform.route_request(
                task_class="ROUTINE",
                routing=ai_platform.RoutingConfig(routine_profile_id="missing-credential"),
                profiles=profiles,
                providers=registry,
                credential_store=self.credential_store(ai_platform, temp_dir),
            )
        self.assertEqual(
            missing_credential_decision.availability.state,
            ai_platform.AvailabilityState.CREDENTIAL_MISSING,
        )
        self.assertEqual(ai_platform.AvailabilityState.CREDENTIAL_INVALID.value, "CREDENTIAL_INVALID")

    def test_settings_store_persists_profiles_without_secrets(self):
        ai_platform = load_ai_platform()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "config" / "ai.json"
            store = ai_platform.AISettingsStore(settings_path)
            settings = ai_platform.default_groq_settings()
            store.save(settings)
            saved_text = settings_path.read_text(encoding="utf-8")
            self.assertIn("groq-default", saved_text)
            self.assertIn("openai/gpt-oss-120b", saved_text)
            self.assertNotIn("SECRET", saved_text)
            loaded = store.load()
            self.assertEqual(loaded.profiles[0].profile_id, "groq-default")
            self.assertEqual(loaded.profiles[0].options["reasoning_effort"], "medium")
            self.assertFalse(any(settings_path.parent.glob("*.tmp")))

            settings_path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ai_platform.AIPlatformError):
                store.load()
            self.assertEqual(settings_path.read_text(encoding="utf-8"), "{bad json")

    def test_credential_reference_public_data_never_exposes_secret(self):
        ai_platform = load_ai_platform()
        credential = ai_platform.CredentialReference(
            credential_ref="groq-default",
            provider_id="groq",
            display_name="Workstation Groq",
        )
        profile = ai_platform.AIProfile(
            profile_id="routine",
            provider_id="groq",
            model_id="llama",
            credential_ref=credential.credential_ref,
            options={"temperature": 0},
        )
        public_profile = profile.public_dict()
        public_credential = credential.public_dict()
        self.assertEqual(public_profile["credential_ref"], "groq-default")
        self.assertNotIn("LOCAL_SECRET", str(public_profile))
        self.assertNotIn("api_key", str(public_profile).lower())
        self.assertNotIn("LOCAL_SECRET", str(public_credential))

    def test_credential_store_path_containment_traversal_and_atomic_read(self):
        ai_platform = load_ai_platform()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.credential_store(ai_platform, temp_dir)
            store.write_secret("groq", "default", "SECRET_ONE")
            path = store.path_for("groq", "default")
            self.assertTrue(path.is_file())
            self.assertTrue(path.is_relative_to(store.root))
            self.assertTrue(store.exists("groq", "default"))
            self.assertEqual(store.read_secret("groq", "default"), "SECRET_ONE")
            self.assertFalse(any(path.parent.glob("*.tmp")))

            for bad_value in ("..", "../x", "x/y", "x\\y", "C:evil", ""):
                with self.subTest(bad_value=bad_value):
                    with self.assertRaises(ValueError):
                        store.path_for("groq", bad_value)

    def test_same_credential_ref_resolves_independently_per_store(self):
        ai_platform = load_ai_platform()
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = self.credential_store(ai_platform, first_dir)
            second = self.credential_store(ai_platform, second_dir)
            first.write_secret("groq", "default", "FIRST")
            second.write_secret("groq", "default", "SECOND")
            self.assertEqual(first.read_secret("groq", "default"), "FIRST")
            self.assertEqual(second.read_secret("groq", "default"), "SECOND")

    def test_registry_construction_and_lazy_factory_do_not_test_connection(self):
        ai_platform = load_ai_platform()
        provider = FakeProvider(ai_platform)
        registry = ai_platform.ProviderRegistry({"fake": provider})
        self.assertEqual(len(registry), 1)
        self.assertEqual(provider.connection_tests, [])
        self.assertEqual(provider.local_availability_calls, [])

        lazy = ai_platform.LazyProviderRegistry()
        factory_calls = []

        def factory():
            factory_calls.append("called")
            return FakeProvider(ai_platform, provider_id="lazy")

        lazy.register_factory("lazy", factory)
        self.assertEqual(factory_calls, [])
        self.assertEqual(lazy.get("missing"), None)
        self.assertEqual(factory_calls, [])
        self.assertEqual(lazy.get("lazy").metadata.provider_id, "lazy")
        self.assertEqual(factory_calls, ["called"])

    def test_lazy_factory_failures_and_id_mismatch_are_contained(self):
        ai_platform = load_ai_platform()
        lazy = ai_platform.LazyProviderRegistry()
        lazy.register_factory("broken", lambda: (_ for _ in ()).throw(ImportError("missing optional sdk")))
        lazy.register_factory("groq", lambda: FakeProvider(ai_platform, provider_id="not-groq"))
        profiles = ai_platform.AIProfileStore(
            {
                "broken-profile": ai_platform.AIProfile("broken-profile", "broken", "model"),
                "mismatch-profile": ai_platform.AIProfile("mismatch-profile", "groq", "model"),
            }
        )

        broken = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="broken-profile"),
            profiles=profiles,
            providers=lazy,
        )
        mismatch = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="mismatch-profile"),
            profiles=profiles,
            providers=lazy,
        )
        self.assertEqual(broken.availability.state, ai_platform.AvailabilityState.PROVIDER_MISSING)
        self.assertEqual(mismatch.availability.state, ai_platform.AvailabilityState.PROVIDER_MISSING)

    def test_local_availability_exception_is_sanitized_unavailable(self):
        ai_platform = load_ai_platform()
        provider = FakeProvider(ai_platform, fail_local=True)
        decision = ai_platform.route_request(
            task_class="ROUTINE",
            routing=ai_platform.RoutingConfig(routine_profile_id="profile"),
            profiles=ai_platform.AIProfileStore(
                {"profile": ai_platform.AIProfile("profile", "fake", "fake-model")}
            ),
            providers=ai_platform.ProviderRegistry({"fake": provider}),
        )
        self.assertEqual(decision.availability.state, ai_platform.AvailabilityState.UNAVAILABLE)
        self.assertNotIn("secret value", decision.availability.message)

    def test_provider_neutral_request_response_are_json_safe(self):
        ai_platform = load_ai_platform()
        message = ai_platform.AIMessage(role="user", content="hello", metadata={"source": "test"})
        tool_schema = {
            "name": "send_message",
            "arguments": {
                "type": "object",
                "properties": {"channel_id": {"type": "string"}},
                "required": ["channel_id"],
                "additionalProperties": False,
            },
        }
        request = ai_platform.AIRequest(
            model_id="model",
            messages=(message,),
            tools=(tool_schema,),
            options={"temperature": 0},
        )
        tool_call = ai_platform.AIToolCall(
            call_id="call-1",
            tool_name="send_message",
            arguments={"channel_id": "123", "content": "hi"},
        )
        response = ai_platform.AIResponse(
            content="plan",
            tool_calls=(tool_call,),
            finish_reason="tool_calls",
            metadata={"provider": "fake"},
        )

        json.dumps(request.public_dict())
        json.dumps(response.public_dict())
        self.assertEqual(response.public_dict()["tool_calls"][0]["arguments"]["channel_id"], "123")
        with self.assertRaises(ValueError):
            ai_platform.AIToolCall(tool_name="send_message", arguments={"bad": object()})

    def test_tool_message_contract_invariants(self):
        ai_platform = load_ai_platform()
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="tool", content="{}")
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="user", content="hello", tool_call_id="call-1")
        with self.assertRaisesRegex(ValueError, "tool_calls"):
            ai_platform.AIMessage(
                role="user",
                content="hello",
                tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="send_message", arguments={}),),
            )
        with self.assertRaisesRegex(ValueError, "call_id"):
            ai_platform.AIMessage(
                role="assistant",
                content="",
                tool_calls=(ai_platform.AIToolCall(tool_name="send_message", arguments={}),),
            )
        message = ai_platform.AIMessage(
            role="assistant",
            content="",
            tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="send_message", arguments={}),),
        )
        self.assertEqual(message.public_dict()["tool_calls"][0]["id"], "call-1")

    def test_async_provider_methods_exist_but_routing_does_not_call_them(self):
        ai_platform = load_ai_platform()
        provider = FakeProvider(ai_platform)
        request = ai_platform.AIRequest(
            model_id="fake-model",
            messages=(ai_platform.AIMessage(role="user", content="hello"),),
        )
        self.assertTrue(asyncio.iscoroutinefunction(provider.test_connection))
        self.assertTrue(asyncio.iscoroutinefunction(provider.generate))
        connection = asyncio.run(provider.test_connection("credential"))
        response = asyncio.run(provider.generate(request, "credential"))
        self.assertTrue(connection.ok)
        self.assertEqual(response.content, "not called by routing")

    def test_compare_contract_requires_two_distinct_profiles(self):
        ai_platform = load_ai_platform()
        request = ai_platform.ComparePlanRequest(
            profile_ids=("a", "b"),
            tool_risk=ai_platform.ToolRisk.DESTRUCTIVE,
        )
        self.assertEqual(request.profile_ids, ("a", "b"))
        self.assertEqual(request.tool_risk, ai_platform.ToolRisk.DESTRUCTIVE)
        with self.assertRaises(ValueError):
            ai_platform.ComparePlanRequest(profile_ids=("a",), tool_risk=ai_platform.ToolRisk.READ)
        with self.assertRaises(ValueError):
            ai_platform.ComparePlanRequest(profile_ids=("a", "a"), tool_risk=ai_platform.ToolRisk.READ)


if __name__ == "__main__":
    unittest.main()
