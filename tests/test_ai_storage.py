"""AI storage: shared provider connections, the base set, each bot's choice, migrations."""

import asyncio
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

PATH_MODULES = (
    "ai_storage",
    "ai_connections",
    "ai_providers",
    "ai_orchestrator",
    "ai_platform",
    "ai_groq",
    "ai_gemini",
    "admin_ai",
    "GamePresence",
    "config_store",
    "instance_store",
    "bot_registry",
    "app_paths",
)
# Obviously fake test values; tests never use real keys.
KEY_A = "test-key-connection-a"
KEY_B = "test-key-connection-b"
KEY_C = "test-key-connection-c"


class CapturingTransport:
    """Records the auth header each request carries; answers like the provider."""

    def __init__(self, body):
        self.body = body
        self.auth = []
        self.urls = []

    def post_json(self, *, url, headers, body, timeout_seconds, max_response_bytes):
        self.auth.append(headers.get("Authorization") or headers.get("x-goog-api-key"))
        self.urls.append(body.get("model") or url)
        return 200, self.body


GROQ_OK = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode("utf-8")
GEMINI_OK = json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}).encode("utf-8")


class RecordingProvider:
    """Provider-neutral fake: records which connection/model each orchestrator used."""

    def __init__(self, ai_platform, provider_id, calls, credentials):
        self._metadata = ai_platform.ProviderMetadata(
            provider_id=provider_id,
            display_name=provider_id,
            models=(ai_platform.ProviderModel("any", "Any", supports_tool_calls=True),),
        )
        self.ai_platform = ai_platform
        self.calls = calls
        self.credentials = credentials

    @property
    def metadata(self):
        return self._metadata

    def get_local_availability(self, *, credential_ref=None, credential_available=False):
        state = self.ai_platform.AvailabilityState.AVAILABLE if credential_available else self.ai_platform.AvailabilityState.CREDENTIAL_MISSING
        return self.ai_platform.Availability(state, "fake")

    async def test_connection(self, credential_ref=None):
        raise AssertionError("not used")

    async def generate(self, request, credential_ref=None):
        self.calls.append((credential_ref, request.model_id))
        return self.ai_platform.AIResponse(content="ok")


class AIStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        os.environ["DARKABYSS_DATA_DIR"] = str(self.data_root)
        for name in PATH_MODULES:
            sys.modules.pop(name, None)
        self.ai_storage = importlib.import_module("ai_storage")
        self.ai_connections = importlib.import_module("ai_connections")
        self.ai_providers = importlib.import_module("ai_providers")
        self.ai_platform = importlib.import_module("ai_platform")
        self.ai_orchestrator = importlib.import_module("ai_orchestrator")
        self.instance_store = importlib.import_module("instance_store")
        self.app_paths = importlib.import_module("app_paths")
        self.admin = self.instance_store.create_instance("admin", "admin-main", "Kairo")
        self.presence = self.instance_store.create_instance("game_presence", "gp-main", "Game Pings")
        self.store = self.ai_connections.ConnectionStore()

    # -- helpers ----------------------------------------------------------------------

    def stores(self, instance_id):
        return self.ai_storage.for_instance_id(instance_id)

    def connection(self, connection_id, provider_id="groq", model="openai/gpt-oss-120b", name=None):
        return self.ai_connections.Connection(connection_id, provider_id, name or connection_id, model, {"reasoning_effort": "medium"})

    def add(self, connection_id, provider_id="groq", model="openai/gpt-oss-120b", key=None):
        self.store.upsert(self.connection(connection_id, provider_id, model), key)

    def route(self, planner=None, executor=None, fallback=None, cross=False):
        return self.ai_connections.RouteSelection(planner, executor, fallback, cross)

    def custom(self, instance_id, **route):
        self.stores(instance_id).selection.save(self.ai_connections.BotSelection("custom", self.route(**route)))

    def standard_setup(self):
        """Base set: Groq main (key A) plans, Gemini main (key B) executes; Groq backup (key C) spare."""
        self.add("groq-main", key=KEY_A)
        self.add("gemini-main", "gemini", "gemini-3.8-flash", key=KEY_B)
        self.add("groq-2", key=KEY_C)
        self.store.set_base(self.route("groq-main", "gemini-main", cross=True))

    def creative_request(self):
        return self.ai_orchestrator.OrchestratorRequest(
            messages=(self.ai_platform.AIMessage(role="user", content="hi"),), task_class="CREATIVE", allowed_tool_names=()
        )

    # -- layout ------------------------------------------------------------------------

    def test_layout_keys_are_per_connection_and_never_in_settings(self):
        self.standard_setup()
        self.assertEqual(self.store.path, self.data_root / "config" / "ai_connections.json")
        key_file = self.data_root / "secrets" / "ai_connections" / "groq" / "groq-main.secret"
        self.assertEqual(key_file.read_text(encoding="utf-8").strip(), KEY_A)
        text = self.store.path.read_text(encoding="utf-8")
        for secret in (KEY_A, KEY_B, KEY_C):
            self.assertNotIn(secret, text)
        stores = self.stores("gp-main")
        self.assertEqual(stores.selection.path, (self.presence.paths.data_dir / "ai_selection.json").resolve())
        self.assertEqual(stores.usage.path, (self.presence.paths.data_dir / "ai_usage.json").resolve())
        self.assertEqual(stores.credentials.root, (self.data_root / "secrets" / "ai_connections").resolve())

    def test_every_bot_uses_the_base_set_by_default(self):
        self.standard_setup()
        for instance_id in ("admin-main", "gp-main"):
            settings = self.stores(instance_id).settings.load()
            self.assertEqual([p.profile_id for p in settings.profiles], ["groq-main", "gemini-main"])
            self.assertEqual(settings.routing.planner_profile_id, "groq-main")
            self.assertEqual(settings.routing.routine_profile_id, "gemini-main")
            self.assertEqual(settings.routing.creative_profile_id, "gemini-main")
            self.assertEqual(settings.routing.routine_fallback_profile_ids, ("groq-main",))  # cross fallback
            self.assertEqual(settings.routing.planner_fallback_profile_ids, ("gemini-main",))
            self.assertTrue(all(p.credential_ref == p.profile_id for p in settings.profiles))
        self.assertFalse(self.stores("gp-main").selection.path.exists())  # default = base set, nothing stored

    def test_own_choice_limits_a_bot_to_its_connections(self):
        self.standard_setup()
        self.custom("gp-main", executor="groq-2")
        settings = self.stores("gp-main").settings.load()
        self.assertEqual([p.profile_id for p in settings.profiles], ["groq-2"])
        self.assertEqual((settings.routing.creative_profile_id, settings.routing.creative_fallback_profile_ids), ("groq-2", ()))
        # The other bot is unaffected.
        self.assertEqual([p.profile_id for p in self.stores("admin-main").settings.load().profiles], ["groq-main", "gemini-main"])
        # Back to the base set, keeping the stored custom route for later.
        selection = self.stores("gp-main").selection
        selection.save(self.ai_connections.BotSelection("base", selection.load().custom))
        self.assertEqual(len(self.stores("gp-main").settings.load().profiles), 2)
        self.assertEqual(selection.load().custom.executor, "groq-2")

    def test_two_connections_of_one_provider_use_their_own_keys(self):
        self.standard_setup()
        self.custom("gp-main", executor="groq-2")
        self.store.set_base(self.route(executor="groq-main"))
        seen = {}
        for instance_id in ("admin-main", "gp-main"):
            stores = self.stores(instance_id)
            orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores)
            provider = orchestrator._providers.get("groq")
            self.assertIsInstance(provider, importlib.import_module("ai_groq").GroqProvider)  # shared implementation
            provider._transport = CapturingTransport(GROQ_OK)
            provider._retry_delays = ()
            result = asyncio.run(orchestrator.orchestrate(self.creative_request()))
            self.assertEqual(result.status, self.ai_orchestrator.OrchestratorStatus.COMPLETED)
            seen[instance_id] = provider._transport.auth
        self.assertEqual(seen, {"admin-main": [f"Bearer {KEY_A}"], "gp-main": [f"Bearer {KEY_C}"]})

    def test_fallbacks_stay_inside_the_bots_selection(self):
        self.standard_setup()
        self.add("groq-nokey")  # selected, but no key saved
        self.custom("gp-main", executor="groq-nokey")
        calls = []
        registry = self.ai_platform.ProviderRegistry(
            {pid: RecordingProvider(self.ai_platform, pid, calls, None) for pid in ("groq", "gemini")}
        )
        result = asyncio.run(self.ai_orchestrator.AIOrchestrator(stores=self.stores("gp-main"), provider_registry=registry).orchestrate(self.creative_request()))
        self.assertNotEqual(result.status, self.ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(calls, [])  # never silently borrows the base set's working keys

        self.custom("gp-main", executor="groq-nokey", fallback="gemini-main")
        result = asyncio.run(self.ai_orchestrator.AIOrchestrator(stores=self.stores("gp-main"), provider_registry=registry).orchestrate(self.creative_request()))
        self.assertEqual(result.status, self.ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(calls, [("gemini-main", "gemini-3.8-flash")])  # its own explicit fallback

    def test_route_selection_routing(self):
        route = self.route("a", "b", "c", cross=True).routing()
        self.assertEqual((route.planner_profile_id, route.routine_profile_id, route.creative_profile_id), ("a", "b", "b"))
        self.assertEqual(route.routine_fallback_profile_ids, ("a", "c"))
        self.assertEqual(route.planner_fallback_profile_ids, ("b", "c"))
        single = self.route(executor="b", fallback="b").routing()
        self.assertEqual((single.planner_profile_id, single.routine_fallback_profile_ids), ("b", ()))
        restricted = self.route("gone", "b", "gone2").restricted_to(["b"])
        self.assertEqual((restricted.planner, restricted.executor, restricted.fallback), (None, "b", None))

    def test_deleted_connection_in_a_choice_fails_closed(self):
        self.standard_setup()
        self.custom("gp-main", executor="groq-2")
        self.store.remove("groq-2")
        settings = self.stores("gp-main").settings.load()
        self.assertEqual(settings.profiles, ())
        self.assertIsNone(settings.routing.routine_profile_id)
        self.assertFalse(self.store.credentials.exists("groq", "groq-2"))  # its key is gone too
        self.assertTrue(self.store.credentials.exists("groq", "groq-main"))

    def test_base_set_connection_cannot_be_removed(self):
        self.standard_setup()
        with self.assertRaises(self.ai_connections.ConnectionsError):
            self.store.remove("groq-main")
        self.assertTrue(self.store.credentials.exists("groq", "groq-main"))

    def test_blank_key_keeps_the_saved_key_and_provider_cannot_change(self):
        self.add("groq-main", key=KEY_A)
        self.store.upsert(self.connection("groq-main", name="Renamed", model="other-model"), "   ")
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-main"), KEY_A)
        self.assertEqual(self.store.load().get("groq-main").name, "Renamed")
        with self.assertRaises(self.ai_connections.ConnectionsError):
            self.store.upsert(self.connection("groq-main", "gemini", "gemini-3.8-flash"))

    def test_corrupted_files_fail_closed_and_are_kept(self):
        self.standard_setup()
        for path, content in (
            (self.store.path, "{broken"),
            (self.stores("gp-main").selection.path, '{"schema_version": 1, "mode": "sideways"}'),
        ):
            with self.subTest(file=path.name):
                original = path.read_text(encoding="utf-8") if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                stores = self.stores("gp-main")
                with self.assertRaises(self.ai_platform.AIPlatformError):
                    stores.settings.load()
                orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores, provider_registry=self.ai_platform.ProviderRegistry())
                result = asyncio.run(orchestrator.orchestrate(self.creative_request()))
                self.assertNotEqual(result.status, self.ai_orchestrator.OrchestratorStatus.COMPLETED)
                self.assertEqual(path.read_text(encoding="utf-8"), content)  # never rewritten
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_validation_and_path_traversal(self):
        for bad in ("../admin-main", "..", "a/b", "A", "", None, "gp-main\\..\\admin-main"):
            with self.subTest(instance=bad), self.assertRaises(Exception):
                self.ai_storage.for_instance_id(bad, must_exist=False)
        forged = SimpleNamespace(id="gp-main", paths=SimpleNamespace(root=self.admin.paths.root))
        with self.assertRaises(self.ai_storage.AIStorageError):
            self.ai_storage.for_instance(forged)
        for bad_id in ("../x", "Groq", "a b", "", "x" * 65):
            with self.subTest(connection=bad_id), self.assertRaises(self.ai_platform.AIPlatformError):
                self.connection(bad_id)
        for kwargs in ({"provider_id": "openai"}, {"name": ""}, {"name": "x" * 61}):
            with self.subTest(**kwargs), self.assertRaises(self.ai_platform.AIPlatformError):
                values = {"connection_id": "ok-1", "provider_id": "groq", "name": "ok", "model_id": "m", **kwargs}
                self.ai_connections.Connection(**values)
        with self.assertRaises(self.ai_platform.AIPlatformError):
            self.ai_connections.Connection("ok-1", "groq", "ok", "m", {"temperature": 3})
        for provider, ref in (("..", "groq-main"), ("groq", "../../token"), ("groq", "a/b")):
            with self.subTest(provider=provider, ref=ref), self.assertRaises(Exception):
                self.store.credentials.path_for(provider, ref)

    # -- orchestrator / provider catalog ---------------------------------------------------------

    def test_orchestrator_requires_and_uses_the_bots_stores(self):
        with self.assertRaises(ValueError):
            self.ai_orchestrator.AIOrchestrator()
        with self.assertRaises(ValueError):
            self.ai_orchestrator.build_default_provider_registry(None)
        stores = self.stores("gp-main")
        orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores)
        self.assertIs(orchestrator._settings_store, stores.settings)
        self.assertIs(orchestrator._credential_store, stores.credentials)
        for provider_id in ("groq", "gemini"):
            self.assertIs(orchestrator._providers.get(provider_id)._credential_store, stores.credentials)

    def test_provider_catalog_is_the_extension_point(self):
        self.assertEqual(self.ai_providers.provider_ids(), ("groq", "gemini"))
        self.assertTrue(self.ai_providers.model_choices("groq"))
        self.assertEqual(self.ai_providers.model_display_name("gemini", "gemini-3.8-flash"), "Gemini 3.8 Flash")
        with self.assertRaises(self.ai_providers.UnknownProviderError):
            self.ai_providers.get_spec("openai")

        # A future provider = an adapter module + one catalog entry; nothing else changes.
        created = []
        ai_platform = self.ai_platform

        class FutureProvider:
            metadata = ai_platform.ProviderMetadata(
                provider_id="future", display_name="Future", models=(ai_platform.ProviderModel("f-1", "Future 1"),)
            )

            def __init__(self, credential_store, *, usage_recorder=None, retry_delays=()):
                created.append((credential_store, usage_recorder, retry_delays))

        module = types.ModuleType("ai_future_test")
        module.FutureProvider = FutureProvider
        module.DEFAULT_RETRY_DELAYS = (1.0,)
        sys.modules["ai_future_test"] = module
        self.addCleanup(sys.modules.pop, "ai_future_test", None)
        spec = self.ai_providers.ProviderSpec("future", "Future", "F", "ai_future_test", "FutureProvider", "f-1", "Paste key", "somewhere")
        self.ai_providers.PROVIDERS["future"] = spec
        self.addCleanup(self.ai_providers.PROVIDERS.pop, "future", None)
        stores = self.stores("admin-main")
        registry = self.ai_orchestrator.build_default_provider_registry(stores.credentials, usage_store=stores.usage)
        self.assertIsInstance(registry.get("future"), FutureProvider)
        self.assertIs(created[0][0], stores.credentials)
        self.assertEqual(created[0][2], (1.0,))
        self.assertIsNotNone(created[0][1])  # usage recorder of this bot
        self.ai_connections.Connection("future-1", "future", "Future main", "f-1")  # accepted by connections

    def test_admin_transport_and_game_presence_use_their_instance(self):
        self.standard_setup()
        self.custom("gp-main", executor="groq-2")
        admin_ai = importlib.import_module("admin_ai")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertIsNone(admin_ai.AITransport(load_config=lambda: {}).get_orchestrator())  # no stores: fail closed
        orchestrator = admin_ai.AITransport(load_config=lambda: {}, ai_stores=self.stores("admin-main")).get_orchestrator()
        self.assertEqual([p.profile_id for p in orchestrator._settings_store.load().profiles], ["groq-main", "gemini-main"])
        presence_ai = importlib.import_module("GamePresence").InstanceAI("gp-main").get()
        self.assertEqual([p.profile_id for p in presence_ai._orchestrator._settings_store.load().profiles], ["groq-2"])

    # -- migration: per-bot settings -> connections ----------------------------------------------

    def write_own(self, instance, profiles, routing, keys):
        settings_store, credentials = self.ai_storage.own_stores(instance)
        settings_store.save(self.ai_platform.AISettings(profiles=tuple(profiles), routing=routing))
        for provider, ref, value in keys:
            credentials.write_secret(provider, ref, value)
        return settings_store, credentials

    def kairo_own_settings(self, instance=None):
        groq = self.ai_platform.AIProfile("groq-default", "groq", "openai/gpt-oss-120b", "groq-default", {"reasoning_effort": "medium"})
        gemini = self.ai_platform.AIProfile("gemini-default", "gemini", "gemini-3.8-flash", "gemini-default", {"reasoning_effort": "low"})
        routing = self.ai_platform.RoutingConfig(
            routine_profile_id="gemini-default",
            planner_profile_id="groq-default",
            creative_profile_id="gemini-default",
            routine_fallback_profile_ids=("groq-default",),
            planner_fallback_profile_ids=("gemini-default",),
        )
        return self.write_own(instance or self.admin, (groq, gemini), routing, (("groq", "groq-default", KEY_A), ("gemini", "gemini-default", KEY_B)))

    def test_single_bots_keys_become_the_base_set(self):
        own_settings, own_credentials = self.kairo_own_settings()
        before = self.ai_storage.for_instance_id("admin-main").settings  # view, not the old file
        result = self.ai_storage.migrate_instances_to_connections()
        self.assertEqual(result.status, "migrated")
        self.assertIn("now the base set", result.message)
        config = self.store.load()
        self.assertEqual([(c.connection_id, c.name, c.model_id) for c in config.connections],
                         [("groq-main", "Groq main", "openai/gpt-oss-120b"), ("gemini-main", "Gemini main", "gemini-3.8-flash")])
        self.assertEqual(config.get("gemini-main").options, {"reasoning_effort": "low"})
        self.assertEqual(config.base, self.route("groq-main", "gemini-main", cross=True))
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-main"), KEY_A)
        # Kairo and the Game Presence bot both use it (base set by default).
        old_routing = own_settings.load().routing
        for instance_id in ("admin-main", "gp-main"):
            routing = self.stores(instance_id).settings.load().routing
            self.assertEqual(routing.planner_fallback_profile_ids, ("gemini-main",))
            self.assertEqual(routing.routine_fallback_profile_ids, ("groq-main",))
            self.assertEqual(len(old_routing.routine_fallback_profile_ids), len(routing.routine_fallback_profile_ids))
        # Copy only, no secret in the message, idempotent.
        self.assertTrue(own_settings.path.is_file())
        self.assertEqual(own_credentials.read_secret("groq", "groq-default"), KEY_A)
        for secret in (KEY_A, KEY_B):
            self.assertNotIn(secret, result.message)
            self.assertNotIn(secret, self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(self.ai_storage.migrate_instances_to_connections().status, "already")
        self.assertEqual(len(self.store.load().connections), 2)
        self.assertIsNotNone(before)

    def test_several_bots_with_keys_keep_their_own_connections(self):
        second = self.instance_store.create_instance("admin", "admin-second", "Second")
        self.kairo_own_settings()
        self.write_own(
            second,
            (self.ai_platform.AIProfile("groq-default", "groq", "openai/gpt-oss-120b", "groq-default"),),
            self.ai_platform.RoutingConfig(routine_profile_id="groq-default"),
            (("groq", "groq-default", KEY_C),),
        )
        result = self.ai_storage.migrate_instances_to_connections()
        self.assertEqual(result.status, "migrated")
        config = self.store.load()
        self.assertEqual(config.base.connection_ids(), ())  # ambiguous: no base guessed
        self.assertEqual(self.stores("admin-second").selection.load().mode, "custom")
        second_route = self.stores("admin-second").settings.load()
        self.assertEqual([p.profile_id for p in second_route.profiles], ["groq-admin-second"])
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-admin-second"), KEY_C)
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-admin-main"), KEY_A)
        self.assertEqual(self.stores("gp-main").settings.load().profiles, ())  # no keys of others

    def test_existing_base_set_and_bot_choice_are_never_replaced(self):
        self.add("groq-main", key="test-key-existing")
        self.store.set_base(self.route(executor="groq-main"))
        self.stores("admin-main").selection.save(self.ai_connections.BotSelection("base", self.route()))
        self.kairo_own_settings()
        self.ai_storage.migrate_instances_to_connections()
        config = self.store.load()
        self.assertEqual(config.base, self.route(executor="groq-main"))
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-main"), "test-key-existing")
        self.assertEqual(self.stores("admin-main").selection.load().mode, "base")  # the user's choice kept
        self.assertTrue(config.get("groq-admin-main"))  # its keys still available as connections

    def test_unreadable_own_settings_are_skipped_and_retried(self):
        settings_store, _credentials = self.ai_storage.own_stores(self.admin)
        settings_store.path.parent.mkdir(parents=True, exist_ok=True)
        settings_store.path.write_text("{broken", encoding="utf-8")
        result = self.ai_storage.migrate_instances_to_connections()
        self.assertEqual(result.status, "failed")
        self.assertIn("admin-main", result.message)
        self.assertNotIn("admin-main", self.store.load().migrated_instances)
        self.assertEqual(settings_store.path.read_text(encoding="utf-8"), "{broken")

    def test_unreadable_connections_file_blocks_migration_safely(self):
        self.kairo_own_settings()
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.ai_storage.migrate_instances_to_connections().status, "failed")
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{broken")

    # -- migration: very first global store -> admin -> base set -------------------------------

    def write_legacy(self, *, settings=True, keys=(("groq", "groq-default", KEY_A), ("gemini", "gemini-default", KEY_B))):
        legacy_settings = self.ai_platform.AISettingsStore(self.app_paths.CONFIG_DIR / "ai.json")
        if settings:
            legacy_settings.save(
                self.ai_platform.AISettings(
                    profiles=(self.ai_platform.AIProfile("groq-default", "groq", "legacy-model", "groq-default"),),
                    routing=self.ai_platform.RoutingConfig(routine_profile_id="groq-default"),
                )
            )
        legacy_credentials = self.ai_platform.CredentialStore(self.app_paths.DATA_ROOT / "secrets" / "ai")
        for provider, ref, value in keys:
            legacy_credentials.write_secret(provider, ref, value)
        return legacy_settings, legacy_credentials

    def test_admin_start_runs_the_whole_chain(self):
        legacy_settings, legacy_credentials = self.write_legacy()
        sys.modules.pop("Admin", None)
        admin_module = importlib.import_module("Admin")
        output = io.StringIO()
        with redirect_stdout(output):
            stores = admin_module.resolve_ai_stores(SimpleNamespace(instance_id="admin-main"))
        self.assertEqual(stores.instance_id, "admin-main")
        profiles = stores.settings.load().profiles
        self.assertEqual([(p.profile_id, p.model_id) for p in profiles], [("groq-main", "legacy-model")])
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-main"), KEY_A)
        self.assertIn("copied to bot instance admin-main", output.getvalue())
        self.assertIn("now the base set", output.getvalue())
        self.assertNotIn(KEY_A, output.getvalue())
        self.assertTrue(legacy_settings.path.is_file())
        self.assertTrue(legacy_credentials.exists("groq", "groq-default"))

    def test_manager_bootstrap_runs_migrations_once(self):
        if importlib.util.find_spec("PySide6") is None:
            self.skipTest("PySide6 not installed")
        self.kairo_own_settings()
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        for name in ("manager_gui", "manager_dashboard", "manager_game_presence", "manager_terminal", "manager_groups", "admin_instance", "manager_core"):
            sys.modules.pop(name, None)
        manager_gui = importlib.import_module("manager_gui")
        output = io.StringIO()
        with redirect_stdout(output):
            manager_gui.bootstrap_for_gui()
            manager_gui.bootstrap_for_gui()  # idempotent
        self.assertEqual(output.getvalue().count("now the base set"), 1)
        self.assertEqual(self.store.credentials.read_secret("gemini", "gemini-main"), KEY_B)

    def test_legacy_global_store_copy_rules(self):
        legacy_settings, legacy_credentials = self.write_legacy()
        own_settings, own_credentials = self.ai_storage.own_stores(self.admin)
        own_credentials.write_secret("groq", "groq-default", "test-key-already-there")
        result = self.ai_storage.migrate_legacy_global_ai()
        self.assertEqual(result.status, "migrated")
        self.assertEqual(result.copied_keys, ("gemini/gemini-default",))  # existing key never overwritten
        self.assertEqual(own_credentials.read_secret("groq", "groq-default"), "test-key-already-there")
        self.assertNotIn(KEY_A, repr(result))
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "already")
        self.assertTrue(legacy_settings.path.is_file())

    def test_legacy_global_store_ambiguous_or_missing_destination(self):
        self.write_legacy()
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai([self.instance_store.load_instance("gp-main")]).status, "no_destination")
        self.instance_store.create_instance("admin", "admin-second")
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "ambiguous")
        self.assertFalse(self.ai_storage.migration_marker_path().exists())

    def test_corrupted_legacy_settings_fail_safe(self):
        self.write_legacy(settings=False)
        legacy_path = self.app_paths.CONFIG_DIR / "ai.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "failed")
        self.assertFalse(self.ai_storage.migration_marker_path().exists())
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), "{broken")

    def test_nothing_to_migrate(self):
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "nothing")
        self.assertEqual(self.ai_storage.migrate_instances_to_connections().status, "nothing")
        self.assertEqual(self.ai_storage.run_migrations(), [])


if __name__ == "__main__":
    unittest.main()
