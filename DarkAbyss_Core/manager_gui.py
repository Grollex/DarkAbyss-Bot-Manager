from __future__ import annotations

import json
import os
import sys
import tempfile
from urllib.parse import urlencode
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QInputDialog,
    QTabBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import admin_instance
import ai_platform
import app_paths
import manager_dashboard as dash
import manager_game_presence
import manager_groups
import manager_terminal
import runtime_layout
import bot_registry
import config_store
import instance_store
import manager_core


REFRESH_INTERVAL_MS = 1500
GROQ_PROFILE_ID = "groq-default"
GROQ_PROVIDER_ID = "groq"
GROQ_CREDENTIAL_REF = "groq-default"
GEMINI_PROFILE_ID = "gemini-default"
GEMINI_PROVIDER_ID = "gemini"
GEMINI_CREDENTIAL_REF = "gemini-default"

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
    # AI-6 server management (still never Administrator). Discord only lets the
    # bot grant/overwrite permissions it holds itself, so common member
    # permissions are included for roles and channel overwrites it creates.
    "Create Invite": 1 << 0,
    "Manage Server": 1 << 5,
    "Add Reactions": 1 << 6,
    "View Audit Log": 1 << 7,
    "Video": 1 << 9,
    "Embed Links": 1 << 14,
    "Attach Files": 1 << 15,
    "Mention Everyone": 1 << 17,
    "Use External Emojis": 1 << 18,
    "Connect": 1 << 20,
    "Speak": 1 << 21,
    "Mute Members": 1 << 22,
    "Deafen Members": 1 << 23,
    "Move Members": 1 << 24,
    "Use Voice Activity": 1 << 25,
    "Change Nickname": 1 << 26,
    "Manage Nicknames": 1 << 27,
    "Manage Webhooks": 1 << 29,
    "Manage Expressions": 1 << 30,
    "Use Application Commands": 1 << 31,
    "Manage Events": 1 << 33,
    "Manage Threads": 1 << 34,
    "Create Public Threads": 1 << 35,
    "Create Private Threads": 1 << 36,
    "Send Messages in Threads": 1 << 38,
    "Send Polls": 1 << 49,
    "Pin Messages": 1 << 51,
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
    "ai_users": (
        "AI allowed user IDs",
        "Discord user IDs, которым разрешена команда /ai.\n\n"
        "AI-доступ выдаётся только явно: Discord Administrator и списки доступа для /execute его НЕ дают.\n"
        "Если оба AI-списка пустые, /ai не может использовать никто.\n\n"
        "Рекомендуется выдавать AI-доступ через роль (AI allowed role IDs), а не через отдельных пользователей.",
    ),
    "ai_channel": (
        "AI control channel ID",
        "ID одного текстового канала, где Kairo отвечает на обычные сообщения без /ai.\n\n"
        "- Работает только для пользователей/ролей из AI allowed user IDs / AI allowed role IDs; остальным доступ не появляется.\n"
        "- Discord Administrator и списки /execute доступ к AI не дают.\n"
        "- Если поле заполнено, бот запрашивает Message Content Intent: включи его в Discord Developer Portal "
        "(Application -> Bot -> Privileged Gateway Intents -> Message Content Intent), иначе бот не подключится.\n"
        "- После включения/выключения нужно перезапустить бота.\n"
        "- Пустое поле = обычные сообщения AI не обрабатывает (/ai продолжает работать).\n"
        "Это не audit channel.",
    ),
    "ai_roles": (
        "AI allowed role IDs",
        "Discord role IDs, участникам которых разрешена команда /ai.\n\n"
        "Это рекомендуемый способ выдачи AI-доступа. Discord Administrator сам по себе доступ к /ai не даёт.\n"
        "Как получить role ID: Developer Mode → Server Settings → Roles → правый клик → Copy Role ID.",
    ),
}

AI_CONTROL_CHANNEL_EXPLANATION = (
    "AI control channel: Kairo answers normal messages in this one channel (AI whitelist still required). "
    "Needs Message Content Intent enabled in Discord Developer Portal and a bot restart. Leave empty to disable."
)
AI_BEHAVIOUR_EXPLANATION = (
    "AI confirmations: 'plan once' shows the AI's plan with one Approve button, then runs its normal actions; "
    "deletions, bans, kicks, purges and permission changes still ask separately with the exact data. "
    "'Every change' asks before each step. Reading message text (purge by text/links, chat summaries) needs "
    "Message Content Intent in the Developer Portal and a bot restart. @mention requests: AI-allowed users "
    "ping the bot or its role in the listed channels (or anywhere); others get a short refusal. Needs "
    "Message Content Intent and a bot restart."
)


def parse_optional_channel_id(text: str, label: str) -> str | None:
    value = text.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"{label} must contain digits only.")
    return value


AI_ACCESS_EXPLANATION = (
    "AI access requires an explicit user or role. Discord Administrator alone does not grant /ai access, "
    "and the /execute lists above do not grant it either. Prefer an AI role. Leave both empty to disable /ai."
)


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


def create_gemini_provider(credential_store: ai_platform.CredentialStore):
    try:
        import ai_gemini
    except Exception as exc:
        raise ai_platform.AIPlatformError("Provider unavailable.") from exc
    return ai_gemini.GeminiProvider(credential_store)


def _profile_by_id(settings: ai_platform.AISettings, profile_id: str) -> ai_platform.AIProfile | None:
    return next((profile for profile in settings.profiles if profile.profile_id == profile_id), None)


def _routing_with_default(routing: ai_platform.RoutingConfig, profile_id: str) -> ai_platform.RoutingConfig:
    """Fill only UNSET routing slots with ``profile_id``; assigned slots are kept.

    Without this, saving only a Gemini key (or saving Gemini before Groq) left
    every task class unrouted and /ai answered "No usable AI profile".
    """
    return replace(
        routing,
        routine_profile_id=routing.routine_profile_id or profile_id,
        planner_profile_id=routing.planner_profile_id or profile_id,
        creative_profile_id=routing.creative_profile_id or profile_id,
    )


class AIProviderSettingsDialog(QDialog):
    def __init__(
        self,
        settings_store: ai_platform.AISettingsStore | None = None,
        credential_store: ai_platform.CredentialStore | None = None,
        provider_factory: Callable[[ai_platform.CredentialStore], object] = create_groq_provider,
        gemini_provider_factory: Callable[[ai_platform.CredentialStore], object] = create_gemini_provider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Providers")
        self._settings_store = settings_store or ai_platform.AISettingsStore()
        self._credential_store = credential_store or ai_platform.CredentialStore()
        self._provider_factory = provider_factory
        self._gemini_provider_factory = gemini_provider_factory
        self._worker_handles: list[_WorkerHandle] = []
        self._provider = None
        self._gemini_provider = None
        self._settings_invalid = False
        self._loaded_settings = ai_platform.AISettings()
        self._test_in_progress = False
        self._gemini_test_in_progress = False

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
        self.gemini_status_label = QLabel("")
        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setEchoMode(QLineEdit.Password)
        self.gemini_show_key_checkbox = QCheckBox("Show key while editing")
        self.gemini_model_combo = QComboBox()
        self.gemini_reasoning_combo = QComboBox()
        self.gemini_reasoning_combo.addItems(["low", "medium", "high"])
        self.gemini_save_button = QPushButton("Save")
        self.gemini_test_button = QPushButton("Test Connection")
        self.gemini_remove_button = QPushButton("Remove Key")
        # AI-6 two-stage routing: a (stronger) planner picks the tools, a
        # (cheaper) executor runs them. Stored as PLANNER vs ROUTINE/CREATIVE.
        self.planner_combo = QComboBox()
        self.executor_combo = QComboBox()
        for combo in (self.planner_combo, self.executor_combo):
            combo.addItem("Groq", GROQ_PROFILE_ID)
            combo.addItem("Gemini", GEMINI_PROFILE_ID)
        self.routing_fallback_checkbox = QCheckBox("If the selected engine fails, try the other one")
        self.routing_save_button = QPushButton("Save Routing")
        self.routing_status_label = QLabel("")
        self.routing_status_label.setWordWrap(True)

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

        groq_tab_layout = QVBoxLayout()
        groq_tab_layout.addLayout(form)
        groq_tab_layout.addLayout(button_row)
        groq_tab = QWidget()
        groq_tab.setLayout(groq_tab_layout)

        gemini_form = QFormLayout()
        gemini_form.addRow("Status:", self.gemini_status_label)
        gemini_form.addRow("API Key:", self.gemini_key_edit)
        gemini_form.addRow("", self.gemini_show_key_checkbox)
        gemini_form.addRow("Model:", self.gemini_model_combo)
        gemini_form.addRow("Reasoning:", self.gemini_reasoning_combo)

        gemini_button_row = QHBoxLayout()
        for button in (self.gemini_save_button, self.gemini_test_button, self.gemini_remove_button):
            gemini_button_row.addWidget(button)
        gemini_button_row.addStretch(1)

        gemini_tab_layout = QVBoxLayout()
        gemini_tab_layout.addLayout(gemini_form)
        gemini_tab_layout.addLayout(gemini_button_row)
        gemini_tab = QWidget()
        gemini_tab.setLayout(gemini_tab_layout)

        routing_form = QFormLayout()
        routing_form.addRow("Planning (reasoning):", self.planner_combo)
        routing_form.addRow("Execution (tool calls):", self.executor_combo)
        routing_form.addRow("", self.routing_fallback_checkbox)
        routing_help = QLabel(
            "Every /ai request is first planned by the planning engine (it sees only a short tool catalog), "
            "then executed by the execution engine with just the tools from the plan. "
            "A stronger model for planning and a faster/cheaper one for execution works well. "
            "Both engines need a saved API key."
        )
        routing_help.setWordWrap(True)
        routing_button_row = QHBoxLayout()
        routing_button_row.addWidget(self.routing_save_button)
        routing_button_row.addStretch(1)
        routing_tab_layout = QVBoxLayout()
        routing_tab_layout.addWidget(routing_help)
        routing_tab_layout.addLayout(routing_form)
        routing_tab_layout.addWidget(self.routing_status_label)
        routing_tab_layout.addLayout(routing_button_row)
        routing_tab_layout.addStretch(1)
        routing_tab = QWidget()
        routing_tab.setLayout(routing_tab_layout)

        self.provider_tabs = QTabWidget()
        self.provider_tabs.addTab(groq_tab, "Groq")
        self.provider_tabs.addTab(gemini_tab, "Gemini")
        self.provider_tabs.addTab(routing_tab, "Routing")

        layout = QVBoxLayout()
        layout.addWidget(self.provider_tabs)
        layout.addWidget(self.close_button)
        self.setLayout(layout)

        self.show_key_checkbox.toggled.connect(self._toggle_key_visibility)
        self.gemini_show_key_checkbox.toggled.connect(self._toggle_gemini_key_visibility)
        self.save_button.clicked.connect(self.save_settings)
        self.test_button.clicked.connect(self.test_connection)
        self.remove_button.clicked.connect(self.remove_key)
        self.gemini_save_button.clicked.connect(self.save_gemini_settings)
        self.gemini_test_button.clicked.connect(self.test_gemini_connection)
        self.gemini_remove_button.clicked.connect(self.remove_gemini_key)
        self.routing_save_button.clicked.connect(self.save_routing)
        self.close_button.clicked.connect(self.accept)

        self._load_provider_metadata()
        self._load_gemini_provider_metadata()
        self._load_settings()
        self._load_gemini_settings()
        self._load_routing()
        self._refresh_status()
        self._refresh_gemini_status()

    def _toggle_key_visibility(self, checked: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _toggle_gemini_key_visibility(self, checked: bool) -> None:
        self.gemini_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

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

    def _load_gemini_provider_metadata(self) -> None:
        self.gemini_model_combo.clear()
        try:
            self._gemini_provider = self._gemini_provider_factory(self._credential_store)
            models = self._gemini_provider.metadata.models
        except Exception:
            self._gemini_provider = None
            self.gemini_status_label.setText("Provider unavailable")
            self.gemini_test_button.setEnabled(False)
            return
        for model in models:
            self.gemini_model_combo.addItem(model.display_name, model.model_id)

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

    def _load_gemini_settings(self) -> None:
        if self._settings_invalid:
            return
        profile = _profile_by_id(self._loaded_settings, GEMINI_PROFILE_ID)
        if profile is None:
            profile = _profile_by_id(ai_platform.default_gemini_settings(), GEMINI_PROFILE_ID)
        if profile is None:
            return
        model_index = self.gemini_model_combo.findData(profile.model_id)
        if model_index >= 0:
            self.gemini_model_combo.setCurrentIndex(model_index)
        reasoning = str(profile.options.get("reasoning_effort", "medium"))
        reasoning_index = self.gemini_reasoning_combo.findText(reasoning)
        self.gemini_reasoning_combo.setCurrentIndex(reasoning_index if reasoning_index >= 0 else 1)

    def _load_routing(self) -> None:
        if self._settings_invalid:
            self.routing_save_button.setEnabled(False)
            self.routing_status_label.setText("AI settings are invalid.")
            return
        routing = self._loaded_settings.routing
        executor = routing.routine_profile_id or GROQ_PROFILE_ID
        planner = routing.planner_profile_id or executor
        for combo, profile_id in ((self.planner_combo, planner), (self.executor_combo, executor)):
            index = combo.findData(profile_id)
            combo.setCurrentIndex(index if index >= 0 else 0)
        self.routing_fallback_checkbox.setChecked(bool(routing.routine_fallback_profile_ids))
        self.routing_status_label.setText(self._routing_summary(routing))

    @staticmethod
    def _routing_summary(routing: ai_platform.RoutingConfig) -> str:
        names = {GROQ_PROFILE_ID: "Groq", GEMINI_PROFILE_ID: "Gemini"}
        planner = names.get(routing.planner_profile_id or "", routing.planner_profile_id or "not set")
        executor = names.get(routing.routine_profile_id or "", routing.routine_profile_id or "not set")
        return f"Current: planning = {planner}, execution = {executor}."

    def _current_routing_settings(self) -> ai_platform.AISettings:
        planner = str(self.planner_combo.currentData())
        executor = str(self.executor_combo.currentData())
        profiles = list(self._loaded_settings.profiles)
        defaults = {
            GROQ_PROFILE_ID: ai_platform.default_groq_settings(),
            GEMINI_PROFILE_ID: ai_platform.default_gemini_settings(),
        }
        for profile_id in {planner, executor}:
            if _profile_by_id(self._loaded_settings, profile_id) is None:
                default_profile = _profile_by_id(defaults[profile_id], profile_id)
                if default_profile is not None:
                    profiles.append(default_profile)

        def other(profile_id: str) -> tuple[str, ...]:
            if not self.routing_fallback_checkbox.isChecked():
                return ()
            return (GEMINI_PROFILE_ID,) if profile_id == GROQ_PROFILE_ID else (GROQ_PROFILE_ID,)

        routing = ai_platform.RoutingConfig(
            routine_profile_id=executor,
            planner_profile_id=planner,
            creative_profile_id=executor,
            routine_fallback_profile_ids=other(executor),
            planner_fallback_profile_ids=other(planner),
            creative_fallback_profile_ids=other(executor),
        )
        return ai_platform.AISettings(profiles=tuple(profiles), routing=routing)

    def save_routing(self) -> None:
        if self._settings_invalid:
            self.routing_status_label.setText("AI settings are invalid.")
            return
        try:
            settings = self._current_routing_settings()
            self._settings_store.save(settings)
            self._loaded_settings = settings
        except Exception:
            self.routing_status_label.setText("Save failed")
            return
        self.routing_status_label.setText("Saved. " + self._routing_summary(settings.routing))

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
        return ai_platform.AISettings(
            profiles=preserved_profiles + (profile,),
            routing=_routing_with_default(self._loaded_settings.routing, GROQ_PROFILE_ID),
        )

    def _current_gemini_settings(self) -> ai_platform.AISettings | None:
        current_model = self.gemini_model_combo.currentData()
        existing_gemini = _profile_by_id(self._loaded_settings, GEMINI_PROFILE_ID)
        if current_model is None:
            if existing_gemini is None:
                return None
            model_id = existing_gemini.model_id
        else:
            model_id = str(current_model)
        reasoning = self.gemini_reasoning_combo.currentText() or "medium"
        profile = ai_platform.AIProfile(
            profile_id=GEMINI_PROFILE_ID,
            provider_id=GEMINI_PROVIDER_ID,
            model_id=str(model_id),
            credential_ref=GEMINI_CREDENTIAL_REF,
            options={"reasoning_effort": reasoning},
        )
        preserved_profiles = tuple(item for item in self._loaded_settings.profiles if item.profile_id != GEMINI_PROFILE_ID)
        return ai_platform.AISettings(
            profiles=preserved_profiles + (profile,),
            routing=_routing_with_default(self._loaded_settings.routing, GEMINI_PROFILE_ID),
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

    def _refresh_gemini_status(self) -> None:
        if self._settings_invalid:
            self.gemini_status_label.setText("AI settings are invalid.")
            return
        if self._gemini_provider is None:
            self.gemini_status_label.setText("Provider unavailable")
            return
        if self._credential_store.exists(GEMINI_PROVIDER_ID, GEMINI_CREDENTIAL_REF):
            self.gemini_key_edit.setPlaceholderText("Key saved locally — leave blank to keep it")
            self.gemini_status_label.setText("Configured — key saved locally")
        else:
            self.gemini_key_edit.setPlaceholderText("Paste Gemini API key")
            self.gemini_status_label.setText("Not configured")

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

    def save_gemini_settings(self) -> None:
        if self._settings_invalid:
            self.gemini_status_label.setText("AI settings are invalid.")
            return
        entered_key = self.gemini_key_edit.text()
        try:
            if entered_key.strip():
                self._credential_store.write_secret(GEMINI_PROVIDER_ID, GEMINI_CREDENTIAL_REF, entered_key.strip())
            settings = self._current_gemini_settings()
            if settings is not None:
                self._settings_store.save(settings)
                self._loaded_settings = settings
        except Exception:
            self.gemini_status_label.setText("Save failed")
            return
        self.gemini_key_edit.clear()
        self._refresh_gemini_status()

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

    def remove_gemini_key(self) -> None:
        answer = QMessageBox.question(
            self,
            "Remove Gemini key?",
            "Remove the locally saved Gemini API key?",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        try:
            self._credential_store.delete_secret(GEMINI_PROVIDER_ID, GEMINI_CREDENTIAL_REF)
        except Exception:
            self.gemini_status_label.setText("Remove failed")
            return
        self.gemini_key_edit.clear()
        self._refresh_gemini_status()

    def test_connection(self) -> None:
        if self._test_in_progress or self._gemini_test_in_progress:
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

    def test_gemini_connection(self) -> None:
        if self._gemini_test_in_progress or self._test_in_progress:
            return
        if self._gemini_provider is None:
            self.gemini_status_label.setText("Provider unavailable")
            return
        if not self._credential_store.exists(GEMINI_PROVIDER_ID, GEMINI_CREDENTIAL_REF):
            self.gemini_status_label.setText("No key saved")
            return
        self._set_gemini_testing_controls(True)
        self.gemini_status_label.setText("Testing Gemini...")

        def run_action() -> ai_platform.Availability:
            provider = self._gemini_provider_factory(self._credential_store)
            import asyncio

            return asyncio.run(provider.test_connection(GEMINI_CREDENTIAL_REF))

        self._start_worker(run_action, self._finish_gemini_test_connection)

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

    def _finish_gemini_test_connection(self, result: ActionResult) -> None:
        self._set_gemini_testing_controls(False)
        self._set_availability_status(self.gemini_status_label, result)

    def _set_availability_status(self, label: QLabel, result: ActionResult) -> None:
        if not result.ok or not isinstance(result.value, ai_platform.Availability):
            label.setText("Network unavailable")
            return
        availability = result.value
        if availability.state == ai_platform.AvailabilityState.AVAILABLE:
            label.setText("Connected")
        elif availability.state == ai_platform.AvailabilityState.CREDENTIAL_INVALID:
            label.setText("Invalid API key")
        elif availability.state == ai_platform.AvailabilityState.ACCESS_FORBIDDEN:
            label.setText("Access forbidden")
        elif availability.state == ai_platform.AvailabilityState.CREDENTIAL_MISSING:
            label.setText("No key saved")
        elif "rate" in availability.message.lower() or "quota" in availability.message.lower():
            label.setText("Rate limit / quota reached")
        elif "unexpected" in availability.message.lower():
            label.setText("Unexpected provider response")
        else:
            label.setText("Network unavailable")

    def _set_testing_controls(self, testing: bool) -> None:
        self._test_in_progress = testing
        self.test_button.setEnabled(not testing)
        self.gemini_test_button.setEnabled(not testing and not self._gemini_test_in_progress)
        self.close_button.setEnabled(not testing and not self._gemini_test_in_progress)
        self.save_button.setEnabled(not testing and not self._settings_invalid)
        self.remove_button.setEnabled(not testing)

    def _set_gemini_testing_controls(self, testing: bool) -> None:
        self._gemini_test_in_progress = testing
        self.gemini_test_button.setEnabled(not testing)
        self.test_button.setEnabled(not testing and not self._test_in_progress)
        self.close_button.setEnabled(not testing and not self._test_in_progress)
        self.gemini_save_button.setEnabled(not testing and not self._settings_invalid)
        self.gemini_remove_button.setEnabled(not testing)

    def closeEvent(self, event) -> None:
        if self._test_in_progress or self._gemini_test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            self.gemini_status_label.setText("Test connection is still in progress")
            event.ignore()
            return
        event.accept()

    def reject(self) -> None:
        if self._test_in_progress or self._gemini_test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            self.gemini_status_label.setText("Test connection is still in progress")
            return
        super().reject()

    def accept(self) -> None:
        if self._test_in_progress or self._gemini_test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            self.gemini_status_label.setText("Test connection is still in progress")
            return
        super().accept()

    def done(self, result: int) -> None:
        if self._test_in_progress or self._gemini_test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            self.gemini_status_label.setText("Test connection is still in progress")
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
        self.ai_allowed_users_edit = QLineEdit()
        self.ai_allowed_users_edit.setPlaceholderText("Optional. Explicit /ai user IDs")
        self.ai_allowed_roles_edit = QLineEdit()
        self.ai_allowed_roles_edit.setPlaceholderText("Recommended. Explicit /ai role IDs")
        self.ai_users_help_button = self._create_help_button("ai_users")
        self.ai_roles_help_button = self._create_help_button("ai_roles")
        self.ai_access_label = QLabel(AI_ACCESS_EXPLANATION)
        self.ai_access_label.setWordWrap(True)
        self.ai_control_channel_edit = QLineEdit()
        self.ai_control_channel_edit.setPlaceholderText("Optional. Channel ID for natural AI messages (empty = off)")
        self.ai_channel_help_button = self._create_help_button("ai_channel")
        self.ai_control_channel_label = QLabel(AI_CONTROL_CHANNEL_EXPLANATION)
        self.ai_control_channel_label.setWordWrap(True)
        self.ai_confirmation_combo = QComboBox()
        self.ai_confirmation_combo.addItem("Approve the AI plan once (recommended)", "plan")
        self.ai_confirmation_combo.addItem("Approve every change separately", "strict")
        self.ai_read_content_checkbox = QCheckBox("AI can read message text (needs Message Content Intent)")
        self.ai_mention_checkbox = QCheckBox("Answer when someone pings the bot or its role (@Kairo)")
        self.ai_mention_channels_edit = QLineEdit()
        self.ai_mention_channels_edit.setPlaceholderText("Channel IDs for @mentions; empty = every channel the bot can read")
        self.ai_behaviour_label = QLabel(AI_BEHAVIOUR_EXPLANATION)
        self.ai_behaviour_label.setWordWrap(True)

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
        ai_form = QFormLayout()
        ai_form.addRow(self._help_label("AI allowed user IDs", self.ai_users_help_button), self.ai_allowed_users_edit)
        ai_form.addRow(self._help_label("AI allowed role IDs", self.ai_roles_help_button), self.ai_allowed_roles_edit)
        ai_form.addRow(self._help_label("AI control channel ID", self.ai_channel_help_button), self.ai_control_channel_edit)
        ai_form.addRow("AI confirmations", self.ai_confirmation_combo)
        ai_form.addRow("", self.ai_read_content_checkbox)
        ai_form.addRow("", self.ai_mention_checkbox)
        ai_form.addRow("@mention channels", self.ai_mention_channels_edit)
        layout = QVBoxLayout()
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addWidget(self.ai_access_label)
        layout.addLayout(ai_form)
        layout.addWidget(self.ai_control_channel_label)
        layout.addWidget(self.ai_behaviour_label)
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
        self.ai_allowed_users_edit.setText(", ".join(str(value) for value in effective.get("ai_allowed_user_ids", [])))
        self.ai_allowed_roles_edit.setText(", ".join(str(value) for value in effective.get("ai_allowed_role_ids", [])))
        ai_control_channel_id = effective.get("ai_control_channel_id")
        self.ai_control_channel_edit.setText("" if ai_control_channel_id is None else str(ai_control_channel_id))
        mode_index = self.ai_confirmation_combo.findData(effective.get("ai_confirmation_mode", "plan"))
        self.ai_confirmation_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        self.ai_read_content_checkbox.setChecked(effective.get("ai_read_message_content") is True)
        self.ai_mention_checkbox.setChecked(effective.get("ai_mention_enabled") is True)
        self.ai_mention_channels_edit.setText(", ".join(str(value) for value in effective.get("ai_mention_channel_ids", [])))
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
            # Explicit AI whitelist fields: written when the user set a value or an
            # override already exists (so clearing works), never auto-populated.
            for key, edit_widget in (
                ("ai_allowed_user_ids", self.ai_allowed_users_edit),
                ("ai_allowed_role_ids", self.ai_allowed_roles_edit),
            ):
                ai_ids = parse_id_list(edit_widget.text())
                if ai_ids or key in overrides:
                    overrides[key] = ai_ids
            ai_channel = parse_optional_channel_id(self.ai_control_channel_edit.text(), "AI control channel ID")
            if ai_channel is not None or "ai_control_channel_id" in overrides:
                overrides["ai_control_channel_id"] = ai_channel
            # Program defaults are "plan" and false: write only real choices or
            # existing overrides, like the AI whitelist fields above.
            confirmation_mode = str(self.ai_confirmation_combo.currentData() or "plan")
            if confirmation_mode != "plan" or "ai_confirmation_mode" in overrides:
                overrides["ai_confirmation_mode"] = confirmation_mode
            read_content = self.ai_read_content_checkbox.isChecked()
            if read_content or "ai_read_message_content" in overrides:
                overrides["ai_read_message_content"] = read_content
            mention_enabled = self.ai_mention_checkbox.isChecked()
            if mention_enabled or "ai_mention_enabled" in overrides:
                overrides["ai_mention_enabled"] = mention_enabled
            mention_channels = parse_id_list(self.ai_mention_channels_edit.text())
            if mention_channels or "ai_mention_channel_ids" in overrides:
                overrides["ai_mention_channel_ids"] = mention_channels
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

        self._provider_tests: dict[str, tuple[bool, str]] = {}
        self._provider_tests_running: set[str] = set()
        self._previous_states: dict[str, str] = {}
        self.group_store = manager_groups.GroupStore(app_paths.CONFIG_DIR / manager_groups.FILE_NAME)
        self.group_tabs = QTabBar()
        self.group_tabs.setExpanding(False)
        self.group_tabs.setDrawBase(False)
        self.group_tabs.currentChanged.connect(lambda _index: self.refresh_instances())
        self.new_group_button = QPushButton("New Group...")
        self.move_group_button = QPushButton("Move to Group...")
        self.remove_group_button = QPushButton("Remove Group")
        self.new_group_button.clicked.connect(self.create_group)
        self.move_group_button.clicked.connect(self.move_selected_to_group)
        self.remove_group_button.clicked.connect(self.remove_current_group)
        self._reload_group_tabs()
        self._version_text = dash.app_version_text(runtime_layout)

        self.setWindowTitle("DarkAbyss Bot Manager")
        self.instance_table = QTableWidget(0, 5)
        self.instance_table.setHorizontalHeaderLabels(["Instance ID", "Display Name", "Bot Type", "Status", "PID"])
        self.instance_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.instance_table.setSelectionMode(QTableWidget.SingleSelection)
        self.instance_table.verticalHeader().setVisible(False)
        self.instance_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.instance_table.itemSelectionChanged.connect(self._update_selected_details)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")

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

        self.pages = QStackedWidget()
        self.nav_buttons: dict[str, QPushButton] = {}
        self._page_index: dict[str, int] = {}
        for name, builder in (
            ("dashboard", self._build_dashboard_page),
            ("bots", self._build_bots_page),
            ("ai", self._build_ai_page),
            ("terminal", self._build_terminal_page),
            ("presence", self._build_presence_page),
            ("commands", self._build_commands_page),
            ("logs", self._build_logs_page),
        ):
            self._page_index[name] = self.pages.addWidget(builder())

        root_layout = QHBoxLayout()
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_sidebar())
        content = QVBoxLayout()
        content.setContentsMargins(22, 18, 22, 10)
        content.addWidget(self.pages, 1)
        content.addWidget(self.status_label)
        root_layout.addLayout(content, 1)
        root = QWidget()
        root.setObjectName("root")
        root.setLayout(root_layout)
        self.setCentralWidget(root)
        self.show_page("dashboard")
        self.activity.add("Manager", "Manager started.")

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self.refresh_instances)
        if auto_refresh:
            self.refresh_timer.start()

        self.refresh_instances()
        self.refresh_ai_overview()

    # -- layout ------------------------------------------------------------

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        logo = QLabel(dash.LOGO_GLYPH)
        logo.setObjectName("brandLogo")
        title = QLabel("DarkAbyss")
        title.setObjectName("brandTitle")
        subtitle = dash.muted(f"Bot Manager · {self._version_text}")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand = QHBoxLayout()
        brand.addWidget(logo)
        brand.addLayout(brand_text, 1)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 16)
        layout.setSpacing(6)
        layout.addLayout(brand)
        layout.addSpacing(18)
        for name, label in (
            ("dashboard", "\U0001f3e0   Dashboard"),
            ("bots", "\U0001f916   Bots"),
            ("ai", "\U0001f9e0   AI Providers"),
            ("terminal", "\U0001f4ac   AI Terminal"),
            ("presence", "\U0001f3ae   Game Presence"),
            ("commands", "∕   Commands && Tools"),
            ("logs", "\U0001f4c4   Logs"),
        ):
            button = dash.nav_button(label, lambda _checked=False, page=name: self.show_page(page))
            self.nav_buttons[name] = button
            layout.addWidget(button)
        settings_nav = dash.nav_button("⚙   Bot Setup", lambda _checked=False: self._open_setup_from_nav())
        settings_nav.setCheckable(False)
        layout.addWidget(settings_nav)
        layout.addStretch(1)

        status = QFrame()
        status.setObjectName("sideStatus")
        self.side_bot_dot = dash.dot("muted")
        self.side_bot_label = QLabel("Bot status: —")
        self.side_discord_label = dash.muted("Discord: —")
        self.side_uptime_label = dash.muted("Uptime: —")
        bot_row = QHBoxLayout()
        bot_row.addWidget(self.side_bot_dot)
        bot_row.addWidget(self.side_bot_label, 1)
        status_layout = QVBoxLayout(status)
        status_layout.setContentsMargins(14, 12, 14, 12)
        status_layout.addLayout(bot_row)
        status_layout.addWidget(self.side_discord_label)
        status_layout.addWidget(self.side_uptime_label)
        layout.addWidget(status)
        return sidebar

    @staticmethod
    def _scroll_page(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _build_dashboard_page(self) -> QWidget:
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(250)
        hero_title = QLabel("DarkAbyss")
        hero_title.setObjectName("heroTitle")
        hero_subtitle = QLabel("Bot Manager")
        hero_subtitle.setObjectName("heroSubtitle")
        tagline = QLabel(dash.APP_TAGLINE)
        tagline.setObjectName("heroTagline")
        chips = QHBoxLayout()
        chips.setSpacing(10)
        for text in dash.FEATURE_CHIPS:
            chip = QLabel(text)
            chip.setObjectName("chip")
            chips.addWidget(chip)
        chips.addStretch(1)
        quote = QLabel(dash.HERO_QUOTE)
        quote.setObjectName("heroQuote")
        quote.setAlignment(Qt.AlignRight | Qt.AlignTop)
        hero_left = QVBoxLayout()
        hero_left.setSpacing(2)
        hero_left.addWidget(hero_title)
        hero_left.addWidget(hero_subtitle)
        hero_left.addSpacing(10)
        hero_left.addWidget(tagline)
        hero_left.addStretch(1)
        hero_left.addLayout(chips)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(34, 26, 30, 22)
        hero_layout.addLayout(hero_left, 1)
        hero_layout.addWidget(quote, 0, Qt.AlignTop)

        self.discord_card = dash.StatCard("\U0001f3ae", "Discord")
        self.ai_card = dash.StatCard("\U0001f9e0", "AI Providers")
        self.commands_card = dash.StatCard("∕", "Commands")
        self.routing_card = dash.StatCard("\U0001f500", "AI Routing")
        self.discord_card.clicked.connect(lambda: self.show_page("bots"))
        self.ai_card.clicked.connect(lambda: self.show_page("ai"))
        self.commands_card.clicked.connect(lambda: self.show_page("commands"))
        self.routing_card.clicked.connect(lambda: self.open_ai_providers("Routing"))
        cards = QHBoxLayout()
        cards.setSpacing(14)
        for card in (self.discord_card, self.ai_card, self.commands_card, self.routing_card):
            cards.addWidget(card, 1)

        bots_panel = dash.Panel("\U0001f916", "Bots")
        manage_bots = bots_panel.add_header_button("Manage Bots")
        manage_bots.clicked.connect(lambda: self.show_page("bots"))
        avatar = QLabel(dash.LOGO_GLYPH)
        avatar.setObjectName("avatar")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(84, 84)
        self.bot_name_label = QLabel("No bot selected")
        self.bot_name_label.setObjectName("cardTitle")
        self.bot_state_dot = dash.dot("muted")
        self.bot_state_label = QLabel("Offline")
        state_row = QHBoxLayout()
        state_row.addWidget(self.bot_state_dot)
        state_row.addWidget(self.bot_state_label)
        state_row.addStretch(1)
        self.bot_facts_label = dash.muted("")
        self.bot_facts_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bot_text = QVBoxLayout()
        bot_text.setSpacing(4)
        bot_text.addWidget(self.bot_name_label)
        bot_text.addLayout(state_row)
        bot_text.addWidget(self.bot_facts_label)
        bot_text.addStretch(1)
        self.dashboard_toggle_button = QPushButton("▶  Start")
        self.dashboard_toggle_button.clicked.connect(self.toggle_selected_bot)
        dashboard_logs_button = QPushButton("\U0001f4c4  View Logs")
        dashboard_logs_button.clicked.connect(lambda: self.show_page("logs"))
        dashboard_setup_button = QPushButton("⚙  Settings")
        dashboard_setup_button.clicked.connect(self._open_setup_from_nav)
        bot_buttons = QVBoxLayout()
        for button in (self.dashboard_toggle_button, dashboard_logs_button, dashboard_setup_button):
            bot_buttons.addWidget(button)
        bot_buttons.addStretch(1)
        bot_row = QFrame()
        bot_row.setObjectName("row")
        bot_row_layout = QHBoxLayout(bot_row)
        bot_row_layout.setContentsMargins(14, 14, 14, 14)
        bot_row_layout.setSpacing(16)
        bot_row_layout.addWidget(avatar, 0, Qt.AlignTop)
        bot_row_layout.addLayout(bot_text, 1)
        bot_row_layout.addLayout(bot_buttons)
        bots_panel.body.addWidget(bot_row)

        activity_panel = dash.Panel("\U0001f4c3", "Recent Activity")
        view_all = activity_panel.add_header_button("View All Logs")
        view_all.clicked.connect(lambda: self.show_page("logs"))
        self.activity = dash.ActivityFeed(visible_rows=6)
        activity_panel.body.addWidget(self.activity)

        providers_panel = dash.Panel("\U0001f9e0", "AI Providers")
        configure = providers_panel.add_header_button("Configure")
        configure.clicked.connect(lambda: self.open_ai_providers())
        self.provider_rows: list[dash.ProviderRow] = []
        for row in self._make_provider_rows():
            providers_panel.body.addWidget(row)

        quick_panel = dash.Panel("⚡", "Quick Actions")
        self.quick_toggle_button = dash.styled_button("▶  Start Bot", "primary")
        self.quick_toggle_button.clicked.connect(self.toggle_selected_bot)
        quick_test_button = dash.styled_button("\U0001f9e0  Test AI Providers", "secondary")
        quick_test_button.clicked.connect(self.test_all_providers)
        quick_logs_button = dash.styled_button("\U0001f4c4  View Logs", "quick")
        quick_logs_button.clicked.connect(lambda: self.show_page("logs"))
        quick_setup_button = dash.styled_button("⚙  Bot Setup", "quick")
        quick_setup_button.clicked.connect(self._open_setup_from_nav)
        quick_grid = QGridLayout()
        quick_grid.setSpacing(12)
        quick_grid.addWidget(self.quick_toggle_button, 0, 0)
        quick_grid.addWidget(quick_test_button, 0, 1)
        quick_grid.addWidget(quick_logs_button, 1, 0)
        quick_grid.addWidget(quick_setup_button, 1, 1)
        quick_panel.body.addLayout(quick_grid)

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(bots_panel)
        left.addWidget(activity_panel, 1)
        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(providers_panel)
        right.addWidget(quick_panel)
        right.addStretch(1)
        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addLayout(left, 11)
        columns.addLayout(right, 9)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        layout.addWidget(hero)
        layout.addLayout(cards)
        layout.addLayout(columns)
        layout.addStretch(1)
        return self._scroll_page(inner)

    def _build_terminal_page(self) -> QWidget:
        self.terminal_panel = manager_terminal.TerminalPanel(self._terminal_bots)
        return self.terminal_panel

    def _build_presence_page(self) -> QWidget:
        self.presence_panel = manager_game_presence.GamePresencePanel(
            self._terminal_bots, self._config_api, self.restart_instance
        )
        return self.presence_panel

    def _presence_enabled(self, instance_id: str) -> bool:
        try:
            section = self._config_api.get_config_snapshot(instance_id).effective.get("game_presence")
        except Exception:
            return False
        return isinstance(section, dict) and section.get("enabled") is True

    def restart_instance(self, instance_id: str) -> None:
        """Restart one bot through the normal lifecycle path (used by module pages)."""
        if self.current_group() is not None:
            self._reload_group_tabs(select=None)
            self.group_tabs.setCurrentIndex(0)
            self.refresh_instances()
        if self._select_instance_by_id(instance_id):
            self.restart_selected()

    def _terminal_bots(self) -> list[tuple[str, str, manager_core.InstanceInfo]]:
        bots = []
        for info in self._last_infos:
            group = self.group_store.group_of(info.instance_id)
            bots.append((info.instance_id, f"{group} · {info.display_name}", info))
        return bots

    # -- bot groups (tabs) -----------------------------------------------------

    ALL_GROUPS_TAB = "All bots"

    def current_group(self) -> str | None:
        """Selected group tab; None = all bots."""
        index = self.group_tabs.currentIndex()
        data = self.group_tabs.tabData(index) if index >= 0 else None
        return data if isinstance(data, str) else None

    def _reload_group_tabs(self, select: str | None = None) -> None:
        current = select if select is not None else self.current_group()
        self.group_tabs.blockSignals(True)
        while self.group_tabs.count():
            self.group_tabs.removeTab(0)
        self.group_tabs.addTab(self.ALL_GROUPS_TAB)
        self.group_tabs.setTabData(0, None)
        for name in self.group_store.groups():
            index = self.group_tabs.addTab(name)
            self.group_tabs.setTabData(index, name)
        target = 0
        for index in range(self.group_tabs.count()):
            if self.group_tabs.tabData(index) == current:
                target = index
        self.group_tabs.setCurrentIndex(target)
        self.group_tabs.blockSignals(False)
        self.remove_group_button.setEnabled(current not in (None, manager_groups.DEFAULT_GROUP))

    def create_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New Group", "Group name (e.g. a person or a server):")
        if not ok:
            return
        try:
            created = self.group_store.add_group(name)
        except (manager_groups.GroupError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._reload_group_tabs(select=created)
        self.activity.add("Manager", f"Group {created} created.")
        self.refresh_instances()

    def move_selected_to_group(self) -> None:
        instance_id = self.selected_instance_id()
        if instance_id is None:
            self._show_error("Select a bot first.")
            return
        groups = self.group_store.groups()
        current = self.group_store.group_of(instance_id)
        group, ok = QInputDialog.getItem(
            self, "Move to Group", f"Group for {instance_id}:", groups, groups.index(current) if current in groups else 0, False
        )
        if not ok:
            return
        try:
            self.group_store.assign(instance_id, group)
        except (manager_groups.GroupError, OSError) as exc:
            self._show_error(str(exc))
            return
        self.activity.add("Manager", f"{instance_id} moved to group {group}.")
        self.refresh_instances()

    def remove_current_group(self) -> None:
        group = self.current_group()
        if group in (None, manager_groups.DEFAULT_GROUP):
            return
        answer = QMessageBox.question(
            self,
            "Remove group?",
            f"Remove group {group}? Its bots move to {manager_groups.DEFAULT_GROUP}; nothing else changes.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        try:
            self.group_store.remove_group(group)
        except (manager_groups.GroupError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._reload_group_tabs(select=manager_groups.DEFAULT_GROUP)
        self.refresh_instances()

    def _make_provider_rows(self) -> list[dash.ProviderRow]:
        rows = []
        for provider_id, name, logo, tab in (
            (GROQ_PROVIDER_ID, "Groq", "groq", "Groq"),
            (GEMINI_PROVIDER_ID, "Gemini", "✦", "Gemini"),
        ):
            row = dash.ProviderRow(provider_id, name, logo)
            row.test_button.clicked.connect(lambda _checked=False, pid=provider_id: self.test_provider(pid))
            row.settings_button.clicked.connect(lambda _checked=False, tab_name=tab: self.open_ai_providers(tab_name))
            self.provider_rows.append(row)
            rows.append(row)
        return rows

    def _build_bots_page(self) -> QWidget:
        title = QLabel("Bots")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "Every bot instance has its own token, config, logs and data. Select one to start, stop or set it up."
        )
        button_row = QHBoxLayout()
        for button in (
            self.refresh_button,
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.setup_button,
            self.edit_config_button,
            self.create_admin_button,
        ):
            button_row.addWidget(button)
        button_row.addStretch(1)
        group_row = QHBoxLayout()
        group_row.addWidget(self.group_tabs, 1)
        for button in (self.new_group_button, self.move_group_button, self.remove_group_button):
            group_row.addWidget(button)
        details_title = QLabel("Selected instance")
        details_title.setObjectName("cardTitle")
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 16, 18, 16)
        panel_layout.setSpacing(12)
        panel_layout.addLayout(group_row)
        panel_layout.addWidget(self.instance_table, 2)
        panel_layout.addLayout(button_row)
        panel_layout.addWidget(details_title)
        panel_layout.addWidget(self.details_view, 1)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(panel, 1)
        return page

    def _build_ai_page(self) -> QWidget:
        title = QLabel("AI Providers")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "Keys stay on this PC. The planning engine reads a short tool catalog and writes the plan; "
            "the execution engine runs it with only the planned tools."
        )
        description.setWordWrap(True)
        routing_panel = dash.Panel("\U0001f500", "Routing")
        routing_button = routing_panel.add_header_button("Change Routing")
        routing_button.clicked.connect(lambda: self.open_ai_providers("Routing"))
        self.ai_routing_label = QLabel("")
        self.ai_routing_label.setWordWrap(True)
        routing_panel.body.addWidget(self.ai_routing_label)
        providers_panel = dash.Panel("\U0001f9e0", "Providers")
        providers_panel.header.addWidget(self.ai_providers_button)
        for row in self._make_provider_rows():
            providers_panel.body.addWidget(row)
        safety_panel = dash.Panel("\U0001f6e1", "How the AI acts on your server")
        safety = QLabel(
            "• Only Discord actions through the bot's tools: no Windows commands, files or browser.\n"
            "• Access: AI allowed user/role IDs in Bot Setup (Discord admins do not get AI automatically).\n"
            "• Confirmations: 'plan once' (default) or 'every change' in Bot Setup; deletions, bans, kicks "
            "and permission changes always ask with exact data.\n"
            "• Nobody can use the bot to act on roles or members at or above their own highest role, "
            "and Administrator is never granted."
        )
        safety.setWordWrap(True)
        safety_panel.body.addWidget(safety)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(routing_panel)
        layout.addWidget(providers_panel)
        layout.addWidget(safety_panel)
        layout.addStretch(1)
        return self._scroll_page(inner)

    def _build_commands_page(self) -> QWidget:
        title = QLabel("Commands & Tools")
        title.setObjectName("heroSubtitle")
        self.tool_count, tool_groups = dash.ai_tool_summary()
        description = dash.muted(
            f"Slash commands: {', '.join(dash.SLASH_COMMANDS)}. The AI assistant can use {self.tool_count} "
            "Discord tools; destructive ones always ask for confirmation."
        )
        description.setWordWrap(True)
        self.tools_tree = QTreeWidget()
        self.tools_tree.setHeaderLabels(["Tool", "Risk", "What it does"])
        self.tools_tree.setRootIsDecorated(True)
        self.tools_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tools_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        risk_colors = {"read": dash.COLORS["info"], "normal": dash.COLORS["ok"], "destructive": dash.COLORS["bad"]}
        for category, tools in tool_groups.items():
            parent = QTreeWidgetItem([f"{category} ({len(tools)})", "", ""])
            for name, risk, text in tools:
                child = QTreeWidgetItem([name, risk, text])
                child.setForeground(1, QColor(risk_colors.get(risk, dash.COLORS["muted"])))
                parent.addChild(child)
            self.tools_tree.addTopLevelItem(parent)
        self.tools_tree.expandAll()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self.tools_tree, 1)
        return page

    def _build_logs_page(self) -> QWidget:
        title = QLabel("Logs")
        title.setObjectName("heroSubtitle")
        self.log_instance_combo = QComboBox()
        self.log_instance_combo.currentIndexChanged.connect(lambda _index: self.refresh_logs())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh_logs)
        open_folder = QPushButton("\U0001f4c2  Open Folder")
        open_folder.clicked.connect(self.open_log_folder)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Bot:"))
        controls.addWidget(self.log_instance_combo, 1)
        controls.addWidget(refresh)
        controls.addWidget(open_folder)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt;")
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addLayout(controls)
        layout.addWidget(self.log_view, 1)
        return page

    # -- navigation and dashboard state ------------------------------------

    def show_page(self, name: str) -> None:
        index = self._page_index.get(name)
        if index is None:
            return
        self.pages.setCurrentIndex(index)
        for page_name, button in self.nav_buttons.items():
            button.setChecked(page_name == name)
        if name == "logs":
            self.refresh_logs()
        if name == "terminal":
            self.terminal_panel.refresh_targets()
        if name == "presence":
            self.presence_panel.refresh()

    def _open_setup_from_nav(self) -> None:
        if self.selected_instance_id() is None and self.instance_table.rowCount() > 0:
            self.instance_table.selectRow(0)
        self.setup_selected_bot()

    def _selected_info(self) -> manager_core.InstanceInfo | None:
        instance_id = self.selected_instance_id()
        info = next((item for item in self._last_infos if item.instance_id == instance_id), None)
        return info or (self._last_infos[0] if self._last_infos else None)

    def toggle_selected_bot(self) -> None:
        info = self._selected_info()
        if info is None:
            self._show_error("Add a bot first.")
            return
        self._select_instance_by_id(info.instance_id)
        if info.state == manager_core.STATE_RUNNING:
            self.stop_selected()
        else:
            self.start_selected()

    def _record_state_changes(self, infos: list[manager_core.InstanceInfo]) -> None:
        for info in infos:
            previous = self._previous_states.get(info.instance_id)
            if previous is not None and previous != info.state:
                if info.state == manager_core.STATE_RUNNING:
                    self.activity.add("Bot", f"{info.display_name} started (PID {info.pid}).")
                elif info.exit_code not in (None, 0):
                    self.activity.add("Error", f"{info.display_name} stopped with exit code {info.exit_code}. See Logs.")
                else:
                    self.activity.add("Bot", f"{info.display_name} stopped.")
            self._previous_states[info.instance_id] = info.state

    def _refresh_dashboard(self) -> None:
        infos = self._last_infos
        running = [info for info in infos if info.state == manager_core.STATE_RUNNING]
        info = self._selected_info()
        if info is None:
            connection, color = "No bot", "muted"
        else:
            connection, color = dash.bot_connection_state(info)
        self.discord_card.update_card(
            connection if info is not None else "No bot yet",
            color,
            f"{len(running)} of {len(infos)} bot(s) running",
        )
        tool_text = f"{self.tool_count} AI tools" if self.tool_count else "AI tools unavailable"
        self.commands_card.update_card(f"{len(dash.SLASH_COMMANDS)} slash commands", "accent", tool_text)

        is_running = info is not None and info.state == manager_core.STATE_RUNNING
        toggle_text = "■  Stop Bot" if is_running else "▶  Start Bot"
        self.quick_toggle_button.setText(toggle_text)
        self.dashboard_toggle_button.setText("■  Stop" if is_running else "▶  Start")
        busy = info is not None and info.instance_id in self._busy_instances
        self.quick_toggle_button.setEnabled(info is not None and not busy)
        self.dashboard_toggle_button.setEnabled(info is not None and not busy)
        if info is None:
            self.bot_name_label.setText("No bot yet")
            self.bot_state_label.setText("Use Bots → Add Bot")
            dash.set_dot_color(self.bot_state_dot, "muted")
            self.bot_facts_label.setText("")
        else:
            self.bot_name_label.setText(info.display_name)
            self.bot_state_label.setText(connection)
            dash.colored(self.bot_state_label, color)
            dash.set_dot_color(self.bot_state_dot, color)
            facts = [
                f"ID: {info.instance_id}",
                f"Type: {info.bot_type_display_name} {info.bot_version}",
                f"Commands: {len(dash.SLASH_COMMANDS)} slash · {self.tool_count} AI tools",
            ]
            facts.append(f"Modules: AI assistant · Game Presence {'on' if self._presence_enabled(info.instance_id) else 'off'}")
            if info.pid is not None:
                facts.append(f"PID: {info.pid}")
            if info.uptime_seconds is not None:
                facts.append(f"Uptime: {format_uptime(info.uptime_seconds)}")
            self.bot_facts_label.setText("\n".join(facts))

        self.side_bot_label.setText(f"Bot status: {'Online' if connection == 'Online' else ('Running' if is_running else 'Offline')}")
        dash.set_dot_color(self.side_bot_dot, color if info is not None else "muted")
        self.side_discord_label.setText(f"Discord: {connection if is_running else 'not connected'}")
        uptime = format_uptime(info.uptime_seconds) if info is not None and info.uptime_seconds is not None else "—"
        self.side_uptime_label.setText(f"Uptime: {uptime}")
        self._refresh_log_choices()

    def refresh_ai_overview(self) -> None:
        overview = dash.provider_overview(
            ai_platform.AISettingsStore(),
            ai_platform.CredentialStore(),
            (
                (GROQ_PROVIDER_ID, GROQ_PROFILE_ID, GROQ_CREDENTIAL_REF),
                (GEMINI_PROVIDER_ID, GEMINI_PROFILE_ID, GEMINI_CREDENTIAL_REF),
            ),
        )
        names = {GROQ_PROFILE_ID: "Groq", GEMINI_PROFILE_ID: "Gemini"}
        providers = overview["providers"]
        configured = [name for pid, name in ((GROQ_PROVIDER_ID, "Groq"), (GEMINI_PROVIDER_ID, "Gemini")) if providers[pid]["configured"]]
        for row in self.provider_rows:
            facts = providers[row.provider_id]
            tested = self._provider_tests.get(row.provider_id)
            if row.provider_id in self._provider_tests_running:
                text, color = "Testing...", "warn"
            elif tested is not None:
                text, color = (tested[1], "ok") if tested[0] else (tested[1], "bad")
            elif facts["configured"]:
                text, color = "Configured", "info"
            else:
                text, color = "Not configured", "muted"
            row.set_state(text, color, facts["model"], facts["roles"])
            row.test_button.setEnabled(facts["configured"] and row.provider_id not in self._provider_tests_running)
        if not overview["valid"]:
            self.ai_card.update_card("Settings invalid", "bad", "Open AI Providers to fix")
        else:
            self.ai_card.update_card(
                f"{len(configured)} configured", "ok" if configured else "muted", ", ".join(configured) or "Add a Groq or Gemini key"
            )
        planner = names.get(overview["planner"] or "", overview["planner"] or "not set")
        executor = names.get(overview["executor"] or "", overview["executor"] or "not set")
        routed = bool(overview["planner"] and overview["executor"])
        self.routing_card.update_card(f"plan: {planner}", "accent" if routed else "muted", f"run: {executor}", "ok" if routed else "muted")
        self.ai_routing_label.setText(
            f"Planning (thinks): {planner}\nExecution (acts): {executor}\n"
            "Change it in AI Providers → Routing. Using one engine for both is fine too."
        )

    # -- AI provider tests ---------------------------------------------------

    def test_provider(self, provider_id: str) -> None:
        if provider_id in self._provider_tests_running:
            return
        credential_ref = GROQ_CREDENTIAL_REF if provider_id == GROQ_PROVIDER_ID else GEMINI_CREDENTIAL_REF
        factory = create_groq_provider if provider_id == GROQ_PROVIDER_ID else create_gemini_provider
        name = "Groq" if provider_id == GROQ_PROVIDER_ID else "Gemini"
        credential_store = ai_platform.CredentialStore()
        if not credential_store.exists(provider_id, credential_ref):
            self._set_light_error(f"{name}: no API key saved. Open AI Providers to add one.")
            return
        self._provider_tests_running.add(provider_id)
        self.refresh_ai_overview()

        def run_action() -> ai_platform.Availability:
            import asyncio

            return asyncio.run(factory(credential_store).test_connection(credential_ref))

        self._start_worker(run_action, lambda result: self._finish_provider_test(provider_id, name, result))

    def test_all_providers(self) -> None:
        credential_store = ai_platform.CredentialStore()
        started = False
        for provider_id, credential_ref in ((GROQ_PROVIDER_ID, GROQ_CREDENTIAL_REF), (GEMINI_PROVIDER_ID, GEMINI_CREDENTIAL_REF)):
            if credential_store.exists(provider_id, credential_ref):
                self.test_provider(provider_id)
                started = True
        if not started:
            self._set_light_error("No AI provider key is saved yet. Open AI Providers to add one.")

    def _finish_provider_test(self, provider_id: str, name: str, result: ActionResult) -> None:
        self._provider_tests_running.discard(provider_id)
        availability = result.value if result.ok else None
        if isinstance(availability, ai_platform.Availability) and availability.ok:
            self._provider_tests[provider_id] = (True, "Connected")
            self.activity.add("AI", f"{name} connection test successful.")
        else:
            state = getattr(availability, "state", None)
            reason = {
                ai_platform.AvailabilityState.CREDENTIAL_INVALID: "Invalid API key",
                ai_platform.AvailabilityState.ACCESS_FORBIDDEN: "Access forbidden",
                ai_platform.AvailabilityState.CREDENTIAL_MISSING: "No key saved",
            }.get(state, "Unavailable")
            self._provider_tests[provider_id] = (False, reason)
            self.activity.add("Error", f"{name} connection test failed: {reason}.")
        self.refresh_ai_overview()

    # -- logs ------------------------------------------------------------------

    def _refresh_log_choices(self) -> None:
        current = self.log_instance_combo.currentData()
        wanted = [(info.instance_id, info.display_name) for info in self._last_infos]
        existing = [self.log_instance_combo.itemData(index) for index in range(self.log_instance_combo.count())]
        if existing == [instance_id for instance_id, _name in wanted]:
            return
        self.log_instance_combo.blockSignals(True)
        self.log_instance_combo.clear()
        for instance_id, display_name in wanted:
            self.log_instance_combo.addItem(f"{display_name} ({instance_id})", instance_id)
        index = self.log_instance_combo.findData(current)
        self.log_instance_combo.setCurrentIndex(index if index >= 0 else 0)
        self.log_instance_combo.blockSignals(False)

    def _log_info(self) -> manager_core.InstanceInfo | None:
        instance_id = self.log_instance_combo.currentData()
        return next((info for info in self._last_infos if info.instance_id == instance_id), None)

    def refresh_logs(self) -> None:
        info = self._log_info()
        if info is None:
            self.log_view.setPlainText("No bot selected.")
            return
        stderr = dash.read_log_tail(info.stderr_log_path)
        stdout = dash.read_log_tail(info.stdout_log_path, 16 * 1024)
        self.log_view.setPlainText(
            f"=== {info.stderr_log_path.name} (Discord + errors) ===\n{stderr or '(empty)'}\n\n"
            f"=== {info.stdout_log_path.name} (bot messages) ===\n{stdout or '(empty)'}"
        )
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def open_log_folder(self) -> None:
        info = self._log_info()
        if info is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(info.logs_dir)))

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
        self._record_state_changes(infos)
        group = self.current_group()
        visible = infos if group is None else [info for info in infos if self.group_store.group_of(info.instance_id) == group]
        self.instance_table.setRowCount(len(visible))
        for row_index, info in enumerate(visible):
            for column_index, value in enumerate(instance_info_to_display_row(info)):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.instance_table.setItem(row_index, column_index, item)
        self._restore_selection(selected_id)
        self._set_status(f"Loaded {len(infos)} instance(s).")
        self._update_selected_details()
        self._refresh_dashboard()

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
        group = self.current_group()
        if group is not None:
            try:
                self.group_store.assign(instance_id, group)
            except (manager_groups.GroupError, OSError):
                pass
        self._set_status(f"Created bot {instance_id}. Opening setup...")
        self.refresh_instances()
        self._select_instance_by_id(instance_id)
        self.setup_selected_bot()

    def open_ai_providers(self, tab: str | None = None) -> None:
        dialog = AIProviderSettingsDialog(parent=self)
        if tab:
            tabs = getattr(dialog, "provider_tabs", None)
            if isinstance(tabs, QTabWidget):
                for index in range(tabs.count()):
                    if tabs.tabText(index) == tab:
                        tabs.setCurrentIndex(index)
                        break
        dialog.exec()
        # Keys, models or routing may have changed; earlier test results may be stale.
        self._provider_tests.clear()
        self.refresh_ai_overview()

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
        self.move_group_button.setEnabled(has_selection)
        self.remove_group_button.setEnabled(self.current_group() not in (None, manager_groups.DEFAULT_GROUP))
        if hasattr(self, "quick_toggle_button") and busy:
            # Disable the dashboard Start/Stop immediately while an action runs.
            self.quick_toggle_button.setEnabled(False)
            self.dashboard_toggle_button.setEnabled(False)

    def _set_status(self, message: str) -> None:
        self._last_error = ""
        self.status_label.setText(message)

    def _set_light_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(message)

    def _show_error(self, message: str) -> None:
        self._last_error = message
        self.status_label.setText(f"Error: {message}")
        if hasattr(self, "activity"):
            self.activity.add("Error", message.splitlines()[0][:160] if message else "Error")
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
    app.setStyleSheet(dash.THEME_QSS)
    window = ManagerMainWindow(manager=manager)
    window.setMinimumSize(1100, 720)
    window.resize(1400, 900)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
