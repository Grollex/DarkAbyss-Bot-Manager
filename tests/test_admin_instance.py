import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
VALID_ADMIN_CONFIG = {
    "allow_server_administrators": True,
    "allowed_user_ids": [],
    "allowed_role_ids": [],
    "audit_channel_id": None,
}


def load_modules(data_root: Path, *names: str):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in ("Admin", "admin_instance", "instance_store", "bot_registry", "app_paths"):
        sys.modules.pop(module_name, None)
    return [importlib.import_module(name) for name in names]


class AdminInstanceMigrationTests(unittest.TestCase):
    def set_old_legacy_paths(self, app_paths, legacy_root: Path) -> tuple[Path, Path]:
        legacy_config = legacy_root / "admin_config.json"
        legacy_token = legacy_root / "admin_bot_token.txt"
        app_paths.LEGACY_SOURCE_ADMIN_CONFIG_PATH = legacy_config
        app_paths.LEGACY_SOURCE_ADMIN_TOKEN_PATH = legacy_token
        app_paths.LEGACY_ADMIN_CONFIG_PATH = legacy_config
        app_paths.LEGACY_ADMIN_TOKEN_PATH = legacy_token
        return legacy_config, legacy_token

    def test_phase1_config_and_token_migrate_to_admin_main(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            phase1_config = b'{\n  "allow_server_administrators": false,\n  "allowed_user_ids": ["123"],\n  "allowed_role_ids": [],\n  "audit_channel_id": null\n}\n'
            phase1_token = b"FAKE_PHASE1_TOKEN"
            app_paths.ADMIN_CONFIG_PATH.write_bytes(phase1_config)
            app_paths.ADMIN_TOKEN_PATH.write_bytes(phase1_token)

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.id, "admin-main")
            self.assertEqual(instance.paths.config.read_bytes(), phase1_config)
            self.assertEqual(instance.paths.token.read_bytes(), phase1_token)
            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), phase1_config)
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_bytes(), phase1_token)

    def test_old_source_adjacent_config_and_token_migrate_when_only_old_sources_exist(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            old_config = b'{"old_source_config": true}\n'
            old_token = b"FAKE_OLD_SOURCE_TOKEN"
            legacy_config_path.write_bytes(old_config)
            legacy_token_path.write_bytes(old_token)

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.config.read_bytes(), old_config)
            self.assertEqual(instance.paths.token.read_bytes(), old_token)
            self.assertEqual(legacy_config_path.read_bytes(), old_config)
            self.assertEqual(legacy_token_path.read_bytes(), old_token)

    def test_phase1_config_and_token_win_over_old_source_adjacent_sources(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            phase1_config = b'{"phase1_config": true}\n'
            phase1_token = b"FAKE_PHASE1_TOKEN"
            old_config = b'{"old_source_config": true}\n'
            old_token = b"FAKE_OLD_SOURCE_TOKEN"
            app_paths.ADMIN_CONFIG_PATH.write_bytes(phase1_config)
            app_paths.ADMIN_TOKEN_PATH.write_bytes(phase1_token)
            legacy_config_path.write_bytes(old_config)
            legacy_token_path.write_bytes(old_token)

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.config.read_bytes(), phase1_config)
            self.assertEqual(instance.paths.token.read_bytes(), phase1_token)
            self.assertEqual(legacy_config_path.read_bytes(), old_config)
            self.assertEqual(legacy_token_path.read_bytes(), old_token)

    def test_old_real_token_used_when_phase1_token_is_placeholder(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            _legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_TOKEN_PATH.write_text(app_paths.TOKEN_PLACEHOLDER, encoding="utf-8")
            legacy_token_path.write_bytes(b"FAKE_OLD_SOURCE_REAL_TOKEN")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.token.read_bytes(), b"FAKE_OLD_SOURCE_REAL_TOKEN")
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER)
            self.assertEqual(legacy_token_path.read_bytes(), b"FAKE_OLD_SOURCE_REAL_TOKEN")

    def test_phase1_real_token_wins_over_old_real_token(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            _legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_TOKEN_PATH.write_bytes(b"FAKE_PHASE1_REAL_TOKEN")
            legacy_token_path.write_bytes(b"FAKE_OLD_SOURCE_REAL_TOKEN")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.token.read_bytes(), b"FAKE_PHASE1_REAL_TOKEN")
            self.assertEqual(legacy_token_path.read_bytes(), b"FAKE_OLD_SOURCE_REAL_TOKEN")

    def test_existing_admin_main_wins_over_phase1_and_old_source_adjacent_sources(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, instance_store, app_paths = load_modules(
                Path(data_dir),
                "admin_instance",
                "instance_store",
                "app_paths",
            )
            legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            existing = instance_store.create_instance("admin", "admin-main")
            existing_config = b'{"existing_instance": true}\n'
            existing_token = b"FAKE_EXISTING_INSTANCE_TOKEN"
            phase1_config = b'{"phase1_config": true}\n'
            phase1_token = b"FAKE_PHASE1_TOKEN"
            old_config = b'{"old_source_config": true}\n'
            old_token = b"FAKE_OLD_SOURCE_TOKEN"
            existing.paths.config.write_bytes(existing_config)
            existing.paths.token.write_bytes(existing_token)
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_CONFIG_PATH.write_bytes(phase1_config)
            app_paths.ADMIN_TOKEN_PATH.write_bytes(phase1_token)
            legacy_config_path.write_bytes(old_config)
            legacy_token_path.write_bytes(old_token)

            loaded = admin_instance.ensure_admin_instance()

            self.assertEqual(loaded.paths.config.read_bytes(), existing_config)
            self.assertEqual(loaded.paths.token.read_bytes(), existing_token)
            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), phase1_config)
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_bytes(), phase1_token)
            self.assertEqual(legacy_config_path.read_bytes(), old_config)
            self.assertEqual(legacy_token_path.read_bytes(), old_token)

    def test_phase1_config_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            app_paths.CONFIG_DIR.mkdir(parents=True)
            phase1_config = (
                b'{\r\n'
                b'    "allow_server_administrators": false,\r\n'
                b'    "allowed_user_ids": ["123"],\r\n'
                b'    "allowed_role_ids": [],\r\n'
                b'    "audit_channel_id": null\r\n'
                b'}\r\n'
            )
            app_paths.ADMIN_CONFIG_PATH.write_bytes(phase1_config)

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.config.read_bytes(), phase1_config)

    def test_existing_admin_main_is_authoritative_and_untouched(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, instance_store, app_paths = load_modules(Path(data_dir), "admin_instance", "instance_store", "app_paths")
            existing = instance_store.create_instance("admin", "admin-main")
            custom_config = b'{"existing": true}\n'
            custom_token = b"FAKE_EXISTING_INSTANCE_TOKEN"
            unknown_file = existing.paths.root / "unknown-user-file.txt"
            existing.paths.config.write_bytes(custom_config)
            existing.paths.token.write_bytes(custom_token)
            unknown_file.write_text("keep", encoding="utf-8")
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_CONFIG_PATH.write_text(json.dumps(VALID_ADMIN_CONFIG), encoding="utf-8")
            app_paths.ADMIN_TOKEN_PATH.write_text("FAKE_PHASE1_DIFFERENT_TOKEN", encoding="utf-8")

            loaded = admin_instance.ensure_admin_instance()

            self.assertEqual(loaded.paths.config.read_bytes(), custom_config)
            self.assertEqual(loaded.paths.token.read_bytes(), custom_token)
            self.assertEqual(unknown_file.read_text(encoding="utf-8"), "keep")

    def test_no_phase1_data_creates_default_config_and_placeholder_token(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(
                json.loads(instance.paths.config.read_text(encoding="utf-8")),
                json.loads(app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_text(encoding="utf-8")),
            )
            self.assertEqual(instance.paths.token.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER + "\n")

    def test_placeholder_phase1_token_is_not_migrated_as_credential(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_TOKEN_PATH.write_text(app_paths.TOKEN_PLACEHOLDER, encoding="utf-8")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.token.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER + "\n")
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER)

    def test_empty_phase1_and_old_tokens_create_placeholder(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            _legacy_config_path, legacy_token_path = self.set_old_legacy_paths(app_paths, Path(legacy_dir))
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_TOKEN_PATH.write_text("", encoding="utf-8")
            legacy_token_path.write_text("   ", encoding="utf-8")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(instance.paths.token.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER + "\n")
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), "")
            self.assertEqual(legacy_token_path.read_text(encoding="utf-8"), "   ")

    def test_migration_failure_leaves_sources_and_unrelated_instances_untouched(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, instance_store, app_paths = load_modules(Path(data_dir), "admin_instance", "instance_store", "app_paths")
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            phase1_config = json.dumps(VALID_ADMIN_CONFIG).encode("utf-8")
            phase1_token = b"FAKE_PHASE1_TOKEN"
            app_paths.ADMIN_CONFIG_PATH.write_bytes(phase1_config)
            app_paths.ADMIN_TOKEN_PATH.write_bytes(phase1_token)
            unrelated = instance_store.create_instance("admin", "admin-second")
            unrelated.paths.config.write_text("unrelated", encoding="utf-8")

            with mock.patch.object(instance_store, "_write_initial_instance_files", side_effect=RuntimeError("staging write failed")):
                with self.assertRaisesRegex(RuntimeError, "staging write failed"):
                    admin_instance.ensure_admin_instance()

            self.assertFalse(instance_store.get_instance_paths("admin-main").root.exists())
            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), phase1_config)
            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_bytes(), phase1_token)
            self.assertEqual(unrelated.paths.config.read_text(encoding="utf-8"), "unrelated")


class AdminRuntimeSelectionTests(unittest.TestCase):
    def write_valid_config(self, path: Path, allowed_user_id: str = "123") -> None:
        config = dict(VALID_ADMIN_CONFIG)
        config["allowed_user_ids"] = [allowed_user_id]
        path.write_text(json.dumps(config), encoding="utf-8")

    def test_no_instance_argument_defaults_to_admin_main(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, = load_modules(Path(data_dir), "Admin")
            args = Admin.parse_args([])
            runtime = Admin.resolve_runtime(args.instance)

            self.assertEqual(runtime.instance_id, "admin-main")

    def test_explicit_instance_selects_admin_second(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance_store.create_instance("admin", "admin-second")

            runtime = Admin.resolve_runtime("admin-second")

            self.assertEqual(runtime.instance_id, "admin-second")
            self.assertIn("admin-second", str(runtime.config_path))

    def test_missing_non_default_instance_fails_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, = load_modules(Path(data_dir), "Admin")

            with self.assertRaisesRegex(RuntimeError, "Invalid admin instance"):
                Admin.resolve_runtime("admin-second")

    def test_corrupt_admin_main_missing_config_fails_as_runtime_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.config.unlink()

            with self.assertRaisesRegex(RuntimeError, "Invalid admin instance 'admin-main'.*config.json"):
                Admin.resolve_runtime("admin-main")

            self.assertTrue(instance.paths.root.exists())
            self.assertFalse(instance.paths.config.exists())

    def test_corrupt_admin_main_malformed_metadata_fails_as_runtime_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.metadata.write_text("{not valid json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Invalid admin instance 'admin-main'.*metadata"):
                Admin.resolve_runtime("admin-main")

            self.assertEqual(instance.paths.metadata.read_text(encoding="utf-8"), "{not valid json")

    def test_non_admin_bot_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, = load_modules(Path(data_dir), "Admin")
            fake_instance = SimpleNamespace(bot_type="other", id="other-main")

            with mock.patch.object(Admin.instance_store, "load_instance", return_value=fake_instance):
                with self.assertRaisesRegex(RuntimeError, "expected 'admin'"):
                    Admin.resolve_runtime("other-main")

    def test_selected_instance_config_path_is_used(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            main = instance_store.create_instance("admin", "admin-main")
            second = instance_store.create_instance("admin", "admin-second")
            self.write_valid_config(main.paths.config, "111")
            self.write_valid_config(second.paths.config, "222")

            config = Admin.load_config(Admin.resolve_runtime("admin-second"))

            self.assertEqual(config["allowed_user_ids"], [222])

    def test_selected_instance_token_path_is_used(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            main = instance_store.create_instance("admin", "admin-main")
            second = instance_store.create_instance("admin", "admin-second")
            main.paths.token.write_text("FAKE_MAIN_TOKEN", encoding="utf-8")
            second.paths.token.write_text("FAKE_SECOND_TOKEN", encoding="utf-8")

            token = Admin.load_token(Admin.resolve_runtime("admin-second"))

            self.assertEqual(token, "FAKE_SECOND_TOKEN")

    def test_placeholder_selected_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance_store.create_instance("admin", "admin-main")
            runtime = Admin.resolve_runtime("admin-main")

            with self.assertRaisesRegex(RuntimeError, "Put the Discord bot token"):
                Admin.load_token(runtime)

    def test_missing_selected_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.token.unlink()
            runtime = Admin.AdminRuntime(
                instance_id="admin-main",
                config_path=instance.paths.config,
                token_path=instance.paths.token,
                lock_path=instance.paths.runtime_dir / "admin_bot.lock",
            )

            with self.assertRaisesRegex(RuntimeError, "Token file not found"):
                Admin.load_token(runtime)

    def test_lock_paths_are_instance_specific(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance_store.create_instance("admin", "admin-main")
            instance_store.create_instance("admin", "admin-second")

            main_runtime = Admin.resolve_runtime("admin-main")
            second_runtime = Admin.resolve_runtime("admin-second")

            self.assertEqual(main_runtime.lock_path.name, "admin_bot.lock")
            self.assertEqual(second_runtime.lock_path.name, "admin_bot.lock")
            self.assertNotEqual(main_runtime.lock_path, second_runtime.lock_path)
            self.assertIn("admin-main", str(main_runtime.lock_path))
            self.assertIn("admin-second", str(second_runtime.lock_path))

    def test_fresh_bootstrap_does_not_generate_phase1_runtime_files(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")

            admin_instance.ensure_admin_instance()

            self.assertTrue((app_paths.INSTANCES_DIR / "admin-main").is_dir())
            self.assertFalse(app_paths.ADMIN_CONFIG_PATH.exists())
            self.assertFalse(app_paths.ADMIN_TOKEN_PATH.exists())

    def test_existing_phase1_files_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, app_paths = load_modules(Path(data_dir), "admin_instance", "app_paths")
            app_paths.CONFIG_DIR.mkdir(parents=True)
            app_paths.SECRETS_DIR.mkdir(parents=True)
            app_paths.ADMIN_CONFIG_PATH.write_text(json.dumps(VALID_ADMIN_CONFIG), encoding="utf-8")
            app_paths.ADMIN_TOKEN_PATH.write_text("FAKE_PHASE1_TOKEN", encoding="utf-8")

            admin_instance.ensure_admin_instance()

            self.assertTrue(app_paths.ADMIN_CONFIG_PATH.exists())
            self.assertTrue(app_paths.ADMIN_TOKEN_PATH.exists())


if __name__ == "__main__":
    unittest.main()
