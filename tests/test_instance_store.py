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
            self.assertTrue(instance.paths.config_meta.is_file())
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

    def test_empty_override_config_metadata_and_placeholder_token_created(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))

            instance = instance_store.create_instance("admin", "admin-main")

            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {})
            self.assertEqual(
                json.loads(instance.paths.config_meta.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "config_version": 1,
                },
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

    def test_update_instance_display_name_changes_only_display_name(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main", display_name="Old Name")
            original_config = instance.paths.config.read_bytes()
            original_token = instance.paths.token.read_bytes()

            updated = instance_store.update_instance_display_name("admin-main", "New Manager Name")
            metadata = json.loads(updated.paths.metadata.read_text(encoding="utf-8"))

            self.assertEqual(updated.display_name, "New Manager Name")
            self.assertEqual(updated.id, "admin-main")
            self.assertEqual(updated.bot_type, "admin")
            self.assertEqual(updated.schema_version, 1)
            self.assertEqual(
                metadata,
                {
                    "schema_version": 1,
                    "id": "admin-main",
                    "bot_type": "admin",
                    "display_name": "New Manager Name",
                },
            )
            self.assertEqual(updated.paths.config.read_bytes(), original_config)
            self.assertEqual(updated.paths.token.read_bytes(), original_token)

    def test_update_instance_display_name_preserves_unknown_metadata_keys(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main", display_name="Old Name")
            metadata = json.loads(instance.paths.metadata.read_text(encoding="utf-8"))
            metadata["future_field"] = {"value": 123}
            metadata["another_future_flag"] = True
            instance.paths.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

            updated = instance_store.update_instance_display_name("admin-main", "New Name")
            updated_metadata = json.loads(updated.paths.metadata.read_text(encoding="utf-8"))

            self.assertEqual(updated_metadata["display_name"], "New Name")
            self.assertEqual(updated_metadata["future_field"], {"value": 123})
            self.assertIs(updated_metadata["another_future_flag"], True)

    def test_update_instance_display_name_rejects_empty_names(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main", display_name="Old Name")

            for invalid_name in ("", "   "):
                with self.subTest(display_name=invalid_name):
                    with self.assertRaisesRegex(instance_store.InstanceStoreError, "display_name"):
                        instance_store.update_instance_display_name("admin-main", invalid_name)

            self.assertEqual(instance_store.load_instance("admin-main").display_name, "Old Name")
            self.assertEqual(json.loads(instance.paths.metadata.read_text(encoding="utf-8"))["display_name"], "Old Name")

    def test_update_instance_display_name_preserves_identity_fields(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance_store.create_instance("admin", "admin-main", display_name="Old Name")

            updated = instance_store.update_instance_display_name("admin-main", "Renamed")

            self.assertEqual(updated.schema_version, 1)
            self.assertEqual(updated.id, "admin-main")
            self.assertEqual(updated.bot_type, "admin")
            self.assertEqual(updated.paths.root.name, "admin-main")

    def test_update_instance_display_name_atomic_write_failure_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main", display_name="Old Name")
            original_metadata = instance.paths.metadata.read_bytes()

            with mock.patch.object(instance_store.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(instance_store.InstanceStoreError, "replace failed"):
                    instance_store.update_instance_display_name("admin-main", "New Name")

            self.assertEqual(instance.paths.metadata.read_bytes(), original_metadata)
            self.assertEqual(instance_store.load_instance("admin-main").display_name, "Old Name")
            self.assertFalse(any(path.name.startswith(".instance.json.") for path in instance.paths.root.iterdir()))

    def test_update_instance_display_name_rejects_symlinked_metadata(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, _app_paths = self.load_modules(Path(data_dir))
            instance = instance_store.create_instance("admin", "admin-main", display_name="Old Name")
            target = instance.paths.root / "external-instance.json"
            target.write_text(instance.paths.metadata.read_text(encoding="utf-8"), encoding="utf-8")
            instance.paths.metadata.unlink()
            try:
                instance.paths.metadata.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlink creation unavailable: {exc}")

            with self.assertRaisesRegex(instance_store.InstanceStoreError, "symlinked metadata"):
                instance_store.update_instance_display_name("admin-main", "New Name")

            self.assertIn("Old Name", target.read_text(encoding="utf-8"))

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
