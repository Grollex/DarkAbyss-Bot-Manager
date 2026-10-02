import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace
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
        "manager_dashboard",
        "manager_groups",
        "manager_terminal",
        "manager_game_presence",
        "admin_terminal",
        "manager_core",
        "config_store",
        "instance_store",
        "bot_registry",
        "app_paths",
        "admin_instance",
        "ai_platform",
        "ai_storage",
        "ai_groq",
        "ai_gemini",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("manager_gui")


def get_qapplication():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _profile_by_id_for_test(settings, profile_id):
    return next(profile for profile in settings.profiles if profile.profile_id == profile_id)


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
    def __init__(self, root=None):
        self.created = []
        self.error = None
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp())
        self.instance_id = "admin-main"
        self.display_names = {"admin-main": "Main"}
        self.instance_root = self.root / "instances" / self.instance_id
        self.token_path = self.instance_root / "secrets" / "token.txt"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text("PUT_DISCORD_BOT_TOKEN_HERE\n", encoding="utf-8")

    def create_instance(self, bot_type, instance_id, display_name=None):
        if self.error is not None:
            raise self.error
        self.created.append((bot_type, instance_id, display_name))
        self.display_names[instance_id] = display_name or "Admin Bot"
        return self.load_instance(instance_id)

    def load_instance(self, instance_id):
        self.instance_id = instance_id
        self.instance_root = self.root / "instances" / instance_id
        self.token_path = self.instance_root / "secrets" / "token.txt"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.token_path.exists():
            self.token_path.write_text("PUT_DISCORD_BOT_TOKEN_HERE\n", encoding="utf-8")
        paths = SimpleNamespace(
            root=self.instance_root,
            config=self.instance_root / "config.json",
            config_meta=self.instance_root / "config.meta.json",
            token=self.token_path,
            secrets_dir=self.instance_root / "secrets",
            runtime_dir=self.instance_root / "runtime",
            logs_dir=self.instance_root / "logs",
            data_dir=self.instance_root / "data",
        )
        return SimpleNamespace(id=instance_id, bot_type="admin", display_name=self.display_names.get(instance_id, "Admin Bot"), paths=paths)

    def update_instance_display_name(self, instance_id, display_name):
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display_name must be a non-empty string.")
        self.display_names[instance_id] = display_name.strip()
        return self.load_instance(instance_id)


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

    def test_default_window_manager_uses_source_launch_strategy(self):
        window = self.manager_gui.ManagerMainWindow(auto_refresh=False)

        def close_without_prompt():
            window._allow_close = True
            window.close()

        self.addCleanup(close_without_prompt)

        self.assertEqual(
            window.manager._launch_spec_builder,
            self.manager_gui.manager_core.build_source_launch_spec,
        )

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

    def test_dashboard_navigation_cards_and_toggle(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.finish_workers_immediately(window)

        for page in ("dashboard", "bots", "ai", "terminal", "presence", "commands", "logs"):
            window.show_page(page)
            self.assertTrue(window.nav_buttons[page].isChecked())
            self.assertEqual(window.pages.currentIndex(), window._page_index[page])
        self.assertIn("1 of 2 bot(s) running", window.discord_card.detail_label.text())
        self.assertIn("slash commands", window.commands_card.value_label.text())
        self.assertGreater(window.tools_tree.topLevelItemCount(), 5)
        self.assertEqual(window.ai_providers_button.text(), "AI Providers...")

        self.select_instance(window, "admin-main")  # running -> the toggle stops it
        self.assertIn("Stop", window.quick_toggle_button.text())
        window.toggle_selected_bot()
        self.assertIn(("stop", "admin-main"), manager.calls)
        self.select_instance(window, "admin-second")  # stopped -> the toggle starts it
        window.toggle_selected_bot()
        self.assertIn(("start", "admin-second"), manager.calls)

    def test_dashboard_records_state_changes_and_logs_page_reads_tails(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        core = self.manager_gui.manager_core
        manager.infos = [manager.info("admin-second", "Second", core.STATE_RUNNING, 99), manager.infos[1]]
        window.refresh_instances()
        self.assertIn("Second started", window.activity.entries[0][2])

        logs = Path(self.temp_dir.name) / "logs"
        logs.mkdir()
        (logs / "process.stderr.log").write_text("line one\nshard has connected to Gateway\n", encoding="utf-8")
        info = replace(
            manager.infos[0],
            logs_dir=logs,
            stderr_log_path=logs / "process.stderr.log",
            stdout_log_path=logs / "process.stdout.log",
        )
        manager.infos = [info, manager.infos[1]]
        window.refresh_instances()
        window.show_page("logs")
        window.log_instance_combo.setCurrentIndex(window.log_instance_combo.findData("admin-second"))
        window.refresh_logs()
        self.assertIn("connected to Gateway", window.log_view.toPlainText())
        self.assertIn("(empty)", window.log_view.toPlainText())

    def test_dashboard_provider_test_requires_a_saved_key(self):
        window = self.make_window()
        started = []
        window._start_worker = lambda action, finished: started.append(action)
        window.test_provider("groq")
        self.assertEqual(started, [])
        self.assertIn("no API key saved", window.last_error)
        self.assertEqual(window.provider_rows[0].status_label.text(), "Not configured")
        self.assertFalse(window.provider_rows[0].test_button.isEnabled())

    def test_bot_connection_state_and_log_tail_helpers(self):
        dash = self.manager_gui.dash
        core = self.manager_gui.manager_core
        manager = FakeManager(core)
        log = Path(self.temp_dir.name) / "stderr.log"

        def state(text, running=True, exit_code=None):
            log.write_text(text, encoding="utf-8")
            info = replace(
                manager.infos[1],
                state=core.STATE_RUNNING if running else core.STATE_EXITED,
                exit_code=exit_code,
                stderr_log_path=log,
            )
            return dash.bot_connection_state(info)

        login = "logging in using static token\n"
        self.assertEqual(state(login + "Shard ID None has connected to Gateway\n"), ("Online", "ok"))
        self.assertEqual(state("old has connected to Gateway\n" + login), ("Connecting...", "warn"))
        self.assertEqual(state(login + "discord.errors.PrivilegedIntentsRequired: ...\n"), ("Login refused", "bad"))
        self.assertEqual(state("", running=False), ("Offline", "muted"))
        self.assertEqual(state("", running=False, exit_code=1), ("Stopped (error)", "bad"))

        log.write_bytes(b"x" * 100 + b"\nlast line\n")
        self.assertEqual(dash.read_log_tail(log, 20), "last line\n")
        self.assertEqual(dash.read_log_tail(Path(self.temp_dir.name) / "missing.log"), "")

    def test_group_store_add_assign_rename_remove(self):
        groups = self.manager_gui.manager_groups
        store = groups.GroupStore(Path(self.temp_dir.name) / "groups.json")
        self.assertEqual(store.groups(), [groups.DEFAULT_GROUP])
        self.assertEqual(store.group_of("admin-main"), groups.DEFAULT_GROUP)
        store.add_group("  Vasya  server ")
        with self.assertRaises(groups.GroupError):
            store.add_group("vasya server")
        with self.assertRaises(groups.GroupError):
            store.add_group("   ")
        store.assign("admin-second", "Vasya server")
        self.assertEqual(store.group_of("admin-second"), "Vasya server")
        store.rename_group("Vasya server", "Vasya")
        self.assertEqual(store.group_of("admin-second"), "Vasya")
        with self.assertRaises(groups.GroupError):
            store.remove_group(groups.DEFAULT_GROUP)
        store.remove_group("Vasya")
        self.assertEqual(store.group_of("admin-second"), groups.DEFAULT_GROUP)
        (Path(self.temp_dir.name) / "groups.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(store.groups(), [groups.DEFAULT_GROUP])

    def test_group_tabs_filter_the_bot_table(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager)
        self.assertEqual(window.instance_table.rowCount(), 2)  # "All bots" tab
        window.group_store.add_group("Friends")
        window.group_store.assign("admin-second", "Friends")
        window._reload_group_tabs(select="Friends")
        window.refresh_instances()
        rows = [window.instance_table.item(row, 0).text() for row in range(window.instance_table.rowCount())]
        self.assertEqual(rows, ["admin-second"])
        self.assertTrue(window.remove_group_button.isEnabled())
        labels = [label for _id, label, _info in window._terminal_bots()]
        self.assertIn("Friends · Second", labels)
        self.assertIn("Main · Main", labels)

    def test_terminal_panel_sends_requests_and_renders_events(self):
        terminal = self.manager_gui.manager_terminal.admin_terminal
        core = self.manager_gui.manager_core
        logs = Path(self.temp_dir.name) / "instances" / "admin-main" / "logs"
        runtime = terminal.runtime_dir_for_logs(logs)
        manager = FakeManager(core)
        manager.infos = [replace(manager.infos[1], logs_dir=logs)]
        terminal.write_bot_status(
            runtime,
            {"bot_name": "Kairo#1", "guilds": [{"id": "10", "name": "Null", "channels": [{"id": "100", "name": "general", "type": "text"}]}]},
        )
        window = self.make_window(manager=manager)
        window.show_page("terminal")
        panel = window.terminal_panel
        self.assertEqual(panel.guild_combo.currentText(), "Null")
        panel.channel_combo.setCurrentIndex(panel.channel_combo.findData("100"))
        panel.input.setPlainText("create a role")
        panel.send()

        request = terminal.take_requests(runtime)[0]
        self.assertEqual((request["guild_id"], request["channel_id"], request["prompt"]), ("10", "100", "create a role"))
        writer = terminal.EventWriter(runtime, request["request_id"])
        writer.emit("message", text="Done.\n-# Groq")
        writer.emit("approval", token="abcd1234", text="Plan", buttons=[{"label": "Approve plan", "approved": True}, {"label": "Cancel", "approved": False}])
        writer.emit("idle")
        panel.poll()

        card = panel._requests[request["request_id"]]["cards"]["abcd1234"]
        self.assertEqual(card.buttons[0].text(), "Approve plan")
        card.buttons[0].click()
        decision = terminal.take_decisions(runtime)[0]
        self.assertEqual((decision["token"], decision["approved"]), ("abcd1234", True))
        writer.emit("resolved", token="abcd1234", text="Plan\n\nApproved.")
        panel.poll()
        self.assertTrue(card.buttons[0].isHidden())

    def test_terminal_panel_explains_stopped_bot(self):
        manager = FakeManager(self.manager_gui.manager_core)
        manager.infos = [manager.infos[0]]  # admin-second is stopped
        window = self.make_window(manager=manager)
        window.show_page("terminal")
        self.assertIn("not running", window.terminal_panel.hint_label.text())
        self.assertFalse(window.terminal_panel.send_button.isEnabled())

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

        labels = "\n".join(label.text() for label in dialog.findChildren(self.manager_gui.QLabel))
        self.assertIn("Advanced configuration", labels)
        self.assertIn("User overrides", labels)
        self.assertIn("Effective config", labels)
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

    def test_setup_dialog_loads_guided_fields_without_showing_token(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        instance_api.token_path.write_text("FAKE_SAVED_TOKEN\n", encoding="utf-8")

        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.token_edit.text(), "")
        self.assertIn("Token is configured", dialog.token_status_label.text())
        self.assertTrue(dialog.allow_admins_checkbox.isChecked())
        self.assertEqual(dialog.allowed_users_edit.text(), "123")
        self.assertEqual(dialog.display_name_edit.text(), "Main")
        self.assertIn("Step 1 of", dialog.current_step_label.text())
        self.assertIn("Use the", dialog.instructions_label.text())
        self.assertIn("help", dialog.instructions_label.text())
        self.assertEqual(dialog.token_help_button.text(), "i")
        self.assertIn("border-radius", dialog.token_help_button.styleSheet())

    def test_setup_dialog_saves_display_name_token_and_structured_config(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        dialog.display_name_edit.setText("My Server Admin")
        dialog.token_edit.setText("FAKE_NEW_TOKEN")
        dialog.allow_admins_checkbox.setChecked(False)
        dialog.allowed_users_edit.setText("111, 222")
        dialog.allowed_roles_edit.setText("333 444")
        dialog.audit_channel_edit.setText("555")

        self.assertTrue(dialog.save_setup())

        self.assertEqual(instance_api.token_path.read_text(encoding="utf-8"), "FAKE_NEW_TOKEN\n")
        self.assertEqual(instance_api.display_names["admin-main"], "My Server Admin")
        self.assertEqual(
            config_api.saved[-1],
            (
                "admin-main",
                {
                    "allowed_user_ids": ["111", "222"],
                    "allow_server_administrators": False,
                    "allowed_role_ids": ["333", "444"],
                    "audit_channel_id": "555",
                },
            ),
        )

    def test_setup_dialog_ai_whitelist_fields_load_save_and_explain(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        config_api.snapshot = replace(
            config_api.snapshot,
            overrides={"allowed_user_ids": ["123"], "ai_allowed_role_ids": ["900"], "unrelated": {"keep": True}},
            effective={
                "allow_server_administrators": True,
                "allowed_user_ids": ["123"],
                "ai_allowed_user_ids": [],
                "ai_allowed_role_ids": ["900"],
            },
        )
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.ai_allowed_users_edit.text(), "")
        self.assertEqual(dialog.ai_allowed_roles_edit.text(), "900")
        explanation = dialog.ai_access_label.text()
        self.assertIn("explicit user or role", explanation)
        self.assertIn("Discord Administrator alone does not grant /ai access", explanation)
        self.assertIn("role", explanation.lower())
        self.assertIn("ai_users", self.manager_gui.SETUP_HELP_TEXT)
        self.assertIn("ai_roles", self.manager_gui.SETUP_HELP_TEXT)

        dialog.ai_allowed_users_edit.setText("111 222")
        dialog.ai_allowed_roles_edit.setText("")
        self.assertTrue(dialog.save_setup())
        saved = config_api.saved[-1][1]
        self.assertEqual(saved["ai_allowed_user_ids"], ["111", "222"])
        self.assertEqual(saved["ai_allowed_role_ids"], [])
        self.assertEqual(saved["allowed_user_ids"], ["123"])
        self.assertTrue(saved["allow_server_administrators"])
        self.assertEqual(saved["unrelated"], {"keep": True})

        dialog.ai_allowed_roles_edit.setText("not-an-id")
        self.assertFalse(dialog.save_setup())
        self.assertIn("digits", dialog.last_error)

    def test_setup_dialog_ai_control_channel_load_save_clear_preserve(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        config_api.snapshot = replace(
            config_api.snapshot,
            overrides={"allowed_user_ids": ["123"], "audit_channel_id": "444", "unrelated": [1, 2]},
            effective={
                "allow_server_administrators": True,
                "allowed_user_ids": ["123"],
                "audit_channel_id": "444",
                "ai_control_channel_id": None,
            },
        )
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.ai_control_channel_edit.text(), "")
        self.assertEqual(dialog.audit_channel_edit.text(), "444")
        explanation = dialog.ai_control_channel_label.text()
        self.assertIn("Message Content Intent", explanation)
        self.assertIn("restart", explanation)
        self.assertIn("whitelist", explanation)
        help_text = self.manager_gui.SETUP_HELP_TEXT["ai_channel"][1]
        self.assertIn("Message Content Intent", help_text)
        self.assertIn("AI allowed user IDs", help_text)
        self.assertIn("audit channel", help_text)

        self.assertTrue(dialog.save_setup())
        self.assertNotIn("ai_control_channel_id", config_api.saved[-1][1])

        dialog.ai_control_channel_edit.setText(" 987654321 ")
        self.assertTrue(dialog.save_setup())
        saved = config_api.saved[-1][1]
        self.assertEqual(saved["ai_control_channel_id"], "987654321")
        self.assertEqual(saved["audit_channel_id"], "444")
        self.assertEqual(saved["unrelated"], [1, 2])
        self.assertEqual(saved["allowed_user_ids"], ["123"])
        self.assertEqual(dialog.ai_control_channel_edit.text(), "987654321")

        dialog.ai_control_channel_edit.setText("")
        self.assertTrue(dialog.save_setup())
        self.assertIsNone(config_api.saved[-1][1]["ai_control_channel_id"])

        dialog.ai_control_channel_edit.setText("chan")
        self.assertFalse(dialog.save_setup())
        self.assertIn("AI control channel ID", dialog.last_error)

    def test_setup_dialog_does_not_add_empty_ai_fields_to_overrides(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        self.assertTrue(dialog.save_setup())

        saved = config_api.saved[-1][1]
        self.assertNotIn("ai_allowed_user_ids", saved)
        self.assertNotIn("ai_allowed_role_ids", saved)
        self.assertNotIn("ai_control_channel_id", saved)
        self.assertNotIn("ai_confirmation_mode", saved)
        self.assertNotIn("ai_read_message_content", saved)

    def test_setup_dialog_ai_confirmation_mode_and_message_reading(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.ai_confirmation_combo.currentData(), "plan")
        self.assertFalse(dialog.ai_read_content_checkbox.isChecked())
        self.assertIn("Message Content Intent", dialog.ai_behaviour_label.text())

        dialog.ai_confirmation_combo.setCurrentIndex(dialog.ai_confirmation_combo.findData("strict"))
        dialog.ai_read_content_checkbox.setChecked(True)
        dialog.ai_mention_checkbox.setChecked(True)
        dialog.ai_mention_channels_edit.setText("111, 222")
        self.assertTrue(dialog.save_setup())
        saved = config_api.saved[-1][1]
        self.assertEqual(saved["ai_confirmation_mode"], "strict")
        self.assertIs(saved["ai_read_message_content"], True)
        self.assertIs(saved["ai_mention_enabled"], True)
        self.assertEqual(saved["ai_mention_channel_ids"], ["111", "222"])
        dialog.ai_mention_channels_edit.setText("general")
        self.assertFalse(dialog.save_setup())

    def test_setup_dialog_rejects_invalid_discord_ids(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)
        dialog.allowed_users_edit.setText("not-an-id")

        self.assertFalse(dialog.save_setup())

        self.assertIn("digits", dialog.last_error)
        self.assertEqual(config_api.saved, [])
        self.assertEqual(instance_api.token_path.read_text(encoding="utf-8"), "PUT_DISCORD_BOT_TOKEN_HERE\n")

    def test_setup_dialog_empty_token_preserves_existing_token(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        config_api = FakeConfigApi()
        instance_api.token_path.write_text("FAKE_EXISTING_TOKEN\n", encoding="utf-8")
        dialog = self.manager_gui.BotSetupDialog("admin-main", instance_api, config_api)
        self.addCleanup(dialog.close)

        dialog.token_edit.setText("")
        self.assertTrue(dialog.save_setup())

        self.assertEqual(instance_api.token_path.read_text(encoding="utf-8"), "FAKE_EXISTING_TOKEN\n")
        self.assertNotIn("FAKE_EXISTING_TOKEN", dialog.token_status_label.text())

    def test_application_id_validation_and_invite_url(self):
        with self.assertRaisesRegex(ValueError, "required"):
            self.manager_gui.build_discord_invite_url("")
        with self.assertRaisesRegex(ValueError, "digits"):
            self.manager_gui.build_discord_invite_url("abc")

        invite_url = self.manager_gui.build_discord_invite_url("1234567890")

        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(invite_url)
        params = parse_qs(parsed.query)
        permissions = int(params["permissions"][0])
        self.assertEqual(params["client_id"], ["1234567890"])
        self.assertEqual(params["scope"], ["bot applications.commands"])
        self.assertEqual(params["integration_type"], ["0"])
        self.assertEqual(permissions, self.manager_gui.DISCORD_ADMIN_BOT_PERMISSIONS)
        self.assertTrue(permissions & self.manager_gui.DISCORD_PERMISSION_BITS["View Channels"])
        self.assertTrue(permissions & self.manager_gui.DISCORD_PERMISSION_BITS["Moderate Members"])
        self.assertFalse(permissions & (1 << 3))
        self.assertNotIn("token", invite_url.lower())
        self.assertNotIn("client_secret", invite_url.lower())

    def test_intent_acknowledgement_is_user_confirmed_not_verified(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)

        self.assertFalse(dialog.intent_ack_checkbox.isChecked())
        dialog.pages.setCurrentIndex(dialog.pages.count() - 1)
        self.assertIn("Manager cannot verify", dialog.ready_summary_label.text())
        self.assertIn("USER-CONFIRMED DISCORD STEPS", dialog.ready_summary_label.text())

    def test_ready_summary_empty_display_name_is_missing(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)
        dialog.display_name_edit.setText("   ")

        dialog.pages.setCurrentIndex(dialog.pages.count() - 1)

        self.assertIn("[MISSING] Manager display name", dialog.ready_summary_label.text())
        self.assertNotIn("[OK] Manager display name: missing", dialog.ready_summary_label.text())

    def test_finish_buttons_only_available_on_final_page(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)

        dialog.pages.setCurrentIndex(0)
        self.assertTrue(dialog.finish_button.isHidden())
        self.assertTrue(dialog.start_button.isHidden())
        self.assertFalse(dialog.next_button.isHidden())

        dialog.pages.setCurrentIndex(dialog.pages.count() - 1)
        self.assertFalse(dialog.finish_button.isHidden())
        self.assertTrue(dialog.finish_button.isEnabled())
        self.assertFalse(dialog.start_button.isHidden())
        self.assertTrue(dialog.next_button.isHidden())

    def test_finish_saves_and_closes_without_starting(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)
        dialog.pages.setCurrentIndex(dialog.pages.count() - 1)
        dialog.accept = mock.Mock()
        dialog.save_setup = mock.Mock(return_value=True)

        dialog.finish_setup()

        dialog.save_setup.assert_called_once()
        dialog.accept.assert_called_once()
        self.assertFalse(dialog.start_requested)

    def test_finish_does_not_close_when_save_fails(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)
        dialog.pages.setCurrentIndex(dialog.pages.count() - 1)
        dialog.accept = mock.Mock()
        dialog.save_setup = mock.Mock(return_value=False)

        dialog.finish_setup()

        dialog.save_setup.assert_called_once()
        dialog.accept.assert_not_called()
        self.assertFalse(dialog.start_requested)

    def test_bot_installation_guidance_covers_private_and_shareable_modes(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)
        labels = "\n".join(label.text() for label in dialog.findChildren(self.manager_gui.QLabel))

        self.assertIn("Installation mode is your choice", labels)
        self.assertIn("Public Bot = OFF", labels)
        self.assertIn("Public Bot = ON", labels)
        self.assertIn("generated invite link", labels)
        self.assertIn("Install Link = None", labels)

        with mock.patch.object(self.manager_gui.QMessageBox, "information") as information:
            dialog.installation_help_button.click()
        body = information.call_args.args[2]
        self.assertIn("PRIVATE:", body)
        self.assertIn("Public Bot = OFF", body)
        self.assertIn("owner/developer team", body)
        self.assertIn("SHAREABLE:", body)
        self.assertIn("Public Bot = ON", body)
        self.assertIn("Manager-generated invite link", body)
        self.assertIn("does not change DarkAbyss access control", body)
        self.assertNotIn("must be OFF", body)

    def test_open_installation_page_uses_application_id_when_valid(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)
        dialog.application_id_edit.setText("1234567890")

        with mock.patch.object(self.manager_gui.QDesktopServices, "openUrl") as open_url:
            dialog.open_application_installation_page()

        self.assertEqual(open_url.call_args.args[0].toString(), "https://discord.com/developers/applications/1234567890/installation")

    def test_setup_help_buttons_open_topic_instructions(self):
        dialog = self.manager_gui.BotSetupDialog("admin-main", FakeInstanceApi(self.temp_dir.name), FakeConfigApi())
        self.addCleanup(dialog.close)

        with mock.patch.object(self.manager_gui.QMessageBox, "information") as information:
            dialog.display_name_help_button.click()
            dialog.application_help_button.click()
            dialog.installation_help_button.click()
            dialog.token_help_button.click()
            dialog.intent_help_button.click()
            dialog.users_help_button.click()
            dialog.roles_help_button.click()
            dialog.audit_help_button.click()
            dialog.administrators_help_button.click()

        titles = [call.args[1] for call in information.call_args_list]
        bodies = [call.args[2] for call in information.call_args_list]
        self.assertEqual(
            titles,
            [
                "Manager display name",
                "Discord Application ID",
                "Bot installation access",
                "Discord bot token",
                "Server Members Intent",
                "Allowed user IDs",
                "Allowed role IDs",
                "Audit channel ID",
                "Server administrators",
            ],
        )
        self.assertIn("local label", bodies[0])
        self.assertIn("Application ID", bodies[1])
        self.assertIn("PRIVATE:", bodies[2])
        self.assertIn("SHAREABLE:", bodies[2])
        self.assertIn("Developer Portal", bodies[3])
        self.assertIn("privileged", bodies[4])
        self.assertIn("Copy User ID", bodies[5])
        self.assertIn("Copy Role ID", bodies[6])
        self.assertIn("Copy Channel ID", bodies[7])
        self.assertIn("Administrator", bodies[8])

    def test_setup_selected_bot_opens_setup_dialog(self):
        window = self.make_window(instance_api=FakeInstanceApi(self.temp_dir.name), config_api=FakeConfigApi())
        self.select_instance(window, "admin-main")
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.start_requested = False

        with mock.patch.object(self.manager_gui, "BotSetupDialog", return_value=fake_dialog) as dialog_class:
            window.setup_selected_bot()

        dialog_class.assert_called_once()
        self.assertEqual(dialog_class.call_args.args[0], "admin-main")

    def test_main_setup_workflow_does_not_open_raw_json_editor(self):
        window = self.make_window(instance_api=FakeInstanceApi(self.temp_dir.name), config_api=FakeConfigApi())
        self.select_instance(window, "admin-main")
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.start_requested = False

        with mock.patch.object(self.manager_gui, "ConfigEditorDialog") as config_editor, mock.patch.object(
            self.manager_gui, "BotSetupDialog", return_value=fake_dialog
        ):
            window.setup_selected_bot()

        config_editor.assert_not_called()

    def test_advanced_json_button_remains_functional(self):
        window = self.make_window(instance_api=FakeInstanceApi(self.temp_dir.name), config_api=FakeConfigApi())
        self.select_instance(window, "admin-main")
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted

        with mock.patch.object(self.manager_gui, "ConfigEditorDialog", return_value=fake_dialog) as dialog_class:
            window.edit_selected_config()

        self.assertEqual(window.edit_config_button.text(), "Advanced JSON...")
        self.assertIn("Advanced", window.edit_config_button.toolTip())
        dialog_class.assert_called_once()

    def test_setup_save_and_start_delegates_to_existing_lifecycle_path(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager=manager, instance_api=FakeInstanceApi(self.temp_dir.name), config_api=FakeConfigApi())
        self.finish_workers_immediately(window)
        self.select_instance(window, "admin-main")
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.start_requested = True

        with mock.patch.object(self.manager_gui, "BotSetupDialog", return_value=fake_dialog):
            window.setup_selected_bot()

        self.assertIn(("start", "admin-main"), manager.calls)

    def test_create_admin_instance_calls_instance_store_and_opens_setup(self):
        instance_api = FakeInstanceApi()
        window = self.make_window(instance_api=instance_api)
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.values.return_value = ("admin", "admin-second", "Second")
        fake_setup = mock.Mock()
        fake_setup.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_setup.start_requested = False

        with mock.patch.object(self.manager_gui, "CreateBotInstanceDialog", return_value=fake_dialog), mock.patch.object(
            self.manager_gui, "BotSetupDialog", return_value=fake_setup
        ) as setup_class:
            window.create_bot_instance()

        self.assertEqual(instance_api.created, [("admin", "admin-second", "Second")])
        setup_class.assert_called_once()
        self.assertEqual(setup_class.call_args.args[0], "admin-second")

    def test_create_admin_instance_handles_filesystem_error(self):
        instance_api = FakeInstanceApi()
        instance_api.error = OSError("disk failed")
        window = self.make_window(instance_api=instance_api)
        window._show_error = lambda message: setattr(window, "_last_error", message)
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.values.return_value = ("admin", "admin-second", "Second")

        with mock.patch.object(self.manager_gui, "CreateBotInstanceDialog", return_value=fake_dialog):
            window.create_bot_instance()

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

    def make_ai_dialog(self, provider_factory=None, gemini_provider_factory=None):
        ai_platform = self.manager_gui.ai_platform
        settings_store = ai_platform.AISettingsStore(Path(self.temp_dir.name) / "config" / "ai.json")
        credential_store = ai_platform.CredentialStore(Path(self.temp_dir.name) / "secrets" / "ai")
        provider_factory = provider_factory or self.fake_groq_provider_factory()
        gemini_provider_factory = gemini_provider_factory or self.fake_gemini_provider_factory()
        dialog = self.manager_gui.AIProviderSettingsDialog(
            settings_store=settings_store,
            credential_store=credential_store,
            provider_factory=provider_factory,
            gemini_provider_factory=gemini_provider_factory,
        )
        self.addCleanup(dialog.close)
        return dialog, settings_store, credential_store

    def fake_groq_provider_factory(self, state=None):
        ai_platform = self.manager_gui.ai_platform

        class FakeProvider:
            metadata = ai_platform.ProviderMetadata(
                provider_id="groq",
                display_name="Groq",
                models=(
                    ai_platform.ProviderModel(
                        model_id="openai/gpt-oss-120b",
                        display_name="GPT-OSS 120B",
                    ),
                ),
            )

            async def test_connection(self, credential_ref):
                return ai_platform.Availability(state or ai_platform.AvailabilityState.AVAILABLE, "SECRET must not show")

        return lambda credential_store: FakeProvider()

    def fake_gemini_provider_factory(self, state=None):
        ai_platform = self.manager_gui.ai_platform

        class FakeProvider:
            metadata = ai_platform.ProviderMetadata(
                provider_id="gemini",
                display_name="Google Gemini",
                models=(
                    ai_platform.ProviderModel(
                        model_id="gemini-3.8-flash",
                        display_name="Gemini 3.8 Flash",
                    ),
                    ai_platform.ProviderModel(
                        model_id="gemini-3.5-flash-lite",
                        display_name="Gemini 3.5 Flash Lite",
                    ),
                ),
            )

            async def test_connection(self, credential_ref):
                return ai_platform.Availability(state or ai_platform.AvailabilityState.AVAILABLE, "SECRET must not show")

        return lambda credential_store: FakeProvider()

    def test_ai_providers_button_exists_and_does_not_require_selection(self):
        window = self.make_window()
        self.assertEqual(window.ai_providers_button.text(), "AI Providers...")
        window.instance_table.clearSelection()
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        with mock.patch.object(self.manager_gui, "AIProviderSettingsDialog", return_value=fake_dialog) as dialog_class:
            window.open_ai_providers()
        dialog_class.assert_called_once()
        fake_dialog.exec.assert_called_once()

    def test_ai_provider_dialog_has_groq_and_gemini_tabs(self):
        dialog, _, _ = self.make_ai_dialog()

        labels = [dialog.provider_tabs.tabText(index) for index in range(dialog.provider_tabs.count())]

        self.assertIn("Groq", labels)
        self.assertIn("Gemini", labels)
        self.assertEqual(dialog.gemini_model_combo.currentData(), "gemini-3.8-flash")
        self.assertEqual(dialog.gemini_key_edit.text(), "")
        self.assertEqual(dialog.gemini_key_edit.placeholderText(), "Paste Gemini API key")

    def test_ai_provider_routing_tab_sets_planner_and_executor(self):
        dialog, settings_store, _ = self.make_ai_dialog()
        labels = [dialog.provider_tabs.tabText(index) for index in range(dialog.provider_tabs.count())]
        self.assertIn("Routing", labels)
        dialog.planner_combo.setCurrentIndex(dialog.planner_combo.findData("gemini-default"))
        dialog.executor_combo.setCurrentIndex(dialog.executor_combo.findData("groq-default"))
        dialog.routing_fallback_checkbox.setChecked(True)
        dialog.save_routing()
        settings = settings_store.load()
        self.assertEqual(settings.routing.planner_profile_id, "gemini-default")
        self.assertEqual(settings.routing.routine_profile_id, "groq-default")
        self.assertEqual(settings.routing.creative_profile_id, "groq-default")
        self.assertEqual(settings.routing.routine_fallback_profile_ids, ("gemini-default",))
        self.assertEqual(settings.routing.planner_fallback_profile_ids, ("groq-default",))
        self.assertEqual({profile.profile_id for profile in settings.profiles}, {"groq-default", "gemini-default"})
        self.assertIn("planning = Gemini", dialog.routing_status_label.text())
        reopened, _, _ = self.make_ai_dialog()
        self.assertEqual(reopened.planner_combo.currentData(), "gemini-default")
        self.assertTrue(reopened.routing_fallback_checkbox.isChecked())

    def test_ai_provider_dialog_secret_save_preserve_remove_and_settings(self):
        dialog, settings_store, credential_store = self.make_ai_dialog()
        self.assertEqual(dialog.key_edit.echoMode(), self.manager_gui.QLineEdit.Password)
        self.assertEqual(dialog.key_edit.text(), "")
        self.assertEqual(dialog.model_combo.currentData(), "openai/gpt-oss-120b")

        dialog.key_edit.setText("  SECRET_KEY\n")
        dialog.reasoning_combo.setCurrentText("high")
        dialog.save_settings()
        self.assertEqual(dialog.key_edit.text(), "")
        self.assertEqual(credential_store.read_secret("groq", "groq-default"), "SECRET_KEY")
        settings_text = settings_store.path.read_text(encoding="utf-8")
        self.assertIn("openai/gpt-oss-120b", settings_text)
        self.assertIn('"reasoning_effort": "high"', settings_text)
        self.assertNotIn("SECRET_KEY", settings_text)

        reopened, _, same_store = self.make_ai_dialog()
        self.assertEqual(reopened.key_edit.text(), "")
        self.assertEqual(reopened.status_label.text(), "Configured — key saved locally")
        self.assertEqual(reopened.key_edit.placeholderText(), "Key saved locally — leave blank to keep it")
        self.assertIn("Configured", reopened.status_label.text())
        self.assertIn("key saved locally", reopened.status_label.text())
        self.assertIn("leave blank to keep it", reopened.key_edit.placeholderText())
        reopened.save_settings()
        self.assertEqual(same_store.read_secret("groq", "groq-default"), "SECRET_KEY")
        self.assertEqual(reopened.key_edit.text(), "")
        self.assertNotIn("SECRET_KEY", reopened.status_label.text())
        self.assertNotIn("SECRET_KEY", reopened.key_edit.placeholderText())

        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok):
            reopened.remove_key()
        self.assertFalse(same_store.exists("groq", "groq-default"))
        self.assertEqual(reopened.key_edit.placeholderText(), "Paste Groq API key")

    def test_ai_provider_save_preserves_other_profiles_and_existing_routing(self):
        dialog, settings_store, credential_store = self.make_ai_dialog()
        ai_platform = self.manager_gui.ai_platform
        existing = ai_platform.AISettings(
            profiles=(
                ai_platform.AIProfile("gemini-default", "gemini", "gemini-model"),
                ai_platform.AIProfile("openrouter-test", "openrouter", "openrouter-model"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="gemini-default",
                planner_profile_id="openrouter-test",
                creative_profile_id="gemini-default",
                routine_fallback_profile_ids=("openrouter-test",),
                planner_fallback_profile_ids=("gemini-default",),
                creative_fallback_profile_ids=("openrouter-test", "gemini-default"),
            ),
        )
        settings_store.save(existing)

        dialog.close()
        dialog = self.manager_gui.AIProviderSettingsDialog(
            settings_store=settings_store,
            credential_store=credential_store,
            provider_factory=self.fake_groq_provider_factory(),
        )
        self.addCleanup(dialog.close)
        dialog.reasoning_combo.setCurrentText("low")
        dialog.save_settings()

        loaded = settings_store.load()
        profile_ids = {profile.profile_id for profile in loaded.profiles}
        self.assertEqual(profile_ids, {"gemini-default", "openrouter-test", "groq-default"})
        self.assertEqual(loaded.routing.routine_profile_id, "gemini-default")
        self.assertEqual(loaded.routing.planner_profile_id, "openrouter-test")
        self.assertEqual(loaded.routing.creative_profile_id, "gemini-default")
        self.assertEqual(loaded.routing.routine_fallback_profile_ids, ("openrouter-test",))
        self.assertEqual(loaded.routing.planner_fallback_profile_ids, ("gemini-default",))
        self.assertEqual(loaded.routing.creative_fallback_profile_ids, ("openrouter-test", "gemini-default"))

    def test_gemini_secret_save_preserve_remove_and_settings(self):
        dialog, settings_store, credential_store = self.make_ai_dialog()
        self.assertEqual(dialog.gemini_key_edit.echoMode(), self.manager_gui.QLineEdit.Password)
        self.assertEqual(dialog.gemini_key_edit.text(), "")
        self.assertEqual(dialog.gemini_model_combo.currentData(), "gemini-3.8-flash")

        dialog.gemini_key_edit.setText("  GEMINI_SECRET\n")
        dialog.gemini_reasoning_combo.setCurrentText("high")
        dialog.save_gemini_settings()
        self.assertEqual(dialog.gemini_key_edit.text(), "")
        self.assertEqual(credential_store.read_secret("gemini", "gemini-default"), "GEMINI_SECRET")
        settings_text = settings_store.path.read_text(encoding="utf-8")
        self.assertIn("gemini-default", settings_text)
        self.assertIn("gemini-3.8-flash", settings_text)
        self.assertIn('"reasoning_effort": "high"', settings_text)
        self.assertNotIn("GEMINI_SECRET", settings_text)

        reopened, _, same_store = self.make_ai_dialog()
        self.assertEqual(reopened.gemini_key_edit.text(), "")
        self.assertEqual(reopened.gemini_status_label.text(), "Configured — key saved locally")
        self.assertEqual(reopened.gemini_key_edit.placeholderText(), "Key saved locally — leave blank to keep it")
        self.assertIn("Configured", reopened.gemini_status_label.text())
        self.assertIn("leave blank to keep it", reopened.gemini_key_edit.placeholderText())
        reopened.save_gemini_settings()
        self.assertEqual(same_store.read_secret("gemini", "gemini-default"), "GEMINI_SECRET")
        self.assertNotIn("GEMINI_SECRET", reopened.gemini_status_label.text())
        self.assertNotIn("GEMINI_SECRET", reopened.gemini_key_edit.placeholderText())

        reopened.gemini_show_key_checkbox.setChecked(True)
        self.assertEqual(reopened.gemini_key_edit.echoMode(), self.manager_gui.QLineEdit.Normal)
        reopened.gemini_show_key_checkbox.setChecked(False)
        self.assertEqual(reopened.gemini_key_edit.echoMode(), self.manager_gui.QLineEdit.Password)

        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok):
            reopened.remove_gemini_key()
        self.assertFalse(same_store.exists("gemini", "gemini-default"))
        self.assertEqual(reopened.gemini_key_edit.placeholderText(), "Paste Gemini API key")

    def test_gemini_only_save_routes_unset_task_classes_to_gemini(self):
        # Regression: saving only Gemini left routing empty ("No usable AI profile").
        dialog, settings_store, _ = self.make_ai_dialog()
        dialog.gemini_key_edit.setText("GEMINI_SECRET")
        dialog.save_gemini_settings()

        routing = settings_store.load().routing
        self.assertEqual(routing.routine_profile_id, "gemini-default")
        self.assertEqual(routing.planner_profile_id, "gemini-default")
        self.assertEqual(routing.creative_profile_id, "gemini-default")

        # A later Groq save fills nothing: every slot is already assigned.
        reopened, same_store, _ = self.make_ai_dialog()
        reopened.key_edit.setText("GROQ_SECRET")
        reopened.save_settings()
        self.assertEqual(same_store.load().routing.planner_profile_id, "gemini-default")

    def test_gemini_save_preserves_groq_profiles_and_existing_routing(self):
        dialog, settings_store, credential_store = self.make_ai_dialog()
        ai_platform = self.manager_gui.ai_platform
        existing = ai_platform.AISettings(
            profiles=(
                ai_platform.AIProfile("groq-default", "groq", "openai/gpt-oss-120b", "groq-default"),
                ai_platform.AIProfile("openrouter-test", "openrouter", "openrouter-model"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="groq-default",
                planner_profile_id="openrouter-test",
                creative_profile_id="groq-default",
                routine_fallback_profile_ids=("openrouter-test",),
                planner_fallback_profile_ids=("groq-default",),
                creative_fallback_profile_ids=("openrouter-test", "groq-default"),
            ),
        )
        settings_store.save(existing)

        dialog.close()
        dialog = self.manager_gui.AIProviderSettingsDialog(
            settings_store=settings_store,
            credential_store=credential_store,
            provider_factory=self.fake_groq_provider_factory(),
            gemini_provider_factory=self.fake_gemini_provider_factory(),
        )
        self.addCleanup(dialog.close)
        dialog.gemini_reasoning_combo.setCurrentText("low")
        dialog.save_gemini_settings()

        loaded = settings_store.load()
        profile_ids = {profile.profile_id for profile in loaded.profiles}
        self.assertEqual(profile_ids, {"groq-default", "openrouter-test", "gemini-default"})
        self.assertEqual(loaded.routing.routine_profile_id, "groq-default")
        self.assertEqual(loaded.routing.planner_profile_id, "openrouter-test")
        self.assertEqual(loaded.routing.creative_profile_id, "groq-default")
        self.assertEqual(loaded.routing.routine_fallback_profile_ids, ("openrouter-test",))
        self.assertEqual(loaded.routing.planner_fallback_profile_ids, ("groq-default",))
        self.assertEqual(loaded.routing.creative_fallback_profile_ids, ("openrouter-test", "groq-default"))
        self.assertEqual(_profile_by_id_for_test(loaded, "gemini-default").options["reasoning_effort"], "low")

    def test_ai_provider_malformed_settings_not_overwritten_by_save(self):
        dialog, settings_store, _credential_store = self.make_ai_dialog()
        dialog.close()
        settings_store.path.parent.mkdir(parents=True, exist_ok=True)
        settings_store.path.write_bytes(b"{bad json")

        dialog = self.manager_gui.AIProviderSettingsDialog(
            settings_store=settings_store,
            credential_store=self.manager_gui.ai_platform.CredentialStore(Path(self.temp_dir.name) / "secrets2" / "ai"),
            provider_factory=self.fake_groq_provider_factory(),
        )
        self.addCleanup(dialog.close)
        self.assertIn("AI settings are invalid", dialog.status_label.text())
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.key_edit.setText("SECRET_KEY")
        dialog.save_settings()
        self.assertEqual(settings_store.path.read_bytes(), b"{bad json")
        self.assertIn("AI settings are invalid", dialog.status_label.text())

    def test_ai_provider_dialog_provider_unavailable_is_contained(self):
        dialog, _, _ = self.make_ai_dialog(provider_factory=lambda _store: (_ for _ in ()).throw(RuntimeError("boom SECRET")))
        self.assertIn("Provider unavailable", dialog.status_label.text())
        self.assertFalse(dialog.test_button.isEnabled())

    def test_gemini_provider_unavailable_is_contained(self):
        dialog, _, _ = self.make_ai_dialog(
            gemini_provider_factory=lambda _store: (_ for _ in ()).throw(RuntimeError("boom SECRET"))
        )
        self.assertIn("Provider unavailable", dialog.gemini_status_label.text())
        self.assertFalse(dialog.gemini_test_button.isEnabled())

    def test_ai_provider_test_connection_uses_worker_and_sanitized_status(self):
        dialog, _, credential_store = self.make_ai_dialog()
        credential_store.write_secret("groq", "groq-default", "SECRET_KEY")
        dialog.test_connection()
        self.assertFalse(dialog.test_button.isEnabled())
        self.assertEqual(dialog.status_label.text(), "Testing Groq...")
        self.wait_until(lambda: dialog.status_label.text() == "Connected")
        self.assertTrue(dialog.test_button.isEnabled())
        self.assertNotIn("SECRET_KEY", dialog.status_label.text())

    def test_gemini_test_connection_uses_worker_and_sanitized_status(self):
        dialog, _, credential_store = self.make_ai_dialog()
        credential_store.write_secret("gemini", "gemini-default", "GEMINI_SECRET")
        dialog.test_gemini_connection()
        self.assertFalse(dialog.gemini_test_button.isEnabled())
        self.assertEqual(dialog.gemini_status_label.text(), "Testing Gemini...")
        self.wait_until(lambda: dialog.gemini_status_label.text() == "Connected")
        self.assertTrue(dialog.gemini_test_button.isEnabled())
        self.assertNotIn("GEMINI_SECRET", dialog.gemini_status_label.text())

    def test_ai_provider_status_mapping_is_sanitized(self):
        ai_platform = self.manager_gui.ai_platform
        cases = [
            (ai_platform.AvailabilityState.CREDENTIAL_INVALID, "Invalid API key"),
            (ai_platform.AvailabilityState.ACCESS_FORBIDDEN, "Access forbidden"),
            (ai_platform.AvailabilityState.CREDENTIAL_MISSING, "No key saved"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Rate limit / quota reached", "quota"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Unexpected provider response", "unexpected"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Network unavailable", "plain unavailable"),
        ]
        for case in cases:
            state = case[0]
            expected = case[1]
            message = case[2] if len(case) > 2 else "SECRET must not show"
            with self.subTest(expected=expected):
                dialog, _, _ = self.make_ai_dialog()
                dialog._finish_test_connection(
                    self.manager_gui.ActionResult(True, "OK", ai_platform.Availability(state, message))
                )
                self.assertEqual(dialog.status_label.text(), expected)
                self.assertNotIn("SECRET", dialog.status_label.text())

    def test_gemini_provider_status_mapping_is_sanitized(self):
        ai_platform = self.manager_gui.ai_platform
        cases = [
            (ai_platform.AvailabilityState.CREDENTIAL_INVALID, "Invalid API key"),
            (ai_platform.AvailabilityState.ACCESS_FORBIDDEN, "Access forbidden"),
            (ai_platform.AvailabilityState.CREDENTIAL_MISSING, "No key saved"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Rate limit / quota reached", "quota"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Unexpected provider response", "unexpected"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "Network unavailable", "plain unavailable"),
        ]
        for case in cases:
            state = case[0]
            expected = case[1]
            message = case[2] if len(case) > 2 else "SECRET must not show"
            with self.subTest(expected=expected):
                dialog, _, _ = self.make_ai_dialog()
                dialog._finish_gemini_test_connection(
                    self.manager_gui.ActionResult(True, "OK", ai_platform.Availability(state, message))
                )
                self.assertEqual(dialog.gemini_status_label.text(), expected)
                self.assertNotIn("SECRET", dialog.gemini_status_label.text())

    def test_ai_provider_close_rejected_while_test_in_progress(self):
        dialog, _, _ = self.make_ai_dialog()

        class FakeEvent:
            def __init__(self):
                self.accepted = False
                self.ignored = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        dialog._set_testing_controls(True)
        event = FakeEvent()
        dialog.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertFalse(event.accepted)
        self.assertIn("still in progress", dialog.status_label.text())
        self.assertTrue(dialog.isVisible() or dialog.result() == 0)

        dialog.reject()
        self.assertEqual(dialog.result(), 0)
        self.assertIn("still in progress", dialog.status_label.text())

        dialog.done(self.manager_gui.QDialog.Rejected)
        self.assertEqual(dialog.result(), 0)
        self.assertIn("still in progress", dialog.status_label.text())

        dialog._set_testing_controls(False)
        event = FakeEvent()
        dialog.closeEvent(event)
        self.assertTrue(event.accepted)
        dialog.reject()
        self.assertEqual(dialog.result(), self.manager_gui.QDialog.Rejected)

    def test_gemini_close_rejected_while_test_in_progress(self):
        dialog, _, _ = self.make_ai_dialog()

        class FakeEvent:
            def __init__(self):
                self.accepted = False
                self.ignored = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        dialog._set_gemini_testing_controls(True)
        event = FakeEvent()
        dialog.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertFalse(event.accepted)
        self.assertIn("still in progress", dialog.gemini_status_label.text())
        dialog.reject()
        self.assertEqual(dialog.result(), 0)
        dialog._set_gemini_testing_controls(False)
        event = FakeEvent()
        dialog.closeEvent(event)
        self.assertTrue(event.accepted)


class PerInstanceManagerTests(unittest.TestCase):
    """Manager: AI settings per bot instance, bot types, Game Presence bots."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()
        self.data_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def gp_info(self, manager, instance_id="gp-main", state=None):
        core = self.manager_gui.manager_core
        return replace(
            manager.info(instance_id, "Games", state or core.STATE_STOPPED, None),
            bot_type="game_presence",
            bot_type_display_name="Game Presence Bot",
        )

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

    def select_ai_bot(self, window, instance_id):
        index = window.ai_bot_combo.findData(instance_id)
        self.assertGreaterEqual(index, 0)
        window.ai_bot_combo.setCurrentIndex(index)

    def test_ai_page_lists_every_bot_and_dialog_gets_that_bots_stores(self):
        manager = FakeManager(self.manager_gui.manager_core)
        manager.infos = [*manager.infos, self.gp_info(manager)]
        window = self.make_window(manager=manager)
        ids = [window.ai_bot_combo.itemData(i) for i in range(window.ai_bot_combo.count())]
        self.assertEqual(ids, ["admin-main", "admin-second", "gp-main"])
        self.assertEqual(window.ai_bot_combo.currentData(), "admin-main")

        self.select_ai_bot(window, "gp-main")
        self.assertIn("Games (gp-main)", window.dashboard_ai_bot_label.text())
        self.assertIn("wording", window.ai_bot_hint.text())
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        with mock.patch.object(self.manager_gui, "AIProviderSettingsDialog", return_value=fake_dialog) as dialog_class:
            window.open_ai_providers()
        kwargs = dialog_class.call_args.kwargs
        instance_root = (self.data_root / "instances" / "gp-main").resolve()
        self.assertEqual(kwargs["settings_store"].path, instance_root / "data" / "ai.json")
        self.assertEqual(kwargs["credential_store"].root, instance_root / "secrets" / "ai")
        self.assertIn("gp-main", kwargs["bot_label"])

    def test_connection_test_uses_the_selected_bots_key(self):
        window = self.make_window()
        second = window.ai_stores_for("admin-second")
        second.credentials.write_secret("groq", "groq-default", "test-key-second-bot")
        started = []
        window._start_worker = lambda action, finished: started.append(action)

        window.test_provider("groq")  # admin-main is selected and has no key
        self.assertEqual(started, [])
        self.assertIn("no API key saved for this bot", window.last_error)
        self.assertEqual(window.provider_rows[0].status_label.text(), "Not configured")

        self.select_ai_bot(window, "admin-second")
        self.assertEqual(window.provider_rows[0].status_label.text(), "Configured")
        used = []

        class FakeProvider:
            async def test_connection(self, credential_ref):
                return self.availability

        def factory(credential_store):
            used.append(credential_store.root)
            provider = FakeProvider()
            provider.availability = window_ai.Availability(window_ai.AvailabilityState.AVAILABLE, "ok")
            return provider

        window_ai = self.manager_gui.ai_platform
        with mock.patch.object(self.manager_gui, "create_groq_provider", factory):
            window.test_provider("groq")
            self.assertEqual(len(started), 1)
            started[0]()
        self.assertEqual(used, [second.credentials.root])
        self.assertNotEqual(used[0], window.ai_stores_for("admin-main").credentials.root)

    def test_ai_dialog_has_no_global_default_store(self):
        with self.assertRaises(ValueError):
            self.manager_gui.AIProviderSettingsDialog()

    def test_dashboard_shows_capabilities_per_bot_type(self):
        manager = FakeManager(self.manager_gui.manager_core)
        manager.infos = [*manager.infos, self.gp_info(manager)]
        window = self.make_window(manager=manager)
        self.select_instance_row(window, "admin-main")
        text = window.bot_facts_label.text()
        self.assertIn("Can do: Slash commands", text)
        self.assertNotIn("Game Presence", text)
        self.assertNotIn("Modules:", text)

        self.select_instance_row(window, "gp-main")
        text = window.bot_facts_label.text()
        self.assertIn("Can do: Game suggestions", text)
        self.assertNotIn("slash", text)

    def select_instance_row(self, window, instance_id):
        for row_index in range(window.instance_table.rowCount()):
            if window.instance_table.item(row_index, 0).text() == instance_id:
                window.instance_table.selectRow(row_index)
                window._refresh_dashboard()
                return
        self.fail(f"Missing row for {instance_id}")

    def test_ai_terminal_lists_only_admin_bots_and_presence_page_only_presence_bots(self):
        manager = FakeManager(self.manager_gui.manager_core)
        manager.infos = [*manager.infos, self.gp_info(manager)]
        window = self.make_window(manager=manager)
        self.assertEqual(sorted(bot[0] for bot in window._terminal_bots()), ["admin-main", "admin-second"])
        window.show_page("presence")
        combo = window.presence_panel.bot_combo
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], ["gp-main"])

    def test_add_bot_offers_registered_bot_types(self):
        dialog = self.manager_gui.CreateBotInstanceDialog()
        self.addCleanup(dialog.close)
        types_ = [dialog.type_combo.itemData(i) for i in range(dialog.type_combo.count())]
        self.assertEqual(types_[0], "admin")
        self.assertIn("game_presence", types_)
        dialog.type_combo.setCurrentIndex(types_.index("game_presence"))
        self.assertIn("OWN Discord application", dialog.type_hint.text())
        dialog.instance_id_edit.setText("gp-main")
        self.assertEqual(dialog.values(), ("game_presence", "gp-main", None))

    def test_create_game_presence_bot_through_the_standard_flow(self):
        instance_api = FakeInstanceApi()
        window = self.make_window(instance_api=instance_api)
        fake_dialog = mock.Mock()
        fake_dialog.exec.return_value = self.manager_gui.QDialog.Accepted
        fake_dialog.values.return_value = ("game_presence", "gp-main", "Games")
        fake_setup = mock.Mock()
        fake_setup.exec.return_value = self.manager_gui.QDialog.Rejected
        with mock.patch.object(self.manager_gui, "CreateBotInstanceDialog", return_value=fake_dialog), mock.patch.object(
            self.manager_gui, "BotSetupDialog", return_value=fake_setup
        ):
            window.create_bot_instance()
        self.assertEqual(instance_api.created, [("game_presence", "gp-main", "Games")])

    def test_game_presence_setup_wizard_saves_only_name_and_token(self):
        instance_api = FakeInstanceApi(self.temp_dir.name)
        original_load = instance_api.load_instance

        def load_instance(instance_id):
            return replace_namespace(original_load(instance_id), bot_type="game_presence")

        instance_api.load_instance = load_instance
        config_api = FakeConfigApi()
        dialog = self.manager_gui.BotSetupDialog("gp-main", instance_api, config_api)
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.is_game_presence)
        self.assertEqual(dialog.pages.count(), 5)
        self.assertIn("Presence Intent", dialog.intent_ack_checkbox.text())

        dialog.application_id_edit.setText("1234567890")
        dialog.pages.setCurrentWidget(dialog.invite_page)
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(dialog.invite_link_edit.text()).query)
        self.assertEqual(params["scope"], ["bot"])
        # View Channels + Send Messages only: no moderation, no slash commands.
        self.assertEqual(int(params["permissions"][0]), (1 << 10) | (1 << 11))

        dialog.display_name_edit.setText("Game Pings")
        dialog.token_edit.setText("FAKE_GP_TOKEN")
        self.assertTrue(dialog.save_setup())
        self.assertEqual(instance_api.token_path.read_text(encoding="utf-8"), "FAKE_GP_TOKEN\n")
        self.assertEqual(instance_api.display_names["gp-main"], "Game Pings")
        self.assertEqual(config_api.saved, [])  # Game Presence settings live on their own page


def replace_namespace(namespace, **changes):
    return SimpleNamespace(**{**vars(namespace), **changes})


if __name__ == "__main__":
    unittest.main()
