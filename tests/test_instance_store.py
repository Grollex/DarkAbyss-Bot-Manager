import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


class InstanceStoreTests(unittest.TestCase):
    def load_modules(self, data_root: Path):
        sys.path.insert(0, str(CORE_ROOT))
        os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
        for module_name in ("instance_store", "bot_registry", "app_paths"):
            sys.modules.pop(module_name, None)
        instance_store = importlib.import_module("instance_store")
        app_paths = importlib.import_module("app_paths")
        return instance_store, app_paths

    def test_create_instance_creates_expected_layout_and_metadata(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, app_paths = self.load_modules(Path(data_dir))

            instance = instance_store.create_instance("admin", "admin-main")

            self.assertEqual(instance.id, "admin-main")
            self.assertEqual(instance.bot_type, "admin")
            self.assertEqual(instance.display_name, "Admin Bot")
            self.assertTrue(instance.paths.metadata.is_file())
            self.assertTrue(instance.paths.config.is_file())
            self.assertTrue(instance.paths.token.is_file())
            self.assertTrue(instance.paths.runtime_dir.is_dir())
            self.assertTrue(instance.paths.logs_dir.is_dir())
            self.assertTrue(instance.paths.data_dir.is_dir())
            self.assertTrue(str(instance.paths.root).startswith(str(app_paths.INSTANCES_DIR)))
            self.assertEqual(
                json.loads(instance.paths.metadata.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "id": "admin-main",
                    "bot_type": "admin",
                    "display_name": "Admin Bot",
                },
            )

    def test_default_config_is_copied_and_placeholder_token_created(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))

            instance = instance_store.create_instance("admin", "admin-main")

            self.assertEqual(
                json.loads(instance.paths.config.read_text(encoding="utf-8")),
                json.loads((PROJECT_ROOT / "DarkAbyss_Core" / "defaults" / "admin_config.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(instance.paths.token.read_text(encoding="utf-8"), "PUT_DISCORD_BOT_TOKEN_HERE\n")

    def test_existing_instance_is_not_reinitialized_or_overwritten(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main")
            custom_config = b'{"custom": true}\n'
            instance.paths.config.write_bytes(custom_config)
            instance.paths.token.write_text("FAKE_CUSTOM_TOKEN", encoding="utf-8")
            unknown_file = instance.paths.root / "unknown-user-file.txt"
            unknown_file.write_text("keep me", encoding="utf-8")

            with self.assertRaises(instance_store.InstanceAlreadyExistsError):
                instance_store.create_instance("admin", "admin-main")

            self.assertEqual(instance.paths.config.read_bytes(), custom_config)
            self.assertEqual(instance.paths.token.read_text(encoding="utf-8"), "FAKE_CUSTOM_TOKEN")
            self.assertEqual(unknown_file.read_text(encoding="utf-8"), "keep me")

    def test_failed_create_cleans_staging_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, app_paths = self.load_modules(Path(data_dir))
            existing = instance_store.create_instance("admin", "admin-existing")
            existing.paths.config.write_text("existing config", encoding="utf-8")

            with mock.patch.object(instance_store, "_write_initial_instance_files", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    instance_store.create_instance("admin", "admin-main")

            failed_paths = instance_store.get_instance_paths("admin-main")
            self.assertFalse(failed_paths.root.exists())
            self.assertEqual(existing.paths.config.read_text(encoding="utf-8"), "existing config")
            self.assertFalse(any(path.name.startswith("admin-main.") for path in instance_store.get_staging_root().iterdir()))

            retried = instance_store.create_instance("admin", "admin-main")
            self.assertTrue(retried.paths.metadata.is_file())

    def test_load_rejects_missing_config(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.config.unlink()

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "config.json"):
                instance_store.load_instance("admin-main")

    def test_load_rejects_missing_token(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.token.unlink()

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "secrets/token.txt"):
                instance_store.load_instance("admin-main")

    def test_load_rejects_missing_required_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.logs_dir.rmdir()

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "logs/"):
                instance_store.load_instance("admin-main")

    def test_list_instances_ignores_internal_staging_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance_store.create_instance("admin", "admin-main")
            staging_dir = instance_store.get_staging_root()
            leftover = staging_dir / "admin-second.leftover.tmp"
            leftover.mkdir(parents=True)
            (leftover / "not-an-instance.txt").write_text("partial", encoding="utf-8")

            self.assertEqual([instance.id for instance in instance_store.list_instances()], ["admin-main"])

    def test_leftover_staging_does_not_block_valid_load_or_unrelated_create(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            main = instance_store.create_instance("admin", "admin-main")
            leftover = instance_store.get_staging_root() / "crashed.tmp"
            leftover.mkdir(parents=True)
            (leftover / "instance.json").write_text("{not complete", encoding="utf-8")

            loaded = instance_store.load_instance("admin-main")
            second = instance_store.create_instance("admin", "admin-second")

            self.assertEqual(loaded.paths.root, main.paths.root)
            self.assertTrue(second.paths.metadata.is_file())
            self.assertTrue(leftover.exists())

    def test_instance_json_directory_is_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            paths = instance_store.get_instance_paths("admin-main")
            paths.root.mkdir(parents=True)
            paths.metadata.mkdir()

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "Missing instance metadata"):
                instance_store.load_instance("admin-main")

    def test_display_name_defaults_only_when_omitted(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))

            defaulted = instance_store.create_instance("admin", "admin-main", display_name=None)
            named = instance_store.create_instance("admin", "admin-second", display_name="Admin Second")

            self.assertEqual(defaulted.display_name, "Admin Bot")
            self.assertEqual(named.display_name, "Admin Second")

    def test_empty_display_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))

            for invalid_name in ("", "   "):
                with self.subTest(display_name=invalid_name):
                    with self.assertRaisesRegex(instance_store.InstanceStoreError, "display_name"):
                        instance_store.create_instance("admin", "admin-main", display_name=invalid_name)
                    self.assertFalse(instance_store.get_instance_paths("admin-main").root.exists())

    def test_multiple_instances_have_separate_paths_and_listed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            first = instance_store.create_instance("admin", "admin-main")
            second = instance_store.create_instance("admin", "admin-second", display_name="Admin Second")

            self.assertNotEqual(first.paths.config, second.paths.config)
            self.assertNotEqual(first.paths.token, second.paths.token)
            self.assertNotEqual(first.paths.runtime_dir, second.paths.runtime_dir)
            self.assertNotEqual(first.paths.logs_dir, second.paths.logs_dir)
            self.assertNotEqual(first.paths.data_dir, second.paths.data_dir)
            self.assertEqual([instance.id for instance in instance_store.list_instances()], ["admin-main", "admin-second"])

    def test_invalid_instance_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            invalid_ids = ["../evil", "..\\evil", "/absolute", "a/b", "a\\b", "", "   "]

            for invalid_id in invalid_ids:
                with self.subTest(invalid_id=invalid_id):
                    with self.assertRaises(instance_store.InstanceStoreError):
                        instance_store.create_instance("admin", invalid_id)

    def test_instance_with_unknown_bot_type_is_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            paths = instance_store.get_instance_paths("admin-main")
            paths.root.mkdir(parents=True)
            paths.metadata.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "admin-main",
                        "bot_type": "missing-type",
                        "display_name": "Broken Instance",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "unknown bot_type"):
                instance_store.load_instance("admin-main")


if __name__ == "__main__":
    unittest.main()
