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


class BotRegistryTests(unittest.TestCase):
    def load_modules(self, data_root: Path):
        sys.path.insert(0, str(CORE_ROOT))
        os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
        for module_name in ("bot_registry", "app_paths"):
            sys.modules.pop(module_name, None)
        return importlib.import_module("bot_registry")

    def write_manifest(self, bots_dir: Path, directory_name: str, payload: dict | str):
        bot_dir = bots_dir / directory_name
        bot_dir.mkdir(parents=True)
        manifest_path = bot_dir / "manifest.json"
        if isinstance(payload, str):
            manifest_path.write_text(payload, encoding="utf-8")
        else:
            manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return manifest_path

    def valid_manifest(self, bot_type_id: str = "admin") -> dict:
        return {
            "schema_version": 1,
            "id": bot_type_id,
            "display_name": "Admin Bot",
            "version": "1.0.0",
            "entrypoint": "DarkAbyss_Core/Admin.py",
            "default_config": "DarkAbyss_Core/defaults/admin_config.json",
            "config_schema": "bots/admin/config.schema.json",
            "config_version": 1,
        }

    def test_registry_discovers_admin_bot_type(self):
        with tempfile.TemporaryDirectory() as data_dir:
            bot_registry = self.load_modules(Path(data_dir))

            bot_types = bot_registry.discover_bot_types()

            self.assertIn("admin", bot_types)
            self.assertEqual(bot_types["admin"].display_name, "Admin Bot")

    def test_admin_manifest_validates_successfully(self):
        with tempfile.TemporaryDirectory() as data_dir:
            bot_registry = self.load_modules(Path(data_dir))

            bot_type = bot_registry.get_bot_type("admin")

            self.assertEqual(bot_type.schema_version, 1)
            self.assertEqual(bot_type.id, "admin")
            self.assertTrue(bot_type.entrypoint.samefile(PROJECT_ROOT / "DarkAbyss_Core" / "Admin.py"))
            self.assertTrue(bot_type.default_config.samefile(PROJECT_ROOT / "DarkAbyss_Core" / "defaults" / "admin_config.json"))
            self.assertTrue(bot_type.config_schema.samefile(PROJECT_ROOT / "bots" / "admin" / "config.schema.json"))
            self.assertEqual(bot_type.config_version, 1)

    def test_missing_manifest_is_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            (Path(bots_dir) / "admin").mkdir()

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "missing manifest"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_malformed_manifest_is_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            self.write_manifest(Path(bots_dir), "admin", "{not json")

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "malformed manifest"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_bot_type_id_and_path_traversal_attempts_are_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            self.write_manifest(Path(bots_dir), "admin", {**self.valid_manifest("../evil")})

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "Invalid bot type id"):
                bot_registry.discover_bot_types(Path(bots_dir))

        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            manifest = self.valid_manifest("admin")
            manifest["entrypoint"] = "../evil.py"
            self.write_manifest(Path(bots_dir), "admin", manifest)

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "path traversal"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_manifest_program_paths_must_be_files(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            manifest = self.valid_manifest("admin")
            manifest["default_config"] = "DarkAbyss_Core/defaults"
            self.write_manifest(Path(bots_dir), "admin", manifest)

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "must be a file"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_manifest_config_schema_path_is_safely_validated(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            manifest = self.valid_manifest("admin")
            manifest["config_schema"] = "../evil.schema.json"
            self.write_manifest(Path(bots_dir), "admin", manifest)

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "path traversal"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_manifest_bool_config_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as bots_dir:
            bot_registry = self.load_modules(Path(data_dir))
            manifest = self.valid_manifest("admin")
            manifest["config_version"] = True
            self.write_manifest(Path(bots_dir), "admin", manifest)

            with self.assertRaisesRegex(bot_registry.BotRegistryError, "config_version"):
                bot_registry.discover_bot_types(Path(bots_dir))

    def test_registry_discovery_does_not_import_or_execute_admin_py(self):
        with tempfile.TemporaryDirectory() as data_dir:
            bot_registry = self.load_modules(Path(data_dir))
            sys.modules.pop("Admin", None)
            sys.modules.pop("DarkAbyss_Core.Admin", None)

            with mock.patch("builtins.__import__", side_effect=AssertionError("unexpected import")):
                bot_types = bot_registry.discover_bot_types()

            self.assertIn("admin", bot_types)
            self.assertNotIn("Admin", sys.modules)
            self.assertNotIn("DarkAbyss_Core.Admin", sys.modules)


if __name__ == "__main__":
    unittest.main()
