import importlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
VALID_ADMIN_CONFIG = {
    "allow_server_administrators": True,
    "allowed_user_ids": [],
    "allowed_role_ids": [],
    "audit_channel_id": None,
}
# AI-4: explicit /ai whitelist fields come from program defaults; older
# instance overrides inherit them as empty lists without any migration.
AI_WHITELIST_DEFAULTS = {
    "ai_allowed_user_ids": [],
    "ai_allowed_role_ids": [],
    # AI-5: natural control channel disabled by default.
    "ai_control_channel_id": None,
    # AI-6.2: approve the AI plan once; message text reading off by default.
    "ai_confirmation_mode": "plan",
    "ai_read_message_content": False,
}


def load_modules(data_root: Path, *names: str):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in (
        "Admin",
        "admin_instance",
        "config_store",
        "instance_store",
        "bot_registry",
        "app_paths",
    ):
        sys.modules.pop(module_name, None)
    return [importlib.import_module(name) for name in names]


def copy_via_json(payload):
    return json.loads(json.dumps(payload))


class ConfigStoreTests(unittest.TestCase):
    def create_instance(self, instance_store, instance_id: str = "admin-main"):
        return instance_store.create_instance("admin", instance_id)

    def create_file_symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"file symlinks are not supported for this test environment: {exc}")

    def test_program_default_remains_immutable_after_effective_load(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(Path(data_dir), "config_store", "instance_store", "app_paths")
            instance = self.create_instance(instance_store)
            before = app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_bytes()

            effective = config_store.load_effective_config(instance.id)
            effective["allow_server_administrators"] = False

            self.assertEqual(app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_bytes(), before)

    def test_empty_override_inherits_admin_defaults(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(Path(data_dir), "config_store", "instance_store", "app_paths")
            instance = self.create_instance(instance_store)

            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {})
            self.assertEqual(
                config_store.load_effective_config(instance.id),
                json.loads(app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_text(encoding="utf-8")),
            )

    def test_load_config_overrides_returns_empty_fresh_copy_for_fresh_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)

            overrides = config_store.load_config_overrides(instance.id)
            overrides["allowed_user_ids"] = ["999"]

            self.assertEqual(config_store.load_config_overrides(instance.id), {})
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {})

    def test_config_snapshot_contains_independent_dicts_and_current_version(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            instance.paths.config.write_text('{"allowed_user_ids": ["123"]}\n', encoding="utf-8")

            snapshot = config_store.get_config_snapshot(instance.id)

            self.assertEqual(snapshot.instance_id, instance.id)
            self.assertEqual(snapshot.bot_type, "admin")
            self.assertEqual(snapshot.config_version, 1)
            self.assertEqual(snapshot.overrides, {"allowed_user_ids": ["123"]})
            self.assertEqual(snapshot.defaults, json.loads(app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_text(encoding="utf-8")))
            self.assertEqual(snapshot.effective["allowed_user_ids"], ["123"])

            snapshot.defaults["allow_server_administrators"] = False
            snapshot.overrides["allowed_user_ids"].append("456")
            snapshot.effective["allowed_role_ids"].append("789")

            fresh_snapshot = config_store.get_config_snapshot(instance.id)
            self.assertTrue(fresh_snapshot.defaults["allow_server_administrators"])
            self.assertEqual(fresh_snapshot.overrides["allowed_user_ids"], ["123"])
            self.assertEqual(fresh_snapshot.effective["allowed_role_ids"], [])

    def test_partial_override_changes_only_specified_field(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config.write_text('{"allow_server_administrators": false}\n', encoding="utf-8")

            effective = config_store.load_effective_config(instance.id)

            self.assertFalse(effective["allow_server_administrators"])
            self.assertEqual(effective["allowed_user_ids"], [])
            self.assertEqual(effective["allowed_role_ids"], [])
            self.assertIsNone(effective["audit_channel_id"])

    def test_lists_replace_defaults(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config.write_text('{"allowed_user_ids": ["123"]}\n', encoding="utf-8")

            effective = config_store.load_effective_config(instance.id)

            self.assertEqual(effective["allowed_user_ids"], ["123"])

    def test_save_partial_override_persists_only_override_and_effective_reflects_it(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)

            saved = config_store.save_config_overrides(instance.id, {"allow_server_administrators": False})

            self.assertEqual(saved, {"allow_server_administrators": False})
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {"allow_server_administrators": False})
            effective = config_store.load_effective_config(instance.id)
            self.assertFalse(effective["allow_server_administrators"])
            self.assertEqual(effective["allowed_user_ids"], [])

    def test_save_list_override_replaces_default_list(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)

            config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123", "456"]})

            self.assertEqual(config_store.load_effective_config(instance.id)["allowed_user_ids"], ["123", "456"])

    def test_recursive_dictionary_merge_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, = load_modules(Path(data_dir), "config_store")

            merged = config_store._merge_config(
                {"outer": {"keep": 1, "replace": 2}, "list": [1]},
                {"outer": {"replace": 3}, "list": [2]},
            )

            self.assertEqual(merged, {"outer": {"keep": 1, "replace": 3}, "list": [2]})

    def test_save_recursive_dict_override_merges_with_synthetic_defaults(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, bot_registry = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store)
            default_path = Path(data_dir) / "synthetic-default.json"
            default_payload = {
                **VALID_ADMIN_CONFIG,
                "nested": {
                    "keep": "default",
                    "change": "default",
                },
            }
            default_path.write_text(json.dumps(default_payload), encoding="utf-8")
            bot_type = replace(bot_registry.get_bot_type("admin"), default_config=default_path)

            with mock.patch.object(config_store.bot_registry, "get_bot_type", return_value=bot_type):
                config_store.save_config_overrides(instance.id, {"nested": {"change": "override"}})
                effective = config_store.load_effective_config(instance.id)

            self.assertEqual(effective["nested"], {"keep": "default", "change": "override"})
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {"nested": {"change": "override"}})

    def test_existing_full_phase3_config_preserves_effective_values(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            full_config = dict(VALID_ADMIN_CONFIG)
            full_config["allow_server_administrators"] = False
            full_config["allowed_user_ids"] = ["123"]
            instance.paths.config.write_text(json.dumps(full_config), encoding="utf-8")
            instance.paths.config_meta.unlink()

            effective = config_store.load_effective_config(instance.id)

            self.assertEqual(effective, {**full_config, **AI_WHITELIST_DEFAULTS})
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), full_config)

    def test_new_instance_has_current_metadata_and_override_semantics(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)

            self.assertEqual(config_store.get_config_version(instance.id), 1)
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {})
            self.assertEqual(
                json.loads(instance.paths.config_meta.read_text(encoding="utf-8")),
                {"schema_version": 1, "config_version": 1},
            )

    def test_missing_config_meta_is_legacy_v0(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config_meta.unlink()

            self.assertEqual(config_store.get_config_version(instance.id), 0)

    def test_v0_to_v1_migration_creates_one_byte_identical_backup(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            original_config = (
                b'{\r\n'
                b'  "allow_server_administrators": false,\r\n'
                b'  "allowed_user_ids": ["123"],\r\n'
                b'  "allowed_role_ids": [],\r\n'
                b'  "audit_channel_id": null\r\n'
                b'}\r\n'
            )
            instance.paths.config.write_bytes(original_config)
            instance.paths.config_meta.unlink()

            self.assertEqual(config_store.ensure_config_current(instance.id), 1)

            backup_root = app_paths.BACKUPS_DIR / "instances" / instance.id / "config"
            backups = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "config.json").read_bytes(), original_config)
            backup_meta = json.loads((backups[0] / "backup.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_meta["instance_id"], instance.id)
            self.assertEqual(backup_meta["from_config_version"], 0)
            self.assertEqual(backup_meta["to_config_version"], 1)
            self.assertEqual(backup_meta["config_sha256"], config_store.hashlib.sha256(original_config).hexdigest())
            self.assertEqual(instance.paths.config.read_bytes(), original_config)

            self.assertEqual(config_store.ensure_config_current(instance.id), 1)
            backups_after_second_run = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(backups_after_second_run, backups)

    def test_load_effective_config_auto_migrates_legacy_v0_once(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            original_config = json.dumps(
                {
                    "allow_server_administrators": False,
                    "allowed_user_ids": ["123"],
                    "allowed_role_ids": [],
                    "audit_channel_id": None,
                },
                indent=2,
            ).encode("utf-8")
            token_bytes = b"FAKE_TOKEN_KEEP"
            instance.paths.config.write_bytes(original_config)
            instance.paths.token.write_bytes(token_bytes)
            instance.paths.config_meta.unlink()

            effective = config_store.load_effective_config(instance.id)

            self.assertEqual(effective["allow_server_administrators"], False)
            self.assertEqual(effective["allowed_user_ids"], ["123"])
            self.assertEqual(json.loads(instance.paths.config_meta.read_text(encoding="utf-8"))["config_version"], 1)
            self.assertEqual(instance.paths.config.read_bytes(), original_config)
            self.assertEqual(instance.paths.token.read_bytes(), token_bytes)
            backup_root = app_paths.BACKUPS_DIR / "instances" / instance.id / "config"
            backups = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "config.json").read_bytes(), original_config)

            second_effective = config_store.load_effective_config(instance.id)
            second_backups = [path for path in backup_root.iterdir() if path.is_dir()]
            self.assertEqual(second_effective, effective)
            self.assertEqual(second_backups, backups)

    def test_admin_load_config_auto_migrates_legacy_admin_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store, app_paths = load_modules(Path(data_dir), "Admin", "instance_store", "app_paths")
            instance = instance_store.create_instance("admin", "admin-main")
            legacy_config = json.dumps(
                {
                    "allow_server_administrators": False,
                    "allowed_user_ids": ["123"],
                    "allowed_role_ids": [],
                    "audit_channel_id": None,
                }
            ).encode("utf-8")
            token_bytes = b"FAKE_TOKEN_KEEP"
            instance.paths.config.write_bytes(legacy_config)
            instance.paths.token.write_bytes(token_bytes)
            instance.paths.config_meta.unlink()

            loaded = Admin.load_config(Admin.resolve_runtime("admin-main"))

            self.assertFalse(loaded["allow_server_administrators"])
            self.assertEqual(loaded["allowed_user_ids"], [123])
            self.assertEqual(instance.paths.config.read_bytes(), legacy_config)
            self.assertEqual(instance.paths.token.read_bytes(), token_bytes)
            backups = [path for path in (app_paths.BACKUPS_DIR / "instances" / instance.id / "config").iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 1)

    def test_legacy_v0_with_unsupported_target_version_fails_without_effective_config(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, bot_registry = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store)
            instance.paths.config.write_text(json.dumps(VALID_ADMIN_CONFIG), encoding="utf-8")
            instance.paths.config_meta.unlink()
            original_bot_type = bot_registry.get_bot_type("admin")
            future_bot_type = replace(original_bot_type, config_version=2)

            with mock.patch.object(config_store.bot_registry, "get_bot_type", return_value=future_bot_type):
                with mock.patch.object(config_store, "_load_effective_config_current", wraps=config_store._load_effective_config_current) as helper:
                    with self.assertRaisesRegex(config_store.ConfigMigrationError, "no migration path"):
                        config_store.load_effective_config(instance.id)

            helper.assert_not_called()
            self.assertFalse(instance.paths.config_meta.exists())

    def test_malformed_legacy_config_is_not_marked_migrated(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = b"{not-json"
            instance.paths.config.write_bytes(original)
            instance.paths.config_meta.unlink()

            with self.assertRaisesRegex(config_store.ConfigStoreError, "Invalid instance config JSON"):
                config_store.ensure_config_current(instance.id)

            self.assertEqual(instance.paths.config.read_bytes(), original)
            self.assertFalse(instance.paths.config_meta.exists())

    def test_future_config_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config_meta.write_text('{"schema_version": 1, "config_version": 999}\n', encoding="utf-8")

            with self.assertRaisesRegex(config_store.ConfigStoreError, "newer than supported"):
                config_store.load_effective_config(instance.id)

    def test_malformed_config_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config_meta.write_text('{"schema_version": "bad", "config_version": 1}\n', encoding="utf-8")

            with self.assertRaisesRegex(config_store.ConfigStoreError, "schema_version"):
                config_store.get_config_version(instance.id)

    def test_bool_config_metadata_versions_are_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)

            for metadata in (
                {"schema_version": True, "config_version": 1},
                {"schema_version": 1, "config_version": False},
            ):
                with self.subTest(metadata=metadata):
                    instance.paths.config_meta.write_text(json.dumps(metadata), encoding="utf-8")
                    with self.assertRaises(config_store.ConfigStoreError):
                        config_store.get_config_version(instance.id)

    def test_bool_config_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, bot_registry = load_modules(Path(data_dir), "config_store", "bot_registry")
            schema_path = Path(data_dir) / "schema.json"
            schema_path.write_text('{"schema_version": true, "type": "object"}\n', encoding="utf-8")
            bot_type = replace(bot_registry.get_bot_type("admin"), config_schema=schema_path)

            with self.assertRaisesRegex(config_store.ConfigStoreError, "schema version"):
                config_store._load_schema(bot_type)

    def test_invalid_utf8_json_is_wrapped_as_config_store_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config.write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(config_store.ConfigStoreError, "encoding"):
                config_store.load_effective_config(instance.id)

    def test_migration_failure_does_not_modify_config_or_token(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            config_bytes = json.dumps(VALID_ADMIN_CONFIG).encode("utf-8")
            token_bytes = b"FAKE_TOKEN_KEEP"
            instance.paths.config.write_bytes(config_bytes)
            instance.paths.token.write_bytes(token_bytes)
            instance.paths.config_meta.unlink()

            with mock.patch.object(config_store, "_create_config_backup", side_effect=config_store.ConfigMigrationError("backup failed")):
                with self.assertRaisesRegex(config_store.ConfigMigrationError, "backup failed"):
                    config_store.ensure_config_current(instance.id)

            self.assertEqual(instance.paths.config.read_bytes(), config_bytes)
            self.assertEqual(instance.paths.token.read_bytes(), token_bytes)
            self.assertFalse(instance.paths.config_meta.exists())

    def test_metadata_atomic_write_failure_does_not_mark_migration_successful(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            config_bytes = json.dumps(VALID_ADMIN_CONFIG).encode("utf-8")
            token_bytes = b"FAKE_TOKEN_KEEP"
            instance.paths.config.write_bytes(config_bytes)
            instance.paths.token.write_bytes(token_bytes)
            instance.paths.config_meta.unlink()

            with mock.patch.object(config_store, "_atomic_write_bytes", side_effect=OSError("atomic write failed")):
                with self.assertRaisesRegex(config_store.ConfigMigrationError, "atomic write failed"):
                    config_store.ensure_config_current(instance.id)

            self.assertEqual(instance.paths.config.read_bytes(), config_bytes)
            self.assertEqual(instance.paths.token.read_bytes(), token_bytes)
            self.assertFalse(instance.paths.config_meta.exists())
            backup_root = app_paths.BACKUPS_DIR / "instances" / instance.id / "config"
            self.assertTrue(backup_root.is_dir())
            self.assertEqual(len([path for path in backup_root.iterdir() if path.is_dir()]), 1)

    def test_symlinked_legacy_config_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            target_bytes = b"FAKE_SECRET_TARGET_BYTES"
            token_bytes = b"FAKE_TOKEN_KEEP"
            target_path = instance.paths.root / "target-secret.txt"
            target_path.write_bytes(target_bytes)
            instance.paths.token.write_bytes(token_bytes)
            instance.paths.config.unlink()
            instance.paths.config_meta.unlink()
            self.create_file_symlink_or_skip(target_path, instance.paths.config)

            with self.assertRaisesRegex(config_store.ConfigStoreError, "symlink"):
                config_store.load_effective_config(instance.id)
            with self.assertRaisesRegex(config_store.ConfigStoreError, "symlink"):
                config_store.ensure_config_current(instance.id)

            backup_root = app_paths.BACKUPS_DIR / "instances" / instance.id / "config"
            self.assertFalse(backup_root.exists())
            self.assertFalse(instance.paths.config_meta.exists())
            self.assertEqual(target_path.read_bytes(), target_bytes)
            self.assertEqual(instance.paths.token.read_bytes(), token_bytes)

    def test_symlinked_config_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            metadata_target = instance.paths.root / "metadata-target.json"
            metadata_bytes = b'{"schema_version": 1, "config_version": 1}\n'
            metadata_target.write_bytes(metadata_bytes)
            instance.paths.config_meta.unlink()
            self.create_file_symlink_or_skip(metadata_target, instance.paths.config_meta)

            with self.assertRaisesRegex(config_store.ConfigStoreError, "symlink"):
                config_store.get_config_version(instance.id)
            with self.assertRaisesRegex(config_store.ConfigStoreError, "symlink"):
                config_store.load_effective_config(instance.id)

            self.assertEqual(metadata_target.read_bytes(), metadata_bytes)

    def test_save_rejects_symlinked_config_without_writing_target(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            target_path = instance.paths.root / "target.txt"
            target_bytes = b"TARGET_BYTES"
            target_path.write_bytes(target_bytes)
            instance.paths.config.unlink()
            self.create_file_symlink_or_skip(target_path, instance.paths.config)

            with self.assertRaisesRegex(config_store.ConfigStoreError, "symlink"):
                config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})

            self.assertEqual(target_path.read_bytes(), target_bytes)

    def test_schema_failure_is_clear(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            instance.paths.config.write_text('{"allow_server_administrators": "yes"}\n', encoding="utf-8")

            with self.assertRaisesRegex(config_store.ConfigValidationError, "allow_server_administrators"):
                config_store.load_effective_config(instance.id)

    def test_save_invalid_override_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = b'{\n  "allowed_user_ids": ["123"]\n}\n'
            instance.paths.config.write_bytes(original)

            with self.assertRaisesRegex(config_store.ConfigValidationError, "allow_server_administrators"):
                config_store.save_config_overrides(instance.id, {"allow_server_administrators": "yes"})

            self.assertEqual(instance.paths.config.read_bytes(), original)

    def test_save_rejects_non_dict_overrides(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = instance.paths.config.read_bytes()

            with self.assertRaisesRegex(config_store.ConfigValidationError, "overrides"):
                config_store.save_config_overrides(instance.id, ["not", "object"])

            self.assertEqual(instance.paths.config.read_bytes(), original)

    def test_save_migrates_legacy_v0_before_writing(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            legacy = json.dumps(VALID_ADMIN_CONFIG).encode("utf-8")
            instance.paths.config.write_bytes(legacy)
            instance.paths.config_meta.unlink()

            config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})

            self.assertEqual(json.loads(instance.paths.config_meta.read_text(encoding="utf-8"))["config_version"], 1)
            backups = [path for path in (app_paths.BACKUPS_DIR / "instances" / instance.id / "config").iterdir() if path.is_dir()]
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "config.json").read_bytes(), legacy)
            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), {"allowed_user_ids": ["123"]})

    def test_save_unsupported_migration_path_preserves_config(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, bot_registry = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store)
            original = json.dumps(VALID_ADMIN_CONFIG).encode("utf-8")
            instance.paths.config.write_bytes(original)
            instance.paths.config_meta.unlink()
            bot_type = replace(bot_registry.get_bot_type("admin"), config_version=2)

            with mock.patch.object(config_store.bot_registry, "get_bot_type", return_value=bot_type):
                with self.assertRaisesRegex(config_store.ConfigMigrationError, "no migration path"):
                    config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})

            self.assertEqual(instance.paths.config.read_bytes(), original)
            self.assertFalse(instance.paths.config_meta.exists())

    def test_save_future_config_version_preserves_config(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = instance.paths.config.read_bytes()
            instance.paths.config_meta.write_text('{"schema_version": 1, "config_version": 999}\n', encoding="utf-8")

            with self.assertRaisesRegex(config_store.ConfigStoreError, "newer than supported"):
                config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})

            self.assertEqual(instance.paths.config.read_bytes(), original)

    def test_save_atomic_write_failure_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = b'{\n  "allowed_user_ids": ["123"]\n}\n'
            instance.paths.config.write_bytes(original)

            with mock.patch.object(config_store, "_atomic_write_bytes", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(config_store.ConfigStoreError, "replace failed"):
                    config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["456"]})

            self.assertEqual(instance.paths.config.read_bytes(), original)

    def test_save_malformed_current_config_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            original = b"{not-json"
            instance.paths.config.write_bytes(original)

            with self.assertRaisesRegex(config_store.ConfigStoreError, "Invalid instance config JSON"):
                config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})

            self.assertEqual(instance.paths.config.read_bytes(), original)

    def test_save_does_not_mutate_caller_dict(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store = load_modules(Path(data_dir), "config_store", "instance_store")
            instance = self.create_instance(instance_store)
            proposed = {"allowed_user_ids": ["123"], "nested": {"value": ["keep"]}}
            original = copy_via_json(proposed)

            config_store.save_config_overrides(instance.id, proposed)

            self.assertEqual(proposed, original)

    def test_two_instances_have_independent_overrides_versions_and_backups(self):
        with tempfile.TemporaryDirectory() as data_dir:
            config_store, instance_store, app_paths = load_modules(
                Path(data_dir),
                "config_store",
                "instance_store",
                "app_paths",
            )
            first = self.create_instance(instance_store, "admin-main")
            second = self.create_instance(instance_store, "admin-second")
            first.paths.config.write_text('{"allowed_user_ids": ["111"]}\n', encoding="utf-8")
            second.paths.config.write_text('{"allowed_user_ids": ["222"]}\n', encoding="utf-8")
            first.paths.config_meta.unlink()

            config_store.ensure_config_current(first.id)

            self.assertEqual(config_store.load_effective_config(first.id)["allowed_user_ids"], ["111"])
            self.assertEqual(config_store.load_effective_config(second.id)["allowed_user_ids"], ["222"])
            self.assertEqual(config_store.get_config_version(first.id), 1)
            self.assertEqual(config_store.get_config_version(second.id), 1)
            self.assertTrue((app_paths.BACKUPS_DIR / "instances" / "admin-main" / "config").is_dir())
            self.assertFalse((app_paths.BACKUPS_DIR / "instances" / "admin-second" / "config").exists())

    def test_admin_load_config_uses_effective_config_and_validation(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, instance_store = load_modules(Path(data_dir), "Admin", "instance_store")
            instance = instance_store.create_instance("admin", "admin-main")
            instance.paths.config.write_text('{"allowed_user_ids": ["123"]}\n', encoding="utf-8")

            loaded = Admin.load_config(Admin.resolve_runtime("admin-main"))

            self.assertTrue(loaded["allow_server_administrators"])
            self.assertEqual(loaded["allowed_user_ids"], [123])
            self.assertEqual(loaded["allowed_role_ids"], [])
            self.assertIsNone(loaded["audit_channel_id"])

    def test_admin_load_config_after_save_sees_saved_effective_override(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Admin, config_store, instance_store = load_modules(Path(data_dir), "Admin", "config_store", "instance_store")
            instance = instance_store.create_instance("admin", "admin-main")

            config_store.save_config_overrides(instance.id, {"allowed_user_ids": ["123"]})
            loaded = Admin.load_config(Admin.resolve_runtime(instance.id))

            self.assertEqual(loaded["allowed_user_ids"], [123])
            self.assertTrue(loaded["allow_server_administrators"])

    def test_admin_legacy_config_migration_preserves_values(self):
        with tempfile.TemporaryDirectory() as data_dir:
            admin_instance, config_store, app_paths = load_modules(
                Path(data_dir),
                "admin_instance",
                "config_store",
                "app_paths",
            )
            app_paths.CONFIG_DIR.mkdir(parents=True)
            legacy_config = dict(VALID_ADMIN_CONFIG)
            legacy_config["allow_server_administrators"] = False
            legacy_config["allowed_user_ids"] = ["123"]
            app_paths.ADMIN_CONFIG_PATH.write_text(json.dumps(legacy_config), encoding="utf-8")

            instance = admin_instance.ensure_admin_instance()

            self.assertEqual(json.loads(instance.paths.config.read_text(encoding="utf-8")), legacy_config)
            self.assertEqual(config_store.load_effective_config(instance.id), {**legacy_config, **AI_WHITELIST_DEFAULTS})


if __name__ == "__main__":
    unittest.main()
