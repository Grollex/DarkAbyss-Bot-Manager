import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


class AppPathsTests(unittest.TestCase):
    def load_app_paths(self, data_root: Path, legacy_root: Path):
        sys.path.insert(0, str(CORE_ROOT))
        os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
        sys.modules.pop("app_paths", None)
        app_paths = importlib.import_module("app_paths")
        app_paths.LEGACY_ADMIN_CONFIG_PATH = legacy_root / "admin_config.json"
        app_paths.LEGACY_ADMIN_TOKEN_PATH = legacy_root / "admin_bot_token.txt"
        return app_paths

    def test_first_run_creates_directories_and_default_config(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            app_paths = self.load_app_paths(Path(data_dir), Path(legacy_dir))

            app_paths.ensure_user_data()

            self.assertTrue(app_paths.CONFIG_DIR.is_dir())
            self.assertTrue(app_paths.SECRETS_DIR.is_dir())
            self.assertTrue(app_paths.RUNTIME_DIR.is_dir())
            self.assertTrue(app_paths.LOGS_DIR.is_dir())
            self.assertEqual(
                json.loads(app_paths.ADMIN_CONFIG_PATH.read_text(encoding="utf-8")),
                {
                    "allow_server_administrators": True,
                    "allowed_user_ids": [],
                    "allowed_role_ids": [],
                    "audit_channel_id": None,
                },
            )

    def test_existing_user_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            app_paths = self.load_app_paths(Path(data_dir), Path(legacy_dir))
            app_paths.ensure_user_directories()
            custom_config = b'{\n  "allow_server_administrators": false,\n  "allowed_user_ids": ["123"],\n  "allowed_role_ids": [],\n  "audit_channel_id": null\n}\n'
            app_paths.ADMIN_CONFIG_PATH.write_bytes(custom_config)

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), custom_config)

    def test_existing_token_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            app_paths = self.load_app_paths(Path(data_dir), Path(legacy_dir))
            app_paths.ensure_user_directories()
            fake_token = "FAKE_TEST_TOKEN_KEEP_ME"
            app_paths.ADMIN_TOKEN_PATH.write_text(fake_token, encoding="utf-8")

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), fake_token)

    def test_legacy_config_imports_when_destination_absent(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            legacy_root = Path(legacy_dir)
            legacy_config = b'{\n  "allow_server_administrators": false,\n  "allowed_user_ids": ["123"],\n  "allowed_role_ids": ["456"],\n  "audit_channel_id": null\n}\n'
            (legacy_root / "admin_config.json").write_bytes(legacy_config)
            app_paths = self.load_app_paths(Path(data_dir), legacy_root)

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), legacy_config)

    def test_legacy_config_does_not_overwrite_new_config(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            legacy_root = Path(legacy_dir)
            (legacy_root / "admin_config.json").write_text(
                '{"allow_server_administrators": true, "allowed_user_ids": [], "allowed_role_ids": [], "audit_channel_id": null}',
                encoding="utf-8",
            )
            app_paths = self.load_app_paths(Path(data_dir), legacy_root)
            app_paths.ensure_user_directories()
            new_config = b'{\n  "allow_server_administrators": false,\n  "allowed_user_ids": ["789"],\n  "allowed_role_ids": [],\n  "audit_channel_id": null\n}\n'
            app_paths.ADMIN_CONFIG_PATH.write_bytes(new_config)

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_CONFIG_PATH.read_bytes(), new_config)

    def test_legacy_token_imports_when_destination_absent(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            legacy_root = Path(legacy_dir)
            legacy_token_path = legacy_root / "admin_bot_token.txt"
            legacy_token_path.write_text("FAKE_LEGACY_TOKEN", encoding="utf-8")
            app_paths = self.load_app_paths(Path(data_dir), legacy_root)

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), "FAKE_LEGACY_TOKEN")
            self.assertEqual(legacy_token_path.read_text(encoding="utf-8"), "FAKE_LEGACY_TOKEN")

    def test_placeholder_legacy_token_creates_normal_placeholder_when_destination_absent(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            legacy_root = Path(legacy_dir)
            app_paths = self.load_app_paths(Path(data_dir), legacy_root)
            app_paths.LEGACY_ADMIN_TOKEN_PATH.write_text(
                app_paths.TOKEN_PLACEHOLDER,
                encoding="utf-8",
            )

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER + "\n")
            self.assertEqual(app_paths.LEGACY_ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), app_paths.TOKEN_PLACEHOLDER)

    def test_placeholder_legacy_token_does_not_overwrite_existing_token(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            legacy_root = Path(legacy_dir)
            app_paths = self.load_app_paths(Path(data_dir), legacy_root)
            app_paths.ensure_user_directories()
            app_paths.ADMIN_TOKEN_PATH.write_text("FAKE_TEST_TOKEN_KEEP_ME", encoding="utf-8")
            app_paths.LEGACY_ADMIN_TOKEN_PATH.write_text(
                app_paths.TOKEN_PLACEHOLDER,
                encoding="utf-8",
            )

            app_paths.ensure_user_data()

            self.assertEqual(app_paths.ADMIN_TOKEN_PATH.read_text(encoding="utf-8"), "FAKE_TEST_TOKEN_KEEP_ME")

    def test_lock_path_is_inside_data_root_runtime(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as legacy_dir:
            app_paths = self.load_app_paths(Path(data_dir), Path(legacy_dir))

            self.assertEqual(app_paths.ADMIN_LOCK_PATH.parent, app_paths.RUNTIME_DIR)
            self.assertTrue(str(app_paths.ADMIN_LOCK_PATH).startswith(str(app_paths.DATA_ROOT)))
            self.assertNotEqual(app_paths.ADMIN_LOCK_PATH.parent, app_paths.PROGRAM_ROOT)

    def test_direct_script_initializes_override_data_root(self):
        with tempfile.TemporaryDirectory() as data_dir:
            data_root = Path(data_dir)
            env = os.environ.copy()
            env["DARKABYSS_DATA_DIR"] = str(data_root)

            result = subprocess.run(
                [sys.executable, str(CORE_ROOT / "app_paths.py")],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(data_root.resolve()), result.stdout)
            self.assertTrue((data_root / "config" / "admin.json").is_file())
            self.assertTrue((data_root / "secrets" / "admin_bot_token.txt").is_file())
            self.assertTrue((data_root / "runtime").is_dir())
            self.assertTrue((data_root / "logs").is_dir())
            self.assertEqual(
                (data_root / "secrets" / "admin_bot_token.txt").read_text(encoding="utf-8"),
                "PUT_DISCORD_BOT_TOKEN_HERE\n",
            )


if __name__ == "__main__":
    unittest.main()
