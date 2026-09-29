import hashlib
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


def load_update_engine(data_root: Path):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in ("update_engine", "app_paths"):
        sys.modules.pop(module_name, None)
    return importlib.import_module("update_engine")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_release(root: Path, version: str, files: dict[str, bytes], schema_version: int = 1) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    for relative_path, payload in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        manifest_files.append(
            {
                "path": relative_path,
                "sha256": sha256_bytes(payload),
                "size": len(payload),
            }
        )
    (root / "release.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "version": version,
                "files": manifest_files,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


class UpdateEngineTests(unittest.TestCase):
    def create_file_symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlinks are not supported for this test environment: {exc}")

    def create_dir_symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks are not supported for this test environment: {exc}")

    def with_engine(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        data_root = root / "user-data"
        install_root = root / "install-root"
        release_root = root / "release"
        update_engine = load_update_engine(data_root)
        return update_engine, data_root, install_root, release_root

    def valid_release(self, release_root: Path, version: str = "1.0.0", files: dict[str, bytes] | None = None) -> Path:
        return write_release(
            release_root,
            version,
            files
            or {
                "DarkAbyss_Core/example.py": b"print('hello')\n",
                "bots/admin/manifest.json": b'{"id":"admin"}\n',
            },
        )

    def test_valid_release_manifest_accepted(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root)

        info = update_engine.inspect_release(release_root)

        self.assertEqual(info.version, "1.0.0")
        self.assertEqual([item.path for item in info.files], ["DarkAbyss_Core/example.py", "bots/admin/manifest.json"])

    def test_malformed_manifest_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        release_root.mkdir()
        (release_root / "release.json").write_text("{bad", encoding="utf-8")

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "Malformed"):
            update_engine.inspect_release(release_root)

    def test_unsupported_schema_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
        manifest["schema_version"] = 999
        (release_root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "schema"):
            update_engine.inspect_release(release_root)

    def test_absolute_path_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        release_root.mkdir()
        (release_root / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "1.0.0",
                    "files": [{"path": "/absolute.txt", "sha256": sha256_bytes(b"x"), "size": 1}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "relative"):
            update_engine.inspect_release(release_root)

    def test_parent_traversal_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        release_root.mkdir()
        outside = release_root.parent / "escape.txt"
        outside.write_bytes(b"x")
        (release_root / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "1.0.0",
                    "files": [{"path": "../escape.txt", "sha256": sha256_bytes(b"x"), "size": 1}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "traversal"):
            update_engine.inspect_release(release_root)

    def test_windows_drive_path_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        release_root.mkdir()
        (release_root / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "1.0.0",
                    "files": [{"path": "C:/payload.txt", "sha256": sha256_bytes(b"x"), "size": 1}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "relative"):
            update_engine.inspect_release(release_root)

    def test_duplicate_release_path_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"x"})
        manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
        manifest["files"].append({"path": "APP/file.txt", "sha256": sha256_bytes(b"x"), "size": 1})
        (release_root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(update_engine.ReleaseManifestError, "Duplicate"):
            update_engine.inspect_release(release_root)

    def test_missing_file_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"x"})
        (release_root / "app" / "file.txt").unlink()

        with self.assertRaisesRegex(update_engine.ReleaseVerificationError, "Missing"):
            update_engine.inspect_release(release_root)

    def test_wrong_size_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"x"})
        manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
        manifest["files"][0]["size"] = 999
        (release_root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(update_engine.ReleaseVerificationError, "size"):
            update_engine.inspect_release(release_root)

    def test_wrong_sha256_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"x"})
        manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        (release_root / "release.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(update_engine.ReleaseVerificationError, "sha256"):
            update_engine.inspect_release(release_root)

    def test_symlink_escape_rejected_where_supported(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        release_root.mkdir()
        outside = release_root.parent / "secret.txt"
        outside.write_bytes(b"SECRET")
        link = release_root / "app" / "linked.txt"
        link.parent.mkdir()
        self.create_file_symlink_or_skip(outside, link)
        (release_root / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "1.0.0",
                    "files": [{"path": "app/linked.txt", "sha256": sha256_bytes(b"SECRET"), "size": 6}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(update_engine.ReleaseVerificationError, "symlink"):
            update_engine.inspect_release(release_root)

    def test_valid_release_stages_successfully_and_files_match(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})

        result = update_engine.stage_release(release_root, install_root)

        self.assertEqual(result.version, "1.0.0")
        self.assertEqual((result.version_dir / "app" / "file.txt").read_bytes(), b"payload")
        self.assertEqual((result.version_dir / "release.json").read_text(encoding="utf-8"), (release_root / "release.json").read_text(encoding="utf-8"))

    def test_incomplete_staging_cleaned_on_copy_failure(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/one.txt": b"one", "app/two.txt": b"two"})
        original_copyfile = update_engine.shutil.copyfile

        def failing_copyfile(source, destination):
            if Path(source).name == "two.txt":
                raise OSError("copy failed")
            return original_copyfile(source, destination)

        with mock.patch.object(update_engine.shutil, "copyfile", side_effect=failing_copyfile):
            with self.assertRaisesRegex(update_engine.ReleaseStageError, "copy failed"):
                update_engine.stage_release(release_root, install_root)

        self.assertFalse((install_root / "versions" / "1.0.0").exists())
        staging_root = install_root / "updates" / "staging"
        leftovers = [] if not staging_root.exists() else list(staging_root.iterdir())
        self.assertEqual(leftovers, [])

    def test_symlinked_updates_directory_rejected_before_outside_write(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})
        install_root.mkdir()
        outside = install_root.parent / "outside-updates"
        outside.mkdir()
        self.create_dir_symlink_or_skip(outside, install_root / "updates")

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "symlink"):
            update_engine.stage_release(release_root, install_root)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((install_root / "versions" / "1.0.0").exists())
        self.assertFalse((install_root / "current.json").exists())

    def test_symlinked_staging_directory_rejected_before_outside_write(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})
        staging_parent = install_root / "updates"
        staging_parent.mkdir(parents=True)
        outside = install_root.parent / "outside-staging"
        outside.mkdir()
        self.create_dir_symlink_or_skip(outside, staging_parent / "staging")

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "symlink"):
            update_engine.stage_release(release_root, install_root)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((install_root / "versions" / "1.0.0").exists())
        self.assertFalse((install_root / "current.json").exists())

    def test_symlinked_versions_directory_rejected(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})
        install_root.mkdir()
        outside = install_root.parent / "outside-versions"
        outside.mkdir()
        self.create_dir_symlink_or_skip(outside, install_root / "versions")

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "symlink"):
            update_engine.stage_release(release_root, install_root)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((install_root / "current.json").exists())

    def test_existing_version_not_overwritten(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})
        update_engine.stage_release(release_root, install_root)

        with self.assertRaisesRegex(update_engine.ReleaseStageError, "already exists"):
            update_engine.stage_release(release_root, install_root)

        self.assertEqual((install_root / "versions" / "1.0.0" / "app" / "file.txt").read_bytes(), b"payload")

    def test_activation_writes_current_json(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root)
        update_engine.stage_release(release_root, install_root)

        result = update_engine.activate_staged_release("1.0.0", install_root)

        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(update_engine.get_current_version(install_root), "1.0.0")
        self.assertEqual(json.loads((install_root / "current.json").read_text(encoding="utf-8"))["version"], "1.0.0")

    def test_old_current_json_without_previous_version_remains_readable(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        (install_root / "current.json").write_text(
            json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )

        state = update_engine.get_activation_state(install_root)

        self.assertEqual(update_engine.get_current_version(install_root), "1.0.0")
        self.assertEqual(state.version, "1.0.0")
        self.assertIsNone(state.previous_version)

    def test_first_activation_records_null_previous_version(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)

        result = update_engine.activate_staged_release("1.0.0", install_root)
        pointer = json.loads((install_root / "current.json").read_text(encoding="utf-8"))

        self.assertIsNone(result.previous_version)
        self.assertIsNone(pointer["previous_version"])

    def test_activation_records_previous_version_and_state_reader(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0")
        self.valid_release(release_root / "two", version="1.1.0")
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)

        result = update_engine.activate_staged_release("1.1.0", install_root)
        state = update_engine.get_activation_state(install_root)
        pointer = json.loads((install_root / "current.json").read_text(encoding="utf-8"))

        self.assertEqual(result.previous_version, "1.0.0")
        self.assertEqual(state.version, "1.1.0")
        self.assertEqual(state.previous_version, "1.0.0")
        self.assertEqual(pointer["previous_version"], "1.0.0")

    def test_rollback_to_version_switches_pointer_only(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0", files={"app/file.txt": b"one"})
        self.valid_release(release_root / "two", version="1.1.0", files={"app/file.txt": b"two"})
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)
        update_engine.activate_staged_release("1.1.0", install_root)

        result = update_engine.rollback_to_version("1.0.0", install_root)
        state = update_engine.get_activation_state(install_root)

        self.assertTrue(result.changed)
        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(result.previous_version, "1.1.0")
        self.assertEqual(state.version, "1.0.0")
        self.assertEqual(state.previous_version, "1.1.0")
        self.assertTrue((install_root / "versions" / "1.0.0").is_dir())
        self.assertTrue((install_root / "versions" / "1.1.0").is_dir())

    def test_rollback_to_previous_uses_recorded_previous_version(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0")
        self.valid_release(release_root / "two", version="1.1.0")
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)
        update_engine.activate_staged_release("1.1.0", install_root)

        result = update_engine.rollback_to_previous(install_root)

        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(result.previous_version, "1.1.0")
        self.assertEqual(update_engine.get_activation_state(install_root).previous_version, "1.1.0")

    def test_rollback_to_previous_without_previous_fails(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.0.0", install_root)

        with self.assertRaisesRegex(update_engine.RollbackError, "previous_version"):
            update_engine.rollback_to_previous(install_root)

    def test_rollback_target_missing_fails(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.1.0")
        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.1.0", install_root)

        with self.assertRaisesRegex(update_engine.RollbackError, "does not exist"):
            update_engine.rollback_to_version("1.0.0", install_root)

    def test_rollback_target_corrupt_hash_fails(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0", files={"app/file.txt": b"one"})
        self.valid_release(release_root / "two", version="1.1.0", files={"app/file.txt": b"two"})
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.stage_release(release_root / "two", install_root)
        update_engine.activate_staged_release("1.1.0", install_root)
        (install_root / "versions" / "1.0.0" / "app" / "file.txt").write_bytes(b"corrupt")

        with self.assertRaisesRegex(update_engine.RollbackError, "failed verification"):
            update_engine.rollback_to_version("1.0.0", install_root)

        self.assertEqual(update_engine.get_current_version(install_root), "1.1.0")

    def test_rollback_target_release_version_mismatch_fails(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.1.0")
        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.1.0", install_root)
        mismatch_dir = install_root / "versions" / "1.0.0"
        self.valid_release(mismatch_dir, version="9.9.9", files={"app/file.txt": b"wrong"})

        with self.assertRaisesRegex(update_engine.RollbackError, "metadata mismatch"):
            update_engine.rollback_to_version("1.0.0", install_root)

    def test_rollback_current_version_is_noop(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        before = (install_root / "current.json").read_bytes()

        result = update_engine.rollback_to_version("1.0.0", install_root)

        self.assertFalse(result.changed)
        self.assertEqual((install_root / "current.json").read_bytes(), before)

    def test_rollback_pointer_write_failure_preserves_previous_pointer(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0")
        self.valid_release(release_root / "two", version="1.1.0")
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)
        update_engine.activate_staged_release("1.1.0", install_root)
        before = (install_root / "current.json").read_bytes()

        with mock.patch.object(update_engine.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(update_engine.RollbackError, "replace failed"):
                update_engine.rollback_to_version("1.0.0", install_root)

        self.assertEqual((install_root / "current.json").read_bytes(), before)
        self.assertEqual(update_engine.get_current_version(install_root), "1.1.0")

    def test_recovery_from_missing_current_pointer_to_explicit_target(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)

        result = update_engine.recover_current_pointer("1.0.0", install_root)

        self.assertEqual(result.version, "1.0.0")
        self.assertIsNone(result.previous_version)
        self.assertEqual(update_engine.get_current_version(install_root), "1.0.0")

    def test_recovery_from_malformed_current_pointer(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        (install_root / "current.json").write_text("{bad", encoding="utf-8")

        result = update_engine.recover_current_pointer("1.0.0", install_root)

        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(update_engine.get_current_version(install_root), "1.0.0")

    def test_recovery_pointer_write_failure_preserves_malformed_pointer_and_user_data(self):
        update_engine, data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0", files={"app/file.txt": b"payload"})
        update_engine.stage_release(release_root, install_root)
        token = data_root / "instances" / "admin-main" / "secrets" / "token.txt"
        config = data_root / "instances" / "admin-main" / "config.json"
        database = data_root / "instances" / "admin-main" / "data" / "state.db"
        token.parent.mkdir(parents=True)
        database.parent.mkdir(parents=True)
        token.write_bytes(b"TOKEN_SENTINEL")
        config.write_bytes(b'{"override": true}')
        database.write_bytes(b"DB_SENTINEL")
        user_before = {path: path.read_bytes() for path in (token, config, database)}
        current_path = install_root / "current.json"
        current_path.write_bytes(b"{bad")
        pointer_before = current_path.read_bytes()
        version_file = install_root / "versions" / "1.0.0" / "app" / "file.txt"
        version_before = version_file.read_bytes()

        with mock.patch.object(update_engine.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(update_engine.RecoveryError, "replace failed"):
                update_engine.recover_current_pointer("1.0.0", install_root)

        self.assertEqual(current_path.read_bytes(), pointer_before)
        self.assertEqual(version_file.read_bytes(), version_before)
        self.assertEqual({path: path.read_bytes() for path in (token, config, database)}, user_before)
        self.assertEqual(list(install_root.glob(".current.json.*.tmp")), [])

    def test_recovery_to_missing_target_fails_without_guessing(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)

        with self.assertRaisesRegex(update_engine.RecoveryError, "does not exist"):
            update_engine.recover_current_pointer("2.0.0", install_root)

        self.assertIsNone(update_engine.get_current_version(install_root))

    def test_recovery_to_corrupt_target_fails(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0", files={"app/file.txt": b"one"})
        update_engine.stage_release(release_root, install_root)
        (install_root / "versions" / "1.0.0" / "app" / "file.txt").write_bytes(b"corrupt")

        with self.assertRaisesRegex(update_engine.RecoveryError, "failed verification"):
            update_engine.recover_current_pointer("1.0.0", install_root)

        self.assertFalse((install_root / "current.json").exists())

    def test_recovery_rejects_unnecessary_healthy_pointer(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.0.0", install_root)

        with self.assertRaisesRegex(update_engine.RecoveryError, "healthy"):
            update_engine.recover_current_pointer("1.0.0", install_root)

    def test_install_health_states(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()

        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.NO_CURRENT_POINTER)

        install_root.mkdir(parents=True, exist_ok=True)
        (install_root / "current.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.INVALID_CURRENT_POINTER)

        (install_root / "current.json").write_text(
            json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.CURRENT_VERSION_MISSING)

        self.valid_release(release_root, version="1.0.0", files={"app/file.txt": b"one"})
        update_engine.stage_release(release_root, install_root)
        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.HEALTHY)

        (install_root / "versions" / "1.0.0" / "app" / "file.txt").write_bytes(b"corrupt")
        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.CURRENT_VERSION_CORRUPT)

    def test_malformed_previous_version_is_rejected(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0")
        update_engine.stage_release(release_root, install_root)
        (install_root / "current.json").write_text(
            json.dumps({"schema_version": 1, "version": "1.0.0", "previous_version": True}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(update_engine.CurrentPointerError, "previous_version"):
            update_engine.get_activation_state(install_root)

    def test_rollback_recovery_preserve_user_sentinels_and_versions(self):
        update_engine, data_root, install_root, release_root = self.with_engine()
        token = data_root / "instances" / "admin-main" / "secrets" / "token.txt"
        config = data_root / "instances" / "admin-main" / "config.json"
        database = data_root / "instances" / "admin-main" / "data" / "state.db"
        token.parent.mkdir(parents=True)
        database.parent.mkdir(parents=True)
        token.write_bytes(b"TOKEN_SENTINEL")
        config.write_bytes(b'{"override": true}')
        database.write_bytes(b"DB_SENTINEL")
        before = {path: path.read_bytes() for path in (token, config, database)}
        self.valid_release(release_root / "one", version="1.0.0", files={"app/file.txt": b"one"})
        self.valid_release(release_root / "two", version="1.1.0", files={"app/file.txt": b"two"})
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)
        update_engine.activate_staged_release("1.1.0", install_root)
        update_engine.rollback_to_previous(install_root)
        (install_root / "current.json").write_text("{bad", encoding="utf-8")
        update_engine.recover_current_pointer("1.1.0", install_root)

        self.assertEqual({path: path.read_bytes() for path in (token, config, database)}, before)
        self.assertTrue((install_root / "versions" / "1.0.0").is_dir())
        self.assertTrue((install_root / "versions" / "1.1.0").is_dir())
        self.assertEqual(update_engine.get_current_version(install_root), "1.1.0")

    def test_current_pointer_write_failure_preserves_old_pointer(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0")
        self.valid_release(release_root / "two", version="1.1.0")
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)

        with mock.patch.object(update_engine.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(update_engine.ActivationError, "replace failed"):
                update_engine.activate_staged_release("1.1.0", install_root)

        self.assertEqual(update_engine.get_current_version(install_root), "1.0.0")
        self.assertTrue((install_root / "versions" / "1.1.0").is_dir())

    def test_current_pointer_symlink_is_not_followed_on_activation(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})
        update_engine.stage_release(release_root, install_root)
        outside_current = install_root.parent / "outside-current.json"
        outside_bytes = b'{"schema_version": 1, "version": "outside"}\n'
        outside_current.write_bytes(outside_bytes)
        self.create_file_symlink_or_skip(outside_current, install_root / "current.json")

        with self.assertRaises(update_engine.CurrentPointerError):
            update_engine.activate_staged_release("1.0.0", install_root)

        self.assertEqual(outside_current.read_bytes(), outside_bytes)
        self.assertTrue((install_root / "current.json").is_symlink())

    def test_broken_current_pointer_symlink_is_invalid_and_not_replaced(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, version="1.0.0", files={"app/file.txt": b"payload"})
        update_engine.stage_release(release_root, install_root)
        install_root.mkdir(parents=True, exist_ok=True)
        missing_target = install_root.parent / "missing-current-target.json"
        current_link = install_root / "current.json"
        self.create_file_symlink_or_skip(missing_target, current_link)

        with self.assertRaises(update_engine.CurrentPointerError):
            update_engine.get_activation_state(install_root)
        self.assertEqual(update_engine.check_install_health(install_root).state, update_engine.INVALID_CURRENT_POINTER)
        with self.assertRaisesRegex(update_engine.RecoveryError, "regular file"):
            update_engine.recover_current_pointer("1.0.0", install_root)

        self.assertFalse(missing_target.exists())
        self.assertTrue(current_link.is_symlink())

    def test_previous_version_remains_installed_after_activating_new_one(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root / "one", version="1.0.0", files={"app/file.txt": b"one"})
        self.valid_release(release_root / "two", version="1.1.0", files={"app/file.txt": b"two"})
        update_engine.stage_release(release_root / "one", install_root)
        update_engine.activate_staged_release("1.0.0", install_root)
        update_engine.stage_release(release_root / "two", install_root)

        result = update_engine.activate_staged_release("1.1.0", install_root)

        self.assertEqual(result.previous_version, "1.0.0")
        self.assertEqual(update_engine.get_current_version(install_root), "1.1.0")
        self.assertTrue((install_root / "versions" / "1.0.0").is_dir())
        self.assertTrue((install_root / "versions" / "1.1.0").is_dir())

    def test_list_installed_versions_deterministic(self):
        update_engine, _data_root, install_root, _release_root = self.with_engine()
        (install_root / "versions" / "2.0.0").mkdir(parents=True)
        (install_root / "versions" / "1.0.0").mkdir()

        self.assertEqual(update_engine.list_installed_versions(install_root), ["1.0.0", "2.0.0"])

    def test_malformed_current_json_rejected(self):
        update_engine, _data_root, install_root, _release_root = self.with_engine()
        install_root.mkdir()
        (install_root / "current.json").write_text("{bad", encoding="utf-8")

        with self.assertRaisesRegex(update_engine.CurrentPointerError, "Malformed"):
            update_engine.get_current_version(install_root)

    def test_future_current_schema_rejected(self):
        update_engine, _data_root, install_root, _release_root = self.with_engine()
        (install_root / "versions" / "1.0.0").mkdir(parents=True)
        (install_root / "current.json").write_text('{"schema_version": 999, "version": "1.0.0"}\n', encoding="utf-8")

        with self.assertRaisesRegex(update_engine.CurrentPointerError, "schema"):
            update_engine.get_current_version(install_root)

    def test_reserved_user_data_release_roots_rejected(self):
        update_engine, _data_root, _install_root, release_root = self.with_engine()
        for reserved_root in ("instances", "secrets", "runtime", "logs", "backups", "user_data", "config", "databases"):
            with self.subTest(reserved_root=reserved_root):
                candidate = release_root / reserved_root
                self.valid_release(candidate, files={f"{reserved_root}/sentinel.txt": b"x"})
                with self.assertRaisesRegex(update_engine.ReleaseManifestError, "reserved"):
                    update_engine.inspect_release(candidate)

    def test_data_root_content_unchanged_across_install_and_activate(self):
        update_engine, data_root, install_root, release_root = self.with_engine()
        sentinel_path = data_root / "instances" / "admin-main" / "data" / "db.sqlite"
        sentinel_path.parent.mkdir(parents=True)
        sentinel_path.write_bytes(b"USER_DB_SENTINEL")
        before = sentinel_path.read_bytes()
        self.valid_release(release_root, files={"app/file.txt": b"program"})

        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.0.0", install_root)

        self.assertEqual(sentinel_path.read_bytes(), before)
        self.assertFalse((data_root / "versions").exists())
        self.assertFalse((data_root / "updates").exists())

    def test_install_root_equal_data_root_rejected(self):
        update_engine, data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "disjoint"):
            update_engine.stage_release(release_root, data_root)

    def test_install_root_inside_data_root_rejected(self):
        update_engine, data_root, _install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "disjoint"):
            update_engine.stage_release(release_root, data_root / "program")

    def test_data_root_inside_install_root_rejected(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        install_root = root / "install-root"
        data_root = install_root / "user-data"
        release_root = root / "release"
        update_engine = load_update_engine(data_root)
        self.valid_release(release_root, files={"app/file.txt": b"payload"})

        with self.assertRaisesRegex(update_engine.UpdateEngineError, "DATA_ROOT"):
            update_engine.stage_release(release_root, install_root)

    def test_sibling_install_root_and_data_root_remain_valid(self):
        update_engine, _data_root, install_root, release_root = self.with_engine()
        self.valid_release(release_root, files={"app/file.txt": b"payload"})

        result = update_engine.stage_release(release_root, install_root)

        self.assertEqual(result.version, "1.0.0")

    def test_token_config_database_sentinels_remain_identical(self):
        update_engine, data_root, install_root, release_root = self.with_engine()
        token = data_root / "instances" / "admin-main" / "secrets" / "token.txt"
        config = data_root / "instances" / "admin-main" / "config.json"
        database = data_root / "instances" / "admin-main" / "data" / "state.db"
        token.parent.mkdir(parents=True)
        database.parent.mkdir(parents=True)
        token.write_bytes(b"TOKEN_SENTINEL")
        config.write_bytes(b'{"override": true}')
        database.write_bytes(b"DB_SENTINEL")
        before = {path: path.read_bytes() for path in (token, config, database)}
        self.valid_release(release_root, files={"DarkAbyss_Core/update_code.py": b"program"})

        update_engine.stage_release(release_root, install_root)
        update_engine.activate_staged_release("1.0.0", install_root)

        self.assertEqual({path: path.read_bytes() for path in (token, config, database)}, before)


if __name__ == "__main__":
    unittest.main()
