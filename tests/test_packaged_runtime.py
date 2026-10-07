import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
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


def load_release_artifacts(data_root: Path):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in (
        "github_updates",
        "launcher",
        "runtime_layout",
        "update_engine",
        "build_release_artifacts",
    ):
        sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        "build_release_artifacts",
        PROJECT_ROOT / "packaging" / "build_release_artifacts.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_release_artifacts"] = module
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

    def test_packaged_launch_spec_for_game_presence_bot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry, runtime_layout = reset_core_modules(
                root / "data", "manager_core", "instance_store", "bot_registry", "runtime_layout"
            )
            instance = instance_store.create_instance("game_presence", "gp-main")
            instance.paths.token.write_text("FAKE_GP_TOKEN", encoding="utf-8")
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            app_exe = version_dir / runtime_layout.app_executable_name()
            app_exe.write_bytes(b"fake exe")

            spec = manager_core.build_packaged_launch_spec(instance, bot_registry.get_bot_type("game_presence"), app_exe, version_dir)

            self.assertEqual(spec.args, ("--bot-runner", "game_presence", "--instance", "gp-main"))
            self.assertEqual(spec.stdout_log_path, instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME)
            self.assertNotIn("GamePresence.py", " ".join(spec.command))
            self.assertNotIn("FAKE_GP_TOKEN", " ".join(spec.command))

    def test_app_entry_game_presence_bot_runner_dispatches_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, = reset_core_modules(Path(data_dir), "app_entry")
            received = []
            args = app_entry.parse_args(["--bot-runner", "game_presence", "--instance", "gp-main"])
            result = app_entry.dispatch(
                args,
                admin_main=lambda argv: self.fail("Admin entrypoint must not run for a Game Presence instance"),
                game_presence_main=lambda argv: received.append(argv) or 0,
            )
            self.assertEqual(result, 0)
            self.assertEqual(received, [["--instance", "gp-main"]])
            with self.assertRaises(app_entry.AppEntryError):
                app_entry.dispatch(app_entry.parse_args(["--bot-runner", "game_presence"]), game_presence_main=lambda argv: 0)

    def test_app_entry_runs_the_real_game_presence_entrypoint(self):
        with tempfile.TemporaryDirectory() as data_dir:
            sys.modules.pop("GamePresence", None)
            app_entry, instance_store = reset_core_modules(Path(data_dir), "app_entry", "instance_store")
            instance_store.create_instance("game_presence", "gp-main")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = app_entry.main(["--bot-runner", "game_presence", "--instance", "gp-main"])
            sys.modules.pop("GamePresence", None)
            self.assertEqual(code, 1)  # placeholder token: actionable message, no network, no traceback
            self.assertIn("no Discord token yet", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_packaged_launch_spec_for_stream_director_bot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager_core, instance_store, bot_registry, runtime_layout = reset_core_modules(
                root / "data", "manager_core", "instance_store", "bot_registry", "runtime_layout"
            )
            instance = instance_store.create_instance("stream_director", "stream-main")
            instance.paths.token.write_text("FAKE_SD_TOKEN", encoding="utf-8")
            (instance.paths.secrets_dir / "twitch_oauth.json").write_text('{"access_token": "FAKE_TWITCH"}', encoding="utf-8")
            version_dir = root / "install" / "versions" / "1.0.0"
            version_dir.mkdir(parents=True)
            app_exe = version_dir / runtime_layout.app_executable_name()
            app_exe.write_bytes(b"fake exe")

            spec = manager_core.build_packaged_launch_spec(instance, bot_registry.get_bot_type("stream_director"), app_exe, version_dir)

            self.assertEqual(spec.args, ("--bot-runner", "stream_director", "--instance", "stream-main"))
            command = " ".join(spec.command)
            self.assertNotIn("StreamDirector.py", command)
            self.assertNotIn("FAKE_SD_TOKEN", command)
            self.assertNotIn("FAKE_TWITCH", command)
            self.assertNotIn("FAKE_TWITCH", " ".join(f"{key}={value}" for key, value in spec.env.items()))

    def test_app_entry_stream_director_bot_runner_dispatches_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, = reset_core_modules(Path(data_dir), "app_entry")
            received = []
            args = app_entry.parse_args(["--bot-runner", "stream_director", "--instance", "stream-main"])
            result = app_entry.dispatch(
                args,
                admin_main=lambda argv: self.fail("Admin entrypoint must not run for a Stream Director instance"),
                game_presence_main=lambda argv: self.fail("Game Presence entrypoint must not run for a Stream Director instance"),
                stream_director_main=lambda argv: received.append(argv) or 0,
            )
            self.assertEqual(result, 0)
            self.assertEqual(received, [["--instance", "stream-main"]])
            with self.assertRaises(app_entry.AppEntryError):
                app_entry.dispatch(app_entry.parse_args(["--bot-runner", "stream_director"]), stream_director_main=lambda argv: 0)

    def test_app_entry_runs_the_real_stream_director_entrypoint(self):
        with tempfile.TemporaryDirectory() as data_dir:
            for name in ("StreamDirector", "stream_director", "stream_director_config", "stream_director_store", "stream_director_twitch", "stream_director_discord"):
                sys.modules.pop(name, None)
            app_entry, instance_store = reset_core_modules(Path(data_dir), "app_entry", "instance_store")
            instance_store.create_instance("stream_director", "stream-main")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = app_entry.main(["--bot-runner", "stream_director", "--instance", "stream-main"])
            sys.modules.pop("StreamDirector", None)
            self.assertEqual(code, 1)  # placeholder token: actionable message, no network, no traceback
            self.assertIn("no Discord token yet", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            # A Game Presence instance is refused by the Stream Director entrypoint.
            instance_store.create_instance("game_presence", "gp-main")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = app_entry.main(["--bot-runner", "stream_director", "--instance", "gp-main"])
            sys.modules.pop("StreamDirector", None)
            self.assertEqual(code, 1)
            self.assertIn("expected 'stream_director'", output.getvalue())

    def test_stream_director_bot_wiring_without_network(self):
        with tempfile.TemporaryDirectory() as data_dir:
            for name in ("StreamDirector", "stream_director", "stream_director_config", "stream_director_store", "stream_director_twitch", "stream_director_discord"):
                sys.modules.pop(name, None)
            instance_store, = reset_core_modules(Path(data_dir), "instance_store")
            instance_store.create_instance("stream_director", "stream-main")
            stream_bot = importlib.import_module("StreamDirector")
            runtime = stream_bot.resolve_runtime("stream-main")
            intents = stream_bot.build_intents()
            self.assertFalse(intents.message_content or intents.members or intents.presences)
            self.assertTrue(intents.guilds and intents.guild_messages)

            async def build():
                bot = stream_bot.StreamDirectorBot(runtime)
                status = bot.status()
                await bot.http.close()
                return bot, status

            import asyncio

            bot, status = asyncio.run(build())
            self.assertEqual(status["diagnosis"]["code"], "not_configured")
            self.assertEqual(status["twitch"]["state"], "not_configured")
            self.assertTrue(bot.director.available)
            self.assertEqual({command.name for command in bot.tree.get_commands()} >= {"moment", "challenge", "suggest", "stream"}, True)
            sys.modules.pop("StreamDirector", None)

    def test_every_bot_type_is_packaged_and_dispatchable(self):
        with tempfile.TemporaryDirectory() as data_dir:
            app_entry, bot_registry = reset_core_modules(Path(data_dir), "app_entry", "bot_registry")
            spec_text = (PROJECT_ROOT / "packaging" / "DarkAbyssApp.spec").read_text(encoding="utf-8")
            types_ = bot_registry.discover_bot_types()
            self.assertEqual(set(types_), {"admin", "game_presence", "stream_director"})
            for type_id, bot_type in types_.items():
                with self.subTest(bot_type=type_id):
                    entry = bot_type.entrypoint
                    # The manifest entrypoint must exist in the bundle (registry validation) and
                    # be importable in-process by app_entry.
                    self.assertIn(f'"DarkAbyss_Core" / "{entry.name}"', spec_text)
                    self.assertIn(f'"{entry.stem}"', spec_text)
                    calls = []
                    args = app_entry.parse_args(["--bot-runner", type_id, "--instance", "x-1"])
                    app_entry.dispatch(
                        args,
                        admin_main=lambda argv: calls.append(("admin", argv)) or 0,
                        game_presence_main=lambda argv: calls.append(("game_presence", argv)) or 0,
                        stream_director_main=lambda argv: calls.append(("stream_director", argv)) or 0,
                    )
                    self.assertEqual(calls, [(type_id, ["--instance", "x-1"])])
            for module in (
                "ai_storage", "ai_usage", "ai_connections", "ai_providers", "ai_groq", "ai_gemini", "game_presence", "game_presence_discord",
                "stream_director", "stream_director_config", "stream_director_store", "stream_director_twitch", "stream_director_discord",
                "bot_i18n", "bot_i18n_ru_admin", "bot_i18n_ru_game_presence", "bot_i18n_ru_stream_director",
                "bot_events", "social_awareness", "social_memory", "social_signals", "manager_kairo",
                "content_filter", "admin_tools_filter", "manager_content_filter", "locked_json", "message_policy",
            ):
                self.assertIn(f'"{module}"', spec_text)

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

    def create_assembled_distribution(self, root: Path, version: str = "1.2.3") -> Path:
        assembler = load_assembler(root / "data")
        app_bundle = root / "raw-app"
        app_bundle.mkdir()
        (app_bundle / assembler.runtime_layout.app_executable_name()).write_bytes(b"app exe")
        (app_bundle / "_internal").mkdir()
        (app_bundle / "_internal" / "support.dll").write_bytes(b"dll")
        launcher_exe = root / assembler.runtime_layout.launcher_executable_name()
        launcher_exe.write_bytes(b"launcher")
        return assembler.assemble_distribution(
            version=version,
            app_bundle_dir=app_bundle,
            launcher_executable=launcher_exe,
            output_dir=root / "DarkAbyssBotManager",
        )

    def test_release_artifacts_create_update_and_fresh_install_zips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"

            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                tag="v1.2.3",
                distribution=distribution,
                output=output,
            )

            self.assertEqual(artifacts.update_zip.name, "darkabyss-release-1.2.3.zip")
            self.assertEqual(artifacts.fresh_install_zip.name, "DarkAbyssBotManager-1.2.3-windows.zip")
            self.assertTrue(artifacts.update_sha256.is_file())
            self.assertTrue(artifacts.fresh_install_sha256.is_file())
            # `sha256sum -c` compatible: "<hex>  <name>\n", no CR.
            self.assertEqual(
                artifacts.update_sha256.read_bytes(),
                f"{sha256_bytes(artifacts.update_zip.read_bytes())}  darkabyss-release-1.2.3.zip\n".encode(),
            )

            with zipfile.ZipFile(artifacts.update_zip) as archive:
                update_members = [info.filename for info in archive.infolist()]
            self.assertEqual(update_members, sorted(update_members))
            self.assertIn("release.json", update_members)
            self.assertIn(artifacts_builder.runtime_layout.app_executable_name(), update_members)
            self.assertIn("_internal/support.dll", update_members)
            self.assertNotIn(artifacts_builder.runtime_layout.launcher_executable_name(), update_members)
            self.assertNotIn("current.json", update_members)
            self.assertFalse(any(member.startswith("versions/") for member in update_members))
            self.assertFalse(any(member.startswith("DarkAbyssBotManager/") for member in update_members))
            self.assertFalse(any(member.startswith("secrets/") or member.startswith("instances/") for member in update_members))

            with zipfile.ZipFile(artifacts.fresh_install_zip) as archive:
                fresh_members = [info.filename for info in archive.infolist()]
            self.assertEqual(fresh_members, sorted(fresh_members))
            self.assertIn("DarkAbyssBotManager/Launcher.exe", fresh_members)
            self.assertIn("DarkAbyssBotManager/current.json", fresh_members)
            self.assertIn("DarkAbyssBotManager/versions/1.2.3/DarkAbyssApp.exe", fresh_members)
            self.assertIn("DarkAbyssBotManager/versions/1.2.3/release.json", fresh_members)

            artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)
            for archive_path in (artifacts.update_zip, artifacts.fresh_install_zip):
                expected = f"{sha256_bytes(archive_path.read_bytes())}  {archive_path.name}\n"
                self.assertEqual((archive_path.with_name(archive_path.name + ".sha256")).read_text(encoding="utf-8"), expected)

    def test_release_artifacts_reject_output_equal_distribution_without_deleting_sentinel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            sentinel = distribution / "current.json"
            before = sentinel.read_bytes()

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "separate"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=distribution,
                )

            self.assertEqual(sentinel.read_bytes(), before)
            self.assertTrue((distribution / "versions" / "1.2.3" / "release.json").is_file())

    def test_release_artifacts_reject_output_parent_containing_distribution_without_deleting_sentinel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            sentinel = distribution / "current.json"
            before = sentinel.read_bytes()

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "contain"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root,
                )

            self.assertEqual(sentinel.read_bytes(), before)
            self.assertTrue((distribution / "Launcher.exe").is_file())

    def test_release_artifacts_reject_output_inside_distribution_without_deleting_sentinel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            sentinel = distribution / "versions" / "1.2.3" / "DarkAbyssApp.exe"
            before = sentinel.read_bytes()
            nested_output = distribution / "artifacts"

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "inside"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=nested_output,
                )

            self.assertEqual(sentinel.read_bytes(), before)
            self.assertFalse(nested_output.exists())

    def test_release_artifacts_accept_sibling_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "release-artifacts"

            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )

            self.assertTrue(artifacts.update_zip.is_file())
            self.assertTrue(artifacts.fresh_install_zip.is_file())

    def test_release_artifacts_update_zip_prepares_and_stages_through_github_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                tag="v1.2.3",
                distribution=distribution,
                output=root / "artifacts",
            )
            github_updates = artifacts_builder.github_updates
            install_root = root / "install"
            release = github_updates.GitHubReleaseInfo(
                tag_name="v1.2.3",
                version="1.2.3",
                name=None,
                draft=False,
                prerelease=False,
                assets=(
                    github_updates.GitHubReleaseAsset(
                        name="darkabyss-release-1.2.3.zip",
                        download_url="https://github.com/example/project/releases/download/v1.2.3/darkabyss-release-1.2.3.zip",
                        size=artifacts.update_zip.stat().st_size,
                        sha256=sha256_bytes(artifacts.update_zip.read_bytes()),
                    ),
                ),
            )
            downloaded = github_updates.DownloadedRelease(
                release=release,
                asset=release.assets[0],
                archive_path=artifacts.update_zip,
                byte_count=artifacts.update_zip.stat().st_size,
                sha256=sha256_bytes(artifacts.update_zip.read_bytes()),
            )

            prepared = github_updates.prepare_downloaded_release(downloaded, install_root)
            staged = artifacts_builder.update_engine.stage_release(prepared.release_root, install_root)

            self.assertEqual(prepared.version, "1.2.3")
            self.assertEqual(staged.version, "1.2.3")
            self.assertTrue((staged.version_dir / "release.json").is_file())

    def test_release_artifacts_fresh_install_launcher_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=root / "artifacts",
            )
            extract_root = root / "fresh"

            with zipfile.ZipFile(artifacts.fresh_install_zip) as archive:
                archive.extractall(extract_root)

            target = artifacts_builder.launcher.resolve_current_app(extract_root / "DarkAbyssBotManager")
            self.assertEqual(target.version, "1.2.3")
            self.assertEqual(
                target.app_executable,
                (extract_root / "DarkAbyssBotManager" / "versions" / "1.2.3" / "DarkAbyssApp.exe").resolve(),
            )

    def test_release_artifacts_reject_malformed_or_mismatched_tags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")

            for tag in ("1.2.3", "v../bad", "v"):
                with self.subTest(tag=tag):
                    with self.assertRaises(artifacts_builder.ReleaseArtifactError):
                        artifacts_builder.build_release_artifacts(
                            version="1.2.3",
                            tag=tag,
                            distribution=distribution,
                            output=root / f"artifacts-{tag.replace('/', '_')}",
                        )

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "does not match"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    tag="v1.2.4",
                    distribution=distribution,
                    output=root / "artifacts-mismatch",
                )

    def test_release_artifacts_reject_user_data_roots_in_distribution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            secret = distribution / "versions" / "1.2.3" / "secrets" / "token.txt"
            secret.parent.mkdir()
            secret.write_text("FAKE_SHOULD_NOT_PACKAGE", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_reject_source_adjacent_token_file_in_distribution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            token = distribution / "versions" / "1.2.3" / "DarkAbyss_Core" / "admin_bot_token.txt"
            token.parent.mkdir()
            token.write_text("FAKE_SHOULD_NOT_PACKAGE", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_update_zip_is_manifest_driven_and_rejects_unmanifested_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            extra = distribution / "versions" / "1.2.3" / "unexpected.txt"
            extra.write_text("not in release.json", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_fresh_install_zip_is_manifest_driven(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            extra = distribution / "root-extra.txt"
            extra.write_text("not allowed", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_reject_second_installed_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            old_version = distribution / "versions" / "1.0.0"
            old_version.mkdir()
            (old_version / "release.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "exactly"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_reject_empty_second_version_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            (distribution / "versions" / "9.9.9").mkdir()

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "exactly"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def test_release_artifacts_reject_nested_unexpected_secret_config_database_log_files(self):
        cases = [
            "_internal/DarkAbyss_Core/random-secret.txt",
            "_internal/package/config.json",
            "_internal/package/state.sqlite",
            "_internal/package/state.db",
            "_internal/package/runtime.log",
            "_internal/.git/config",
        ]
        for relative in cases:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    distribution = self.create_assembled_distribution(root, "1.2.3")
                    artifacts_builder = load_release_artifacts(root / "data")
                    extra = distribution / "versions" / "1.2.3" / Path(*relative.split("/"))
                    extra.parent.mkdir(parents=True, exist_ok=True)
                    extra.write_text("not allowed", encoding="utf-8")

                    with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                        artifacts_builder.build_release_artifacts(
                            version="1.2.3",
                            distribution=distribution,
                            output=root / "artifacts",
                        )

    def test_release_artifacts_verify_only_rejects_stale_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            with artifacts.update_zip.open("ab") as handle:
                handle.write(b"tamper")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "Checksum"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_verify_only_rejects_malformed_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            artifacts.fresh_install_sha256.write_text("not a checksum\n", encoding="utf-8")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "Checksum"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_verify_only_rejects_fresh_zip_extra_root_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            self.append_zip_member(artifacts.fresh_install_zip, "DarkAbyssBotManager/extra.txt", b"extra")
            artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_verify_only_rejects_fresh_zip_unmanifested_version_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            self.append_zip_member(
                artifacts.fresh_install_zip,
                "DarkAbyssBotManager/versions/1.2.3/unexpected.txt",
                b"unexpected",
            )
            artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "unexpected"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_update_zip_exceeding_default_download_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")

            with mock.patch.object(artifacts_builder.github_updates, "DEFAULT_MAX_ARTIFACT_BYTES", 10):
                with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "download size limit"):
                    artifacts_builder.build_release_artifacts(
                        version="1.2.3",
                        distribution=distribution,
                        output=root / "artifacts",
                    )

    def test_release_artifacts_verify_fresh_zip_rejects_traversal_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            self.write_malicious_fresh_zip(artifacts.fresh_install_zip, "../evil.txt")
            artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "traversal"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)
            self.assertFalse((output / "evil.txt").exists())

    def test_release_artifacts_verify_fresh_zip_rejects_absolute_and_drive_paths(self):
        for name in ("/evil.txt", "C:/evil.txt"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    distribution = self.create_assembled_distribution(root, "1.2.3")
                    artifacts_builder = load_release_artifacts(root / "data")
                    output = root / "artifacts"
                    artifacts = artifacts_builder.build_release_artifacts(
                        version="1.2.3",
                        distribution=distribution,
                        output=output,
                    )
                    self.write_malicious_fresh_zip(artifacts.fresh_install_zip, name)
                    artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

                    with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "relative"):
                        artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_verify_fresh_zip_rejects_duplicate_case_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            with zipfile.ZipFile(artifacts.fresh_install_zip, "w") as archive:
                archive.writestr("DarkAbyssBotManager/current.json", b"{}")
                archive.writestr("darkabyssbotmanager/current.json", b"{}")
            artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "case-colliding"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_verify_fresh_zip_rejects_symlink_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            output = root / "artifacts"
            artifacts = artifacts_builder.build_release_artifacts(
                version="1.2.3",
                distribution=distribution,
                output=output,
            )
            symlink_info = zipfile.ZipInfo("DarkAbyssBotManager/linked.txt")
            symlink_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(artifacts.fresh_install_zip, "w") as archive:
                archive.writestr(symlink_info, b"target")
            artifacts_builder._write_sha256_file(artifacts.fresh_install_zip)

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "symlink"):
                artifacts_builder.verify_release_artifacts(version="1.2.3", artifacts_dir=output)

    def test_release_artifacts_reject_symlinked_input_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            distribution = self.create_assembled_distribution(root, "1.2.3")
            artifacts_builder = load_release_artifacts(root / "data")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            self.create_file_symlink_or_skip(outside, distribution / "versions" / "1.2.3" / "linked.txt")

            with self.assertRaisesRegex(artifacts_builder.ReleaseArtifactError, "symlink"):
                artifacts_builder.build_release_artifacts(
                    version="1.2.3",
                    distribution=distribution,
                    output=root / "artifacts",
                )

    def write_malicious_fresh_zip(self, path: Path, member_name: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member_name, b"evil")

    def append_zip_member(self, path: Path, member_name: str, payload: bytes) -> None:
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(member_name, payload)


if __name__ == "__main__":
    unittest.main()
