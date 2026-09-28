import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
FAKE_CHILD = Path(__file__).resolve().parent / "fake_child_process.py"
VALID_ADMIN_CONFIG = {
    "allow_server_administrators": True,
    "allowed_user_ids": [],
    "allowed_role_ids": [],
    "audit_channel_id": None,
}


def load_modules(data_root: Path, *names: str):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in (
        "manager_core",
        "admin_instance",
        "instance_store",
        "bot_registry",
        "app_paths",
    ):
        sys.modules.pop(module_name, None)
    return [importlib.import_module(name) for name in names]


class ManagerCoreTests(unittest.TestCase):
    def create_instance(self, instance_store, instance_id: str):
        instance = instance_store.create_instance("admin", instance_id)
        instance.paths.config.write_text(json.dumps(VALID_ADMIN_CONFIG), encoding="utf-8")
        instance.paths.token.write_text("FAKE_TEST_TOKEN", encoding="utf-8")
        return instance

    def fake_builder(self, manager_core, extra_args: tuple[str, ...] = ()):
        def build(instance, bot_type):
            env = os.environ.copy()
            env["DARKABYSS_DATA_DIR"] = str(manager_core.app_paths.DATA_ROOT.resolve())
            return manager_core.LaunchSpec(
                executable=sys.executable,
                args=(str(FAKE_CHILD), "--instance", instance.id, *extra_args),
                cwd=PROJECT_ROOT,
                env=env,
                stdout_log_path=instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME,
                stderr_log_path=instance.paths.logs_dir / manager_core.STDERR_LOG_NAME,
            )

        return build

    def wait_for_state(self, manager, instance_id: str, state: str, timeout: float = 5.0):
        deadline = time.time() + timeout
        status = manager.status(instance_id)
        while time.time() < deadline:
            status = manager.status(instance_id)
            if status.state == state:
                return status
            time.sleep(0.05)
        self.fail(f"{instance_id} did not reach state {state}; last status was {status}")

    def wait_for_log_text(self, path: Path, text: str, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        content = ""
        while time.time() < deadline:
            if path.exists():
                content = path.read_text(encoding="utf-8")
                if text in content:
                    return content
            time.sleep(0.05)
        self.fail(f"{path} did not contain {text!r}; last content was {content!r}")

    def wait_for_process_exit(self, record, timeout: float = 5.0) -> int:
        deadline = time.time() + timeout
        exit_code = record.process.poll()
        while time.time() < deadline:
            exit_code = record.process.poll()
            if exit_code is not None:
                return exit_code
            time.sleep(0.05)
        self.fail(f"process {record.process.pid} did not exit")

    def test_launch_spec_uses_manifest_entrypoint_without_shell(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store, bot_registry, app_paths = load_modules(
                Path(data_dir),
                "manager_core",
                "instance_store",
                "bot_registry",
                "app_paths",
            )
            instance = self.create_instance(instance_store, "admin-main")
            bot_type = bot_registry.get_bot_type("admin")

            spec = manager_core.build_source_launch_spec(instance, bot_type)

            self.assertEqual(spec.executable, sys.executable)
            self.assertEqual(spec.args, (str(bot_type.entrypoint), "--instance", "admin-main"))
            self.assertEqual(spec.command, (sys.executable, str(bot_type.entrypoint), "--instance", "admin-main"))
            self.assertNotIn("shell", spec.command)
            self.assertEqual(spec.env["DARKABYSS_DATA_DIR"], str(app_paths.DATA_ROOT.resolve()))

    def test_child_environment_contains_resolved_data_root(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store, bot_registry = load_modules(
                Path(data_dir),
                "manager_core",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store, "admin-main")
            bot_type = bot_registry.get_bot_type("admin")

            spec = manager_core.build_source_launch_spec(instance, bot_type)

            self.assertEqual(spec.env["DARKABYSS_DATA_DIR"], str(manager_core.app_paths.DATA_ROOT.resolve()))

    def test_launch_cwd_is_deterministic(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as other_cwd:
            manager_core, instance_store, bot_registry = load_modules(
                Path(data_dir),
                "manager_core",
                "instance_store",
                "bot_registry",
            )
            instance = self.create_instance(instance_store, "admin-main")
            bot_type = bot_registry.get_bot_type("admin")
            old_cwd = Path.cwd()
            try:
                os.chdir(other_cwd)
                spec = manager_core.build_source_launch_spec(instance, bot_type)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(spec.cwd, bot_registry.PROJECT_ROOT)
            self.assertNotEqual(spec.cwd, Path(other_cwd))

    def test_initial_status_is_stopped(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            status = manager.status("admin-main")

            self.assertEqual(status.state, manager_core.STATE_STOPPED)
            self.assertIsNone(status.pid)
            self.assertIsNone(status.exit_code)

    def test_start_running_and_stop(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--stdout", "hello stdout", "--stderr", "hello stderr"))
            )

            status = manager.start("admin-main")

            self.assertEqual(status.state, manager_core.STATE_RUNNING)
            self.assertIsNotNone(status.pid)
            self.assertIsNotNone(status.started_at)
            self.assertGreaterEqual(status.uptime_seconds, 0)
            self.assertIsNone(status.exit_code)

            stopped = manager.stop("admin-main", timeout=2)
            self.assertEqual(stopped.state, manager_core.STATE_STOPPED)
            self.assertIsNotNone(stopped.exit_code)

    def test_duplicate_start_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            manager.start("admin-main")
            try:
                with self.assertRaisesRegex(manager_core.ProcessAlreadyRunningError, "already running"):
                    manager.start("admin-main")
            finally:
                manager.shutdown_all(timeout=2)

    def test_two_instances_run_independently(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            main = manager.start("admin-main")
            second = manager.start("admin-second")
            try:
                self.assertNotEqual(main.pid, second.pid)

                manager.stop("admin-second", timeout=2)

                self.assertEqual(manager.status("admin-second").state, manager_core.STATE_STOPPED)
                self.assertEqual(manager.status("admin-main").state, manager_core.STATE_RUNNING)
            finally:
                manager.shutdown_all(timeout=2)

    def test_natural_child_exit_captures_exit_code(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--mode", "exit", "--exit-code", "7"))
            )

            manager.start("admin-main")
            status = self.wait_for_state(manager, "admin-main", manager_core.STATE_EXITED)

            self.assertEqual(status.exit_code, 7)
            self.assertIsNone(status.pid)

    def test_restart_stopped_and_running_instance(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            first = manager.restart("admin-main", timeout=2)
            first_pid = first.pid
            self.assertEqual(first.state, manager_core.STATE_RUNNING)

            second = manager.restart("admin-main", timeout=2)
            try:
                self.assertEqual(second.state, manager_core.STATE_RUNNING)
                self.assertIsNotNone(second.pid)
                self.assertNotEqual(first_pid, second.pid)
            finally:
                manager.shutdown_all(timeout=2)

    def test_stdout_stderr_logs_and_append_across_restart(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--stdout", "line-out", "--stderr", "line-err"))
            )

            manager.start("admin-main")
            self.wait_for_log_text(instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME, "line-out")
            manager.stop("admin-main", timeout=2)
            manager.start("admin-main")
            self.wait_for_log_text(instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME, "line-out\nline-out")
            manager.stop("admin-main", timeout=2)

            stdout_text = (instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME).read_text(encoding="utf-8")
            stderr_text = (instance.paths.logs_dir / manager_core.STDERR_LOG_NAME).read_text(encoding="utf-8")
            self.assertGreaterEqual(stdout_text.count("line-out"), 2)
            self.assertGreaterEqual(stderr_text.count("line-err"), 2)

    def test_instances_use_separate_log_files(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            main = self.create_instance(instance_store, "admin-main")
            second = self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core, ("--stdout", "shared-output")))

            manager.start("admin-main")
            manager.start("admin-second")
            self.wait_for_log_text(main.paths.logs_dir / manager_core.STDOUT_LOG_NAME, "shared-output")
            self.wait_for_log_text(second.paths.logs_dir / manager_core.STDOUT_LOG_NAME, "shared-output")
            manager.shutdown_all(timeout=2)

            main_log = main.paths.logs_dir / manager_core.STDOUT_LOG_NAME
            second_log = second.paths.logs_dir / manager_core.STDOUT_LOG_NAME
            self.assertNotEqual(main_log, second_log)
            self.assertIn("shared-output", main_log.read_text(encoding="utf-8"))
            self.assertIn("shared-output", second_log.read_text(encoding="utf-8"))

    def test_missing_instance_fails_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, = load_modules(Path(data_dir), "manager_core")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            with self.assertRaisesRegex(manager_core.ManagerCoreError, "Invalid bot instance"):
                manager.start("missing-instance")

    def test_malformed_instance_fails_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            instance.paths.config.unlink()
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            with self.assertRaisesRegex(manager_core.ManagerCoreError, "config.json"):
                manager.start("admin-main")

    def test_shutdown_all_stops_all_running_children(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            manager.start("admin-main")
            manager.start("admin-second")

            results = manager.shutdown_all(timeout=2)

            self.assertEqual(set(results), {"admin-main", "admin-second"})
            self.assertEqual(manager.status("admin-main").state, manager_core.STATE_STOPPED)
            self.assertEqual(manager.status("admin-second").state, manager_core.STATE_STOPPED)

    def test_start_failure_does_not_register_running_state(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def failing_popen(*_args, **_kwargs):
                raise OSError("boom")

            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core),
                popen_factory=failing_popen,
            )

            with self.assertRaisesRegex(manager_core.ProcessStartError, "boom"):
                manager.start("admin-main")

            self.assertEqual(manager.status("admin-main").state, manager_core.STATE_STOPPED)

    def test_status_of_one_instance_does_not_change_another(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            manager.start("admin-main")
            try:
                second_status = manager.status("admin-second")
                main_status = manager.status("admin-main")

                self.assertEqual(second_status.state, manager_core.STATE_STOPPED)
                self.assertEqual(main_status.state, manager_core.STATE_RUNNING)
            finally:
                manager.shutdown_all(timeout=2)

    def test_log_handles_close_after_natural_exit(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--mode", "exit", "--exit-code", "0"))
            )

            manager.start("admin-main")
            self.wait_for_state(manager, "admin-main", manager_core.STATE_EXITED)

            record = manager._records["admin-main"]
            self.assertTrue(record.stdout_handle.closed)
            self.assertTrue(record.stderr_handle.closed)

    def test_natural_exit_then_start_again_closes_old_handles_without_prior_status(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            call_count = 0

            def build(instance, bot_type):
                nonlocal call_count
                call_count += 1
                extra_args = ("--mode", "exit", "--exit-code", "0") if call_count == 1 else ()
                return self.fake_builder(manager_core, extra_args)(instance, bot_type)

            manager = manager_core.BotProcessManager(build)
            manager.start("admin-main")
            old_record = manager._records["admin-main"]
            self.wait_for_process_exit(old_record)

            new_status = manager.start("admin-main")
            try:
                self.assertTrue(old_record.stdout_handle.closed)
                self.assertTrue(old_record.stderr_handle.closed)
                self.assertEqual(new_status.state, manager_core.STATE_RUNNING)
                self.assertIsNot(manager._records["admin-main"], old_record)
            finally:
                manager.shutdown_all(timeout=2)

    def test_natural_exit_then_restart_closes_old_handles_without_prior_status(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            call_count = 0

            def build(instance, bot_type):
                nonlocal call_count
                call_count += 1
                extra_args = ("--mode", "exit", "--exit-code", "0") if call_count == 1 else ()
                return self.fake_builder(manager_core, extra_args)(instance, bot_type)

            manager = manager_core.BotProcessManager(build)
            manager.start("admin-main")
            old_record = manager._records["admin-main"]
            self.wait_for_process_exit(old_record)

            restarted = manager.restart("admin-main", timeout=2)
            try:
                self.assertTrue(old_record.stdout_handle.closed)
                self.assertTrue(old_record.stderr_handle.closed)
                self.assertEqual(restarted.state, manager_core.STATE_RUNNING)
                self.assertIsNot(manager._records["admin-main"], old_record)
            finally:
                manager.shutdown_all(timeout=2)

    def test_natural_exit_then_shutdown_all_closes_handles_without_prior_status(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--mode", "exit", "--exit-code", "0"))
            )
            manager.start("admin-main")
            record = manager._records["admin-main"]
            self.wait_for_process_exit(record)

            results = manager.shutdown_all(timeout=2)

            self.assertEqual(results["admin-main"].state, manager_core.STATE_EXITED)
            self.assertTrue(record.stdout_handle.closed)
            self.assertTrue(record.stderr_handle.closed)

    def test_repeated_natural_exit_start_cycles_close_old_handles(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--mode", "exit", "--exit-code", "0"))
            )
            old_records = []

            for _ in range(5):
                manager.start("admin-main")
                record = manager._records["admin-main"]
                old_records.append(record)
                self.wait_for_process_exit(record)

            manager.status("admin-main")

            for record in old_records:
                self.assertTrue(record.stdout_handle.closed)
                self.assertTrue(record.stderr_handle.closed)


if __name__ == "__main__":
    unittest.main()
