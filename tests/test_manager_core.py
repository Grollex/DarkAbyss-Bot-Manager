import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
FAKE_CHILD = Path(__file__).resolve().parent / "fake_child_process.py"
VALID_ADMIN_CONFIG = {
    "allow_server_administrators": True,
    "allowed_user_ids": [],
    "allowed_role_ids": [],
    "audit_channel_id": None,
}


class FakeProcess:
    _next_pid = 50000

    def __init__(
        self,
        *,
        exit_code: int | None = None,
        terminate_error: Exception | None = None,
        kill_error: Exception | None = None,
        wait_error: Exception | None = None,
        timeout_once: bool = False,
        block_wait_event: threading.Event | None = None,
    ):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.exit_code = exit_code
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_error = wait_error
        self.timeout_once = timeout_once
        self.block_wait_event = block_wait_event
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.terminate_called = threading.Event()

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminate_called.set()
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated = True

    def kill(self):
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        if self.block_wait_event is not None and not self.killed:
            self.block_wait_event.wait()
        if self.timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        if self.exit_code is None:
            self.exit_code = 0
        return self.exit_code


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

    def install_fake_record(self, manager_core, manager, instance, process):
        stdout_handle = (instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME).open("ab")
        stderr_handle = (instance.paths.logs_dir / manager_core.STDERR_LOG_NAME).open("ab")
        record = manager_core._ProcessRecord(
            instance_id=instance.id,
            bot_type=instance.bot_type,
            process=process,
            started_at=manager_core._now(),
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
        )
        manager._records[instance.id] = record
        return record

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

    def test_start_forces_missing_darkabyss_data_dir_without_mutating_launch_spec_env(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            captured = {}
            original_env = {"CUSTOM": "value"}

            def builder(instance, _bot_type):
                return manager_core.LaunchSpec(
                    executable=sys.executable,
                    args=(str(FAKE_CHILD), "--instance", instance.id),
                    cwd=PROJECT_ROOT,
                    env=original_env,
                    stdout_log_path=instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME,
                    stderr_log_path=instance.paths.logs_dir / manager_core.STDERR_LOG_NAME,
                )

            def fake_popen(*_args, **kwargs):
                captured["env"] = kwargs["env"]
                return FakeProcess()

            manager = manager_core.BotProcessManager(builder, popen_factory=fake_popen)
            manager.start("admin-main")
            try:
                self.assertEqual(captured["env"]["DARKABYSS_DATA_DIR"], str(manager_core.app_paths.DATA_ROOT.resolve()))
                self.assertEqual(captured["env"]["CUSTOM"], "value")
                self.assertNotIn("DARKABYSS_DATA_DIR", original_env)
                self.assertIsNot(captured["env"], original_env)
            finally:
                manager.shutdown_all(timeout=1)

    def test_start_overrides_wrong_darkabyss_data_dir_without_mutating_launch_spec_env(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            captured = {}
            original_env = {"DARKABYSS_DATA_DIR": "wrong-path"}

            def builder(instance, _bot_type):
                return manager_core.LaunchSpec(
                    executable=sys.executable,
                    args=(str(FAKE_CHILD), "--instance", instance.id),
                    cwd=PROJECT_ROOT,
                    env=original_env,
                    stdout_log_path=instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME,
                    stderr_log_path=instance.paths.logs_dir / manager_core.STDERR_LOG_NAME,
                )

            def fake_popen(*_args, **kwargs):
                captured["env"] = kwargs["env"]
                return FakeProcess()

            manager = manager_core.BotProcessManager(builder, popen_factory=fake_popen)
            manager.start("admin-main")
            try:
                self.assertEqual(captured["env"]["DARKABYSS_DATA_DIR"], str(manager_core.app_paths.DATA_ROOT.resolve()))
                self.assertEqual(original_env["DARKABYSS_DATA_DIR"], "wrong-path")
                self.assertIsNot(captured["env"], original_env)
            finally:
                manager.shutdown_all(timeout=1)

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

    def test_get_instance_info_returns_metadata_status_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            info = manager.get_instance_info("admin-main")

            self.assertEqual(info.instance_id, "admin-main")
            self.assertEqual(info.display_name, "Admin Bot")
            self.assertEqual(info.bot_type, "admin")
            self.assertEqual(info.bot_type_display_name, "Admin Bot")
            self.assertEqual(info.bot_version, "1.0.0")
            self.assertEqual(info.state, manager_core.STATE_STOPPED)
            self.assertEqual(info.config_path, instance.paths.config)
            self.assertEqual(info.logs_dir, instance.paths.logs_dir)
            self.assertEqual(info.stdout_log_path, instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME)
            self.assertEqual(info.stderr_log_path, instance.paths.logs_dir / manager_core.STDERR_LOG_NAME)
            self.assertFalse(hasattr(info, "token"))

    def test_list_status_and_instance_info_are_sorted_by_instance_id(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-second")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            self.assertEqual([status.instance_id for status in manager.list_status()], ["admin-main", "admin-second"])
            self.assertEqual([info.instance_id for info in manager.list_instance_info()], ["admin-main", "admin-second"])

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

    def test_invalid_timeouts_are_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            for timeout in (-1, "1", True, float("nan"), float("inf"), float("-inf")):
                with self.subTest(timeout=timeout):
                    with self.assertRaisesRegex(manager_core.ManagerCoreError, "timeout"):
                        manager.stop("admin-main", timeout=timeout)
                    with self.assertRaisesRegex(manager_core.ManagerCoreError, "timeout"):
                        manager.restart("admin-main", timeout=timeout)
                    with self.assertRaisesRegex(manager_core.ManagerCoreError, "timeout"):
                        manager.shutdown_all(timeout=timeout)

    def test_zero_timeout_escalates_to_kill(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            process = FakeProcess(timeout_once=True)
            self.install_fake_record(manager_core, manager, instance, process)

            status = manager.stop("admin-main", timeout=0)

            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertEqual(status.state, manager_core.STATE_STOPPED)
            self.assertEqual(status.exit_code, -9)

    def test_terminate_failure_does_not_report_stopped(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            self.install_fake_record(
                manager_core,
                manager,
                instance,
                FakeProcess(terminate_error=OSError("terminate failed")),
            )

            with self.assertRaisesRegex(manager_core.ProcessStopError, "terminate failed"):
                manager.stop("admin-main", timeout=1)

            try:
                self.assertEqual(manager.status("admin-main").state, manager_core.STATE_RUNNING)
            finally:
                manager._close_record_handles(manager._records["admin-main"])

    def test_kill_failure_reports_process_stop_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            self.install_fake_record(
                manager_core,
                manager,
                instance,
                FakeProcess(timeout_once=True, kill_error=OSError("kill failed")),
            )

            with self.assertRaisesRegex(manager_core.ProcessStopError, "kill failed"):
                manager.stop("admin-main", timeout=0)

            try:
                self.assertEqual(manager.status("admin-main").state, manager_core.STATE_RUNNING)
            finally:
                manager._close_record_handles(manager._records["admin-main"])

    def test_wait_failure_reports_process_stop_error_without_stopped_state(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            self.install_fake_record(
                manager_core,
                manager,
                instance,
                FakeProcess(wait_error=OSError("wait failed")),
            )

            with self.assertRaisesRegex(manager_core.ProcessStopError, "wait failed"):
                manager.stop("admin-main", timeout=1)

            try:
                self.assertEqual(manager.status("admin-main").state, manager_core.STATE_RUNNING)
            finally:
                manager._close_record_handles(manager._records["admin-main"])

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

    def test_natural_exit_visible_consistently_through_all_read_apis(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core, ("--mode", "exit", "--exit-code", "7"))
            )

            manager.start("admin-main")
            info = None
            deadline = time.time() + 5
            while time.time() < deadline:
                info = manager.get_instance_info("admin-main")
                if info.state == manager_core.STATE_EXITED:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(info)
            self.assertEqual(info.state, manager_core.STATE_EXITED)
            self.assertEqual(info.exit_code, 7)

            status = manager.status("admin-main")
            listed_status = manager.list_status()[0]
            listed_info = manager.list_instance_info()[0]

            self.assertEqual(status.state, manager_core.STATE_EXITED)
            self.assertEqual(listed_status.state, manager_core.STATE_EXITED)
            self.assertEqual(listed_info.state, manager_core.STATE_EXITED)
            self.assertEqual(status.exit_code, 7)
            self.assertEqual(listed_status.exit_code, 7)
            self.assertEqual(listed_info.exit_code, 7)

    def test_two_concurrent_starts_same_instance_do_not_duplicate_child(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker():
                barrier.wait()
                try:
                    results.append(manager.start("admin-main"))
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            second.start()
            first.join()
            second.join()
            try:
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].state, manager_core.STATE_RUNNING)
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], manager_core.ProcessAlreadyRunningError)
                self.assertEqual(manager.status("admin-main").pid, results[0].pid)
            finally:
                manager.shutdown_all(timeout=2)

    def test_blocking_stop_for_one_instance_does_not_block_other_status(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            main = self.create_instance(instance_store, "admin-main")
            second = self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            release_wait = threading.Event()
            main_process = FakeProcess(block_wait_event=release_wait)
            second_process = FakeProcess()
            self.install_fake_record(manager_core, manager, main, main_process)
            self.install_fake_record(manager_core, manager, second, second_process)
            stop_error = []

            def stop_main():
                try:
                    manager.stop("admin-main", timeout=5)
                except Exception as exc:
                    stop_error.append(exc)

            stop_thread = threading.Thread(target=stop_main)
            stop_thread.start()
            self.assertTrue(main_process.terminate_called.wait(timeout=2))
            self.assertTrue(stop_thread.is_alive())

            second_status = manager.status("admin-second")

            self.assertEqual(second_status.state, manager_core.STATE_RUNNING)
            self.assertTrue(stop_thread.is_alive())
            release_wait.set()
            stop_thread.join(timeout=2)
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(stop_error, [])
            manager.shutdown_all(timeout=2)

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

    def test_invalid_instance_id_public_apis_raise_manager_core_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, = load_modules(Path(data_dir), "manager_core")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            calls = (
                lambda: manager.start("../bad"),
                lambda: manager.stop("../bad"),
                lambda: manager.restart("../bad"),
                lambda: manager.status("../bad"),
                lambda: manager.get_instance_info("../bad"),
            )

            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaisesRegex(manager_core.ManagerCoreError, "Invalid bot instance"):
                        call()

    def test_malformed_instance_fails_clearly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            instance.paths.config.unlink()
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            with self.assertRaisesRegex(manager_core.ManagerCoreError, "config.json"):
                manager.start("admin-main")

    def test_malformed_instance_through_listing_raises_manager_core_error(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store, app_paths = load_modules(
                Path(data_dir),
                "manager_core",
                "instance_store",
                "app_paths",
            )
            self.create_instance(instance_store, "admin-main")
            malformed_root = app_paths.INSTANCES_DIR / "broken-instance"
            malformed_root.mkdir()
            (malformed_root / "instance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "broken-instance",
                        "bot_type": "admin",
                        "display_name": "Broken Admin",
                    }
                ),
                encoding="utf-8",
            )
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            with self.assertRaisesRegex(manager_core.ManagerCoreError, "Failed to list bot instances"):
                manager.list_status()
            with self.assertRaisesRegex(manager_core.ManagerCoreError, "Failed to list bot instances"):
                manager.list_instance_info()

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

    def test_shutdown_all_attempts_other_instances_when_one_stop_fails(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            main = self.create_instance(instance_store, "admin-main")
            second = self.create_instance(instance_store, "admin-second")
            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))
            self.install_fake_record(
                manager_core,
                manager,
                main,
                FakeProcess(terminate_error=OSError("terminate failed")),
            )
            second_record = self.install_fake_record(manager_core, manager, second, FakeProcess())

            results = manager.shutdown_all(timeout=1)

            self.assertIsInstance(results["admin-main"], manager_core.ProcessStopError)
            self.assertEqual(results["admin-second"].state, manager_core.STATE_STOPPED)
            self.assertTrue(second_record.stdout_handle.closed)
            self.assertTrue(second_record.stderr_handle.closed)
            manager._close_record_handles(manager._records["admin-main"])

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

    def test_launch_spec_builder_failure_leaves_no_running_state(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def broken_builder(_instance, _bot_type):
                raise OSError("builder failed")

            manager = manager_core.BotProcessManager(broken_builder)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "builder failed"):
                manager.start("admin-main")

            self.assertEqual(manager.status("admin-main").state, manager_core.STATE_STOPPED)

    def test_popen_failure_closes_opened_log_handles(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")
            captured = {}

            def failing_popen(*_args, **kwargs):
                captured["stdout"] = kwargs["stdout"]
                captured["stderr"] = kwargs["stderr"]
                raise OSError("popen failed")

            manager = manager_core.BotProcessManager(
                self.fake_builder(manager_core),
                popen_factory=failing_popen,
            )

            with self.assertRaisesRegex(manager_core.ProcessStartError, "popen failed"):
                manager.start("admin-main")

            self.assertTrue(captured["stdout"].closed)
            self.assertTrue(captured["stderr"].closed)
            self.assertEqual(manager.status("admin-main").state, manager_core.STATE_STOPPED)

    def test_log_open_failure_leaves_no_running_state(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            instance = self.create_instance(instance_store, "admin-main")
            blocked_log_path = instance.paths.logs_dir / manager_core.STDOUT_LOG_NAME
            blocked_log_path.mkdir()

            manager = manager_core.BotProcessManager(self.fake_builder(manager_core))

            with self.assertRaises(manager_core.ProcessStartError):
                manager.start("admin-main")

            self.assertEqual(manager.status("admin-main").state, manager_core.STATE_STOPPED)

    def test_invalid_launch_spec_cwd_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def bad_builder(instance, bot_type):
                spec = self.fake_builder(manager_core)(instance, bot_type)
                return manager_core.LaunchSpec(
                    executable=spec.executable,
                    args=spec.args,
                    cwd=Path(data_dir) / "missing-cwd",
                    env=spec.env,
                    stdout_log_path=spec.stdout_log_path,
                    stderr_log_path=spec.stderr_log_path,
                )

            manager = manager_core.BotProcessManager(bad_builder)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "cwd"):
                manager.start("admin-main")

    def test_invalid_launch_spec_env_is_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def bad_builder(instance, bot_type):
                spec = self.fake_builder(manager_core)(instance, bot_type)
                return manager_core.LaunchSpec(
                    executable=spec.executable,
                    args=spec.args,
                    cwd=spec.cwd,
                    env={"VALID": "yes", "BAD": 123},
                    stdout_log_path=spec.stdout_log_path,
                    stderr_log_path=spec.stderr_log_path,
                )

            manager = manager_core.BotProcessManager(bad_builder)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "env"):
                manager.start("admin-main")

    def test_launch_spec_log_paths_must_stay_inside_instance_logs_dir(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def bad_builder(instance, bot_type):
                spec = self.fake_builder(manager_core)(instance, bot_type)
                return manager_core.LaunchSpec(
                    executable=spec.executable,
                    args=spec.args,
                    cwd=spec.cwd,
                    env=spec.env,
                    stdout_log_path=instance.paths.root / "process.stdout.log",
                    stderr_log_path=spec.stderr_log_path,
                )

            manager = manager_core.BotProcessManager(bad_builder)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "stdout_log_path"):
                manager.start("admin-main")

    def test_launch_spec_args_must_be_tuple_of_strings(self):
        with tempfile.TemporaryDirectory() as data_dir:
            manager_core, instance_store = load_modules(Path(data_dir), "manager_core", "instance_store")
            self.create_instance(instance_store, "admin-main")

            def bad_builder(instance, bot_type):
                spec = self.fake_builder(manager_core)(instance, bot_type)
                return manager_core.LaunchSpec(
                    executable=spec.executable,
                    args=["not", "a", "tuple"],
                    cwd=spec.cwd,
                    env=spec.env,
                    stdout_log_path=spec.stdout_log_path,
                    stderr_log_path=spec.stderr_log_path,
                )

            manager = manager_core.BotProcessManager(bad_builder)

            with self.assertRaisesRegex(manager_core.ProcessStartError, "args"):
                manager.start("admin-main")

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
