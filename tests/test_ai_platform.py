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
        self.assertEqual(ai_platform.AvailabilityState.ACCESS_FORBIDDEN.value, "ACCESS_FORBIDDEN")

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
        metadata = {"gemini": {"thought_signature": "opaque", "nested": ["a", {"b": 1}]}}
        tool_call = ai_platform.AIToolCall(
            call_id="call-1",
            tool_name="send_message",
            arguments={"channel_id": "123", "content": "hi"},
            metadata=metadata,
        )
        metadata["gemini"]["thought_signature"] = "mutated"
        response = ai_platform.AIResponse(
            content="plan",
            tool_calls=(tool_call,),
            finish_reason="tool_calls",
            metadata={"provider": "fake"},
        )

        json.dumps(request.public_dict())
        json.dumps(response.public_dict())
        self.assertEqual(response.public_dict()["tool_calls"][0]["arguments"]["channel_id"], "123")
        self.assertEqual(response.public_dict()["tool_calls"][0]["metadata"]["gemini"]["thought_signature"], "opaque")
        self.assertEqual(response.public_dict()["tool_calls"][0]["metadata"]["gemini"]["nested"], ["a", {"b": 1}])
        self.assertNotIn("SECRET", str(response.public_dict()))
        with self.assertRaises(TypeError):
            response.tool_calls[0].metadata["gemini"] = {}
        with self.assertRaises(TypeError):
            response.tool_calls[0].metadata["gemini"]["thought_signature"] = "mutated"
        with self.assertRaises(TypeError):
            response.tool_calls[0].metadata["gemini"]["nested"][1]["b"] = 2
        public_copy = response.tool_calls[0].public_dict()
        public_copy["metadata"]["gemini"]["nested"][1]["b"] = 99
        self.assertEqual(response.tool_calls[0].metadata["gemini"]["nested"][1]["b"], 1)
        with self.assertRaises(ValueError):
            ai_platform.AIToolCall(tool_name="send_message", arguments={"bad": object()})
        with self.assertRaises(ValueError):
            ai_platform.AIToolCall(tool_name="send_message", arguments={}, metadata={"bad": object()})

    def test_tool_message_contract_invariants(self):
        ai_platform = load_ai_platform()
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="tool", content="{}")
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="user", content="hello", tool_call_id="call-1")
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="system", content="hello", tool_call_id="call-1")
        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            ai_platform.AIMessage(role="assistant", content="hello", tool_call_id="call-1")
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

    def test_message_and_response_metadata_are_recursively_immutable(self):
        ai_platform = load_ai_platform()
        metadata = {"gemini": {"visible_text_parts": [{"text": "A", "thought_signature": None}]}}
        message = ai_platform.AIMessage(role="assistant", content="A", metadata=metadata)
        metadata["gemini"]["visible_text_parts"][0]["text"] = "mutated"
        self.assertEqual(message.metadata["gemini"]["visible_text_parts"][0]["text"], "A")
        with self.assertRaises(TypeError):
            message.metadata["gemini"]["visible_text_parts"][0]["text"] = "mutated"
        public_message = message.public_dict()
        public_message["metadata"]["gemini"]["visible_text_parts"][0]["text"] = "changed"
        self.assertEqual(message.metadata["gemini"]["visible_text_parts"][0]["text"], "A")
        json.dumps(message.public_dict())

        response_metadata = {"gemini": {"nested": ["x", {"y": 1}]}}
        response = ai_platform.AIResponse(content="ok", metadata=response_metadata)
        response_metadata["gemini"]["nested"][1]["y"] = 2
        self.assertEqual(response.metadata["gemini"]["nested"][1]["y"], 1)
        with self.assertRaises(TypeError):
            response.metadata["gemini"]["nested"][1]["y"] = 3
        public_response = response.public_dict()
        public_response["metadata"]["gemini"]["nested"][1]["y"] = 4
        self.assertEqual(response.metadata["gemini"]["nested"][1]["y"], 1)
        json.dumps(response.public_dict())

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

    def test_compare_contract_is_bounded_immutable_and_typed(self):
        ai_platform = load_ai_platform()
        maximum = ai_platform.MAX_COMPARE_PROFILES
        self.assertEqual(maximum, 4)

        source_ids = ["pa", "pb"]
        request = ai_platform.ComparePlanRequest(source_ids, ai_platform.ToolRisk.READ)
        source_ids.append("pc")
        source_ids[0] = "zz"
        self.assertEqual(request.profile_ids, ("pa", "pb"))
        self.assertIs(type(request.profile_ids), tuple)

        at_max = ai_platform.ComparePlanRequest(tuple(f"p{i}" for i in range(maximum)), ai_platform.ToolRisk.NORMAL)
        self.assertEqual(len(at_max.profile_ids), maximum)
        with self.assertRaises(ValueError):
            ai_platform.ComparePlanRequest(tuple(f"p{i}" for i in range(maximum + 1)), ai_platform.ToolRisk.READ)
        with self.assertRaises(ValueError):
            ai_platform.ComparePlanRequest(["a", "b", "a"], ai_platform.ToolRisk.READ)
        for invalid_ids in ("ab", ("a", ""), ("a", "b c"), ("a", 5), None, 7):
            with self.subTest(profile_ids=invalid_ids), self.assertRaises(ValueError):
                ai_platform.ComparePlanRequest(invalid_ids, ai_platform.ToolRisk.READ)

        self.assertIs(ai_platform.ComparePlanRequest(("a", "b"), "DESTRUCTIVE").tool_risk, ai_platform.ToolRisk.DESTRUCTIVE)
        for invalid_risk in ("destructive-ish", "", None, 1, object()):
            with self.subTest(tool_risk=invalid_risk), self.assertRaises(ValueError):
                ai_platform.ComparePlanRequest(("a", "b"), invalid_risk)

        first = ai_platform.ComparePlanRequest(["a", "b"], "READ")
        second = ai_platform.ComparePlanRequest(("a", "b"), ai_platform.ToolRisk.READ)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_routing_config_fallback_fields_are_validated_and_json_safe(self):
        ai_platform = load_ai_platform()
        routing = ai_platform.RoutingConfig(
            routine_profile_id="routine",
            routine_fallback_profile_ids=("fallback-1", "fallback-2"),
            planner_fallback_profile_ids=("planner-fallback",),
        )
        self.assertEqual(routing.fallback_profiles_for(ai_platform.TaskClass.ROUTINE), ("fallback-1", "fallback-2"))
        self.assertEqual(routing.fallback_profiles_for(ai_platform.TaskClass.PLANNER), ("planner-fallback",))
        self.assertEqual(routing.fallback_profiles_for(ai_platform.TaskClass.CREATIVE), ())
        self.assertEqual(routing.fallback_profiles_for(ai_platform.TaskClass.DIRECT), ())

        public = ai_platform.AISettings(routing=routing).public_dict()
        json.dumps(public)
        self.assertEqual(public["routing"]["routine_fallback_profile_ids"], ["fallback-1", "fallback-2"])

        with self.assertRaises(ValueError):
            ai_platform.RoutingConfig(routine_fallback_profile_ids=("dup", "dup"))
        with self.assertRaises(ValueError):
            ai_platform.RoutingConfig(routine_fallback_profile_ids=("bad id",))
        with self.assertRaises(ValueError):
            ai_platform.RoutingConfig(routine_fallback_profile_ids="not-array")

    def test_ai_settings_store_loads_old_schema_and_round_trips_fallback_fields(self):
        ai_platform = load_ai_platform()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "ai.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": [
                            {
                                "profile_id": "routine",
                                "provider_id": "fake",
                                "model_id": "fake-model",
                                "credential_ref": None,
                                "options": {},
                                "enabled": True,
                            }
                        ],
                        "routing": {"routine_profile_id": "routine"},
                    }
                ),
                encoding="utf-8",
            )
            store = ai_platform.AISettingsStore(settings_path)
            loaded = store.load()
            self.assertEqual(loaded.routing.routine_fallback_profile_ids, ())

            updated = ai_platform.AISettings(
                profiles=loaded.profiles,
                routing=ai_platform.RoutingConfig(
                    routine_profile_id="routine",
                    routine_fallback_profile_ids=("fallback",),
                    planner_fallback_profile_ids=("planner",),
                    creative_fallback_profile_ids=("creative",),
                ),
            )
            store.save(updated)
            reloaded = store.load()
            self.assertEqual(reloaded.routing.routine_fallback_profile_ids, ("fallback",))
            self.assertEqual(reloaded.routing.planner_fallback_profile_ids, ("planner",))
            self.assertEqual(reloaded.routing.creative_fallback_profile_ids, ("creative",))

            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            raw["routing"]["routine_fallback_profile_ids"] = "bad"
            settings_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ai_platform.AIPlatformError):
                store.load()


if __name__ == "__main__":
    unittest.main()
