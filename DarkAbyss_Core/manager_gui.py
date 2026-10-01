from __future__ import annotations

import json
import os
import sys
import tempfile
from urllib.parse import urlencode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import admin_instance
import ai_platform
import app_paths
import bot_registry
import config_store
import instance_store
import manager_core


REFRESH_INTERVAL_MS = 1500
GROQ_PROFILE_ID = "groq-default"
GROQ_PROVIDER_ID = "groq"
GROQ_CREDENTIAL_REF = "groq-default"

DISCORD_DEVELOPER_PORTAL_URL = "https://discord.com/developers/applications"
DISCORD_INVITE_BASE_URL = "https://discord.com/oauth2/authorize"
DISCORD_BOT_INVITE_SCOPES = ("bot", "applications.commands")
DISCORD_PERMISSION_BITS = {
    "View Channels": 1 << 10,
    "Send Messages": 1 << 11,
    "Read Message History": 1 << 16,
    "Manage Messages": 1 << 13,
    "Manage Channels": 1 << 4,
    "Manage Roles": 1 << 28,
    "Moderate Members": 1 << 40,
    "Kick Members": 1 << 1,
    "Ban Members": 1 << 2,
}
DISCORD_ADMIN_BOT_PERMISSIONS = sum(DISCORD_PERMISSION_BITS.values())

SETUP_HELP_TEXT: dict[str, tuple[str, str]] = {
    "display_name": (
        "Manager display name",
        "This is only the local label shown inside DarkAbyss Bot Manager.\n\n"
        "It does not rename the Discord bot account. To rename the Discord bot username, change it in Discord Developer Portal.",
    ),
    "application": (
        "Discord Application ID",
        "How to get it:\n"
        "1) Open Discord Developer Portal.\n"
        "2) Choose your Application.\n"
        "3) General Information → Application ID → Copy.\n\n"
        "Application ID is public. Do not enter a Client Secret here.\n\n"
        "Both private and shareable bot installation modes are supported.",
    ),
    "installation": (
        "Bot installation access",
        "PRIVATE:\n"
        "- Bot → Public Bot = OFF.\n"
        "- Only the application owner/developer team can add the bot.\n"
        "- Installation → Install Link = None may be required for private applications.\n"
        "- Manager can still generate a manual Guild Install URL for the owner.\n\n"
        "SHAREABLE:\n"
        "- Bot → Public Bot = ON.\n"
        "- Another user with permission to manage/install apps on their server can use the Manager-generated invite link.\n"
        "- Enabling Public Bot does not change DarkAbyss access control, commands, whitelist, token, or runtime behavior.\n\n"
        "Public Bot does not automatically publish the application in a directory.",
    ),
    "token": (
        "Discord bot token",
        "Как получить token:\n"
        "1) Открой Discord Developer Portal.\n"
        "2) Applications → выбери своё приложение или создай New Application.\n"
        "3) Bot → Reset Token / Copy Token.\n"
        "4) Вставь token в это поле и нажми Save Setup.\n\n"
        "Важно: token — это пароль бота. Не отправляй его в чат, не коммить в Git и не вставляй в файлы программы.",
    ),
    "users": (
        "Allowed user IDs",
        "Это Discord user IDs, которым разрешены admin-команды бота.\n\n"
        "Как получить user ID:\n"
        "1) Discord Settings → Advanced → включи Developer Mode.\n"
        "2) Правый клик по пользователю → Copy User ID.\n"
        "3) Вставляй несколько ID через запятую или пробел.\n\n"
        "Если список пустой, доступ зависит от роли administrators и ролей ниже.",
    ),
    "roles": (
        "Allowed role IDs",
        "Это Discord role IDs, которым разрешены admin-команды бота.\n\n"
        "Как получить role ID:\n"
        "1) В Discord включи Developer Mode.\n"
        "2) Server Settings → Roles.\n"
        "3) Правый клик по роли → Copy Role ID.\n"
        "4) Вставляй несколько ID через запятую или пробел.",
    ),
    "audit": (
        "Audit channel ID",
        "Это канал, куда бот может писать служебные/audit-сообщения, если команда это использует.\n\n"
        "Как получить channel ID:\n"
        "1) В Discord включи Developer Mode.\n"
        "2) Правый клик по каналу → Copy Channel ID.\n"
        "3) Поле можно оставить пустым, если audit-канал не нужен.",
    ),
    "intent": (
        "Server Members Intent",
        "The Admin bot enables discord.Intents.members, so Discord requires the privileged Server Members Intent toggle.\n\n"
        "Manager cannot verify this Discord-side setting locally. Check the box only after enabling it in Discord Developer Portal.",
    ),
    "administrators": (
        "Server administrators",
        "Если включено, пользователи с Discord permission Administrator смогут использовать admin-команды.\n\n"
        "Если хочешь более жёсткий доступ, выключи этот пункт и укажи конкретные user IDs или role IDs.",
    ),
}


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


def parse_id_list(text: str) -> list[str]:
    normalized = text.replace(",", " ")
    values = [value.strip() for value in normalized.split() if value.strip()]
    invalid = [value for value in values if not value.isdigit()]
    if invalid:
        raise ValueError(f"IDs must contain digits only: {', '.join(invalid)}")
    return values


def parse_optional_id(text: str) -> str | None:
    value = text.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("Audit channel ID must contain digits only.")
    return value


def validate_application_id(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("Discord Application ID is required to generate an invite link.")
    if not value.isdigit():
        raise ValueError("Discord Application ID must contain digits only.")
    return value


def build_discord_invite_url(application_id: str) -> str:
    client_id = validate_application_id(application_id)
    query = urlencode(
        {
            "client_id": client_id,
            "permissions": str(DISCORD_ADMIN_BOT_PERMISSIONS),
            "scope": " ".join(DISCORD_BOT_INVITE_SCOPES),
            "integration_type": "0",
        }
    )
    return f"{DISCORD_INVITE_BASE_URL}?{query}"


def is_placeholder_token(token: str) -> bool:
    return token.strip() in {"", app_paths.TOKEN_PLACEHOLDER}


def token_status_text(token_path: Path) -> str:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "Token file is missing or unreadable."
    if is_placeholder_token(token):
        return "Token is not configured yet."
    return "Token is configured. Leave token field empty to keep it unchanged."


def _ensure_safe_token_path(instance: instance_store.BotInstance) -> Path:
    expected = instance_store.get_instance_paths(instance.id).token
    if instance.paths.token != expected:
        raise instance_store.InstanceStoreError(f"Unexpected token path for instance {instance.id!r}.")
    token_path = instance.paths.token
    if token_path.is_symlink():
        raise instance_store.InstanceStoreError(f"Refusing to write token through a symlink: {token_path}")
    root = instance.paths.root.resolve()
    try:
        token_path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise instance_store.InstanceStoreError(f"Token path escapes instance root: {token_path}") from exc
    return token_path


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


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
        self.setWindowTitle(f"Advanced JSON: {instance_id}")

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
        warning_label = QLabel(
            "Advanced configuration.\n"
            "Setup Bot covers all currently supported Admin settings.\n"
            "Edit raw JSON only if you know why you need an override."
        )
        warning_label.setWordWrap(True)
        layout.addWidget(warning_label)
        layout.addWidget(QLabel("User overrides: values explicitly overriding program defaults."))
        layout.addWidget(self.overrides_edit)
        layout.addWidget(QLabel("Effective config: final read-only config after defaults + overrides are merged."))
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


def create_groq_provider(credential_store: ai_platform.CredentialStore):
    try:
        import ai_groq
    except Exception as exc:
        raise ai_platform.AIPlatformError("Provider unavailable.") from exc
    return ai_groq.GroqProvider(credential_store)


def _profile_by_id(settings: ai_platform.AISettings, profile_id: str) -> ai_platform.AIProfile | None:
    return next((profile for profile in settings.profiles if profile.profile_id == profile_id), None)


class AIProviderSettingsDialog(QDialog):
    def __init__(
        self,
        settings_store: ai_platform.AISettingsStore | None = None,
        credential_store: ai_platform.CredentialStore | None = None,
        provider_factory: Callable[[ai_platform.CredentialStore], object] = create_groq_provider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Providers")
        self._settings_store = settings_store or ai_platform.AISettingsStore()
        self._credential_store = credential_store or ai_platform.CredentialStore()
        self._provider_factory = provider_factory
        self._worker_handles: list[_WorkerHandle] = []
        self._provider = None
        self._settings_invalid = False
        self._loaded_settings = ai_platform.AISettings()
        self._test_in_progress = False

        self.status_label = QLabel("")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.show_key_checkbox = QCheckBox("Show key while editing")
        self.model_combo = QComboBox()
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(["low", "medium", "high"])
        self.save_button = QPushButton("Save")
        self.test_button = QPushButton("Test Connection")
        self.remove_button = QPushButton("Remove Key")
        self.close_button = QPushButton("Close")

        form = QFormLayout()
        form.addRow("Status:", self.status_label)
        form.addRow("API Key:", self.key_edit)
        form.addRow("", self.show_key_checkbox)
        form.addRow("Model:", self.model_combo)
        form.addRow("Reasoning:", self.reasoning_combo)

        button_row = QHBoxLayout()
        for button in (self.save_button, self.test_button, self.remove_button, self.close_button):
            button_row.addWidget(button)
        button_row.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Groq"))
        layout.addLayout(form)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self.show_key_checkbox.toggled.connect(self._toggle_key_visibility)
        self.save_button.clicked.connect(self.save_settings)
        self.test_button.clicked.connect(self.test_connection)
        self.remove_button.clicked.connect(self.remove_key)
        self.close_button.clicked.connect(self.accept)

        self._load_provider_metadata()
        self._load_settings()
        self._refresh_status()

    def _toggle_key_visibility(self, checked: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _load_provider_metadata(self) -> None:
        self.model_combo.clear()
        try:
            self._provider = self._provider_factory(self._credential_store)
            models = self._provider.metadata.models
        except Exception:
            self._provider = None
            self.status_label.setText("Provider unavailable")
            self.test_button.setEnabled(False)
            return
        for model in models:
            self.model_combo.addItem(model.display_name, model.model_id)

    def _load_settings(self) -> None:
        try:
            settings = self._settings_store.load()
        except ai_platform.AIPlatformError:
            self._settings_invalid = True
            self._loaded_settings = ai_platform.AISettings()
            self.status_label.setText("AI settings are invalid.")
            self.save_button.setEnabled(False)
            return
        self._settings_invalid = False
        self._loaded_settings = settings
        profile = _profile_by_id(settings, GROQ_PROFILE_ID)
        if profile is None:
            profile = _profile_by_id(ai_platform.default_groq_settings(), GROQ_PROFILE_ID)
        if profile is None:
            return
        model_index = self.model_combo.findData(profile.model_id)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        reasoning = str(profile.options.get("reasoning_effort", "medium"))
        reasoning_index = self.reasoning_combo.findText(reasoning)
        self.reasoning_combo.setCurrentIndex(reasoning_index if reasoning_index >= 0 else 1)

    def _current_settings(self) -> ai_platform.AISettings | None:
        current_model = self.model_combo.currentData()
        existing_groq = _profile_by_id(self._loaded_settings, GROQ_PROFILE_ID)
        if current_model is None:
            if existing_groq is None:
                return None
            model_id = existing_groq.model_id
        else:
            model_id = str(current_model)
        reasoning = self.reasoning_combo.currentText() or "medium"
        profile = ai_platform.AIProfile(
            profile_id=GROQ_PROFILE_ID,
            provider_id=GROQ_PROVIDER_ID,
            model_id=str(model_id),
            credential_ref=GROQ_CREDENTIAL_REF,
            options={"reasoning_effort": reasoning},
        )
        preserved_profiles = tuple(item for item in self._loaded_settings.profiles if item.profile_id != GROQ_PROFILE_ID)
        if not self._loaded_settings.profiles and self._loaded_settings.routing == ai_platform.RoutingConfig():
            routing = ai_platform.RoutingConfig(
                routine_profile_id=GROQ_PROFILE_ID,
                planner_profile_id=GROQ_PROFILE_ID,
                creative_profile_id=GROQ_PROFILE_ID,
            )
        else:
            routing = self._loaded_settings.routing
        return ai_platform.AISettings(
            profiles=preserved_profiles + (profile,),
            routing=routing,
        )

    def _refresh_status(self) -> None:
        if self._settings_invalid:
            self.status_label.setText("AI settings are invalid.")
            return
        if self._provider is None:
            self.status_label.setText("Provider unavailable")
            return
        if self._credential_store.exists(GROQ_PROVIDER_ID, GROQ_CREDENTIAL_REF):
            self.key_edit.setPlaceholderText("Key saved locally — leave blank to keep it")
            self.status_label.setText("Configured — key saved locally")
        else:
            self.key_edit.setPlaceholderText("Paste Groq API key")
            self.status_label.setText("Not configured")

    def save_settings(self) -> None:
        if self._settings_invalid:
            self.status_label.setText("AI settings are invalid.")
            return
        entered_key = self.key_edit.text()
        try:
            if entered_key.strip():
                self._credential_store.write_secret(GROQ_PROVIDER_ID, GROQ_CREDENTIAL_REF, entered_key.strip())
            settings = self._current_settings()
            if settings is not None:
                self._settings_store.save(settings)
                self._loaded_settings = settings
        except Exception:
            self.status_label.setText("Save failed")
            return
        self.key_edit.clear()
        self._refresh_status()

    def remove_key(self) -> None:
        answer = QMessageBox.question(
            self,
            "Remove Groq key?",
            "Remove the locally saved Groq API key?",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        try:
            self._credential_store.delete_secret(GROQ_PROVIDER_ID, GROQ_CREDENTIAL_REF)
        except Exception:
            self.status_label.setText("Remove failed")
            return
        self.key_edit.clear()
        self._refresh_status()

    def test_connection(self) -> None:
        if self._test_in_progress:
            return
        if self._provider is None:
            self.status_label.setText("Provider unavailable")
            return
        if not self._credential_store.exists(GROQ_PROVIDER_ID, GROQ_CREDENTIAL_REF):
            self.status_label.setText("No key saved")
            return
        self._set_testing_controls(True)
        self.status_label.setText("Testing Groq...")

        def run_action() -> ai_platform.Availability:
            provider = self._provider_factory(self._credential_store)
            import asyncio

            return asyncio.run(provider.test_connection(GROQ_CREDENTIAL_REF))

        self._start_worker(run_action, self._finish_test_connection)

    def _finish_test_connection(self, result: ActionResult) -> None:
        self._set_testing_controls(False)
        if not result.ok or not isinstance(result.value, ai_platform.Availability):
            self.status_label.setText("Network unavailable")
            return
        availability = result.value
        if availability.state == ai_platform.AvailabilityState.AVAILABLE:
            self.status_label.setText("Connected")
        elif availability.state == ai_platform.AvailabilityState.CREDENTIAL_INVALID:
            self.status_label.setText("Invalid API key")
        elif availability.state == ai_platform.AvailabilityState.ACCESS_FORBIDDEN:
            self.status_label.setText("Access forbidden")
        elif availability.state == ai_platform.AvailabilityState.CREDENTIAL_MISSING:
            self.status_label.setText("No key saved")
        elif "rate" in availability.message.lower() or "quota" in availability.message.lower():
            self.status_label.setText("Rate limit / quota reached")
        elif "unexpected" in availability.message.lower():
            self.status_label.setText("Unexpected provider response")
        else:
            self.status_label.setText("Network unavailable")

    def _set_testing_controls(self, testing: bool) -> None:
        self._test_in_progress = testing
        self.test_button.setEnabled(not testing)
        self.close_button.setEnabled(not testing)
        self.save_button.setEnabled(not testing and not self._settings_invalid)
        self.remove_button.setEnabled(not testing)

    def closeEvent(self, event) -> None:
        if self._test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            event.ignore()
            return
        event.accept()

    def reject(self) -> None:
        if self._test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            return
        super().reject()

    def accept(self) -> None:
        if self._test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            return
        super().accept()

    def done(self, result: int) -> None:
        if self._test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            return
        super().done(result)

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



class BotSetupDialog(QDialog):
    def __init__(
        self,
        instance_id: str,
        instance_api=instance_store,
        config_api=config_store,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._instance_id = instance_id
        self._instance_api = instance_api
        self._config_api = config_api
        self._last_error = ""
        self.start_requested = False
        self.setWindowTitle(f"Setup Bot: {instance_id}")
        self.resize(760, 540)

        self.step_names = [
            "Manager Name",
            "Discord Application / Token",
            "Server Members Intent",
            "Access Settings",
            "Invite Bot",
            "Ready",
        ]
        self.current_step_label = QLabel("")
        self.current_step_label.setWordWrap(True)
        self.instructions_label = QLabel("Follow the steps. Use the round i buttons for help on each setting.")
        self.instructions_label.setWordWrap(True)

        self.display_name_edit = QLineEdit()
        self.instance_id_label = QLabel(instance_id)
        self.display_name_help_button = self._create_help_button("display_name")

        self.token_status_label = QLabel("")
        self.token_help_button = self._create_help_button("token")
        self.application_help_button = self._create_help_button("application")
        self.installation_help_button = self._create_help_button("installation")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("Paste Discord bot token here")
        self.show_token_checkbox = QCheckBox("Show token while editing")
        self.show_token_checkbox.toggled.connect(self._toggle_token_visibility)
        self.application_id_edit = QLineEdit()
        self.application_id_edit.setPlaceholderText("Discord Application ID, digits only")
        self.application_id_edit.textChanged.connect(lambda _text: self._update_invite_preview())
        self.open_portal_button = QPushButton("Open Discord Developer Portal")
        self.open_portal_button.clicked.connect(self.open_developer_portal)
        self.open_installation_button = QPushButton("Open Installation Settings")
        self.open_installation_button.clicked.connect(self.open_application_installation_page)

        self.intent_help_button = self._create_help_button("intent")
        self.intent_ack_checkbox = QCheckBox("I enabled Server Members Intent in Discord Developer Portal")
        self.open_intent_portal_button = QPushButton("Open Developer Portal")
        self.open_intent_portal_button.clicked.connect(self.open_application_bot_page)

        self.allow_admins_checkbox = QCheckBox("Allow Discord server administrators")
        self.allowed_users_edit = QLineEdit()
        self.allowed_users_edit.setPlaceholderText("Optional. Example: 123456789012345678, 987654321098765432")
        self.allowed_roles_edit = QLineEdit()
        self.allowed_roles_edit.setPlaceholderText("Optional. Example: 111111111111111111, 222222222222222222")
        self.audit_channel_edit = QLineEdit()
        self.audit_channel_edit.setPlaceholderText("Optional Discord channel ID")
        self.users_help_button = self._create_help_button("users")
        self.roles_help_button = self._create_help_button("roles")
        self.audit_help_button = self._create_help_button("audit")
        self.administrators_help_button = self._create_help_button("administrators")

        self.invite_link_edit = QLineEdit()
        self.invite_link_edit.setReadOnly(True)
        self.copy_invite_button = QPushButton("Copy Invite Link")
        self.open_invite_button = QPushButton("Open Invite Page")
        self.copy_invite_button.clicked.connect(self.copy_invite_link)
        self.open_invite_button.clicked.connect(self.open_invite_page)
        self.invited_ack_checkbox = QCheckBox("I invited the bot to my server")

        self.ready_summary_label = QLabel("")
        self.ready_summary_label.setWordWrap(True)
        self.status_label = QLabel("")

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_display_name_page())
        self.pages.addWidget(self._build_token_page())
        self.pages.addWidget(self._build_intent_page())
        self.pages.addWidget(self._build_access_page())
        self.pages.addWidget(self._build_invite_page())
        self.pages.addWidget(self._build_ready_page())
        self.pages.currentChanged.connect(self._on_page_changed)

        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.save_button = QPushButton("Save")
        self.start_button = QPushButton("Save && Start Bot")
        self.finish_button = QPushButton("Finish")
        self.back_button.clicked.connect(self.go_back)
        self.next_button.clicked.connect(self.go_next)
        self.save_button.clicked.connect(self.save_setup)
        self.start_button.clicked.connect(self.save_and_start)
        self.finish_button.clicked.connect(self.finish_setup)

        button_row = QHBoxLayout()
        button_row.addWidget(self.back_button)
        button_row.addWidget(self.next_button)
        button_row.addStretch(1)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.finish_button)

        layout = QVBoxLayout()
        layout.addWidget(self.current_step_label)
        layout.addWidget(self.instructions_label)
        layout.addWidget(self.pages)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self.load_setup()
        self._on_page_changed(0)

    @property
    def last_error(self) -> str:
        return self._last_error

    def _build_display_name_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "Choose the local name shown in DarkAbyss Bot Manager.\n"
            "This does not rename the Discord bot account. The technical instance ID remains unchanged."
        )
        description.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Instance ID", self.instance_id_label)
        form.addRow(self._help_label("Manager display name", self.display_name_help_button), self.display_name_edit)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_token_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "Create or select your Discord Application, open its Bot page, copy the bot token, and paste it here.\n"
            "Existing saved tokens are never displayed back. Leave the token field empty to keep the saved token.\n\n"
            "Installation mode is your choice: keep Public Bot = OFF for owner/dev-team-only installs, or set Public Bot = ON when another server owner needs to use the generated invite link."
        )
        description.setWordWrap(True)
        form = QFormLayout()
        form.addRow(self._help_label("Discord Application ID", self.application_help_button), self.application_id_edit)
        form.addRow(self._help_label("Discord bot token", self.token_help_button), self.token_edit)
        form.addRow("", self.show_token_checkbox)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addWidget(self.token_status_label)
        layout.addLayout(form)
        layout.addWidget(self._help_row(self.open_portal_button, self.installation_help_button))
        layout.addWidget(self.open_installation_button)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_intent_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "The Admin bot needs Discord's privileged Server Members Intent because it reads member information.\n"
            "Manager cannot verify this Discord-side toggle locally, so confirm it only after enabling it in the portal."
        )
        description.setWordWrap(True)
        steps = QLabel(
            "Discord Developer Portal -> Application -> Bot -> Privileged Gateway Intents -> enable Server Members Intent."
        )
        steps.setWordWrap(True)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addWidget(self._help_row(steps, self.intent_help_button))
        layout.addWidget(self.intent_ack_checkbox)
        layout.addWidget(self.open_intent_portal_button)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_access_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "Choose who may use admin commands. Simple setup can leave user IDs, role IDs, and audit channel empty while allowing server administrators."
        )
        description.setWordWrap(True)
        form = QFormLayout()
        form.addRow("", self._help_row(self.allow_admins_checkbox, self.administrators_help_button))
        form.addRow(self._help_label("Allowed user IDs", self.users_help_button), self.allowed_users_edit)
        form.addRow(self._help_label("Allowed role IDs", self.roles_help_button), self.allowed_roles_edit)
        form.addRow(self._help_label("Audit channel ID", self.audit_help_button), self.audit_channel_edit)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_invite_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "Manager generates the invite link with the required granular permissions. It does not request Administrator permission.\n"
            "It targets Discord Guild Install. Private applications may require Installation -> Install Link = None; shareable installs may use Public Bot = ON.\n"
            "Click Open Invite Page, choose your server, review permissions, authorize, then return here."
        )
        description.setWordWrap(True)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addWidget(QLabel("Generated invite link"))
        layout.addWidget(self.invite_link_edit)
        invite_buttons = QHBoxLayout()
        invite_buttons.addWidget(self.copy_invite_button)
        invite_buttons.addWidget(self.open_invite_button)
        invite_buttons.addStretch(1)
        layout.addLayout(invite_buttons)
        layout.addWidget(self.invited_ack_checkbox)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_ready_page(self) -> QWidget:
        page = QWidget()
        description = QLabel(
            "Review the setup status. Local items are verified by Manager. Discord portal/invite items are user-confirmed external steps."
        )
        description.setWordWrap(True)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addWidget(self.ready_summary_label)
        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def load_setup(self) -> None:
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            snapshot = self._config_api.get_config_snapshot(self._instance_id)
        except (instance_store.InstanceStoreError, config_store.ConfigStoreError) as exc:
            self._set_error(str(exc))
            return

        effective = snapshot.effective
        self.display_name_edit.setText(instance.display_name)
        self.token_edit.clear()
        self.token_status_label.setText(token_status_text(instance.paths.token))
        self.allow_admins_checkbox.setChecked(bool(effective.get("allow_server_administrators", True)))
        self.allowed_users_edit.setText(", ".join(str(value) for value in effective.get("allowed_user_ids", [])))
        self.allowed_roles_edit.setText(", ".join(str(value) for value in effective.get("allowed_role_ids", [])))
        audit_channel_id = effective.get("audit_channel_id")
        self.audit_channel_edit.setText("" if audit_channel_id is None else str(audit_channel_id))
        self._update_invite_preview()
        self._update_ready_summary()
        self._set_status("Loaded setup.")

    def save_setup(self) -> bool:
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            display_name = self.display_name_edit.text().strip()
            token = self.token_edit.text().strip()
            overrides = dict(self._config_api.get_config_snapshot(self._instance_id).overrides)
            overrides.update(
                {
                    "allow_server_administrators": self.allow_admins_checkbox.isChecked(),
                    "allowed_user_ids": parse_id_list(self.allowed_users_edit.text()),
                    "allowed_role_ids": parse_id_list(self.allowed_roles_edit.text()),
                    "audit_channel_id": parse_optional_id(self.audit_channel_edit.text()),
                }
            )
            self._instance_api.update_instance_display_name(self._instance_id, display_name)
            self._config_api.save_config_overrides(self._instance_id, overrides)
            if token:
                token_path = _ensure_safe_token_path(instance)
                _atomic_write_text(token_path, f"{token}\n")
        except (ValueError, OSError, instance_store.InstanceStoreError, config_store.ConfigStoreError) as exc:
            self._set_error(str(exc))
            return False
        self._set_status("Setup saved.")
        self.load_setup()
        return True

    def save_and_start(self) -> None:
        if not self.save_setup():
            return
        if not self._token_configured():
            self._set_error("Token is missing. Paste the Discord bot token before starting the bot.")
            return
        self.start_requested = True
        self.accept()

    def finish_setup(self) -> None:
        if not self.save_setup():
            return
        self.start_requested = False
        self.accept()

    def go_back(self) -> None:
        index = self.pages.currentIndex()
        if index > 0:
            self.pages.setCurrentIndex(index - 1)

    def go_next(self) -> None:
        index = self.pages.currentIndex()
        if index < self.pages.count() - 1:
            self.pages.setCurrentIndex(index + 1)

    def open_developer_portal(self) -> None:
        QDesktopServices.openUrl(QUrl(DISCORD_DEVELOPER_PORTAL_URL))

    def open_application_bot_page(self) -> None:
        application_id = self.application_id_edit.text().strip()
        if application_id.isdigit():
            QDesktopServices.openUrl(QUrl(f"{DISCORD_DEVELOPER_PORTAL_URL}/{application_id}/bot"))
            return
        QDesktopServices.openUrl(QUrl(DISCORD_DEVELOPER_PORTAL_URL))

    def open_application_installation_page(self) -> None:
        application_id = self.application_id_edit.text().strip()
        if application_id.isdigit():
            QDesktopServices.openUrl(QUrl(f"{DISCORD_DEVELOPER_PORTAL_URL}/{application_id}/installation"))
            return
        QDesktopServices.openUrl(QUrl(DISCORD_DEVELOPER_PORTAL_URL))

    def copy_invite_link(self) -> None:
        try:
            invite_url = build_discord_invite_url(self.application_id_edit.text())
        except ValueError as exc:
            self._set_error(str(exc))
            return
        QApplication.clipboard().setText(invite_url)
        self._set_status("Invite link copied.")

    def open_invite_page(self) -> None:
        try:
            invite_url = build_discord_invite_url(self.application_id_edit.text())
        except ValueError as exc:
            self._set_error(str(exc))
            return
        QDesktopServices.openUrl(QUrl(invite_url))

    def _toggle_token_visibility(self, checked: bool) -> None:
        self.token_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _create_help_button(self, topic: str) -> QToolButton:
        button = QToolButton()
        button.setText("i")
        button.setFixedSize(22, 22)
        button.setStyleSheet("QToolButton { border: 1px solid palette(mid); border-radius: 11px; font-weight: bold; }")
        button.setToolTip(SETUP_HELP_TEXT[topic][1])
        button.setAccessibleName(f"{SETUP_HELP_TEXT[topic][0]} help")
        button.clicked.connect(lambda _checked=False, selected_topic=topic: self._show_setup_help(selected_topic))
        return button

    def _help_label(self, text: str, button: QToolButton) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(text))
        layout.addWidget(button)
        layout.addStretch(1)
        widget.setLayout(layout)
        return widget

    def _help_row(self, content: QWidget, button: QToolButton) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(content)
        layout.addWidget(button)
        layout.addStretch(1)
        widget.setLayout(layout)
        return widget

    def _show_setup_help(self, topic: str) -> None:
        title, message = SETUP_HELP_TEXT[topic]
        QMessageBox.information(self, title, message)

    def _on_page_changed(self, index: int) -> None:
        self.current_step_label.setText(f"Step {index + 1} of {len(self.step_names)}: {self.step_names[index]}")
        self.back_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.pages.count() - 1)
        final_page = index == self.pages.count() - 1
        self.next_button.setVisible(not final_page)
        self.finish_button.setVisible(final_page)
        self.finish_button.setEnabled(final_page)
        self.start_button.setVisible(final_page)
        self.start_button.setEnabled(final_page)
        if index == 4:
            self._update_invite_preview()
        if final_page:
            self._update_ready_summary()

    def _update_invite_preview(self) -> None:
        try:
            invite_url = build_discord_invite_url(self.application_id_edit.text())
        except ValueError as exc:
            self.invite_link_edit.setText(f"Incomplete: {exc}")
            return
        self.invite_link_edit.setText(invite_url)

    def _token_configured(self) -> bool:
        if self.token_edit.text().strip():
            return True
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            token = instance.paths.token.read_text(encoding="utf-8")
        except (OSError, instance_store.InstanceStoreError):
            return False
        return not is_placeholder_token(token)

    def _update_ready_summary(self) -> None:
        display_name = self.display_name_edit.text().strip()
        display_name_configured = bool(display_name)
        application_id_valid = self.application_id_edit.text().strip().isdigit()
        token_configured = self._token_configured()
        invite_ready = application_id_valid
        lines = [
            "LOCAL VERIFIED:",
            f"{'[OK]' if display_name_configured else '[MISSING]'} Manager display name" + (f": {display_name}" if display_name_configured else ""),
            f"{'[OK]' if token_configured else '[MISSING]'} Token configured locally" + ("" if token_configured else " - paste token before starting"),
            f"{'[OK]' if application_id_valid else '[MISSING]'} Application ID entered" + ("" if application_id_valid else " - required for invite link"),
            f"{'[OK]' if invite_ready else '[MISSING]'} Invite link ready" + ("" if invite_ready else " - enter Application ID"),
            "[OK] Access settings are edited in this wizard and saved locally when you click Save",
            "",
            "USER-CONFIRMED DISCORD STEPS:",
            f"{'[OK]' if self.intent_ack_checkbox.isChecked() else '[ACTION]'} Server Members Intent user-confirmed external step" + ("" if self.intent_ack_checkbox.isChecked() else " - Manager cannot verify this automatically"),
            f"{'[OK]' if self.invited_ack_checkbox.isChecked() else '[ACTION]'} Bot invited user-confirmed external step" + ("" if self.invited_ack_checkbox.isChecked() else " - open invite page and authorize the bot"),
        ]
        self.ready_summary_label.setText("\n".join(lines))

    def _set_status(self, message: str) -> None:
        self._last_error = ""
        self.status_label.setText(message)

    def _set_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(f"Error: {message}")


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
        self.setup_button = QPushButton("Setup Bot")
        self.edit_config_button = QPushButton("Advanced JSON...")
        self.edit_config_button.setToolTip("Advanced/developer settings. Normal bot setup does not require editing JSON.")
        self.create_admin_button = QPushButton("Add Bot")
        self.ai_providers_button = QPushButton("AI Providers...")

        self.refresh_button.clicked.connect(self.refresh_instances)
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button.clicked.connect(self.stop_selected)
        self.restart_button.clicked.connect(self.restart_selected)
        self.setup_button.clicked.connect(self.setup_selected_bot)
        self.edit_config_button.clicked.connect(self.edit_selected_config)
        self.create_admin_button.clicked.connect(self.create_admin_instance)
        self.ai_providers_button.clicked.connect(self.open_ai_providers)

        button_row = QHBoxLayout()
        for button in (
            self.refresh_button,
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.setup_button,
            self.edit_config_button,
            self.create_admin_button,
            self.ai_providers_button,
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

    def setup_selected_bot(self) -> None:
        instance_id = self.selected_instance_id()
        if instance_id is None:
            self._show_error("Select an instance first.")
            return
        dialog = BotSetupDialog(instance_id, self._instance_api, self._config_api, self)
        accepted = dialog.exec()
        self.refresh_instances()
        if accepted == QDialog.Accepted and dialog.start_requested:
            self._select_instance_by_id(instance_id)
            self.start_selected()

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
        self._set_status(f"Created bot {instance_id}. Opening setup...")
        self.refresh_instances()
        self._select_instance_by_id(instance_id)
        self.setup_selected_bot()

    def open_ai_providers(self) -> None:
        dialog = AIProviderSettingsDialog(parent=self)
        dialog.exec()

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
            if self._select_instance_by_id(instance_id):
                return
        if self.instance_table.rowCount() > 0 and not self.instance_table.selectionModel().selectedRows():
            self.instance_table.selectRow(0)

    def _select_instance_by_id(self, instance_id: str) -> bool:
        for row_index in range(self.instance_table.rowCount()):
            item = self.instance_table.item(row_index, 0)
            if item is not None and item.text() == instance_id:
                self.instance_table.selectRow(row_index)
                return True
        return False

    def _update_selected_details(self) -> None:
        instance_id = self.selected_instance_id()
        info = next((item for item in self._last_infos if item.instance_id == instance_id), None)
        self.details_view.setPlainText("" if info is None else instance_info_details(info))
        self._update_buttons()

    def _update_buttons(self) -> None:
        instance_id = self.selected_instance_id()
        busy = instance_id in self._busy_instances if instance_id is not None else False
        has_selection = instance_id is not None
        for button in (self.start_button, self.stop_button, self.restart_button, self.setup_button, self.edit_config_button):
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


def main(manager: manager_core.BotProcessManager | None = None) -> int:
    app = QApplication(sys.argv)
    try:
        bootstrap_for_gui()
    except Exception as exc:
        QMessageBox.critical(None, "DarkAbyss Bot Manager", f"Startup failed: {exc}")
        return 1
    window = ManagerMainWindow(manager=manager)
    window.resize(980, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
