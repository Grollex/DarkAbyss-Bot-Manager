import importlib
import os
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_gui_module(data_root: Path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    sys.path.insert(0, str(CORE_ROOT))
    for module_name in (
        "manager_gui",
        "manager_core",
        "config_store",
        "instance_store",
        "bot_registry",
        "app_paths",
        "admin_instance",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("manager_gui")


def get_qapplication():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@dataclass(frozen=True)
class FakeSnapshot:
    instance_id: str
    bot_type: str
    config_version: int
    overrides: dict
    defaults: dict
    effective: dict


class FakeConfigApi:
    def __init__(self):
        self.saved = []
        self.snapshot = FakeSnapshot(
            instance_id="admin-main",
            bot_type="admin",
            config_version=1,
            overrides={"allowed_user_ids": ["123"]},
            defaults={"allow_server_administrators": True, "allowed_user_ids": []},
            effective={"allow_server_administrators": True, "allowed_user_ids": ["123"]},
        )

    def get_config_snapshot(self, instance_id):
        self.loaded_instance_id = instance_id
        return self.snapshot

    def save_config_overrides(self, instance_id, overrides):
        self.saved.append((instance_id, overrides))
        self.snapshot = FakeSnapshot(
            instance_id=instance_id,
            bot_type="admin",
            config_version=1,
            overrides=overrides,
            defaults=self.snapshot.defaults,
            effective={**self.snapshot.defaults, **overrides},
        )
        return overrides


class FakeInstanceApi:
    def __init__(self):
        self.created = []
        self.error = None

    def create_instance(self, bot_type, instance_id, display_name=None):
        if self.error is not None:
            raise self.error
        self.created.append((bot_type, instance_id, display_name))


class FakeManager:
    def __init__(self, manager_core, infos=None):
        self.manager_core = manager_core
        self.calls = []
        self.shutdown_calls = 0
        self.infos = infos or [
            self.info("admin-second", "Second", manager_core.STATE_STOPPED, None),
            self.info("admin-main", "Main", manager_core.STATE_RUNNING, 4321),
        ]

    def info(self, instance_id, display_name, state, pid):
        return self.manager_core.InstanceInfo(
            instance_id=instance_id,
            display_name=display_name,
            bot_type="admin",
            bot_type_display_name="Discord Admin Bot",
            bot_version="1.0.0",
            state=state,
            pid=pid,
            started_at=datetime.now(timezone.utc) if pid else None,
            uptime_seconds=65.0 if pid else None,
            exit_code=None,
            config_path=Path("C:/tmp") / instance_id / "config.json",
            logs_dir=Path("C:/tmp") / instance_id / "logs",
            stdout_log_path=Path("C:/tmp") / instance_id / "logs" / "process.stdout.log",
            stderr_log_path=Path("C:/tmp") / instance_id / "logs" / "process.stderr.log",
        )

    def list_instance_info(self):
        self.calls.append(("list_instance_info",))
        return list(self.infos)

    def get_instance_info(self, instance_id):
        self.calls.append(("get_instance_info", instance_id))
        return next(info for info in self.infos if info.instance_id == instance_id)

    def start(self, instance_id):
        self.calls.append(("start", instance_id))
        return self.manager_core.ProcessStatus(instance_id, "admin", self.manager_core.STATE_RUNNING, 500, None, None, None)

    def stop(self, instance_id):
        self.calls.append(("stop", instance_id))
        return self.manager_core.ProcessStatus(instance_id, "admin", self.manager_core.STATE_STOPPED, None, None, None, 0)

    def restart(self, instance_id):
        self.calls.append(("restart", instance_id))
        return self.manager_core.ProcessStatus(instance_id, "admin", self.manager_core.STATE_RUNNING, 501, None, None, None)

    def shutdown_all(self):
        self.shutdown_calls += 1
        return {}


class ManagerGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_window(self, manager=None, instance_api=None, config_api=None):
        window = self.manager_gui.ManagerMainWindow(
            manager=manager or FakeManager(self.manager_gui.manager_core),
            instance_api=instance_api or FakeInstanceApi(),
            config_api=config_api or FakeConfigApi(),
            auto_refresh=False,
        )
        def close_without_prompt():
            window._allow_close = True
            window.close()

        self.addCleanup(close_without_prompt)
        return window

    def select_instance(self, window, instance_id):
        for row_index in range(window.instance_table.rowCount()):
            if window.instance_table.item(row_index, 0).text() == instance_id:
                window.instance_table.selectRow(row_index)
                return
        self.fail(f"Missing row for {instance_id}")

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for Qt condition.")

    def finish_workers_immediately(self, window):
        def immediate(action, finished):
            try:
                value = action()
            except Exception as exc:
                finished(self.manager_gui.ActionResult(False, str(exc), exc))
            else:
                finished(self.manager_gui.ActionResult(True, "OK", value))

        window._start_worker = immediate

    def test_gui_module_imports_without_starting_event_loop(self):
        self.assertTrue(hasattr(self.manager_gui, "main"))
        self.assertEqual(self.manager_gui.QApplication.instance(), self.app)

    def test_main_window_populates_two_fake_instances_sorted(self):
        window = self.make_window()

        ids = [window.instance_table.item(row, 0).text() for row in range(window.instance_table.rowCount())]

        self.assertEqual(ids, ["admin-main", "admin-second"])

    def test_status_display_reflects_running_stopped_exited(self):
        manager = FakeManager(
            self.manager_gui.manager_core,
            infos=[
                FakeManager(self.manager_gui.manager_core).info("admin-exited", "Exited", self.manager_gui.manager_core.STATE_EXITED, None),
                FakeManager(self.manager_gui.manager_core).info("admin-main", "Main", self.manager_gui.manager_core.STATE_RUNNING, 4321),
                FakeManager(self.manager_gui.manager_core).info("admin-second", "Second", self.manager_gui.manager_core.STATE_STOPPED, None),
            ],
        )
        window = self.make_window(manager=manager)
        statuses = {window.instance_table.item(row, 0).text(): window.instance_table.item(row, 3).text() for row in range(window.instance_table.rowCount())}

        self.assertEqual(statuses["admin-main"], self.manager_gui.manager_core.STATE_RUNNING)
        self.assertEqual(statuses["admin-second"], self.manager_gui.manager_core.STATE_STOPPED)
        self.assertEqual(statuses["admin-exited"], self.manager_gui.manager_core.STATE_EXITED)

    def test_start_dispatches_manager_start_for_selected_instance(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.finish_workers_immediately(window)
        self.select_instance(window, "admin-second")

        window.start_selected()

        self.assertIn(("start", "admin-second"), manager.calls)

    def test_stop_and_restart_use_worker_path(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.select_instance(window, "admin-main")
        worker_actions = []

        def capture_worker(action, finished):
            worker_actions.append(action)
            finished(self.manager_gui.ActionResult(True, "OK", None))

        window._start_worker = capture_worker
        window.stop_selected()
        window.restart_selected()

        self.assertEqual(len(worker_actions), 2)
        self.assertNotIn(("stop", "admin-main"), manager.calls)
        self.assertNotIn(("restart", "admin-main"), manager.calls)

    def test_real_worker_action_runs_off_gui_thread_and_callback_returns_to_gui_thread(self):
        window = self.make_window()
        gui_thread = self.app.thread()
        observed = {}

        def action():
            observed["action_thread"] = self.manager_gui.QThread.currentThread()
            return "done"

        def finished(result):
            observed["callback_thread"] = self.manager_gui.QThread.currentThread()
            observed["result"] = result

        window._start_worker(action, finished)

        self.wait_until(lambda: "result" in observed)
        self.assertTrue(observed["result"].ok)
        self.assertEqual(observed["result"].value, "done")
        self.assertIsNot(observed["action_thread"], gui_thread)
        self.assertIs(observed["callback_thread"], gui_thread)
        self.wait_until(lambda: not window._worker_handles)
        self.assertEqual(window._worker_handles, [])

    def test_manager_core_error_becomes_visible_error(self):
        class ErrorManager(FakeManager):
            def start(self, instance_id):
                raise self.manager_core.ManagerCoreError("boom")

        window = self.make_window(manager=ErrorManager(self.manager_gui.manager_core))
        window._show_error = lambda message: setattr(window, "_last_error", message)
        self.finish_workers_immediately(window)
        self.select_instance(window, "admin-main")

        window.start_selected()

        self.assertIn("boom", window.last_error)

    def test_config_editor_loads_overrides_via_config_store_api(self):
        config_api = FakeConfigApi()
        dialog = self.manager_gui.ConfigEditorDialog("admin-main", config_api)
        self.addCleanup(dialog.close)

        self.assertEqual(config_api.loaded_instance_id, "admin-main")
        self.assertIn("allowed_user_ids", dialog.overrides_edit.toPlainText())
        self.assertNotIn("token", dialog.overrides_edit.toPlainText().lower())

    def test_valid_json_object_save_calls_save_config_overrides(self):
        config_api = FakeConfigApi()
        dialog = self.manager_gui.ConfigEditorDialog("admin-main", config_api)
        self.addCleanup(dialog.close)
        dialog.overrides_edit.setPlainText('{"audit_channel_id": "123"}')

        dialog.save_overrides()

        self.assertEqual(config_api.saved, [("admin-main", {"audit_channel_id": "123"})])

    def test_invalid_json_does_not_call_save(self):
        config_api = FakeConfigApi()
        dialog = self.manager_gui.ConfigEditorDialog("admin-main", config_api)
        self.addCleanup(dialog.close)
        dialog.overrides_edit.setPlainText("{invalid")

        dialog.save_overrides()

        self.assertEqual(config_api.saved, [])
        self.assertIn("Invalid JSON", dialog.last_error)

    def test_json_array_does_not_call_save(self):
        config_api = FakeConfigApi()
        dialog = self.manager_gui.ConfigEditorDialog("admin-main", config_api)
        self.addCleanup(dialog.close)
        dialog.overrides_edit.setPlainText('["not", "object"]')

        dialog.save_overrides()

        self.assertEqual(config_api.saved, [])
        self.assertIn("object", dialog.last_error)

    def test_create_admin_instance_calls_instance_store(self):
        instance_api = FakeInstanceApi()
        window = self.make_window(instance_api=instance_api)
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.values.return_value = ("admin-second", "Second")

        with mock.patch.object(self.manager_gui, "CreateAdminInstanceDialog", return_value=fake_dialog):
            window.create_admin_instance()

        self.assertEqual(instance_api.created, [("admin", "admin-second", "Second")])

    def test_create_admin_instance_handles_filesystem_error(self):
        instance_api = FakeInstanceApi()
        instance_api.error = OSError("disk failed")
        window = self.make_window(instance_api=instance_api)
        window._show_error = lambda message: setattr(window, "_last_error", message)
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.values.return_value = ("admin-second", "Second")

        with mock.patch.object(self.manager_gui, "CreateAdminInstanceDialog", return_value=fake_dialog):
            window.create_admin_instance()

        self.assertIn("disk failed", window.last_error)

    def test_close_with_running_instances_does_not_silently_exit(self):
        window = self.make_window()
        event = mock.Mock()

        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Cancel):
            window.closeEvent(event)

        event.ignore.assert_called_once()

    def test_close_during_active_lifecycle_action_is_ignored(self):
        window = self.make_window()
        self.select_instance(window, "admin-main")
        event = mock.Mock()
        started = []

        def capture_worker(action, finished):
            started.append((action, finished))

        window._start_worker = capture_worker
        window.start_selected()

        window.closeEvent(event)

        self.assertEqual(len(started), 1)
        event.ignore.assert_called_once()
        self.assertIn("in progress", window.last_error)

    def test_repeated_close_does_not_start_second_shutdown_worker(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        events = [mock.Mock(), mock.Mock()]
        started = []

        def capture_worker(action, finished):
            started.append((action, finished))

        window._start_worker = capture_worker

        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok):
            window.closeEvent(events[0])
            window.closeEvent(events[1])

        self.assertEqual(len(started), 1)
        self.assertEqual(manager.shutdown_calls, 0)
        events[0].ignore.assert_called_once()
        events[1].ignore.assert_called_once()

    def test_stop_all_close_path_calls_shutdown_all(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.finish_workers_immediately(window)
        window.close = mock.Mock()

        window._run_shutdown_for_close()

        self.assertEqual(manager.shutdown_calls, 1)
        window.close.assert_called_once()

    def test_successful_shutdown_completion_allows_close(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.finish_workers_immediately(window)
        window.close = mock.Mock()

        window._run_shutdown_for_close()

        self.assertTrue(window._allow_close)
        window.close.assert_called_once()

    def test_shutdown_failure_keeps_window_open_and_reports_error(self):
        class FailingShutdownManager(FakeManager):
            def shutdown_all(self):
                self.shutdown_calls += 1
                return {"admin-main": self.manager_core.ManagerCoreError("failed stop")}

        manager = FailingShutdownManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.finish_workers_immediately(window)
        window.close = mock.Mock()
        window._show_error = lambda message: setattr(window, "_last_error", message)

        window._run_shutdown_for_close()

        self.assertFalse(window._allow_close)
        self.assertFalse(window._shutdown_in_progress)
        window.close.assert_not_called()
        self.assertIn("failed stop", window.last_error)

    def test_parse_overrides_json_rejects_non_object(self):
        with self.assertRaisesRegex(ValueError, "object"):
            self.manager_gui.parse_overrides_json("[1, 2]")

    def test_instance_info_formatting_does_not_include_token_paths(self):
        info = FakeManager(self.manager_gui.manager_core).info("admin-main", "Main", self.manager_gui.manager_core.STATE_RUNNING, 123)
        details = self.manager_gui.instance_info_details(info)

        self.assertIn("admin-main", details)
        self.assertIn("Config path:", details)
        self.assertNotIn("token", details.lower())


if __name__ == "__main__":
    unittest.main()
