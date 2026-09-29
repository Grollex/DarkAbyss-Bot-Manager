import hashlib
import importlib
import importlib.util
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


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def reset_core_modules(data_root: Path, *names: str):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in (
        "launcher",
        "release_manifest",
        "app_entry",
        "runtime_layout",
        "manager_core",
        "admin_instance",
        "config_store",
        "instance_store",
        "bot_registry",
        "app_paths",
        "update_engine",
    ):
        sys.modules.pop(module_name, None)
    return [importlib.import_module(name) for name in names]


def load_assembler(data_root: Path):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in ("launcher", "release_manifest", "runtime_layout", "update_engine", "assemble_distribution"):
        sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        "assemble_distribution",
        PROJECT_ROOT / "packaging" / "assemble_distribution.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["assemble_distribution"] = module
    spec.loader.exec_module(module)
    return module


def write_release_manifest(root: Path, version: str, files: dict[str, bytes]) -> None:
    manifest_files = []
    for relative_path, payload in files.items():
        file_path = root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(payload)
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
                "schema_version": 1,
                "version": version,
                "files": sorted(manifest_files, key=lambda item: item["path"]),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class PackagedRuntimeTests(unittest.TestCase):
    def create_instance(self, instance_store, instance_id: str = "admin-main"):
        instance = instance_store.create_instance("admin", instance_id)
        instance.paths.config.write_text(json.dumps(VALID_ADMIN_CONFIG), encoding="utf-8")
        instance.paths.token.write_text("FAKE_TEST_TOKEN", encoding="utf-8")
        return instance

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

    def test_packaged_launch_spec_uses_app_executable_and_bot_runner_args(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry, runtime_layout, app_paths = reset_core_modules(
                root / "data",
                "manager_core",
                "instance_store",
                "bot_registry",
                "runtime_layout",
                "app_paths",
            )
            instance = self.create_instance(instance_store)
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            app_exe = version_dir / runtime_layout.app_executable_name()
            app_exe.write_bytes(b"fake exe")

            spec = manager_core.build_packaged_launch_spec(
                instance,
                bot_registry.get_bot_type("admin"),
                app_exe,
                version_dir,
            )

            self.assertEqual(spec.executable, str(app_exe.resolve()))
            self.assertEqual(spec.args, ("--bot-runner", "admin", "--instance", "admin-main"))
            self.assertEqual(spec.cwd, version_dir.resolve())
            self.assertEqual(spec.env["DARKABYSS_DATA_DIR"], str(app_paths.DATA_ROOT.resolve()))
            self.assertEqual(spec.stdout_log_path, instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME)
            self.assertEqual(spec.stderr_log_path, instance.paths.logs_dir / manager_core.STDERR_LOG_NAME)
            self.assertNotIn("FAKE_TEST_TOKEN", " ".join(spec.command))

    def test_packaged_launch_spec_rejects_executable_outside_version_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry = reset_core_modules(
                root / "data",
                "manager_core",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store)
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            outside_exe = root / "outside.exe"
            outside_exe.write_bytes(b"fake exe")

            with self.assertRaisesRegex(manager_core.ProcessStartError, "inside version"):
                manager_core.build_packaged_launch_spec(
                    instance,
                    bot_registry.get_bot_type("admin"),
                    outside_exe,
                    version_dir,
                )

    def test_packaged_launch_spec_rejects_missing_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry = reset_core_modules(
                root / "data",
                "manager_core",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store)
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "missing"):
                manager_core.build_packaged_launch_spec(
                    instance,
                    bot_registry.get_bot_type("admin"),
                    version_dir / "DarkAbyssApp.exe",
                    version_dir,
                )

    def test_packaged_launch_spec_rejects_symlinked_executable_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry, runtime_layout = reset_core_modules(
                root / "data",
                "manager_core",
                "instance_store",
                "bot_registry",
                "runtime_layout",
            )
            instance = self.create_instance(instance_store)
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            real_exe = version_dir / "real.exe"
            real_exe.write_bytes(b"fake exe")
            link = version_dir / runtime_layout.app_executable_name()
            self.create_file_symlink_or_skip(real_exe, link)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "symlink"):
                manager_core.build_packaged_launch_spec(
                    instance,
                    bot_registry.get_bot_type("admin"),
                    link,
                    version_dir,
                )

    def test_current_version_dir_rejects_symlinked_parent_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_layout, = reset_core_modules(root / "data", "runtime_layout")
            real_version_dir = root / "real-version"
            real_version_dir.mkdir()
            app_exe = real_version_dir / runtime_layout.app_executable_name()
            app_exe.write_bytes(b"fake exe")
            versions_dir = root / "install" / "versions"
            versions_dir.mkdir(parents=True)
            symlinked_version_dir = versions_dir / "1.0.0"
            self.create_dir_symlink_or_skip(real_version_dir, symlinked_version_dir)

            with mock.patch.object(runtime_layout.sys, "executable", str(symlinked_version_dir / app_exe.name)):
                with self.assertRaisesRegex(runtime_layout.RuntimeLayoutError, "symlink"):
                    runtime_layout.current_version_dir()

    def test_app_entry_manager_dispatch(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, = reset_core_modules(Path(data_dir), "app_entry")
            calls = []
            args = app_entry.parse_args(["--manager"])

            result = app_entry.dispatch(args, manager_main=lambda manager: calls.append(manager) or 0)

            self.assertEqual(result, 0)
            self.assertEqual(calls[0]._launch_spec_builder, app_entry.manager_core.build_source_launch_spec)

    def test_app_entry_packaged_manager_starts_bot_with_packaged_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_entry, instance_store, runtime_layout = reset_core_modules(
                root / "data",
                "app_entry",
                "instance_store",
                "runtime_layout",
            )
            instance = self.create_instance(instance_store)
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            app_exe = version_dir / runtime_layout.app_executable_name()
            app_exe.write_bytes(b"fake exe")
            captured = {}

            class FakeRunningProcess:
                pid = 12345

                def poll(self):
                    return None

            real_manager_class = app_entry.manager_core.BotProcessManager

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeRunningProcess()

            class CapturingManager(real_manager_class):
                def __init__(self, launch_spec_builder, popen_factory=None):
                    super().__init__(launch_spec_builder=launch_spec_builder, popen_factory=fake_popen)

            def run_manager(manager):
                try:
                    manager.start("admin-main")
                finally:
                    record = manager._records.get("admin-main")
                    if record is not None:
                        manager._close_record_handles(record)
                return 0

            args = app_entry.parse_args(["--manager"])
            with mock.patch.object(app_entry.runtime_layout, "is_frozen", return_value=True), mock.patch.object(
                app_entry.runtime_layout,
                "current_app_executable",
                return_value=app_exe.resolve(),
            ), mock.patch.object(
                app_entry.runtime_layout,
                "current_version_dir",
                return_value=version_dir.resolve(),
            ), mock.patch.object(app_entry.manager_core, "BotProcessManager", CapturingManager):
                result = app_entry.dispatch(args, manager_main=run_manager)

            self.assertEqual(result, 0)
            self.assertEqual(
                captured["command"],
                [str(app_exe.resolve()), "--bot-runner", "admin", "--instance", "admin-main"],
            )
            self.assertFalse(captured["kwargs"]["shell"])
            self.assertEqual(captured["kwargs"]["cwd"], version_dir.resolve())
            self.assertEqual(
                captured["kwargs"]["env"]["DARKABYSS_DATA_DIR"],
                str(app_entry.manager_core.app_paths.DATA_ROOT.resolve()),
            )
            self.assertEqual(captured["kwargs"]["stdout"].name, str(instance.paths.logs_dir / app_entry.manager_core.STDOUT_LOG_NAME))
            self.assertEqual(captured["kwargs"]["stderr"].name, str(instance.paths.logs_dir / app_entry.manager_core.STDERR_LOG_NAME))
            command_text = " ".join(captured["command"])
            self.assertNotIn("Admin.py", command_text)
            self.assertNotIn("FAKE_TEST_TOKEN", command_text)

    def test_app_entry_admin_bot_runner_dispatches_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, = reset_core_modules(Path(data_dir), "app_entry")
            received = []
            args = app_entry.parse_args(["--bot-runner", "admin", "--instance", "admin-second"])

            result = app_entry.dispatch(args, admin_main=lambda argv: received.append(argv) or 0)

            self.assertEqual(result, 0)
            self.assertEqual(received, [["--instance", "admin-second"]])

    def test_app_entry_unknown_bot_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, = reset_core_modules(Path(data_dir), "app_entry")
            args = app_entry.parse_args(["--bot-runner", "unknown", "--instance", "admin-main"])

            with self.assertRaisesRegex(app_entry.AppEntryError, "Unknown bot type"):
                app_entry.dispatch(args, admin_main=lambda _argv: 0)

    def test_resource_root_resolves_program_resources_under_simulated_frozen_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            program_root = root / "program"
            (program_root / "DarkAbyss_Core" / "defaults").mkdir(parents=True)
            (program_root / "bots" / "admin").mkdir(parents=True)
            (program_root / "DarkAbyss_Core" / "Admin.py").write_text("# fake admin\n", encoding="utf-8")
            (program_root / "DarkAbyss_Core" / "defaults" / "admin_config.json").write_text(
                json.dumps(VALID_ADMIN_CONFIG),
                encoding="utf-8",
            )
            (program_root / "bots" / "admin" / "config.schema.json").write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )
            (program_root / "bots" / "admin" / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "admin",
                        "display_name": "Admin Bot",
                        "version": "1.0.0",
                        "entrypoint": "DarkAbyss_Core/Admin.py",
                        "default_config": "DarkAbyss_Core/defaults/admin_config.json",
                        "config_schema": "bots/admin/config.schema.json",
                        "config_version": 1,
                    }
                ),
                encoding="utf-8",
            )
            sys.path.insert(0, str(CORE_ROOT))
            os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
            for module_name in ("bot_registry", "app_paths", "runtime_layout"):
                sys.modules.pop(module_name, None)

            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys,
                "_MEIPASS",
                str(program_root),
                create=True,
            ):
                app_paths = importlib.import_module("app_paths")
                bot_registry = importlib.import_module("bot_registry")
                bot_type = bot_registry.get_bot_type("admin")

            self.assertEqual(app_paths.PROGRAM_ROOT, program_root / "DarkAbyss_Core")
            self.assertEqual(bot_type.manifest_path, (program_root / "bots" / "admin" / "manifest.json").resolve())
            self.assertFalse(str(app_paths.DATA_ROOT.resolve()).startswith(str(program_root.resolve())))

    def test_launcher_valid_current_pointer_resolves_expected_app(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, runtime_layout = reset_core_modules(root / "data", "launcher", "runtime_layout")
            install_root = root / "install"
            version_dir = install_root / "versions" / "1.0.0"
            app_name = runtime_layout.app_executable_name()
            write_release_manifest(version_dir, "1.0.0", {app_name: b"fake exe"})
            (install_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "version": "1.0.0", "previous_version": None}) + "\n",
                encoding="utf-8",
            )

            target = launcher.resolve_current_app(install_root)

            self.assertEqual(target.version, "1.0.0")
            self.assertEqual(target.app_executable, (version_dir / app_name).resolve())
            self.assertEqual(target.command, (str((version_dir / app_name).resolve()), "--manager"))

    def test_launcher_malformed_pointer_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, = reset_core_modules(root / "data", "launcher")
            install_root = root / "install"
            install_root.mkdir()
            (install_root / "current.json").write_text("{bad", encoding="utf-8")

            with self.assertRaisesRegex(launcher.LauncherError, "INVALID_CURRENT_POINTER"):
                launcher.resolve_current_app(install_root)

    def test_launcher_missing_version_fails_without_guessing_another_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, runtime_layout = reset_core_modules(root / "data", "launcher", "runtime_layout")
            install_root = root / "install"
            other_version_dir = install_root / "versions" / "2.0.0"
            write_release_manifest(other_version_dir, "2.0.0", {runtime_layout.app_executable_name(): b"fake exe"})
            (install_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(launcher.LauncherError, "CURRENT_VERSION_MISSING"):
                launcher.resolve_current_app(install_root)

    def test_launcher_missing_app_executable_fails_after_healthy_pointer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, = reset_core_modules(root / "data", "launcher")
            install_root = root / "install"
            version_dir = install_root / "versions" / "1.0.0"
            write_release_manifest(version_dir, "1.0.0", {"program.txt": b"valid release"})
            (install_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(launcher.LauncherError, "executable"):
                launcher.resolve_current_app(install_root)

    def test_launcher_rejects_symlinked_version_directory_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, runtime_layout = reset_core_modules(root / "data", "launcher", "runtime_layout")
            install_root = root / "install"
            outside_version = root / "outside-version"
            write_release_manifest(outside_version, "1.0.0", {runtime_layout.app_executable_name(): b"fake exe"})
            (install_root / "versions").mkdir(parents=True)
            self.create_dir_symlink_or_skip(outside_version, install_root / "versions" / "1.0.0")
            (install_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(launcher.LauncherError, "symlink"):
                launcher.resolve_current_app(install_root)

    def test_launcher_start_uses_no_shell(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher, runtime_layout = reset_core_modules(root / "data", "launcher", "runtime_layout")
            install_root = root / "install"
            version_dir = install_root / "versions" / "1.0.0"
            app_name = runtime_layout.app_executable_name()
            write_release_manifest(version_dir, "1.0.0", {app_name: b"fake exe"})
            (install_root / "current.json").write_text(
                json.dumps({"schema_version": 1, "version": "1.0.0"}) + "\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured.update(kwargs)
                return SimpleNamespace(pid=123)

            launcher.start_manager(install_root, popen_factory=fake_popen)

            self.assertEqual(captured["command"], [str((version_dir / app_name).resolve()), "--manager"])
            self.assertFalse(captured["shell"])
            self.assertEqual(captured["cwd"], version_dir.resolve())

    def test_release_manifest_generation_is_deterministic_and_verifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_manifest, update_engine = reset_core_modules(root / "data", "release_manifest", "update_engine")
            release_root = root / "release"
            (release_root / "z").mkdir(parents=True)
            (release_root / "z" / "last.txt").write_bytes(b"last")
            (release_root / "a").mkdir()
            (release_root / "a" / "first.txt").write_bytes(b"first")

            manifest_path = release_manifest.write_release_manifest(release_root, "1.0.0")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual([item["path"] for item in payload["files"]], ["a/first.txt", "z/last.txt"])
            self.assertEqual(update_engine.inspect_release(release_root).version, "1.0.0")

    def test_release_manifest_generation_rejects_user_data_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_manifest, = reset_core_modules(root / "data", "release_manifest")
            release_root = root / "release"
            (release_root / "secrets").mkdir(parents=True)
            (release_root / "secrets" / "token.txt").write_text("FAKE_TOKEN", encoding="utf-8")

            with self.assertRaisesRegex(release_manifest.ReleaseManifestGenerationError, "reserved"):
                release_manifest.write_release_manifest(release_root, "1.0.0")

    def test_release_manifest_generation_rejects_symlinks_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_manifest, = reset_core_modules(root / "data", "release_manifest")
            release_root = root / "release"
            release_root.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            self.create_file_symlink_or_skip(outside, release_root / "linked.txt")

            with self.assertRaisesRegex(release_manifest.ReleaseManifestGenerationError, "symlink"):
                release_manifest.write_release_manifest(release_root, "1.0.0")

    def test_assemble_distribution_creates_bootable_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            (app_bundle / "_internal").mkdir()
            (app_bundle / "_internal" / "support.dll").write_bytes(b"dll")
            (app_bundle / "bots" / "admin").mkdir(parents=True)
            (app_bundle / "bots" / "admin" / "manifest.json").write_text("{}", encoding="utf-8")
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")
            output = root / "DarkAbyssBotManager"

            result = assembler.assemble_distribution(
                version="9.0.0-test",
                app_bundle_dir=app_bundle,
                launcher_executable=launcher_exe,
                output_dir=output,
            )

            version_dir = output / "versions" / "9.0.0-test"
            self.assertEqual(result, output.resolve())
            self.assertTrue((output / assembler.runtime_layout.launcher_executable_name()).is_file())
            self.assertTrue((version_dir / assembler.runtime_layout.app_executable_name()).is_file())
            self.assertTrue((version_dir / "_internal" / "support.dll").is_file())
            self.assertTrue((version_dir / "release.json").is_file())
            current = json.loads((output / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current, {"schema_version": 1, "version": "9.0.0-test", "previous_version": None})
            release = assembler.update_engine.inspect_release(version_dir)
            self.assertEqual(release.version, "9.0.0-test")
            self.assertNotIn("Launcher.exe", [item.path for item in release.files])
            self.assertFalse((output / "secrets").exists())
            self.assertEqual(
                assembler.launcher.resolve_current_app(output).app_executable,
                (version_dir / assembler.runtime_layout.app_executable_name()).resolve(),
            )

    def test_assemble_distribution_cleans_dirty_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")
            output = root / "DarkAbyssBotManager"
            stale = output / "versions" / "old" / "stale.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")

            assembler.assemble_distribution(
                version="9.0.0-test",
                app_bundle_dir=app_bundle,
                launcher_executable=launcher_exe,
                output_dir=output,
            )

            self.assertFalse(stale.exists())
            self.assertTrue((output / "versions" / "9.0.0-test").is_dir())

    def test_assemble_distribution_rejects_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")

            with self.assertRaises(assembler.update_engine.ReleaseManifestError):
                assembler.assemble_distribution(
                    version="../bad",
                    app_bundle_dir=app_bundle,
                    launcher_executable=launcher_exe,
                    output_dir=root / "out",
                )
            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "Missing DarkAbyssApp"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle,
                    launcher_executable=launcher_exe,
                    output_dir=root / "out",
                )
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "Launcher"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle,
                    launcher_executable=root / "missing.exe",
                    output_dir=root / "out",
                )

    def test_assemble_distribution_rejects_app_bundle_symlink_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            self.create_file_symlink_or_skip(outside, app_bundle / "linked.txt")
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")

            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "symlink"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle,
                    launcher_executable=launcher_exe,
                    output_dir=root / "out",
                )

    def test_assemble_distribution_rejects_app_bundle_root_symlink_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            real_app_bundle = root / "real-app"
            real_app_bundle.mkdir()
            (real_app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            app_bundle_link = root / "raw-app-link"
            self.create_dir_symlink_or_skip(real_app_bundle, app_bundle_link)
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")

            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "symlink"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle_link,
                    launcher_executable=launcher_exe,
                    output_dir=root / "out",
                )

    def test_assemble_distribution_rejects_launcher_symlink_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            real_launcher = root / "real-launcher.exe"
            real_launcher.write_bytes(b"launcher")
            launcher_link = root / assembler.runtime_layout.launcher_executable_name()
            self.create_file_symlink_or_skip(real_launcher, launcher_link)

            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "symlink"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle,
                    launcher_executable=launcher_link,
                    output_dir=root / "out",
                )

    def test_assemble_distribution_rejects_output_root_symlink_without_touching_target_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assembler = load_assembler(root / "data")
            app_bundle = root / "raw-app"
            app_bundle.mkdir()
            (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
            launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
            launcher_exe.write_bytes(b"launcher")
            target_root = root / "outside-output"
            target_root.mkdir()
            target_file = target_root / "keep.txt"
            target_file.write_bytes(b"do not delete")
            output_link = root / "DarkAbyssBotManager"
            self.create_dir_symlink_or_skip(target_root, output_link)

            with self.assertRaisesRegex(assembler.DistributionAssemblyError, "symlink"):
                assembler.assemble_distribution(
                    version="9.0.0-test",
                    app_bundle_dir=app_bundle,
                    launcher_executable=launcher_exe,
                    output_dir=output_link,
                )

            self.assertTrue(output_link.is_symlink())
            self.assertEqual(target_file.read_bytes(), b"do not delete")
            self.assertFalse((target_root / "versions").exists())


if __name__ == "__main__":
    unittest.main()
