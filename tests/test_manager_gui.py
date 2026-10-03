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
        "ai_connections",
        "ai_providers",
        "ai_storage",
        "ai_groq",
        "ai_gemini",
        "app_updates",
        "github_updates",
        "update_engine",
        "manager_stream_director",
        "stream_director",
        "stream_director_config",
        "stream_director_store",
        "stream_director_twitch",
        "manager_setup_state",
        "manager_kairo",
        "social_awareness",
        "social_memory",
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

    def test_first_kairo_start_asks_about_social_awareness_once(self):
        manager = FakeManager(self.manager_gui.manager_core)
        config_api = FakeConfigApi()
        config_api.snapshot.defaults["social_awareness_enabled"] = None
        config_api.snapshot.effective["social_awareness_enabled"] = None
        window = self.make_window(manager=manager, config_api=config_api)
        self.finish_workers_immediately(window)
        self.select_instance(window, "admin-second")
        answers = [None, True]
        window.ask_social_awareness = lambda parent: answers.pop(0)
        window.start_selected()  # cancelled in the question
        self.assertNotIn(("start", "admin-second"), manager.calls)
        self.assertEqual(config_api.saved, [])
        window.start_selected()
        self.assertIn(("start", "admin-second"), manager.calls)
        self.assertIs(config_api.saved[-1][1]["social_awareness_enabled"], True)
        window.ask_social_awareness = lambda parent: self.fail("asked twice")
        window.start_selected()
        self.assertIn("Social Awareness: on", window.details_view.toPlainText())

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
        self.assertIn("Add Connection", window.add_connection_button.text())

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
                    "language": "en",  # the bot's language is saved explicitly
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
        # Only the bot's language is written here (Game Presence keeps Russian by
        # default); the other Game Presence settings live on their own page.
        self.assertEqual(config_api.saved, [("gp-main", {"allowed_user_ids": ["123"], "language": "ru"})])


class ConnectionsUITests(unittest.TestCase):
    """Manager: shared provider connections, the base set and each bot's AI choice."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()
        self.data_root = Path(self.temp_dir.name)
        self.ai_connections = self.manager_gui.ai_connections
        self.ai_platform = self.manager_gui.ai_platform
        self.store = self.ai_connections.ConnectionStore()

    def tearDown(self):
        self.temp_dir.cleanup()

    # -- helpers ----------------------------------------------------------------------

    def manager_with_presence(self):
        manager = FakeManager(self.manager_gui.manager_core)
        core = self.manager_gui.manager_core
        gp = replace(
            manager.info("gp-main", "Game Pings", core.STATE_STOPPED, None),
            bot_type="game_presence",
            bot_type_display_name="Game Presence Bot",
        )
        manager.infos = [*manager.infos, gp]
        return manager

    def make_window(self, manager=None):
        window = self.manager_gui.ManagerMainWindow(
            manager=manager or self.manager_with_presence(),
            instance_api=FakeInstanceApi(),
            config_api=FakeConfigApi(),
            auto_refresh=False,
        )

        def close_without_prompt():
            window._allow_close = True
            window.close()

        self.addCleanup(close_without_prompt)
        return window

    def add(self, connection_id, provider_id="groq", model="openai/gpt-oss-120b", key=None, name=None):
        connection = self.ai_connections.Connection(connection_id, provider_id, name or connection_id, model, {"reasoning_effort": "medium"})
        self.store.upsert(connection, key)
        return connection

    def standard_setup(self):
        self.add("groq-main", key="test-key-a", name="Groq main")
        self.add("gemini-main", "gemini", "gemini-3.8-flash", key="test-key-b", name="Gemini main")
        self.add("groq-2", key="test-key-c", name="Groq backup")
        self.store.set_base(self.ai_connections.RouteSelection("groq-main", "gemini-main", None, True))

    def select_ai_bot(self, window, instance_id):
        index = window.ai_bot_combo.findData(instance_id)
        self.assertGreaterEqual(index, 0)
        window.ai_bot_combo.setCurrentIndex(index)

    def fake_factory(self, state=None, calls=None):
        ai_platform = self.ai_platform

        class FakeProvider:
            def __init__(self, provider_id):
                self.metadata = ai_platform.ProviderMetadata(
                    provider_id=provider_id,
                    display_name=provider_id,
                    models=(
                        ai_platform.ProviderModel("model-a", "Model A"),
                        ai_platform.ProviderModel("model-b", "Model B"),
                    ),
                )

            async def test_connection(self, credential_ref):
                if calls is not None:
                    calls.append(credential_ref)
                return ai_platform.Availability(state or ai_platform.AvailabilityState.AVAILABLE, "SECRET provider text must not show")

        def factory(provider_id, credential_store, usage_recorder=None):
            if calls is not None:
                calls.append(("created", provider_id, credential_store.root, usage_recorder is not None))
            return FakeProvider(provider_id)

        return factory

    def dialog(self, connection=None, **kwargs):
        kwargs.setdefault("provider_factory", self.fake_factory())
        dialog = self.manager_gui.ConnectionDialog(self.store, connection, **kwargs)
        self.addCleanup(dialog.close)
        return dialog

    # -- connection dialog -----------------------------------------------------------------

    def test_new_connection_saves_key_without_showing_it_back(self):
        dialog = self.dialog()
        self.assertEqual([dialog.provider_combo.itemData(i) for i in range(dialog.provider_combo.count())], ["groq", "gemini"])
        self.assertEqual(dialog.name_edit.text(), "Groq main")  # suggested name
        self.assertEqual([dialog.model_combo.itemData(i) for i in range(dialog.model_combo.count())], ["model-a", "model-b"])
        self.assertEqual(dialog.status_label.text(), "No key saved yet")
        self.assertFalse(dialog.test_button.isEnabled())
        dialog.key_edit.setText("  SECRET_TEST_KEY  ")
        dialog.model_combo.setCurrentIndex(1)
        dialog.reasoning_combo.setCurrentText("high")
        self.assertTrue(dialog.save())
        saved = self.store.load().get(dialog.connection.connection_id)
        self.assertEqual((saved.connection_id, saved.provider_id, saved.model_id, saved.options), ("groq-1", "groq", "model-b", {"reasoning_effort": "high"}))
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-1"), "SECRET_TEST_KEY")
        self.assertEqual(dialog.key_edit.text(), "")
        self.assertIn("leave blank to keep it", dialog.key_edit.placeholderText())
        self.assertIn("Configured", dialog.status_label.text())
        self.assertNotIn("SECRET_TEST_KEY", dialog.status_label.text() + dialog.key_edit.placeholderText())
        self.assertFalse(dialog.provider_combo.isEnabled())  # the key belongs to that provider
        self.assertTrue(dialog.test_button.isEnabled())
        self.assertNotIn("SECRET_TEST_KEY", self.store.path.read_text(encoding="utf-8"))

        dialog.name_edit.setText("Groq renamed")
        self.assertTrue(dialog.save())  # blank key keeps the saved one
        self.assertEqual(self.store.credentials.read_secret("groq", "groq-1"), "SECRET_TEST_KEY")
        self.assertEqual(self.store.load().get("groq-1").name, "Groq renamed")

    def test_edit_loads_the_connection_and_invalid_input_is_not_saved(self):
        connection = self.add("gemini-main", "gemini", "model-b", key="test-key", name="Gemini main")
        dialog = self.dialog(connection)
        self.assertEqual(dialog.provider_combo.currentData(), "gemini")
        self.assertEqual(dialog.model_combo.currentData(), "model-b")
        self.assertEqual(dialog.name_edit.text(), "Gemini main")
        dialog.name_edit.setText("   ")
        self.assertFalse(dialog.save())
        self.assertIn("Not saved", dialog.status_label.text())
        self.assertEqual(self.store.load().get("gemini-main").name, "Gemini main")

    def test_test_connection_uses_this_connection_and_shows_sanitized_text(self):
        calls = []
        connection = self.add("groq-2", key="test-key")
        usage = self.manager_gui.ai_usage.AIUsageStore(self.data_root / "config" / "ai_usage_manager.json")
        dialog = self.dialog(connection, provider_factory=self.fake_factory(calls=calls), usage_store=usage)
        started = []
        dialog._start_worker = lambda action, finished: started.append((action, finished))
        dialog.test_connection()
        self.assertFalse(dialog.close_button.isEnabled())
        action, finished = started[0]
        finished(self.manager_gui.ActionResult(True, "OK", action()))
        self.assertEqual(dialog.status_label.text(), "Connected")
        self.assertIn("groq-2", calls)  # this connection's credential reference
        self.assertIn(("created", "groq", self.store.credentials.root, True), calls)  # counted as Manager usage
        ai_platform = self.ai_platform
        for state, message, expected in (
            (ai_platform.AvailabilityState.CREDENTIAL_INVALID, "SECRET", "Invalid API key"),
            (ai_platform.AvailabilityState.ACCESS_FORBIDDEN, "SECRET", "Access forbidden"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "rate limit SECRET", "Rate limit / quota reached"),
            (ai_platform.AvailabilityState.UNAVAILABLE, "SECRET", "Network unavailable"),
        ):
            result = self.manager_gui.ActionResult(True, "OK", ai_platform.Availability(state, message))
            self.assertEqual(self.manager_gui.availability_text(result), expected)
        self.assertEqual(self.manager_gui.availability_text(self.manager_gui.ActionResult(False, "boom SECRET", None)), "Network unavailable")

    def test_close_is_refused_while_a_test_runs(self):
        dialog = self.dialog(self.add("groq-2", key="test-key"))
        dialog._set_testing(True)
        event = mock.Mock()
        dialog.closeEvent(event)
        event.ignore.assert_called_once()
        self.assertIn("still in progress", dialog.status_label.text())
        dialog._set_testing(False)
        event = mock.Mock()
        dialog.closeEvent(event)
        event.accept.assert_called_once()

    def test_remove_is_refused_while_used_and_deletes_the_key_otherwise(self):
        used = self.add("groq-main", key="test-key-a")
        dialog = self.dialog(used, users_of=lambda _cid: ["Base set (Kairo)"])
        self.assertFalse(dialog.remove())
        self.assertIn("Used by: Base set (Kairo)", dialog.status_label.text())
        self.assertTrue(self.store.credentials.exists("groq", "groq-main"))

        spare = self.add("groq-2", key="test-key-c")
        dialog = self.dialog(spare)
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok):
            self.assertTrue(dialog.remove())
        self.assertIsNone(self.store.load().get("groq-2"))
        self.assertFalse(self.store.credentials.exists("groq", "groq-2"))
        self.assertTrue(self.store.credentials.exists("groq", "groq-main"))

    def test_unavailable_provider_is_contained(self):
        def broken(provider_id, credential_store, usage_recorder=None):
            raise self.ai_platform.AIPlatformError("adapter missing")

        dialog = self.dialog(provider_factory=broken)
        self.assertEqual(dialog.model_combo.count(), 0)
        self.assertEqual(dialog.status_label.text(), "Provider unavailable")

    # -- AI page -------------------------------------------------------------------------------

    def test_page_lists_connections_with_users_and_total_usage(self):
        self.standard_setup()
        ai_usage = self.manager_gui.ai_usage
        window = self.make_window()
        window.ai_stores_for("gp-main").selection.save(
            self.ai_connections.BotSelection("custom", self.ai_connections.RouteSelection(None, "groq-2"))
        )
        window.ai_stores_for("admin-main").usage.record(ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", total_tokens=500, input_tokens=400, output_tokens=100, connection_id="groq-main"))
        window.ai_stores_for("admin-second").usage.record(ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", total_tokens=500, input_tokens=400, output_tokens=100, connection_id="groq-main"))
        window.manager_usage.record(ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", success=False, connection_id="groq-main"))
        window.refresh_instances()
        self.assertEqual(list(window.connection_rows), ["groq-main", "gemini-main", "groq-2"])
        row = window.connection_rows["groq-main"]
        self.assertEqual(row.status_label.text(), "Configured")
        self.assertIn("GPT-OSS 120B · Groq", row.model_label.text())
        self.assertEqual(row.role_label.text(), "Used by: Base set (Main, Second)")
        # Total usage of the key: both bots + the Manager's (failed) test.
        self.assertEqual(row.usage_label.text(), "GPT-OSS 120B · 3 req (1 failed) · 1k tok\n800 in / 200 out")
        self.assertEqual(window.connection_rows["groq-2"].role_label.text(), "Used by: Game Pings")
        self.assertEqual(window.connection_rows["groq-2"].usage_label.text(), "No usage yet")
        self.assertIn("Bots on the base set: Main, Second", window.base_status_label.text())

    def test_base_set_editor_saves_and_bots_on_it_follow(self):
        self.standard_setup()
        window = self.make_window()
        self.assertEqual(window.base_planner_combo.currentData(), "groq-main")
        self.assertEqual(window.base_executor_combo.currentData(), "gemini-main")
        self.assertTrue(window.base_cross_checkbox.isChecked())
        window.base_planner_combo.setCurrentIndex(window.base_planner_combo.findData(None))
        window.base_executor_combo.setCurrentIndex(window.base_executor_combo.findData("groq-2"))
        window.base_fallback_combo.setCurrentIndex(window.base_fallback_combo.findData("gemini-main"))
        window.base_cross_checkbox.setChecked(False)
        window.save_base_set()
        self.assertEqual(self.store.load().base, self.ai_connections.RouteSelection(None, "groq-2", "gemini-main", False))
        profiles = window.ai_stores_for("admin-main").settings.load().profiles
        self.assertEqual([p.profile_id for p in profiles], ["groq-2", "gemini-main"])

    def test_each_bot_can_use_the_base_set_or_its_own_connections(self):
        self.standard_setup()
        window = self.make_window()
        self.select_ai_bot(window, "gp-main")
        self.assertTrue(window.bot_mode_base_radio.isChecked())
        self.assertFalse(window.bot_executor_combo.isEnabled())
        self.assertIn("Execution connection", window.ai_bot_hint.text())
        self.assertIn("In use (base set)", window.ai_routing_label.text())

        window.bot_mode_custom_radio.setChecked(True)
        self.assertTrue(window.bot_executor_combo.isEnabled())
        window.save_bot_ai()
        self.assertIn("Choose at least the execution connection", window.bot_status_label.text())
        window.bot_executor_combo.setCurrentIndex(window.bot_executor_combo.findData("groq-2"))
        window.save_bot_ai()
        gp = window.ai_stores_for("gp-main")
        self.assertEqual(gp.selection.load().mode, "custom")
        self.assertEqual([p.profile_id for p in gp.settings.load().profiles], ["groq-2"])
        self.assertIn("In use (own choice)", window.ai_routing_label.text())
        self.assertIn("Groq backup", window.ai_routing_label.text())
        # The admin bot is untouched and its controls load its own state.
        self.select_ai_bot(window, "admin-main")
        self.assertTrue(window.bot_mode_base_radio.isChecked())
        self.assertEqual(window.ai_stores_for("admin-main").selection.load().mode, "base")
        # Back to the base set: the custom route is kept for later.
        self.select_ai_bot(window, "gp-main")
        self.assertTrue(window.bot_mode_custom_radio.isChecked())
        self.assertEqual(window.bot_executor_combo.currentData(), "groq-2")
        window.bot_mode_base_radio.setChecked(True)
        window.save_bot_ai()
        selection = gp.selection.load()
        self.assertEqual((selection.mode, selection.custom.executor), ("base", "groq-2"))
        self.assertEqual(len(gp.settings.load().profiles), 2)

    def test_periodic_refresh_keeps_unsaved_choices(self):
        self.standard_setup()
        window = self.make_window()
        window.base_executor_combo.setCurrentIndex(window.base_executor_combo.findData("groq-2"))
        self.select_ai_bot(window, "gp-main")
        window.bot_mode_custom_radio.setChecked(True)
        window.bot_executor_combo.setCurrentIndex(window.bot_executor_combo.findData("groq-2"))
        window.refresh_instances()
        window.refresh_instances()
        self.assertEqual(window.base_executor_combo.currentData(), "groq-2")
        self.assertTrue(window.bot_mode_custom_radio.isChecked())
        self.assertEqual(window.bot_executor_combo.currentData(), "groq-2")
        # A change saved elsewhere (another window, the store) is picked up.
        self.store.set_base(self.ai_connections.RouteSelection(None, "groq-main"))
        window.refresh_instances()
        self.assertEqual(window.base_executor_combo.currentData(), "groq-main")

    def test_dashboard_shows_the_selected_bots_connections_and_usage(self):
        self.standard_setup()
        ai_usage = self.manager_gui.ai_usage
        window = self.make_window()
        window.ai_stores_for("admin-main").usage.record(
            ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", input_tokens=900, output_tokens=80, total_tokens=980, connection_id="groq-main")
        )
        # Another bot hit the same key later: its fresher limit is shown (limits belong to the key).
        window.ai_stores_for("admin-second").usage.record(
            ai_usage.UsageEvent("groq", "openai/gpt-oss-120b", total_tokens=10, connection_id="groq-main",
                                rate_limits={"remaining_tokens": 5000, "reset_tokens_seconds": 600.0})
        )
        self.select_ai_bot(window, "admin-main")
        window.refresh_instances()
        self.assertEqual(list(window.dashboard_connection_rows), ["groq-main", "gemini-main"])
        row = window.dashboard_connection_rows["groq-main"]
        self.assertIn("planning", row.role_label.text())
        self.assertIn("fallback", row.role_label.text())
        self.assertEqual(row.usage_label.text(), "GPT-OSS 120B · 1 req · 980 tok\n900 in / 80 out · TPM 5k left")
        self.assertIn("Showing AI of: Main (admin-main)", window.dashboard_ai_bot_label.text())
        self.assertIn("plan: Groq main", window.routing_card.value_label.text())
        self.assertFalse(window.dashboard_no_ai_label.isVisible())

    def test_window_test_connection_is_counted_as_manager_usage(self):
        self.standard_setup()
        ai_groq = importlib.import_module("ai_groq")
        window = self.make_window()
        body = json.dumps(
            {"choices": [{"message": {"content": "KAIRO_GROQ_OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}
        ).encode("utf-8")
        seen = []

        class Transport:
            def post_json(self, **kwargs):
                seen.append(kwargs["headers"]["Authorization"])
                return 200, body, {}

        def factory(provider_id, credential_store, usage_recorder=None):
            return ai_groq.GroqProvider(credential_store, transport=Transport(), usage_recorder=usage_recorder)

        started = []
        window._start_worker = lambda action, finished: started.append((action, finished))
        with mock.patch.object(self.manager_gui, "create_provider", factory):
            window.test_connection("groq-2")
            self.assertEqual(window.connection_rows["groq-2"].status_label.text(), "Testing...")
            action, finished = started[0]
            finished(self.manager_gui.ActionResult(True, "OK", action()))
        self.assertEqual(seen, ["Bearer test-key-c"])  # this connection's key only
        self.assertEqual(window.connection_rows["groq-2"].status_label.text(), "Connected")
        self.assertEqual(window.manager_usage.day_usage("groq", None, "groq-2")[0].requests, 1)
        self.assertEqual(window.connection_rows["groq-2"].usage_label.text(), "GPT-OSS 120B · 1 req · 150 tok\n120 in / 30 out")

    def test_test_requires_a_saved_key(self):
        self.add("groq-nokey")
        window = self.make_window()
        started = []
        window._start_worker = lambda action, finished: started.append(action)
        window.test_connection("groq-nokey")
        self.assertEqual(started, [])
        self.assertIn("no API key saved", window.last_error)
        self.assertEqual(window.connection_rows["groq-nokey"].status_label.text(), "No key saved")
        self.assertFalse(window.connection_rows["groq-nokey"].test_button.isEnabled())

    def test_first_saved_connection_becomes_the_base_set(self):
        window = self.make_window()
        self.assertIn("No connections yet", window.connections_status.text())
        self.assertIn("No connections", window.ai_card.value_label.text())
        connection = self.add("groq-1", key="test-key")
        dialog = SimpleNamespace(changed=True, removed=False, connection=connection)
        window._after_connection_dialog(dialog)
        self.assertEqual(self.store.load().base.executor, "groq-1")
        self.assertEqual([p.profile_id for p in window.ai_stores_for("gp-main").settings.load().profiles], ["groq-1"])

    def test_add_and_edit_open_the_dialog_for_the_right_connection(self):
        self.standard_setup()
        window = self.make_window()
        fake = mock.Mock(changed=False, removed=False, connection=None)
        with mock.patch.object(self.manager_gui, "ConnectionDialog", return_value=fake) as dialog_class:
            window.add_connection()
            window.edit_connection("gemini-main")
        self.assertIsNone(dialog_class.call_args_list[0].args[1])
        self.assertEqual(dialog_class.call_args_list[1].args[1].connection_id, "gemini-main")
        self.assertIs(dialog_class.call_args_list[1].kwargs["usage_store"], window.manager_usage)
        self.assertEqual(fake.exec.call_count, 2)

    def test_unreadable_connections_file_is_reported(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{broken", encoding="utf-8")
        window = self.make_window()
        self.assertIn("unreadable", window.connections_status.text())
        self.assertFalse(window.add_connection_button.isEnabled())
        self.assertFalse(window.base_save_button.isEnabled())
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{broken")

    def test_usage_tooltips_and_shared_connections_are_explicit(self):
        self.standard_setup()
        window = self.make_window()
        self.select_ai_bot(window, "admin-main")
        window.refresh_instances()
        dash = self.manager_gui.dash
        dashboard_row = window.dashboard_connection_rows["groq-main"]
        self.assertEqual(dashboard_row.usage_label.toolTip(), dash.BOT_USAGE_TOOLTIP)
        self.assertIn("only the selected bot's own requests", dashboard_row.usage_label.toolTip())
        self.assertEqual(window.connection_rows["groq-main"].usage_label.toolTip(), dash.CONNECTION_USAGE_TOOLTIP)
        self.assertIn("every bot that uses it plus Manager Test Connection", window.connection_rows["groq-main"].usage_label.toolTip())
        # Kairo's dashboard row says who else spends the same key.
        self.assertIn("also used by Second, Game Pings", dashboard_row.role_label.text())
        self.assertNotIn("Main", dashboard_row.role_label.text().split("also used by")[1])

    def test_no_stale_per_bot_key_wording(self):
        window = self.make_window()
        texts = [label.text() for label in window.findChildren(self.manager_gui.QLabel)]
        texts += [box.text() for box in window.findChildren(self.manager_gui.QCheckBox)]
        texts += [self.manager_gui.BOT_TYPE_HINTS[key] for key in self.manager_gui.BOT_TYPE_HINTS]
        for text in texts:
            for stale in ("own AI keys", "own optional AI keys", "keys are stored per bot", "stored per bot"):
                self.assertNotIn(stale, text)
        self.assertTrue(any("Base Set" in text for text in texts))

    def test_dashboard_explains_what_the_game_presence_bot_is_doing(self):
        core = self.manager_gui.manager_core
        manager = self.manager_with_presence()
        logs = self.data_root / "instances" / "gp-main" / "logs"
        runtime = logs.parent / "runtime"
        runtime.mkdir(parents=True)
        manager.infos = [manager.infos[0], manager.infos[1], replace(manager.infos[2], state=core.STATE_RUNNING, pid=77, logs_dir=logs)]

        class ConfigApi(FakeConfigApi):
            def get_config_snapshot(self, instance_id):
                if instance_id == "gp-main":
                    return FakeSnapshot("gp-main", "game_presence", 1, {}, {}, {})
                return super().get_config_snapshot(instance_id)

        window = self.manager_gui.ManagerMainWindow(manager=manager, instance_api=FakeInstanceApi(), config_api=ConfigApi(), auto_refresh=False)
        self.addCleanup(lambda: (setattr(window, "_allow_close", True), window.close()))
        (runtime / "game_presence_status.json").write_text(
            json.dumps({"diagnosis": {"code": "waiting_delay", "text": "Waiting: 2 players in Overwatch 2, check in 1:30."}}),
            encoding="utf-8",
        )
        window.refresh_instances()
        for row in range(window.instance_table.rowCount()):
            if window.instance_table.item(row, 0).text() == "gp-main":
                window.instance_table.selectRow(row)
        window._refresh_dashboard()
        self.assertIn("Game Presence: Waiting: 2 players in Overwatch 2", window.bot_facts_label.text())

        # Stopped and never configured: said plainly instead of only "Offline".
        manager.infos = [manager.infos[0], manager.infos[1], replace(manager.infos[2], state=core.STATE_STOPPED, pid=None)]
        window.refresh_instances()
        window._refresh_dashboard()
        self.assertIn("Game Presence: Not configured: choose a server and a channel", window.bot_facts_label.text())


class SelfUpdateUITests(unittest.TestCase):
    """Sidebar update button: check, confirm, install, stop bots, restart, resume."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()
        self.app_updates = self.manager_gui.app_updates
        self.installed = self.app_updates.InstalledApp(
            install_root=Path(self.temp_dir.name) / "Programs" / "DarkAbyss",
            version="1.0.0",
            launcher=Path(self.temp_dir.name) / "Programs" / "DarkAbyss" / "Launcher.exe",
        )
        self.update = self.app_updates.AvailableUpdate("1.0.1", "v1.0.1", "DarkAbyss 1.0.1")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_window(self, manager=None, installed=True):
        with mock.patch.object(self.app_updates, "current_install", return_value=self.installed if installed else None):
            window = self.manager_gui.ManagerMainWindow(
                manager=manager or FakeManager(self.manager_gui.manager_core),
                instance_api=FakeInstanceApi(),
                config_api=FakeConfigApi(),
                auto_refresh=False,
            )

        def close_without_prompt():
            window._allow_close = True
            window.close()

        self.addCleanup(close_without_prompt)

        def immediate(action, finished):
            try:
                value = action()
            except Exception as exc:
                finished(self.manager_gui.ActionResult(False, str(exc), exc))
            else:
                finished(self.manager_gui.ActionResult(True, "OK", value))

        window._start_worker = immediate
        return window

    def test_source_build_does_not_offer_updates(self):
        window = self.make_window(installed=False)
        self.assertFalse(window.update_check_button.isEnabled())
        self.assertTrue(window.update_install_button.isHidden())
        self.assertEqual(window.update_label.text(), "Updates: packaged app only")
        with mock.patch.object(self.app_updates, "check_for_update") as check:
            window.check_for_updates(silent=True)
        check.assert_not_called()

    def test_check_shows_update_button_and_activity(self):
        window = self.make_window()
        with mock.patch.object(self.app_updates, "check_for_update", return_value=self.update) as check:
            window.check_for_updates(silent=True)
        check.assert_called_once_with("1.0.0")
        self.assertFalse(window.update_install_button.isHidden())
        self.assertEqual(window.update_install_button.text(), "⬆  Update to v1.0.1")
        self.assertEqual(window.update_label.text(), "v1.0.1 is available.")
        self.assertEqual(window.activity.entries[0][1:], ("Update", "Version 1.0.1 is available: use '⬆ Update' in the sidebar."))

        with mock.patch.object(self.app_updates, "check_for_update", return_value=None):
            window.check_for_updates(silent=False)
        self.assertTrue(window.update_install_button.isHidden())
        self.assertTrue(window.update_label.text().startswith("Up to date · checked "))
        self.assertEqual(window.status_label.text(), "You have the newest version.")

    def test_failed_background_check_is_quiet(self):
        window = self.make_window()
        error = self.app_updates.AppUpdateError("Update check failed: GitHub API request failed with HTTP status 503.")
        with mock.patch.object(self.app_updates, "check_for_update", side_effect=error), mock.patch.object(
            self.manager_gui.QMessageBox, "critical"
        ) as critical:
            window.check_for_updates(silent=True)
        critical.assert_not_called()
        self.assertEqual(window.update_label.text(), "Update check failed (retrying later).")
        self.assertIn("503", window.update_label.toolTip())

    def test_install_stops_bots_after_the_switch_restarts_and_resumes_them(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager)
        window.available_update = self.update
        order = []
        manager.shutdown_all = lambda: order.append("shutdown") or {}
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok) as question, mock.patch.object(
            self.app_updates, "install_update", side_effect=lambda update, root, keep_versions: order.append(("install", update.version, root, keep_versions))
        ), mock.patch.object(self.app_updates, "start_launcher", side_effect=lambda installed: order.append("launcher")):
            window.install_available_update()
        prompt = question.call_args[0][2]
        self.assertIn("Install v1.0.1? You have v1.0.0.", prompt)
        self.assertIn("admin-main", prompt)
        self.assertIn(str(self.manager_gui.app_paths.DATA_ROOT), prompt)
        # The running version is protected from pruning while it still runs.
        self.assertEqual(order, [("install", "1.0.1", self.installed.install_root, ("1.0.0",)), "shutdown", "launcher"])
        self.assertTrue(window._allow_close)
        self.assertFalse(window.isVisible())

        # The next Manager starts the bots that were running before the update.
        resumed = self.make_window(FakeManager(self.manager_gui.manager_core))
        self.assertEqual(resumed.resume_bots_after_update(), ["admin-main"])
        self.assertIn(("start", "admin-main"), resumed.manager.calls)
        self.assertEqual(resumed.resume_bots_after_update(), [])

    def test_restart_happens_even_when_bots_cannot_be_listed_after_install(self):
        """Live case (03.10): the new version was active but listing the bots
        failed, so the Manager stayed on the old version."""
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager)
        window.available_update = self.update
        calls = {"n": 0}
        real_list = manager.list_instance_info

        def list_once():
            calls["n"] += 1
            if calls["n"] > 1:
                raise self.manager_gui.manager_core.ManagerCoreError("Failed to list bot instances: runtime/ missing")
            return real_list()

        manager.list_instance_info = list_once
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok), mock.patch.object(
            self.app_updates, "install_update"
        ), mock.patch.object(self.app_updates, "start_launcher") as launcher, mock.patch.object(self.manager_gui.QMessageBox, "critical") as critical:
            window.install_available_update()
        critical.assert_not_called()
        launcher.assert_called_once_with(self.installed)
        self.assertEqual(manager.shutdown_calls, 1)
        self.assertEqual(self.app_updates.take_resume(), ["admin-main"])

    def test_cancelled_or_failed_install_keeps_everything_running(self):
        manager = FakeManager(self.manager_gui.manager_core)
        window = self.make_window(manager)
        window.available_update = self.update
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Cancel), mock.patch.object(
            self.app_updates, "install_update"
        ) as install:
            window.install_available_update()
        install.assert_not_called()

        error = self.app_updates.AppUpdateError("Update to 1.0.1 failed: GitHub asset digest mismatch.")
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Ok), mock.patch.object(
            self.app_updates, "install_update", side_effect=error
        ), mock.patch.object(self.app_updates, "start_launcher") as launcher, mock.patch.object(
            self.manager_gui.QMessageBox, "critical"
        ) as critical:
            window.install_available_update()
        launcher.assert_not_called()
        self.assertEqual(manager.shutdown_calls, 0)
        self.assertIn("Nothing was changed", critical.call_args[0][2])
        self.assertFalse(window._allow_close)
        self.assertTrue(window.update_install_button.isEnabled())
        self.assertFalse(self.app_updates.resume_path().exists())


class StreamDirectorUITests(unittest.TestCase):
    """Manager: Stream Director bot type, setup wizard, its page, Twitch connect, dashboard."""

    CLIENT_ID = "abcdefghijklmnopqrst123456"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()
        self.instance_store = self.manager_gui.instance_store
        self.config_store = self.manager_gui.config_store
        self.panel_module = self.manager_gui.manager_stream_director
        self.sdt = self.panel_module.sdt
        self.instance = self.instance_store.create_instance("stream_director", "stream-main", "Stream Desk")

    def tearDown(self):
        self.temp_dir.cleanup()

    def info(self, state=None):
        core = self.manager_gui.manager_core
        manager = FakeManager(core)
        return replace(
            manager.info("stream-main", "Stream Desk", state or core.STATE_STOPPED, 77 if state == core.STATE_RUNNING else None),
            bot_type="stream_director",
            bot_type_display_name="Stream Director Bot",
            logs_dir=self.instance.paths.logs_dir,
        )

    def immediate(self, action, finished):
        try:
            value = action()
        except Exception as exc:
            finished(self.manager_gui.ActionResult(False, str(exc), exc))
        else:
            finished(self.manager_gui.ActionResult(True, "OK", value))

    def panel(self, state=None, transport=None):
        info = self.info(state)
        restarted = []
        panel = self.panel_module.StreamDirectorPanel(
            lambda: [("stream-main", "Main · Stream Desk", info), ("admin-main", "Main · Admin", FakeManager(self.manager_gui.manager_core).infos[1])],
            self.config_store,
            restarted.append,
            self.immediate,
            transport=transport or (lambda *args: (_ for _ in ()).throw(AssertionError("no network in tests"))),
        )
        self.addCleanup(panel.close)
        return panel, restarted

    def write_bot_status(self):
        runtime = self.instance.paths.runtime_dir
        status = {
            "guilds": [
                {
                    "id": "123456789012345678",
                    "name": "Cozy Corner",
                    "channels": [{"id": "223456789012345678", "name": "live", "type": "text"}, {"id": "9", "name": "Voice", "type": "voice"}],
                }
            ]
        }
        (runtime / "bot_status.json").write_text(json.dumps(status), encoding="utf-8")

    def test_bot_type_is_creatable_with_its_own_setup(self):
        self.assertIn(("stream_director", "Stream Director Bot"), self.manager_gui.creatable_bot_types())
        dialog = self.manager_gui.CreateBotInstanceDialog()
        self.addCleanup(dialog.close)
        dialog.type_combo.setCurrentIndex(dialog.type_combo.findData("stream_director"))
        self.assertIn("Twitch", dialog.type_hint.text())

        setup = self.manager_gui.BotSetupDialog("stream-main", self.instance_store, self.config_store)
        self.addCleanup(setup.close)
        self.assertTrue(setup.is_stream_director and setup.is_light)
        self.assertEqual(setup.pages.count(), 5)  # no admin access page
        self.assertIn("no privileged intent", setup.intent_ack_checkbox.text())
        setup.application_id_edit.setText("1234567890")
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(self.manager_gui.build_discord_invite_url("1234567890", "stream_director")).query)
        self.assertEqual(params["scope"], ["bot applications.commands"])
        bits = int(params["permissions"][0])
        self.assertEqual(bits, self.manager_gui.DISCORD_STREAM_DIRECTOR_PERMISSIONS)
        self.assertTrue(bits & (1 << 35) and bits & (1 << 38) and bits & (1 << 33))  # threads + events
        self.assertFalse(bits & ((1 << 2) | (1 << 1) | (1 << 28) | (1 << 3)))  # no ban/kick/roles/admin
        setup.display_name_edit.setText("Stream Desk")
        setup.token_edit.setText("FAKE_SD_TOKEN")
        self.assertTrue(setup.save_setup())
        self.assertEqual(self.instance.paths.token.read_text(encoding="utf-8"), "FAKE_SD_TOKEN\n")
        self.assertEqual(self.config_store.get_config_snapshot("stream-main").overrides, {"language": "en"})

    def test_page_lists_only_stream_bots_and_saves_normalized_settings(self):
        self.write_bot_status()
        panel, restarted = self.panel()
        self.assertEqual([panel.bot_combo.itemData(i) for i in range(panel.bot_combo.count())], ["stream-main"])
        self.assertEqual(panel.status_title.text(), "Stopped")
        self.assertEqual(panel.guild_combo.itemText(0), self.panel_module.CHOOSE_SERVER)
        panel.guild_combo.setCurrentIndex(1)
        self.assertEqual(panel.channel_combo.count(), 2)  # placeholder + the text channel only
        panel.channel_combo.setCurrentIndex(1)
        panel.team_roles_edit.setText("555555555, 666666666")
        panel.feature_boxes["twitch_chat"].setChecked(False)
        panel.spins["end_grace_minutes"].setValue(10)
        self.assertTrue(panel.save(restart=True))
        overrides = self.config_store.get_config_snapshot("stream-main").overrides
        self.assertEqual(overrides["guild_id"], "123456789012345678")
        self.assertEqual(overrides["channel_id"], "223456789012345678")
        self.assertEqual(overrides["team_role_ids"], ["555555555", "666666666"])
        self.assertFalse(overrides["features"]["twitch_chat"])
        self.assertEqual(overrides["end_grace_minutes"], 10)
        self.assertEqual(restarted, [])  # not running: nothing to restart
        panel.client_id_edit.setText("bad id!")
        self.assertFalse(panel.save())
        self.assertIn("Not saved", panel.result_label.text())

    def test_twitch_connect_disconnect_keeps_tokens_out_of_config_and_ui(self):
        sdt = self.sdt
        calls = []

        def transport(method, url, headers, data, timeout):
            calls.append(url)
            if url.endswith("/oauth2/device"):
                return 200, json.dumps({"device_code": "dev", "user_code": "WXYZ-1234", "verification_uri": "https://www.twitch.tv/activate?public=true", "expires_in": 1800, "interval": 1}).encode()
            if url.endswith("/oauth2/token"):
                return 200, json.dumps({"access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH", "expires_in": 14000, "scope": list(sdt.SCOPES)}).encode()
            if url.endswith("/oauth2/validate"):
                return 200, json.dumps({"login": "cozystreamer", "user_id": "4242", "scopes": list(sdt.SCOPES), "expires_in": 14000}).encode()
            if url.endswith("/oauth2/revoke"):
                return 200, b""
            raise AssertionError(url)

        panel, _restarted = self.panel(transport=transport)
        panel.client_id_edit.setText("short")
        with mock.patch.object(self.panel_module.QDesktopServices, "openUrl") as open_url:
            panel.connect_twitch()
            self.assertEqual(calls, [])  # invalid Client ID: nothing sent
            panel.client_id_edit.setText(self.CLIENT_ID)
            panel.connect_twitch()
        open_url.assert_called_once()
        self.assertIn("activate", open_url.call_args[0][0].toString())
        store = sdt.TokenStore(self.instance.paths.secrets_dir)
        saved = store.load()
        self.assertEqual((saved.login, saved.client_id), ("cozystreamer", self.CLIENT_ID))
        self.assertIn("Connected to Twitch as cozystreamer", panel.device_label.text())
        effective = self.config_store.load_effective_config("stream-main")
        self.assertEqual(effective["twitch_client_id"], self.CLIENT_ID)
        config_text = self.instance.paths.config.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", config_text)
        for label in (panel.twitch_title, panel.twitch_details, panel.device_label, panel.status_details):
            self.assertNotIn("SECRET", label.text())
        self.assertIn("connected", panel.twitch_title.text())
        panel.disconnect_twitch()
        self.assertIsNone(store.load())
        self.assertTrue(calls[-1].endswith("/oauth2/revoke"))

    def test_status_inbox_recap_and_problems(self):
        self.write_bot_status()
        self.config_store.save_config_overrides(
            "stream-main", self.panel_module.sdc.normalize_config({"guild_id": "123456789012345678", "channel_id": "223456789012345678"})
        )
        status = {
            "diagnosis": {"code": "live", "text": "Stream session running."},
            "problem": None,
            "guild_name": "Cozy Corner",
            "channel_name": "live",
            "missing_permissions": ["Create Public Threads"],
            "commands_synced": True,
            "twitch": {"state": "connected", "account": {"login": "cozy", "display_name": "Cozy", "missing_scopes": ["bits:read"]}, "live": True, "failed_subscriptions": ["channel.cheer"]},
            "director": {
                "session": {"id": "4", "status": "live", "source": "twitch", "title": "Road \\[to\\] 100", "started_at": time.time() - 600, "moments": 3},
                "community": {"level": 2, "level_progress": 40, "level_span": 200, "season": "2026-10", "season_points": 140},
                "inbox": [{"id": "9", "kind": "game", "text": "Hades", "link": None, "author": "Fan", "platform": "twitch", "supporters": 2}],
                "accepted_challenges": [{"id": "3", "text": "No healing"}],
                "last_recap": {"Stream": ["⏱ 2h 00m"], "Challenges": ["🏆 Beat \\*it\\*"]},
            },
        }
        (self.instance.paths.runtime_dir / self.panel_module.STATUS_FILE_NAME).write_text(json.dumps(status), encoding="utf-8")
        panel, _ = self.panel(state=self.manager_gui.manager_core.STATE_RUNNING)
        self.assertEqual(panel.status_title.text(), "LIVE — session running")
        details = panel.status_details.text()
        self.assertIn("Missing in the stream channel: Create Public Threads", details)
        self.assertIn("Road [to] 100", details)
        self.assertIn("Community level 2", details)
        self.assertIn("connected as Cozy", panel.twitch_title.text())
        self.assertIn("bits:read", panel.twitch_details.text())
        self.assertIn("#9 Game suggestion: Hades — Fan (twitch) · 👍 2", panel.inbox_label.text())
        self.assertIn("Accepted challenges: #3 No healing", panel.inbox_label.text())
        self.assertIn("🏆 Beat *it*", panel.recap_label.text())
        # A broken state file wins over everything and is never repaired by the Manager.
        state_path = self.instance.paths.data_dir / "stream_director_state.json"
        state_path.write_text("{", encoding="utf-8")
        panel.refresh()
        self.assertEqual(panel.status_title.text(), "State file problem")
        self.assertEqual(state_path.read_text(encoding="utf-8"), "{")

    def test_dashboard_and_ai_page(self):
        core = self.manager_gui.manager_core
        manager = FakeManager(core)
        manager.infos = [*manager.infos, self.info()]
        window = self.manager_gui.ManagerMainWindow(manager=manager, instance_api=FakeInstanceApi(), config_api=self.config_store, auto_refresh=False)

        def close_without_prompt():
            window._allow_close = True
            window.close()

        self.addCleanup(close_without_prompt)
        sd_info = manager.infos[-1]
        self.assertEqual(window._stream_summary(sd_info), "Not configured: choose the stream channel on the Stream Director page.")
        self.assertIsNone(window._stream_summary(manager.infos[0]))
        self.assertNotIn("stream-main", [info.instance_id for info in window._ai_infos()])
        combo_ids = [window.ai_bot_combo.itemData(i) for i in range(window.ai_bot_combo.count())]
        self.assertNotIn("stream-main", combo_ids)
        self.assertIn("stream", window.nav_buttons)
        window.show_page("stream")
        self.assertEqual(window.stream_panel.bot_combo.currentData(), "stream-main")


class SettingsRoundTripTests(unittest.TestCase):
    """UI -> config -> reopened UI with the real instance/config stores (no fakes)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager_gui = load_gui_module(Path(self.temp_dir.name))
        self.app = get_qapplication()
        self.instance_store = self.manager_gui.instance_store
        self.config_store = self.manager_gui.config_store
        self.core = self.manager_gui.manager_core
        for bot_type, instance_id, name in (
            ("admin", "admin-a", "Admin A"),
            ("admin", "admin-b", "Admin B"),
            ("game_presence", "gp-a", "Games A"),
            ("game_presence", "gp-b", "Games B"),
            ("stream_director", "sd-a", "Stream A"),
        ):
            self.instance_store.create_instance(bot_type, instance_id, name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def wizard(self, instance_id, running=False):
        dialog = self.manager_gui.BotSetupDialog(instance_id, self.instance_store, self.config_store, is_running=lambda: running)
        self.addCleanup(dialog.close)
        return dialog

    def info(self, instance_id, bot_type, state="STOPPED"):
        manager = FakeManager(self.core)
        return replace(
            manager.info(instance_id, instance_id, state, 42 if state == "RUNNING" else None),
            bot_type=bot_type,
            logs_dir=self.instance_store.get_instance_paths(instance_id).logs_dir,
        )

    def gp_page(self, states=None):
        states = states or {}
        bots = [(iid, label, self.info(iid, "game_presence", states.get(iid, "STOPPED"))) for iid, label in (("gp-a", "Main · Games A"), ("gp-b", "Main · Games B"))]
        restarted = []
        page = self.manager_gui.manager_game_presence.GamePresencePanel(lambda: list(bots), self.config_store, restarted.append)
        self.addCleanup(page.close)
        return page, restarted

    def kairo_page(self, states=None):
        states = states or {}
        bots = [(iid, label, self.info(iid, "admin", states.get(iid, "STOPPED"))) for iid, label in (("admin-a", "Main · Admin A"), ("admin-b", "Main · Admin B"))]
        restarted = []
        page = self.manager_gui.manager_kairo.KairoPanel(lambda: list(bots), self.config_store, restarted.append)
        self.addCleanup(page.close)
        return page, restarted

    def sd_page(self, state="STOPPED"):
        bots = [("sd-a", "Main · Stream A", self.info("sd-a", "stream_director", state))]
        page = self.manager_gui.manager_stream_director.StreamDirectorPanel(lambda: list(bots), self.config_store, lambda _id: None, lambda action, done: None)
        self.addCleanup(page.close)
        return page

    # -- Bot Setup wizard -----------------------------------------------------------

    def test_admin_wizard_round_trips_every_checkbox_both_ways_and_the_language(self):
        for value in (True, False):
            with self.subTest(value=value):
                dialog = self.wizard("admin-a")
                self.assertEqual(dialog.save_indicator.state, "clean")
                dialog.allow_admins_checkbox.setChecked(value)
                dialog.ai_read_content_checkbox.setChecked(value)
                dialog.ai_mention_checkbox.setChecked(value)
                dialog.intent_ack_checkbox.setChecked(value)
                dialog.invited_ack_checkbox.setChecked(value)
                dialog.application_id_edit.setText("123456789" if value else "")
                select = self.manager_gui.select_language
                select(dialog.language_combo, "ru" if value else "en")
                self.assertTrue(dialog.dirty or not value)
                self.assertTrue(dialog.save_setup())
                self.assertEqual(dialog.save_indicator.state, "saved")
                self.assertIn("next command", dialog.save_indicator.text())
                effective = self.config_store.load_effective_config("admin-a")
                self.assertEqual(
                    (effective["allow_server_administrators"], effective["ai_read_message_content"], effective["ai_mention_enabled"], effective["language"]),
                    (value, value, value, "ru" if value else "en"),
                )
                reopened = self.wizard("admin-a")
                self.assertEqual(
                    (
                        reopened.allow_admins_checkbox.isChecked(),
                        reopened.ai_read_content_checkbox.isChecked(),
                        reopened.ai_mention_checkbox.isChecked(),
                        reopened.intent_ack_checkbox.isChecked(),
                        reopened.invited_ack_checkbox.isChecked(),
                        reopened.application_id_edit.text(),
                        reopened.language_combo.currentData(),
                    ),
                    (value, value, value, value, value, "123456789" if value else "", "ru" if value else "en"),
                )
                self.assertFalse(reopened.dirty)

    def test_wizard_steps_and_invite_survive_reopening(self):
        dialog = self.wizard("gp-a")
        dialog.application_id_edit.setText("1234567890")
        dialog.intent_ack_checkbox.setChecked(True)
        dialog.invited_ack_checkbox.setChecked(True)
        self.assertTrue(dialog.save_setup())
        reopened = self.wizard("gp-a")
        self.assertEqual(reopened.application_id_edit.text(), "1234567890")
        self.assertTrue(reopened.intent_ack_checkbox.isChecked())
        self.assertTrue(reopened.invited_ack_checkbox.isChecked())
        self.assertIn("client_id=1234567890", reopened.invite_link_edit.text())
        # Manager-side memory only: nothing of it went into the bot's config.
        self.assertNotIn("application_id", self.config_store.load_effective_config("gp-a"))

    def test_wizard_feedback_restart_hint_and_unsaved_close(self):
        dialog = self.wizard("sd-a", running=True)
        dialog.token_edit.setText("NEW_TOKEN_VALUE")
        self.assertEqual(dialog.save_indicator.state, "dirty")
        self.assertTrue(dialog.save_setup())
        self.assertIn("Restart required", dialog.save_indicator.text())
        self.assertNotIn("NEW_TOKEN_VALUE", dialog.save_indicator.text() + dialog.status_label.text())
        dialog.show()
        dialog.display_name_edit.setText("Renamed but not saved")
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Cancel):
            dialog.reject()
        self.assertTrue(dialog.isVisible())  # unsaved changes are not lost by accident
        with mock.patch.object(self.manager_gui.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Discard):
            dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.instance_store.load_instance("sd-a").display_name, "Stream A")

    def test_old_configs_show_the_language_the_bot_really_uses(self):
        import game_presence as gp

        self.config_store.save_config_overrides("gp-a", {key: value for key, value in gp.DEFAULT_CONFIG.items() if key != "language"})
        self.assertEqual(self.wizard("gp-a").language_combo.currentData(), "ru")
        self.assertEqual(self.wizard("admin-a").language_combo.currentData(), "en")
        self.assertEqual(self.wizard("sd-a").language_combo.currentData(), "en")
        page, _ = self.gp_page()
        self.assertEqual(page.language_combo.currentData(), "ru")

    # -- Game Presence page ----------------------------------------------------------

    def test_game_presence_checkboxes_round_trip_both_ways(self):
        for value in (True, False):
            with self.subTest(value=value):
                page, _ = self.gp_page()
                page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-a"))
                page.enabled_checkbox.setChecked(value)
                page.voice_checkbox.setChecked(value)
                page.whole_voice_checkbox.setChecked(not value)
                page.ai_checkbox.setChecked(value)
                page.language_combo.setCurrentIndex(page.language_combo.findData("en" if value else "ru"))
                self.assertTrue(page.save())
                self.assertEqual(page.result_label.state, "saved")
                self.assertEqual(self.config_store.load_effective_config("gp-a")["ping_whole_voice"], not value)
                reopened, _ = self.gp_page()
                reopened.bot_combo.setCurrentIndex(reopened.bot_combo.findData("gp-a"))
                self.assertEqual(
                    (
                        reopened.enabled_checkbox.isChecked(),
                        reopened.voice_checkbox.isChecked(),
                        reopened.whole_voice_checkbox.isChecked(),
                        reopened.ai_checkbox.isChecked(),
                        reopened.language_combo.currentData(),
                    ),
                    (value, value, not value, value, "en" if value else "ru"),
                )
                self.assertFalse(reopened.dirty)

    def test_switching_bots_shows_each_bots_values_and_guards_unsaved_changes(self):
        import game_presence as gp

        self.config_store.save_config_overrides(
            "gp-a", {**gp.DEFAULT_CONFIG, "enabled": True, "delay_minutes": 7, "voice_aware": False, "language": "en", "guild_id": "111111", "channel_id": "222222"}
        )
        self.config_store.save_config_overrides("gp-b", {**gp.DEFAULT_CONFIG, "enabled": False, "delay_minutes": 20, "voice_aware": True, "language": "ru"})
        page, _ = self.gp_page({"gp-a": "RUNNING"})
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-a"))
        self.assertEqual((page.spins["delay_minutes"].value(), page.voice_checkbox.isChecked(), page.language_combo.currentData()), (7, False, "en"))
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-b"))
        self.assertEqual((page.spins["delay_minutes"].value(), page.voice_checkbox.isChecked(), page.language_combo.currentData()), (20, True, "ru"))
        self.assertFalse(page.enabled_checkbox.isChecked())

        page.spins["delay_minutes"].setValue(30)
        self.assertEqual(page.result_label.state, "dirty")
        with mock.patch.object(self.manager_gui.manager_game_presence.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Cancel):
            page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-a"))
        self.assertEqual(page.bot_combo.currentData(), "gp-b")  # stays, the edit is kept
        self.assertEqual(page.spins["delay_minutes"].value(), 30)
        with mock.patch.object(self.manager_gui.manager_game_presence.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Discard):
            page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-a"))
        self.assertEqual(page.spins["delay_minutes"].value(), 7)
        self.assertEqual(self.config_store.load_effective_config("gp-b")["delay_minutes"], 20)  # never saved

        page.spins["delay_minutes"].setValue(9)
        self.assertTrue(page.save())
        self.assertIn("Applied live", page.result_label.text())  # gp-a is running
        self.assertEqual(page.spins["delay_minutes"].value(), 9)
        self.assertFalse(page.dirty)

    def test_broken_config_is_not_shown_as_saved_and_not_overwritten(self):
        path = self.instance_store.get_instance_paths("gp-a").config
        path.write_text('{"delay_minutes": 0}', encoding="utf-8")
        page, _ = self.gp_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("gp-a"))
        self.assertIn("Config problem", page.result_label.text())
        self.assertFalse(page.save_button.isEnabled())
        self.assertFalse(page.save())
        self.assertEqual(path.read_text(encoding="utf-8"), '{"delay_minutes": 0}')

    # -- Stream Director page --------------------------------------------------------------

    def test_stream_director_features_and_language_round_trip(self):
        import stream_director_config as sdc

        self.config_store.save_config_overrides("sd-a", sdc.normalize_config({"guild_id": "111111", "channel_id": "222222"}))
        for value in (False, True):
            with self.subTest(value=value):
                page = self.sd_page()
                for box in page.feature_boxes.values():
                    box.setChecked(value)
                page.enabled_checkbox.setChecked(value)
                page.language_combo.setCurrentIndex(page.language_combo.findData("ru" if value else "en"))
                self.assertEqual(page.result_label.state, "dirty")
                self.assertTrue(page.save())
                self.assertIn("starts", page.result_label.text())  # stopped bot: applies on start
                reopened = self.sd_page()
                self.assertEqual({name: box.isChecked() for name, box in reopened.feature_boxes.items()}, {name: value for name in reopened.feature_boxes})
                self.assertEqual((reopened.enabled_checkbox.isChecked(), reopened.language_combo.currentData()), (value, "ru" if value else "en"))
                self.assertFalse(reopened.dirty)

    # -- Kairo page ------------------------------------------------------------------------

    def test_kairo_page_round_trips_language_and_social_awareness(self):
        sa = self.manager_gui.manager_kairo.sa
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        self.assertFalse(page.social_checkbox.isChecked())
        self.assertIn("Not chosen yet", page.choice_note.text())  # never chosen is not shown as a saved "off"
        self.assertFalse(page.dirty)
        for value in (True, False):
            with self.subTest(value=value):
                page.language_combo.setCurrentIndex(page.language_combo.findData("ru" if value else "en"))
                page.social_checkbox.setChecked(value)
                page.channels_edit.setText("111, 222" if value else "")
                page.replies_spin.setValue(7 if value else 4)
                self.assertTrue(page.dirty or not value)
                self.assertTrue(page.save())
                self.assertEqual(page.result_label.state, "saved")
                effective = self.config_store.load_effective_config("admin-a")
                self.assertEqual(
                    (effective["language"], effective[sa.CONFIG_ENABLED], effective[sa.CONFIG_CHANNELS], effective[sa.CONFIG_REPLIES]),
                    ("ru" if value else "en", value, ["111", "222"] if value else [], 7 if value else 4),
                )
                self.assertEqual(self.config_store.load_effective_config("admin-b")[sa.CONFIG_ENABLED], None)  # the other bot is untouched
                reopened, _ = self.kairo_page()
                reopened.bot_combo.setCurrentIndex(reopened.bot_combo.findData("admin-a"))
                self.assertEqual(
                    (reopened.language_combo.currentData(), reopened.social_checkbox.isChecked(), reopened.channels_edit.text(), reopened.replies_spin.value()),
                    ("ru" if value else "en", value, "111, 222" if value else "", 7 if value else 4),
                )
                self.assertEqual(reopened.choice_note.text(), "")  # a real choice now
                self.assertFalse(reopened.dirty)
                # The wizard shows the same stored language.
                self.assertEqual(self.wizard("admin-a").language_combo.currentData(), "ru" if value else "en")

    def test_kairo_page_switching_bots_and_unsaved_edits(self):
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        page.social_checkbox.setChecked(True)
        self.assertEqual(page.result_label.state, "dirty")
        with mock.patch.object(self.manager_gui.manager_kairo.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Cancel):
            page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-b"))
        self.assertEqual(page.bot_combo.currentData(), "admin-a")
        self.assertTrue(page.social_checkbox.isChecked())
        # Showing the page again keeps the edit; Refresh asks before throwing it away.
        page.refresh(keep_edits=True)
        self.assertTrue(page.social_checkbox.isChecked())
        self.assertEqual(page.result_label.state, "dirty")
        with mock.patch.object(self.manager_gui.manager_kairo.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Discard):
            page.refresh()
        self.assertFalse(page.social_checkbox.isChecked())
        self.assertFalse(page.dirty)
        with mock.patch.object(self.manager_gui.manager_kairo.QMessageBox, "question", return_value=self.manager_gui.QMessageBox.Discard):
            page.channels_edit.setText("333")
            page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-b"))
        self.assertEqual((page.bot_combo.currentData(), page.channels_edit.text()), ("admin-b", ""))
        self.assertEqual(self.config_store.load_effective_config("admin-a")["social_awareness_channel_ids"], [])

    def test_kairo_page_says_when_a_restart_is_needed(self):
        page, restarted = self.kairo_page({"admin-a": "RUNNING"})
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        page.language_combo.setCurrentIndex(page.language_combo.findData("ru"))
        self.assertTrue(page.save())
        self.assertIn("Applied live", page.result_label.text())
        self.assertIn("Slash command descriptions", page.result_label.text())
        page.social_checkbox.setChecked(True)
        self.assertTrue(page.save())
        self.assertIn("Restart required", page.result_label.text())  # the running bot did not request Message Content Intent
        self.assertEqual(restarted, [])
        page.replies_spin.setValue(9)
        self.assertTrue(page.save(restart=True))
        self.assertEqual(restarted, ["admin-a"])
        page.channels_edit.setText("not-a-number")
        self.assertFalse(page.save())
        self.assertEqual(page.result_label.state, "error")
        self.assertEqual(self.config_store.load_effective_config("admin-a")["social_awareness_channel_ids"], [])

    def test_kairo_page_locks_on_a_broken_config(self):
        path = self.instance_store.get_instance_paths("admin-a").config
        path.write_text('{"social_awareness_replies_per_hour": 999}', encoding="utf-8")
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        self.assertIn("Config problem", page.result_label.text())
        self.assertFalse(page.save_button.isEnabled())
        self.assertFalse(page.save())
        self.assertEqual(path.read_text(encoding="utf-8"), '{"social_awareness_replies_per_hour": 999}')

    def test_social_awareness_question_is_asked_once_per_bot(self):
        kairo = self.manager_gui.manager_kairo
        asked = []

        def answer(value):
            def ask(parent):
                asked.append(value)
                return value

            return ask

        self.assertFalse(kairo.ensure_social_awareness_choice(None, "admin-a", self.config_store, answer(None)))  # cancel: no start
        self.assertIsNone(self.config_store.load_effective_config("admin-a")["social_awareness_enabled"])
        self.assertTrue(kairo.ensure_social_awareness_choice(None, "admin-a", self.config_store, answer(True)))
        self.assertTrue(self.config_store.load_effective_config("admin-a")["social_awareness_enabled"])
        self.assertTrue(kairo.ensure_social_awareness_choice(None, "admin-a", self.config_store, answer(False)))  # not asked again
        self.assertEqual(asked, [None, True])
        self.assertTrue(kairo.ensure_social_awareness_choice(None, "admin-b", self.config_store, answer(False)))
        self.assertIs(self.config_store.load_effective_config("admin-b")["social_awareness_enabled"], False)
        self.assertEqual(asked, [None, True, False])
        # Other bot types are never asked.
        self.assertFalse(kairo.needs_social_awareness_choice(self.config_store, "gp-a"))



    def test_kairo_server_memory_can_be_seen_forgotten_and_lifted(self):
        kairo = self.manager_gui.manager_kairo
        sm = kairo.sm
        path = self.instance_store.get_instance_paths("admin-a").data_dir / sm.FILE_NAME
        moment = [time.time()]
        memory = sm.SocialMemory(path, clock=lambda: moment[0])
        captain = "Members call Alice 'the Captain' in raids"
        memory.propose(111, "remember", kind="nickname", text=captain)
        moment[0] += sm.CONFIRM_GAP + 1
        memory.propose(111, "remember", kind="nickname", text=captain)
        memory.propose(111, "remember", kind="joke", text="Bob always blames the lag when he loses")
        memory.add_mute(111, "channel", 3600, channel_id=222, by_user_id=333)
        memory.record_outcome(111, sm.Outcome(moment[0], 222, "reply", "named", "engaged"))
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        self.assertEqual(page.memory_guild_combo.currentData(), 111)
        labels = [page.lore_list.item(row).text() for row in range(page.lore_list.count())]
        self.assertEqual(labels, [f"[nickname] {captain}  (noticed 2×)", "[joke] Bob always blames the lag when he loses  (candidate: noticed once)"])
        self.assertEqual(page.quiet_list.count(), 1)
        self.assertIn("channel 222 · asked", page.quiet_list.item(0).text())
        self.assertIn("1 answered", page.feedback_label.text())
        # The other Kairo shows its own (empty) memory, not admin-a's.
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-b"))
        self.assertEqual((page.lore_list.count(), page.quiet_list.count()), (0, 0))
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        page.lore_list.setCurrentRow(1)
        page.forget_lore_button.click()
        self.assertEqual([entry.text for entry in memory.guild(111).lore], [captain])
        with mock.patch.object(kairo.QMessageBox, "question", return_value=kairo.QMessageBox.Cancel):
            page.clear_lore_button.click()
        self.assertEqual(len(memory.guild(111).lore), 1)  # asked first
        with mock.patch.object(kairo.QMessageBox, "question", return_value=kairo.QMessageBox.Yes):
            page.clear_lore_button.click()
        self.assertEqual(memory.guild(111).lore, [])
        page.lift_all_button.click()
        self.assertEqual(memory.active_mutes(111), [])
        self.assertEqual(page.quiet_list.count(), 0)

    def test_kairo_broken_memory_is_shown_and_can_be_reset(self):
        kairo = self.manager_gui.manager_kairo
        path = self.instance_store.get_instance_paths("admin-a").data_dir / kairo.sm.FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        self.assertIn("unreadable", page.memory_problem_label.text())
        self.assertFalse(page.reset_memory_button.isHidden())
        with mock.patch.object(kairo.QMessageBox, "question", return_value=kairo.QMessageBox.Yes):
            page.reset_memory_button.click()
        self.assertEqual(page.memory_problem_label.text(), "")
        self.assertTrue(page.reset_memory_button.isHidden())
        self.assertEqual([item.read_text(encoding="utf-8") for item in path.parent.glob("social_memory.corrupt-*.json")], ["{broken"])

    def test_kairo_lore_switch_round_trips(self):
        sa = self.manager_gui.manager_kairo.sa
        page, _ = self.kairo_page()
        page.bot_combo.setCurrentIndex(page.bot_combo.findData("admin-a"))
        self.assertTrue(page.lore_checkbox.isChecked())  # on by default (only used with Social Awareness)
        page.lore_checkbox.setChecked(False)
        self.assertEqual(page.result_label.state, "dirty")
        self.assertTrue(page.save())
        self.assertIs(self.config_store.load_effective_config("admin-a")[sa.CONFIG_LORE], False)
        reopened, _ = self.kairo_page()
        reopened.bot_combo.setCurrentIndex(reopened.bot_combo.findData("admin-a"))
        self.assertFalse(reopened.lore_checkbox.isChecked())
        self.assertFalse(reopened.dirty)


def replace_namespace(namespace, **changes):
    return SimpleNamespace(**{**vars(namespace), **changes})


if __name__ == "__main__":
    unittest.main()
