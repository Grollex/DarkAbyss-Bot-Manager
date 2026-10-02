"""Per-instance AI storage: keys, models and routing belong to one bot instance."""

import asyncio
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

PATH_MODULES = (
    "ai_storage",
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
KEY_A = "test-key-instance-a"
KEY_B = "test-key-instance-b"


class InstanceAIStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        os.environ["DARKABYSS_DATA_DIR"] = str(self.data_root)
        for name in PATH_MODULES:
            sys.modules.pop(name, None)
        self.ai_storage = importlib.import_module("ai_storage")
        self.ai_platform = importlib.import_module("ai_platform")
        self.ai_orchestrator = importlib.import_module("ai_orchestrator")
        self.instance_store = importlib.import_module("instance_store")
        self.app_paths = importlib.import_module("app_paths")
        self.admin = self.instance_store.create_instance("admin", "admin-main", "Admin")
        self.presence = self.instance_store.create_instance("game_presence", "gp-main", "Games")

    def stores(self, instance_id):
        return self.ai_storage.for_instance_id(instance_id)

    def profile(self, profile_id, provider_id, model):
        return self.ai_platform.AIProfile(profile_id, provider_id, model)

    # -- layout ------------------------------------------------------------------

    def test_files_live_inside_their_own_instance(self):
        stores = self.stores("admin-main")
        self.assertEqual(stores.settings.path, (self.admin.paths.data_dir / "ai.json").resolve())
        self.assertEqual(stores.credentials.root, (self.admin.paths.secrets_dir / "ai").resolve())
        stores.credentials.write_secret("groq", "groq-default", KEY_A)
        key_file = self.admin.paths.secrets_dir / "ai" / "groq" / "groq-default.secret"
        self.assertTrue(key_file.is_file())
        self.assertFalse((self.data_root / "secrets" / "ai").exists())  # no global store
        self.assertFalse((self.data_root / "config" / "ai.json").exists())

    def test_keys_settings_routing_and_fallbacks_are_independent(self):
        admin, presence = self.stores("admin-main"), self.stores("gp-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        presence.credentials.write_secret("gemini", "gemini-default", KEY_B)
        self.assertEqual(admin.credentials.read_secret("groq", "groq-default"), KEY_A)
        self.assertFalse(presence.credentials.exists("groq", "groq-default"))
        self.assertFalse(admin.credentials.exists("gemini", "gemini-default"))

        settings_a = self.ai_platform.AISettings(
            profiles=(self.profile("groq-default", "groq", "model-a"), self.profile("gemini-default", "gemini", "model-g")),
            routing=self.ai_platform.RoutingConfig(
                routine_profile_id="groq-default",
                planner_profile_id="gemini-default",
                routine_fallback_profile_ids=("gemini-default",),
            ),
        )
        settings_b = self.ai_platform.AISettings(
            profiles=(self.profile("gemini-default", "gemini", "model-b"),),
            routing=self.ai_platform.RoutingConfig(creative_profile_id="gemini-default"),
        )
        admin.settings.save(settings_a)
        presence.settings.save(settings_b)
        loaded_a, loaded_b = admin.settings.load(), presence.settings.load()
        self.assertEqual(loaded_a.routing.routine_fallback_profile_ids, ("gemini-default",))
        self.assertEqual(loaded_b.routing.routine_fallback_profile_ids, ())
        self.assertEqual({p.model_id for p in loaded_a.profiles}, {"model-a", "model-g"})
        self.assertEqual({p.model_id for p in loaded_b.profiles}, {"model-b"})

    def test_deleting_one_instance_key_keeps_the_other(self):
        admin, presence = self.stores("admin-main"), self.stores("gp-main")
        admin.credentials.write_secret("groq", "groq-default", KEY_A)
        presence.credentials.write_secret("groq", "groq-default", KEY_B)
        admin.credentials.delete_secret("groq", "groq-default")
        self.assertFalse(admin.credentials.exists("groq", "groq-default"))
        self.assertEqual(presence.credentials.read_secret("groq", "groq-default"), KEY_B)

    def test_new_game_presence_instance_has_no_keys_of_the_admin(self):
        self.stores("admin-main").credentials.write_secret("groq", "groq-default", KEY_A)
        fresh = self.instance_store.create_instance("game_presence", "gp-second")
        stores = self.ai_storage.for_instance(fresh)
        self.assertFalse(stores.credentials.exists("groq", "groq-default"))
        self.assertEqual(stores.settings.load().profiles, ())

    def test_path_traversal_is_impossible(self):
        for bad in ("../admin-main", "..", "a/b", "A", "", None, "gp-main\\..\\admin-main"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.ai_storage.for_instance_id(bad, must_exist=False)
        stores = self.stores("gp-main")
        for provider, ref in (("..", "groq-default"), ("groq", "../../token"), ("groq", "a/b")):
            with self.subTest(provider=provider, ref=ref), self.assertRaises(Exception):
                stores.credentials.path_for(provider, ref)
        forged = SimpleNamespace(id="gp-main", paths=SimpleNamespace(root=self.admin.paths.root))
        with self.assertRaises(self.ai_storage.AIStorageError):
            self.ai_storage.for_instance(forged)

    def test_corrupted_instance_settings_fail_closed(self):
        stores = self.stores("admin-main")
        stores.settings.path.parent.mkdir(parents=True, exist_ok=True)
        stores.settings.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(self.ai_platform.AIPlatformError):
            stores.settings.load()
        orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores, provider_registry=self.ai_platform.ProviderRegistry())
        request = self.ai_orchestrator.OrchestratorRequest(
            messages=(self.ai_platform.AIMessage(role="user", content="hi"),), task_class="ROUTINE"
        )
        result = asyncio.run(orchestrator.orchestrate(request))
        self.assertNotEqual(result.status, self.ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(stores.settings.path.read_text(encoding="utf-8"), "{broken")

    # -- orchestrator / providers ----------------------------------------------------------

    def test_orchestrator_requires_and_uses_instance_stores(self):
        with self.assertRaises(ValueError):
            self.ai_orchestrator.AIOrchestrator()
        with self.assertRaises(ValueError):
            self.ai_orchestrator.build_default_provider_registry(None)
        with self.assertRaises(Exception):
            self.ai_platform.CredentialStore(None)
        with self.assertRaises(Exception):
            self.ai_platform.AISettingsStore(None)
        stores = self.stores("gp-main")
        orchestrator = self.ai_orchestrator.AIOrchestrator(stores=stores)
        self.assertIs(orchestrator._settings_store, stores.settings)
        self.assertIs(orchestrator._credential_store, stores.credentials)
        groq = orchestrator._providers.get("groq")
        gemini = orchestrator._providers.get("gemini")
        self.assertIs(groq._credential_store, stores.credentials)
        self.assertIs(gemini._credential_store, stores.credentials)

    def test_admin_transport_builds_orchestrator_from_its_instance(self):
        admin_ai = importlib.import_module("admin_ai")
        transport = admin_ai.AITransport(load_config=lambda: {})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertIsNone(transport.get_orchestrator())  # no stores -> AI unavailable (fail closed)
        transport = admin_ai.AITransport(load_config=lambda: {}, ai_stores=self.stores("admin-main"))
        orchestrator = transport.get_orchestrator()
        self.assertEqual(orchestrator._credential_store.root, (self.admin.paths.secrets_dir / "ai").resolve())

    def test_game_presence_wording_uses_its_own_instance(self):
        presence_bot = importlib.import_module("GamePresence")
        ai = presence_bot.InstanceAI("gp-main").get()
        self.assertEqual(ai._orchestrator._credential_store.root, (self.presence.paths.secrets_dir / "ai").resolve())
        self.assertNotEqual(ai._orchestrator._credential_store.root, (self.admin.paths.secrets_dir / "ai").resolve())

    # -- migration of the old global store --------------------------------------------------------

    def write_legacy(self, *, settings=True, keys=(("groq", "groq-default", KEY_A), ("gemini", "gemini-default", KEY_B))):
        legacy_settings = self.ai_platform.AISettingsStore(self.app_paths.CONFIG_DIR / "ai.json")
        if settings:
            legacy_settings.save(
                self.ai_platform.AISettings(
                    profiles=(self.profile("groq-default", "groq", "legacy-model"),),
                    routing=self.ai_platform.RoutingConfig(routine_profile_id="groq-default"),
                )
            )
        legacy_credentials = self.ai_platform.CredentialStore(self.app_paths.DATA_ROOT / "secrets" / "ai")
        for provider, ref, value in keys:
            legacy_credentials.write_secret(provider, ref, value)
        return legacy_settings, legacy_credentials

    def test_single_admin_receives_old_settings_and_keys_once(self):
        legacy_settings, legacy_credentials = self.write_legacy()
        result = self.ai_storage.migrate_legacy_global_ai()
        self.assertEqual(result.status, "migrated")
        self.assertEqual(result.instance_id, "admin-main")
        self.assertEqual(set(result.copied_keys), {"groq/groq-default", "gemini/gemini-default"})
        admin = self.stores("admin-main")
        self.assertEqual(admin.credentials.read_secret("groq", "groq-default"), KEY_A)
        self.assertEqual(admin.settings.load().profiles[0].model_id, "legacy-model")
        # Game Presence bots never inherit keys.
        presence = self.stores("gp-main")
        self.assertFalse(presence.credentials.exists("groq", "groq-default"))
        self.assertFalse(presence.settings.path.exists())
        # Old files are kept; secrets never appear in the result.
        self.assertTrue(legacy_settings.path.is_file())
        self.assertTrue(legacy_credentials.exists("groq", "groq-default"))
        self.assertNotIn(KEY_A, repr(result))
        self.assertNotIn(KEY_A, self.ai_storage.migration_marker_path().read_text(encoding="utf-8"))
        # Idempotent.
        admin.credentials.write_secret("groq", "groq-default", "test-key-changed-later")
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "already")
        self.assertEqual(admin.credentials.read_secret("groq", "groq-default"), "test-key-changed-later")

    def test_existing_instance_settings_and_keys_are_never_overwritten(self):
        self.write_legacy()
        admin = self.stores("admin-main")
        admin.credentials.write_secret("groq", "groq-default", "test-key-already-there")
        own = self.ai_platform.AISettings(profiles=(self.profile("groq-default", "groq", "own-model"),))
        admin.settings.save(own)
        result = self.ai_storage.migrate_legacy_global_ai()
        self.assertEqual(result.status, "migrated")
        self.assertFalse(result.copied_settings)
        self.assertEqual(result.copied_keys, ("gemini/gemini-default",))
        self.assertEqual(admin.credentials.read_secret("groq", "groq-default"), "test-key-already-there")
        self.assertEqual(admin.settings.load().profiles[0].model_id, "own-model")

    def test_ambiguous_destination_copies_nothing(self):
        self.write_legacy()
        self.instance_store.create_instance("admin", "admin-second")
        result = self.ai_storage.migrate_legacy_global_ai()
        self.assertEqual(result.status, "ambiguous")
        for instance_id in ("admin-main", "admin-second", "gp-main"):
            stores = self.stores(instance_id)
            self.assertFalse(stores.credentials.exists("groq", "groq-default"))
            self.assertFalse(stores.settings.path.exists())
        self.assertFalse(self.ai_storage.migration_marker_path().exists())

    def test_no_admin_instance_means_no_destination(self):
        self.write_legacy()
        only_presence = [self.instance_store.load_instance("gp-main")]
        result = self.ai_storage.migrate_legacy_global_ai(only_presence)
        self.assertEqual(result.status, "no_destination")
        self.assertFalse(self.stores("gp-main").credentials.exists("groq", "groq-default"))

    def test_corrupted_legacy_settings_fail_safe(self):
        self.write_legacy(settings=False)
        legacy_path = self.app_paths.CONFIG_DIR / "ai.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("{broken", encoding="utf-8")
        result = self.ai_storage.migrate_legacy_global_ai()
        self.assertEqual(result.status, "failed")
        self.assertFalse(self.stores("admin-main").settings.path.exists())
        self.assertFalse(self.ai_storage.migration_marker_path().exists())  # retried next start
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), "{broken")

    def test_nothing_to_migrate(self):
        self.assertEqual(self.ai_storage.migrate_legacy_global_ai().status, "nothing")
        self.assertFalse(self.ai_storage.migration_marker_path().exists())


if __name__ == "__main__":
    unittest.main()
