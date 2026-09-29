from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import admin_instance
import app_paths
import bot_registry
import config_store
import instance_store
import manager_core


REFRESH_INTERVAL_MS = 1500


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    value: object | None = None


@dataclass(frozen=True)
class _WorkerHandle:
    thread: QThread
    worker: QObject
    bridge: QObject


def format_uptime(uptime_seconds: float | None) -> str:
    if uptime_seconds is None:
        return ""
    total_seconds = max(0, int(uptime_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def instance_info_to_display_row(info: manager_core.InstanceInfo) -> list[str]:
    return [
        info.instance_id,
        info.display_name,
        info.bot_type,
        info.state,
        "" if info.pid is None else str(info.pid),
    ]


def instance_info_details(info: manager_core.InstanceInfo) -> str:
    return "\n".join(
        [
            f"Instance ID: {info.instance_id}",
            f"Display name: {info.display_name}",
            f"Bot type: {info.bot_type_display_name}",
            f"Bot version: {info.bot_version}",
            f"State: {info.state}",
            f"PID: {'' if info.pid is None else info.pid}",
            f"Uptime: {format_uptime(info.uptime_seconds)}",
            f"Config path: {info.config_path}",
            f"Logs directory: {info.logs_dir}",
        ]
    )


def parse_overrides_json(text: str) -> dict:
    try:
        loaded = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Config overrides JSON must be an object.")
    return loaded


def pretty_json(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


class _ActionWorker(QObject):
    finished = Signal(object)

    def __init__(self, action: Callable[[], object]) -> None:
        super().__init__()
        self._action = action

    def run(self) -> None:
        try:
            value = self._action()
        except Exception as exc:
            self.finished.emit(ActionResult(False, str(exc), exc))
        else:
            self.finished.emit(ActionResult(True, "OK", value))


class _GuiCallbackBridge(QObject):
    handled = Signal()
    cleanup_requested = Signal(object)

    def __init__(self, callback: Callable[[ActionResult], None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._callback = callback

    @Slot(object)
    def handle_result(self, result: ActionResult) -> None:
        self._callback(result)
        self.handled.emit()

    @Slot()
    def handle_thread_finished(self) -> None:
        self.cleanup_requested.emit(self)


class ConfigEditorDialog(QDialog):
    def __init__(self, instance_id: str, config_api=config_store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._instance_id = instance_id
        self._config_api = config_api
        self._last_error = ""
        self.setWindowTitle(f"Edit Config: {instance_id}")

        self.overrides_edit = QPlainTextEdit()
        self.effective_view = QPlainTextEdit()
        self.effective_view.setReadOnly(True)
        self.status_label = QLabel("")
        self.save_button = QPushButton("Save")
        self.reload_button = QPushButton("Reload")

        button_row = QHBoxLayout()
        button_row.addWidget(self.reload_button)
        button_row.addWidget(self.save_button)
        button_row.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("User overrides JSON"))
        layout.addWidget(self.overrides_edit)
        layout.addWidget(QLabel("Effective config (read-only)"))
        layout.addWidget(self.effective_view)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self.reload_button.clicked.connect(self.load_snapshot)
        self.save_button.clicked.connect(self.save_overrides)
        self.load_snapshot()

    @property
    def last_error(self) -> str:
        return self._last_error

    def load_snapshot(self) -> None:
        try:
            snapshot = self._config_api.get_config_snapshot(self._instance_id)
        except config_store.ConfigStoreError as exc:
            self._set_error(str(exc))
            return
        self.overrides_edit.setPlainText(pretty_json(snapshot.overrides))
        self.effective_view.setPlainText(pretty_json(snapshot.effective))
        self._set_status("Loaded.")

    def save_overrides(self) -> None:
        try:
            overrides = parse_overrides_json(self.overrides_edit.toPlainText())
            self._config_api.save_config_overrides(self._instance_id, overrides)
        except (ValueError, config_store.ConfigStoreError) as exc:
            self._set_error(str(exc))
            return
        self._set_status("Saved.")
        self.load_snapshot()

    def _set_status(self, message: str) -> None:
        self._last_error = ""
        self.status_label.setText(message)

    def _set_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(f"Error: {message}")


class CreateAdminInstanceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Admin Instance")
        self.instance_id_edit = QLineEdit()
        self.display_name_edit = QLineEdit()

        form = QFormLayout()
        form.addRow("Instance ID", self.instance_id_edit)
        form.addRow("Display name (optional)", self.display_name_edit)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

    def values(self) -> tuple[str, str | None]:
        display_name = self.display_name_edit.text().strip()
        return self.instance_id_edit.text().strip(), display_name or None


class ManagerMainWindow(QMainWindow):
    def __init__(
        self,
        manager: manager_core.BotProcessManager | None = None,
        instance_api=instance_store,
        config_api=config_store,
        auto_refresh: bool = True,
    ) -> None:
        super().__init__()
        self.manager = manager or manager_core.BotProcessManager()
        self._instance_api = instance_api
        self._config_api = config_api
        self._busy_instances: set[str] = set()
        self._worker_handles: list[_WorkerHandle] = []
        self._last_infos: list[manager_core.InstanceInfo] = []
        self._last_error = ""
        self._allow_close = False
        self._shutdown_in_progress = False

        self.setWindowTitle("DarkAbyss Bot Manager")
        self.instance_table = QTableWidget(0, 5)
        self.instance_table.setHorizontalHeaderLabels(["Instance ID", "Display Name", "Bot Type", "Status", "PID"])
        self.instance_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.instance_table.setSelectionMode(QTableWidget.SingleSelection)
        self.instance_table.itemSelectionChanged.connect(self._update_selected_details)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.status_label = QLabel("")

        self.refresh_button = QPushButton("Refresh")
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.edit_config_button = QPushButton("Edit Config")
        self.create_admin_button = QPushButton("Create Admin Instance")

        self.refresh_button.clicked.connect(self.refresh_instances)
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button.clicked.connect(self.stop_selected)
        self.restart_button.clicked.connect(self.restart_selected)
        self.edit_config_button.clicked.connect(self.edit_selected_config)
        self.create_admin_button.clicked.connect(self.create_admin_instance)

        button_row = QHBoxLayout()
        for button in (
            self.refresh_button,
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.edit_config_button,
            self.create_admin_button,
        ):
            button_row.addWidget(button)
        button_row.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(self.instance_table)
        layout.addLayout(button_row)
        layout.addWidget(QLabel("Selected instance"))
        layout.addWidget(self.details_view)
        layout.addWidget(self.status_label)

        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self.refresh_instances)
        if auto_refresh:
            self.refresh_timer.start()

        self.refresh_instances()

    @property
    def last_error(self) -> str:
        return self._last_error

    def selected_instance_id(self) -> str | None:
        selected_rows = self.instance_table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        item = self.instance_table.item(selected_rows[0].row(), 0)
        return None if item is None else item.text()

    def refresh_instances(self) -> None:
        selected_id = self.selected_instance_id()
        try:
            infos = sorted(self.manager.list_instance_info(), key=lambda item: item.instance_id)
        except manager_core.ManagerCoreError as exc:
            self._set_light_error(f"Refresh failed: {exc}")
            return
        self._last_infos = infos
        self.instance_table.setRowCount(len(infos))
        for row_index, info in enumerate(infos):
            for column_index, value in enumerate(instance_info_to_display_row(info)):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.instance_table.setItem(row_index, column_index, item)
        self._restore_selection(selected_id)
        self._set_status(f"Loaded {len(infos)} instance(s).")
        self._update_selected_details()

    def start_selected(self) -> None:
        self._dispatch_lifecycle_action("start", lambda instance_id: self.manager.start(instance_id))

    def stop_selected(self) -> None:
        self._dispatch_lifecycle_action("stop", lambda instance_id: self.manager.stop(instance_id))

    def restart_selected(self) -> None:
        self._dispatch_lifecycle_action("restart", lambda instance_id: self.manager.restart(instance_id))

    def edit_selected_config(self) -> None:
        instance_id = self.selected_instance_id()
        if instance_id is None:
            self._show_error("Select an instance first.")
            return
        dialog = ConfigEditorDialog(instance_id, self._config_api, self)
        dialog.exec()
        self.refresh_instances()

    def create_admin_instance(self) -> None:
        dialog = CreateAdminInstanceDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        instance_id, display_name = dialog.values()
        try:
            self._instance_api.create_instance("admin", instance_id, display_name=display_name)
        except (instance_store.InstanceStoreError, bot_registry.BotRegistryError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._set_status(f"Created instance {instance_id}.")
        self.refresh_instances()

    def closeEvent(self, event) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._operation_in_progress():
            self._set_light_error("Operation is still in progress. Please try closing again after it completes.")
            event.ignore()
            return
        try:
            infos = self.manager.list_instance_info()
        except manager_core.ManagerCoreError as exc:
            self._show_error(f"Unable to check running instances before exit: {exc}")
            event.ignore()
            return
        running_infos = [info for info in infos if info.state == manager_core.STATE_RUNNING]
        if not running_infos:
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "Stop running bots?",
            "Managed bot instances are still running. Stop all and exit?",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            event.ignore()
            return
        event.ignore()
        self._run_shutdown_for_close()

    def _dispatch_lifecycle_action(self, action_name: str, action: Callable[[str], object]) -> None:
        instance_id = self.selected_instance_id()
        if instance_id is None:
            self._show_error("Select an instance first.")
            return
        if instance_id in self._busy_instances:
            self._set_light_error(f"Action already in progress for {instance_id}.")
            return
        self._busy_instances.add(instance_id)
        self._update_buttons()

        def run_action() -> object:
            return action(instance_id)

        self._start_worker(
            run_action,
            lambda result: self._finish_lifecycle_action(instance_id, action_name, result),
        )

    def _finish_lifecycle_action(self, instance_id: str, action_name: str, result: ActionResult) -> None:
        self._busy_instances.discard(instance_id)
        self._update_buttons()
        if result.ok:
            self._set_status(f"{action_name.capitalize()} completed for {instance_id}.")
            self.refresh_instances()
        else:
            self.refresh_instances()
            self._show_error(result.message)

    def _run_shutdown_for_close(self) -> None:
        if self._shutdown_in_progress:
            self._set_light_error("Shutdown is already in progress. Please wait.")
            return
        self._shutdown_in_progress = True
        self._set_status("Stopping managed instances before exit...")
        self._start_worker(lambda: self.manager.shutdown_all(), self._finish_shutdown_for_close)

    def _finish_shutdown_for_close(self, result: ActionResult) -> None:
        if not result.ok:
            self._shutdown_in_progress = False
            self.refresh_instances()
            self._show_error(result.message)
            return
        failures = []
        if isinstance(result.value, dict):
            failures = [f"{instance_id}: {value}" for instance_id, value in result.value.items() if isinstance(value, Exception)]
        if failures:
            self._shutdown_in_progress = False
            self.refresh_instances()
            self._show_error("Failed to stop all instances:\n" + "\n".join(failures))
            return
        self._shutdown_in_progress = False
        self._allow_close = True
        self.close()

    def _start_worker(self, action: Callable[[], object], finished: Callable[[ActionResult], None]) -> None:
        thread = QThread(self)
        worker = _ActionWorker(action)
        bridge = _GuiCallbackBridge(finished, self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.handle_result, Qt.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        bridge.handled.connect(thread.quit)
        thread.finished.connect(bridge.handle_thread_finished, Qt.QueuedConnection)
        bridge.cleanup_requested.connect(self._cleanup_worker, Qt.QueuedConnection)
        thread.finished.connect(thread.deleteLater)
        self._worker_handles.append(_WorkerHandle(thread=thread, worker=worker, bridge=bridge))
        thread.start()

    @Slot(object)
    def _cleanup_worker(self, bridge: QObject) -> None:
        self._worker_handles = [handle for handle in self._worker_handles if handle.bridge is not bridge]

    def _operation_in_progress(self) -> bool:
        return bool(self._busy_instances or self._worker_handles or self._shutdown_in_progress)

    def _restore_selection(self, instance_id: str | None) -> None:
        if instance_id is not None:
            for row_index in range(self.instance_table.rowCount()):
                item = self.instance_table.item(row_index, 0)
                if item is not None and item.text() == instance_id:
                    self.instance_table.selectRow(row_index)
                    return
        if self.instance_table.rowCount() > 0 and not self.instance_table.selectionModel().selectedRows():
            self.instance_table.selectRow(0)

    def _update_selected_details(self) -> None:
        instance_id = self.selected_instance_id()
        info = next((item for item in self._last_infos if item.instance_id == instance_id), None)
        self.details_view.setPlainText("" if info is None else instance_info_details(info))
        self._update_buttons()

    def _update_buttons(self) -> None:
        instance_id = self.selected_instance_id()
        busy = instance_id in self._busy_instances if instance_id is not None else False
        has_selection = instance_id is not None
        for button in (self.start_button, self.stop_button, self.restart_button, self.edit_config_button):
            button.setEnabled(has_selection and not busy)

    def _set_status(self, message: str) -> None:
        self._last_error = ""
        self.status_label.setText(message)

    def _set_light_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(message)

    def _show_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(f"Error: {message}")
        QMessageBox.critical(self, "DarkAbyss Bot Manager", message)


def bootstrap_for_gui() -> None:
    app_paths.ensure_user_data()
    admin_instance.ensure_admin_instance()


def main() -> int:
    app = QApplication(sys.argv)
    try:
        bootstrap_for_gui()
    except Exception as exc:
        QMessageBox.critical(None, "DarkAbyss Bot Manager", f"Startup failed: {exc}")
        return 1
    window = ManagerMainWindow()
    window.resize(980, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
