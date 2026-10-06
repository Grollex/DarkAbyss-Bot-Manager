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
    QButtonGroup,
    QRadioButton,
    QSizePolicy,
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
import admin_terminal
import ai_connections
import ai_platform
import ai_providers
import ai_storage
import ai_usage
import app_paths
import app_updates
import bot_i18n
import game_presence
import manager_dashboard as dash
import manager_content_filter
import manager_game_presence
import manager_kairo
import manager_setup_state
import manager_stream_director
import stream_director_config
import manager_groups
import manager_terminal
import runtime_layout
import bot_registry
import config_store
import instance_store
import manager_core


REFRESH_INTERVAL_MS = 1500
UPDATE_FIRST_CHECK_MS = 4000
UPDATES_SOURCE_TEXT = "Updates are installed by the packaged app (started with Launcher.exe)."

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
ADMIN_BOT_TYPE_ID = "admin"
GAME_PRESENCE_BOT_TYPE_ID = "game_presence"
# Game Presence only reads presence/voice and posts suggestions with buttons.
DISCORD_GAME_PRESENCE_PERMISSIONS = DISCORD_PERMISSION_BITS["View Channels"] | DISCORD_PERMISSION_BITS["Send Messages"]
GAME_PRESENCE_INVITE_SCOPES = ("bot",)
STREAM_DIRECTOR_BOT_TYPE_ID = "stream_director"
# Stream Director: its card + thread, slash commands, and Discord events for the next stream.
DISCORD_STREAM_DIRECTOR_PERMISSIONS = (
    DISCORD_PERMISSION_BITS["View Channels"]
    | DISCORD_PERMISSION_BITS["Send Messages"]
    | DISCORD_PERMISSION_BITS["Embed Links"]
    | DISCORD_PERMISSION_BITS["Read Message History"]
    | DISCORD_PERMISSION_BITS["Create Public Threads"]
    | DISCORD_PERMISSION_BITS["Send Messages in Threads"]
    | DISCORD_PERMISSION_BITS["Manage Events"]
)
# Bot types that use AI connections (the AI page lists only these).
AI_BOT_TYPES = frozenset({"admin", "game_presence"})
# Bot types whose setup wizard is only name, token, intents and invite.
LIGHT_SETUP_BOT_TYPES = frozenset({"game_presence", "stream_director"})
# Language each type used before languages existed (old configs keep it).
DEFAULT_BOT_LANGUAGES = {"admin": "en", "game_presence": "ru", "stream_director": "en"}
LANGUAGE_HELP = (
    "Everything this bot writes in Discord: replies, errors, confirmations, buttons, automatic posts and AI answers. "
    "Each bot has its own language."
)


def default_bot_language(bot_type: str) -> str:
    return DEFAULT_BOT_LANGUAGES.get(bot_type, bot_i18n.DEFAULT_LANGUAGE)


def language_combo() -> QComboBox:
    combo = QComboBox()
    for code, label in bot_i18n.LANGUAGES.items():
        combo.addItem(label, code)
    combo.setToolTip(LANGUAGE_HELP)
    return combo


def select_language(combo: QComboBox, language: str) -> None:
    index = combo.findData(language)
    combo.setCurrentIndex(index if index >= 0 else 0)


def apply_notes(bot_type: str, *, token_changed: bool, running: bool) -> str:
    """When the saved setup takes effect (shown after Save)."""
    if bot_type == ADMIN_BOT_TYPE_ID:
        notes = ["Access, AI and language settings apply to the next command (no restart); slash command descriptions change after a restart."]
    elif bot_type == STREAM_DIRECTOR_BOT_TYPE_ID:
        notes = ["The language applies within 15 s while the bot runs; slash command descriptions change after a restart."]
    else:
        notes = ["The language applies within 15 s while the bot runs."]
    if token_changed:
        notes.append("Restart required: the bot is still connected with the old token." if running else "The new token is used when the bot starts.")
    return " ".join(notes)
# What each bot type can do (shown on the dashboard instead of module toggles).
BOT_TYPE_CAPABILITIES: dict[str, str] = {
    ADMIN_BOT_TYPE_ID: "Slash commands · AI assistant · AI terminal · @mentions",
    GAME_PRESENCE_BOT_TYPE_ID: "Game suggestions · Mute/Allow buttons · AI wording (optional)",
    STREAM_DIRECTOR_BOT_TYPE_ID: "Twitch sessions · moments · challenges · polls · inbox · community level · recaps",
}

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
    "gp_intents": (
        "Presence Intent + Server Members Intent",
        "The Game Presence bot reads who plays what (Presence Intent) and the members of your server (Server Members "
        "Intent). Both are privileged: enable them in Discord Developer Portal -> your Game Presence application -> Bot "
        "-> Privileged Gateway Intents. Message Content Intent is NOT needed.\n\n"
        "Manager cannot verify these Discord-side toggles. If one is missing the bot stops with a message naming it.",
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


def build_discord_invite_url(application_id: str, bot_type: str = ADMIN_BOT_TYPE_ID) -> str:
    client_id = validate_application_id(application_id)
    permissions, scopes = {
        GAME_PRESENCE_BOT_TYPE_ID: (DISCORD_GAME_PRESENCE_PERMISSIONS, GAME_PRESENCE_INVITE_SCOPES),
        STREAM_DIRECTOR_BOT_TYPE_ID: (DISCORD_STREAM_DIRECTOR_PERMISSIONS, DISCORD_BOT_INVITE_SCOPES),
    }.get(bot_type, (DISCORD_ADMIN_BOT_PERMISSIONS, DISCORD_BOT_INVITE_SCOPES))
    query = urlencode(
        {
            "client_id": client_id,
            "permissions": str(permissions),
            "scope": " ".join(scopes),
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


def create_provider(provider_id: str, credential_store: ai_platform.CredentialStore, usage_recorder: Callable | None = None):
    """Adapter from the provider catalog (ai_providers); no network on creation."""
    return ai_providers.create_provider(provider_id, credential_store, usage_recorder=usage_recorder)


def availability_text(result: ActionResult) -> str:
    """Short, sanitized Test Connection result (provider texts are never shown)."""
    if not result.ok or not isinstance(result.value, ai_platform.Availability):
        return "Network unavailable"
    availability = result.value
    if availability.state == ai_platform.AvailabilityState.AVAILABLE:
        return "Connected"
    if availability.state == ai_platform.AvailabilityState.CREDENTIAL_INVALID:
        return "Invalid API key"
    if availability.state == ai_platform.AvailabilityState.ACCESS_FORBIDDEN:
        return "Access forbidden"
    if availability.state == ai_platform.AvailabilityState.CREDENTIAL_MISSING:
        return "No key saved"
    message = (availability.message or "").lower()
    if "rate" in message or "quota" in message:
        return "Rate limit / quota reached"
    if "unexpected" in message:
        return "Unexpected provider response"
    return "Network unavailable"


class ConnectionDialog(QDialog):
    """Add or edit one provider connection: provider, name, key, model, options.

    The saved key is never shown back and a blank key field keeps it. Test
    Connection uses THIS connection's saved key and is counted as Manager usage.
    """

    def __init__(
        self,
        store: ai_connections.ConnectionStore,
        connection: ai_connections.Connection | None = None,
        *,
        provider_factory: Callable[..., object] = create_provider,
        usage_store: ai_usage.AIUsageStore | None = None,
        users_of: Callable[[str], list[str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self.connection = connection
        self._provider_factory = provider_factory
        self._usage_recorder = usage_store.recorder() if usage_store is not None else None
        self._users_of = users_of or (lambda _connection_id: [])
        self._worker_handles: list[_WorkerHandle] = []
        self._test_in_progress = False
        self._auto_name = connection is None
        self.removed = False
        self.changed = False
        self.setWindowTitle("Edit Connection" if connection is not None else "Add Connection")
        self.resize(560, 380)

        self.provider_combo = QComboBox()
        for provider_id in ai_providers.provider_ids():
            self.provider_combo.addItem(ai_providers.get_spec(provider_id).display_name, provider_id)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(ai_connections.MAX_NAME)
        self.name_edit.textEdited.connect(lambda _text: setattr(self, "_auto_name", False))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.show_key_checkbox = QCheckBox("Show key while editing")
        self.show_key_checkbox.toggled.connect(
            lambda checked: self.key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        self.key_help = dash.muted("")
        self.key_help.setWordWrap(True)
        self.model_combo = QComboBox()
        self.reasoning_combo = QComboBox()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.save_button = QPushButton("Save")
        self.test_button = QPushButton("Test Connection")
        self.remove_button = QPushButton("Remove Connection")
        self.close_button = QPushButton("Close")

        form = QFormLayout()
        form.addRow("Provider:", self.provider_combo)
        form.addRow("Name:", self.name_edit)
        form.addRow("API key:", self.key_edit)
        form.addRow("", self.show_key_checkbox)
        form.addRow("", self.key_help)
        form.addRow("Model:", self.model_combo)
        form.addRow("Reasoning:", self.reasoning_combo)
        form.addRow("Status:", self.status_label)
        buttons = QHBoxLayout()
        for button in (self.save_button, self.test_button, self.remove_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        note = dash.muted(
            "A connection is one provider account. Bots that use the same connection share its key and its limits; "
            "give a bot its own connection (own key) for separate limits."
        )
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addLayout(buttons)

        self.provider_combo.currentIndexChanged.connect(lambda _index: self._load_provider())
        self.save_button.clicked.connect(self.save)
        self.test_button.clicked.connect(self.test_connection)
        self.remove_button.clicked.connect(self.remove)
        self.close_button.clicked.connect(self.accept)

        if connection is not None:
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(max(self.provider_combo.findData(connection.provider_id), 0))
            self.provider_combo.blockSignals(False)
            self.provider_combo.setEnabled(False)  # the key belongs to that provider
            self.name_edit.setText(connection.name)
        self._load_provider()
        if connection is not None:
            index = self.model_combo.findData(connection.model_id)
            if index < 0:
                self.model_combo.addItem(connection.model_id, connection.model_id)
                index = self.model_combo.count() - 1
            self.model_combo.setCurrentIndex(index)
            reasoning = self.reasoning_combo.findText(str(connection.options.get("reasoning_effort", "medium")))
            self.reasoning_combo.setCurrentIndex(reasoning if reasoning >= 0 else 0)
        self.remove_button.setVisible(connection is not None)
        self._refresh_status()

    def _provider_id(self) -> str:
        return str(self.provider_combo.currentData())

    def _load_provider(self) -> None:
        provider_id = self._provider_id()
        spec = ai_providers.get_spec(provider_id)
        self.model_combo.clear()
        try:
            models = self._provider_factory(provider_id, self._store.credentials).metadata.models
        except Exception:
            models = ()
        for model in models:
            self.model_combo.addItem(model.display_name, model.model_id)
        self.reasoning_combo.clear()
        self.reasoning_combo.addItems(list(spec.reasoning_levels))
        medium = self.reasoning_combo.findText("medium")
        self.reasoning_combo.setCurrentIndex(medium if medium >= 0 else 0)
        self.reasoning_combo.setEnabled("reasoning_effort" in spec.options)
        self.key_help.setText(f"Where to get it: {spec.key_help}")
        if self._auto_name:
            try:
                taken = {item.name for item in self._store.load().connections}
            except ai_platform.AIPlatformError:
                taken = set()
            name = f"{spec.display_name} main"
            number = 2
            while name in taken:
                name = f"{spec.display_name} {number}"
                number += 1
            self.name_edit.setText(name)
        self._refresh_status()

    def _has_key(self) -> bool:
        return self.connection is not None and self._store.has_key(self.connection)

    def _refresh_status(self, prefix: str = "") -> None:
        spec = ai_providers.get_spec(self._provider_id())
        if self._has_key():
            self.key_edit.setPlaceholderText("Key saved locally — leave blank to keep it")
            text = "Configured — key saved locally"
        else:
            self.key_edit.setPlaceholderText(spec.key_placeholder)
            text = "No key saved yet"
        if self.model_combo.count() == 0:
            text = "Provider unavailable"
        self.status_label.setText(f"{prefix}{text}")
        self.test_button.setEnabled(self._has_key() and not self._test_in_progress)
        self.save_button.setEnabled(not self._test_in_progress)
        self.remove_button.setEnabled(not self._test_in_progress)

    def save(self) -> bool:
        provider_id = self._provider_id()
        spec = ai_providers.get_spec(provider_id)
        model = self.model_combo.currentData() or (self.connection.model_id if self.connection else spec.default_model)
        options = {"reasoning_effort": self.reasoning_combo.currentText()} if "reasoning_effort" in spec.options else {}
        try:
            connection_id = self.connection.connection_id if self.connection is not None else self._store.new_connection_id(provider_id)
            connection = ai_connections.Connection(connection_id, provider_id, self.name_edit.text(), str(model), options)
            self._store.upsert(connection, self.key_edit.text())
        except ai_connections.ConnectionsError as exc:
            self.status_label.setText(f"Not saved: {exc}")
            return False
        except (ai_platform.AIPlatformError, ValueError, OSError):
            self.status_label.setText("Save failed")
            return False
        self.connection = connection
        self.changed = True
        self._auto_name = False
        self.key_edit.clear()
        self.provider_combo.setEnabled(False)
        self.remove_button.setVisible(True)
        self.setWindowTitle("Edit Connection")
        self._refresh_status("Saved. ")
        return True

    def remove(self) -> bool:
        if self.connection is None or self._test_in_progress:
            return False
        users = self._users_of(self.connection.connection_id)
        if users:
            self.status_label.setText(f"Used by: {', '.join(users)}. Choose another connection there first.")
            return False
        answer = QMessageBox.question(
            self,
            "Remove connection?",
            f"Remove '{self.connection.name}' and its locally saved API key?",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return False
        try:
            self._store.remove(self.connection.connection_id)
        except ai_platform.AIPlatformError as exc:
            self.status_label.setText(f"Not removed: {exc}")
            return False
        self.removed = True
        self.changed = True
        self.accept()
        return True

    def test_connection(self) -> None:
        if self._test_in_progress:
            return
        if not self._has_key():
            self.status_label.setText("Save a key first")
            return
        connection = self.connection
        recorder = self._usage_recorder
        factory = self._provider_factory
        credentials = self._store.credentials
        self._set_testing(True)
        self.status_label.setText(f"Testing {connection.name}...")

        def run_action() -> ai_platform.Availability:
            import asyncio

            kwargs = {"usage_recorder": recorder} if recorder is not None else {}
            provider = factory(connection.provider_id, credentials, **kwargs)
            return asyncio.run(provider.test_connection(connection.connection_id))

        self._start_worker(run_action, self._finish_test_connection)

    def _finish_test_connection(self, result: ActionResult) -> None:
        self._set_testing(False)
        self.status_label.setText(availability_text(result))

    def _set_testing(self, testing: bool) -> None:
        self._test_in_progress = testing
        self.close_button.setEnabled(not testing)
        self.test_button.setEnabled(not testing and self._has_key())
        self.save_button.setEnabled(not testing)
        self.remove_button.setEnabled(not testing)

    def _testing_blocks_close(self) -> bool:
        if self._test_in_progress:
            self.status_label.setText("Test connection is still in progress")
            return True
        return False

    def closeEvent(self, event) -> None:
        if self._testing_blocks_close():
            event.ignore()
            return
        event.accept()

    def reject(self) -> None:
        if not self._testing_blocks_close():
            super().reject()

    def accept(self) -> None:
        if not self._testing_blocks_close():
            super().accept()

    def done(self, result: int) -> None:
        if not self._testing_blocks_close():
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


def creatable_bot_types() -> list[tuple[str, str]]:
    """(id, display name) of every registered bot type; Admin first."""
    try:
        types = bot_registry.discover_bot_types()
    except (bot_registry.BotRegistryError, OSError):
        return [(ADMIN_BOT_TYPE_ID, "Admin Bot")]
    ordered = sorted(types.values(), key=lambda item: (item.id != ADMIN_BOT_TYPE_ID, item.display_name))
    return [(item.id, item.display_name) for item in ordered]


BOT_TYPE_HINTS = {
    ADMIN_BOT_TYPE_ID: "Server administration with slash commands and the AI assistant.",
    GAME_PRESENCE_BOT_TYPE_ID: (
        "Suggests that members playing the same game get together. Needs its OWN Discord application and "
        "token (create a second application in the Developer Portal). AI wording is optional: it uses the "
        "Base Set or the bot's own connections (AI Providers)."
    ),
    STREAM_DIRECTOR_BOT_TYPE_ID: (
        "Turns every Twitch stream into a Discord community session: live card and thread, moments, challenges, "
        "polls, streamer inbox, community level and goals, recap. Needs its OWN Discord application and token; "
        "Twitch is connected on the Stream Director page. No AI and no privileged intents needed."
    ),
}


class CreateBotInstanceDialog(QDialog):
    """New bot instance: bot type + instance ID + optional display name."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Bot")
        self.type_combo = QComboBox()
        for type_id, display_name in creatable_bot_types():
            self.type_combo.addItem(display_name, type_id)
        self.type_hint = QLabel("")
        self.type_hint.setWordWrap(True)
        self.type_combo.currentIndexChanged.connect(lambda _index: self._update_hint())
        self.instance_id_edit = QLineEdit()
        self.instance_id_edit.setPlaceholderText("lowercase letters, digits, - or _ (e.g. game-presence)")
        self.display_name_edit = QLineEdit()

        form = QFormLayout()
        form.addRow("Bot type", self.type_combo)
        form.addRow("", self.type_hint)
        form.addRow("Instance ID", self.instance_id_edit)
        form.addRow("Display name (optional)", self.display_name_edit)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        self._update_hint()

    def _update_hint(self) -> None:
        self.type_hint.setText(BOT_TYPE_HINTS.get(self.type_combo.currentData(), ""))

    def values(self) -> tuple[str, str, str | None]:
        display_name = self.display_name_edit.text().strip()
        bot_type = self.type_combo.currentData() or ADMIN_BOT_TYPE_ID
        return bot_type, self.instance_id_edit.text().strip(), display_name or None



class BotSetupDialog(QDialog):
    def __init__(
        self,
        instance_id: str,
        instance_api=instance_store,
        config_api=config_store,
        parent: QWidget | None = None,
        is_running: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._instance_id = instance_id
        self._instance_api = instance_api
        self._config_api = config_api
        self._is_running = is_running or (lambda: False)
        self._last_error = ""
        self._saved_form: tuple | None = None
        self._loading = False
        self.start_requested = False
        self.setWindowTitle(f"Setup Bot: {instance_id}")
        self.resize(760, 540)
        # Steps depend on the bot type: a Game Presence bot has no admin access
        # settings (its options live on the Game Presence page).
        try:
            loaded_type = getattr(instance_api.load_instance(instance_id), "bot_type", ADMIN_BOT_TYPE_ID)
        except Exception:
            loaded_type = ADMIN_BOT_TYPE_ID
        self.bot_type = loaded_type if loaded_type in LIGHT_SETUP_BOT_TYPES else ADMIN_BOT_TYPE_ID
        self.is_game_presence = self.bot_type == GAME_PRESENCE_BOT_TYPE_ID
        self.is_stream_director = self.bot_type == STREAM_DIRECTOR_BOT_TYPE_ID
        # Name, token, intents and invite only; the bot's options live on its own page.
        self.is_light = self.bot_type in LIGHT_SETUP_BOT_TYPES

        if self.is_stream_director:
            self.step_names = [
                "Manager Name",
                "Discord Application / Token",
                "Gateway Intents (none privileged)",
                "Invite Bot",
                "Ready",
            ]
        elif self.is_game_presence:
            self.step_names = [
                "Manager Name",
                "Discord Application / Token",
                "Presence + Server Members Intents",
                "Invite Bot",
                "Ready",
            ]
        else:
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
        self.language_combo = language_combo()
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

        self.intent_help_button = self._create_help_button("gp_intents" if self.is_game_presence else "intent")
        self.intent_ack_checkbox = QCheckBox(
            "I enabled Presence Intent and Server Members Intent in Discord Developer Portal"
            if self.is_game_presence
            else (
                "I understand: no privileged intent is needed for Stream Director"
                if self.is_stream_director
                else "I enabled Server Members Intent in Discord Developer Portal"
            )
        )
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
        self.status_label.setWordWrap(True)
        # Saved / unsaved changes / just saved (and when it applies) / not saved.
        self.save_indicator = dash.SaveIndicator()

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_display_name_page())
        self.pages.addWidget(self._build_token_page())
        self.pages.addWidget(self._build_intent_page())
        if not self.is_light:
            self.pages.addWidget(self._build_access_page())
        self.invite_page = self._build_invite_page()
        self.pages.addWidget(self.invite_page)
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
        layout.addWidget(self.save_indicator)
        layout.addLayout(button_row)
        self.setLayout(layout)

        self.load_setup()
        self._on_page_changed(0)
        for edit in (
            self.display_name_edit,
            self.token_edit,
            self.application_id_edit,
            self.allowed_users_edit,
            self.allowed_roles_edit,
            self.audit_channel_edit,
            self.ai_allowed_users_edit,
            self.ai_allowed_roles_edit,
            self.ai_control_channel_edit,
            self.ai_mention_channels_edit,
        ):
            edit.textChanged.connect(lambda _text: self._update_dirty())
        for box in (
            self.intent_ack_checkbox,
            self.invited_ack_checkbox,
            self.allow_admins_checkbox,
            self.ai_read_content_checkbox,
            self.ai_mention_checkbox,
        ):
            box.toggled.connect(lambda _checked: self._update_dirty())
        for combo in (self.language_combo, self.ai_confirmation_combo):
            combo.currentIndexChanged.connect(lambda _index: self._update_dirty())

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
        form.addRow("Bot language", self.language_combo)
        language_note = dash.muted(LANGUAGE_HELP)
        language_note.setWordWrap(True)
        form.addRow("", language_note)
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
        if self.is_stream_director:
            description = QLabel(
                "Stream Director needs no privileged intent: slash commands and buttons work without intents, and it only "
                "counts messages in its own session thread (Message Content Intent stays OFF).\n"
                "Leave Presence, Server Members and Message Content intents disabled for this application."
            )
            steps = QLabel("Nothing to enable in the Developer Portal for intents. Continue to the invite step.")
        elif self.is_game_presence:
            description = QLabel(
                "The Game Presence bot needs two privileged intents of ITS OWN Discord application: Presence Intent "
                "(who plays what) and Server Members Intent. Message Content Intent is not needed.\n"
                "Manager cannot verify these Discord-side toggles locally, so confirm them only after enabling them."
            )
            steps = QLabel(
                "Discord Developer Portal -> your Game Presence application -> Bot -> Privileged Gateway Intents -> "
                "enable Presence Intent and Server Members Intent."
            )
        else:
            description = QLabel(
                "The Admin bot needs Discord's privileged Server Members Intent because it reads member information.\n"
                "Manager cannot verify this Discord-side toggle locally, so confirm it only after enabling it in the portal."
            )
            steps = QLabel(
                "Discord Developer Portal -> Application -> Bot -> Privileged Gateway Intents -> enable Server Members Intent."
            )
        description.setWordWrap(True)
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
        if self.is_stream_director:
            permissions_text = (
                "The Stream Director invite asks for View Channels, Send Messages, Embed Links, Read Message History, "
                "Create Public Threads, Send Messages in Threads and Manage Events (for /nextstream), plus slash commands. "
                "No moderation permissions.\n"
            )
        elif self.is_game_presence:
            permissions_text = (
                "The Game Presence invite asks only for View Channels and Send Messages (no slash commands, no "
                "moderation permissions).\n"
            )
        else:
            permissions_text = "Manager generates the invite link with the required granular permissions. It does not request Administrator permission.\n"
        description = QLabel(
            permissions_text
            + "It targets Discord Guild Install. Private applications may require Installation -> Install Link = None; shareable installs may use Public Bot = ON.\n"
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
        """Show what is stored now: config, token status and the wizard steps."""
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            snapshot = self._config_api.get_config_snapshot(self._instance_id)
        except (instance_store.InstanceStoreError, config_store.ConfigStoreError) as exc:
            self._set_error(str(exc))
            self.save_indicator.show_error(f"the saved setup could not be read ({exc}).")
            return
        self._loading = True
        try:
            self._fill_from(instance, snapshot.effective)
        finally:
            self._loading = False
        self._saved_form = self._form_state()
        self._last_error = ""
        self.status_label.setText("")
        self.save_indicator.show_clean()

    def _fill_from(self, instance: Any, effective: dict[str, Any]) -> None:
        self.display_name_edit.setText(instance.display_name)
        self.token_edit.clear()
        self.token_status_label.setText(token_status_text(instance.paths.token))
        try:
            language = bot_i18n.normalize_language(effective.get("language"), default_bot_language(self.bot_type))
        except bot_i18n.LanguageError:
            language = default_bot_language(self.bot_type)
        select_language(self.language_combo, language)
        steps = manager_setup_state.load(self._instance_id)
        self.application_id_edit.setText(steps.application_id)
        self.intent_ack_checkbox.setChecked(steps.intents_confirmed)
        self.invited_ack_checkbox.setChecked(steps.invited)
        if self.is_light:
            self._update_invite_preview()
            self._update_ready_summary()
            return
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

    def _form_state(self) -> tuple:
        return (
            self.display_name_edit.text().strip(),
            bool(self.token_edit.text().strip()),
            self.language_combo.currentData(),
            self.application_id_edit.text().strip(),
            self.intent_ack_checkbox.isChecked(),
            self.invited_ack_checkbox.isChecked(),
            self.allow_admins_checkbox.isChecked(),
            self.allowed_users_edit.text().strip(),
            self.allowed_roles_edit.text().strip(),
            self.audit_channel_edit.text().strip(),
            self.ai_allowed_users_edit.text().strip(),
            self.ai_allowed_roles_edit.text().strip(),
            self.ai_control_channel_edit.text().strip(),
            self.ai_confirmation_combo.currentData(),
            self.ai_read_content_checkbox.isChecked(),
            self.ai_mention_checkbox.isChecked(),
            self.ai_mention_channels_edit.text().strip(),
        )

    @property
    def dirty(self) -> bool:
        return self._saved_form is not None and self._form_state() != self._saved_form

    def _update_dirty(self) -> None:
        if self._loading or self._saved_form is None:
            return
        if self.dirty:
            self.save_indicator.show_dirty()
        elif self.save_indicator.state == "dirty":
            self.save_indicator.show_clean()

    def _save_steps(self) -> None:
        manager_setup_state.save(
            self._instance_id,
            manager_setup_state.SetupSteps(
                application_id=self.application_id_edit.text().strip(),
                intents_confirmed=self.intent_ack_checkbox.isChecked(),
                invited=self.invited_ack_checkbox.isChecked(),
            ),
        )

    def _saved(self, token_changed: bool) -> None:
        """Reload from storage, then say that it worked and when it applies."""
        self.load_setup()
        self.save_indicator.show_saved(apply_notes(self.bot_type, token_changed=token_changed, running=self._is_running()))

    def reject(self) -> None:
        if self.dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "This setup has unsaved changes. Close without saving them?",
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                return
        super().reject()

    def save_setup(self) -> bool:
        if self.is_light:
            return self._save_name_and_token()
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            display_name = self.display_name_edit.text().strip()
            token = self.token_edit.text().strip()
            overrides = dict(self._config_api.get_config_snapshot(self._instance_id).overrides)
            overrides.update(
                {
                    "language": self.language_combo.currentData() or default_bot_language(self.bot_type),
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
            self._save_steps()
        except (ValueError, OSError, instance_store.InstanceStoreError, config_store.ConfigStoreError) as exc:
            self._set_error(str(exc))
            self.save_indicator.show_error(str(exc))
            return False
        self._saved(token_changed=bool(token))
        return True

    def _save_name_and_token(self) -> bool:
        """Game Presence / Stream Director setup: display name, language and THIS bot's token
        (their other settings live on their own page)."""
        try:
            instance = self._instance_api.load_instance(self._instance_id)
            token = self.token_edit.text().strip()
            snapshot = self._config_api.get_config_snapshot(self._instance_id)
            language = self.language_combo.currentData() or default_bot_language(self.bot_type)
            current = bot_i18n.normalize_language(snapshot.effective.get("language"), default_bot_language(self.bot_type))
            self._instance_api.update_instance_display_name(self._instance_id, self.display_name_edit.text().strip())
            if language != current or "language" not in snapshot.overrides:
                self._config_api.save_config_overrides(self._instance_id, {**snapshot.overrides, "language": language})
            if token:
                _atomic_write_text(_ensure_safe_token_path(instance), f"{token}\n")
            self._save_steps()
        except (ValueError, OSError, instance_store.InstanceStoreError, config_store.ConfigStoreError, bot_i18n.LanguageError) as exc:
            self._set_error(str(exc))
            self.save_indicator.show_error(str(exc))
            return False
        self._saved(token_changed=bool(token))
        page = "Stream Director" if self.is_stream_director else "Game Presence"
        self.status_label.setText(f"Server, channel and the other settings of this bot: the {page} page.")
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
            invite_url = build_discord_invite_url(self.application_id_edit.text(), self.bot_type)
        except ValueError as exc:
            self._set_error(str(exc))
            return
        QApplication.clipboard().setText(invite_url)
        self._set_status("Invite link copied.")

    def open_invite_page(self) -> None:
        try:
            invite_url = build_discord_invite_url(self.application_id_edit.text(), self.bot_type)
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
        if self.pages.widget(index) is self.invite_page:
            self._update_invite_preview()
        if final_page:
            self._update_ready_summary()

    def _update_invite_preview(self) -> None:
        try:
            invite_url = build_discord_invite_url(self.application_id_edit.text(), self.bot_type)
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
            (
                "[NEXT] Server, channel, delays and cooldowns: Manager -> Game Presence page"
                if self.is_game_presence
                else (
                    "[NEXT] Stream channel, Twitch account and community options: Manager -> Stream Director page"
                    if self.is_stream_director
                    else "[OK] Access settings are edited in this wizard and saved locally when you click Save\n"
                    "[INFO] Social Awareness (optional): Manager -> Kairo page; asked once when the bot is started"
                )
            ),
            "",
            "USER-CONFIRMED DISCORD STEPS:",
            f"{'[OK]' if self.intent_ack_checkbox.isChecked() else '[ACTION]'} "
            + ("Presence Intent + Server Members Intent" if self.is_game_presence else ("No privileged intent" if self.is_stream_director else "Server Members Intent"))
            + " user-confirmed external step"
            + ("" if self.intent_ack_checkbox.isChecked() else " - Manager cannot verify this automatically"),
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

        # Shared provider connections (ai_connections) and Test Connection results.
        self.connection_store = ai_connections.ConnectionStore()
        self.manager_usage = ai_usage.AIUsageStore(ai_connections.manager_usage_path())
        self._connection_tests: dict[str, tuple[bool, str]] = {}
        self._connection_tests_running: set[str] = set()
        self._loaded_base: ai_connections.RouteSelection | None = None
        self._loaded_bot: tuple | None = None
        self.ai_bot_combo = QComboBox()
        self.ai_bot_combo.setMinimumWidth(260)
        self.ai_bot_combo.currentIndexChanged.connect(lambda _index: self.refresh_ai_overview())
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
        # Self-update from GitHub Releases: installed (packaged) app only.
        self.installed_app = app_updates.current_install()
        self.available_update: app_updates.AvailableUpdate | None = None
        self._update_check_running = False
        self._update_installing = False
        # One-time Social Awareness question when a Kairo bot is started (replaceable in tests).
        self.ask_social_awareness = manager_kairo.ask_social_awareness
        self._update_error = ""
        self._update_checked_at: datetime | None = None

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
        self.add_connection_button = QPushButton("＋  Add Connection")

        self.refresh_button.clicked.connect(self.refresh_instances)
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button.clicked.connect(self.stop_selected)
        self.restart_button.clicked.connect(self.restart_selected)
        self.setup_button.clicked.connect(self.setup_selected_bot)
        self.edit_config_button.clicked.connect(self.edit_selected_config)
        self.create_admin_button.clicked.connect(self.create_bot_instance)
        self.add_connection_button.clicked.connect(self.add_connection)

        self.pages = QStackedWidget()
        self.nav_buttons: dict[str, QPushButton] = {}
        self._page_index: dict[str, int] = {}
        for name, builder in (
            ("dashboard", self._build_dashboard_page),
            ("bots", self._build_bots_page),
            ("ai", self._build_ai_page),
            ("terminal", self._build_terminal_page),
            ("kairo", self._build_kairo_page),
            ("filter", self._build_filter_page),
            ("presence", self._build_presence_page),
            ("stream", self._build_stream_page),
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

        self.update_timer = QTimer(self)
        self.update_timer.setInterval(app_updates.CHECK_INTERVAL_SECONDS * 1000)
        self.update_timer.timeout.connect(lambda: self.check_for_updates(silent=True))
        if auto_refresh and self.installed_app is not None:
            self.update_timer.start()
            QTimer.singleShot(UPDATE_FIRST_CHECK_MS, lambda: self.check_for_updates(silent=True))

        self.refresh_instances()
        self.refresh_ai_overview()
        self._refresh_update_widgets()

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
            ("kairo", "\U0001f9ed   Kairo"),
            ("filter", "\U0001f6e1   Content Filter"),
            ("presence", "\U0001f3ae   Game Presence"),
            ("stream", "\U0001f3ac   Stream Director"),
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

        self.update_install_button = dash.styled_button("", "primary")
        self.update_install_button.clicked.connect(self.install_available_update)
        self.update_install_button.hide()
        self.update_check_button = dash.styled_button("Check for updates", "link")
        self.update_check_button.clicked.connect(lambda: self.check_for_updates(silent=False))
        self.update_label = dash.muted("")
        self.update_label.setWordWrap(True)
        layout.addWidget(self.update_install_button)
        layout.addWidget(self.update_check_button, 0, Qt.AlignLeft)
        layout.addWidget(self.update_label)

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
        self.routing_card.clicked.connect(lambda: self.show_page("ai"))
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
        self.bot_facts_label.setWordWrap(True)
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
        configure.clicked.connect(lambda: self.show_page("ai"))
        self.dashboard_ai_bot_label = dash.muted("")
        self.dashboard_ai_bot_label.setWordWrap(True)
        providers_panel.body.addWidget(self.dashboard_ai_bot_label)
        self.dashboard_no_ai_label = dash.muted("No AI connection for this bot yet. Open AI Providers to add one.")
        self.dashboard_no_ai_label.setWordWrap(True)
        providers_panel.body.addWidget(self.dashboard_no_ai_label)
        self.dashboard_connections_box = QVBoxLayout()
        self.dashboard_connections_box.setSpacing(10)
        providers_panel.body.addLayout(self.dashboard_connections_box)
        self.dashboard_connection_rows: dict[str, dash.ProviderRow] = {}

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
        # The panel lists only Game Presence bot instances; Admin instances are
        # used just to detect settings left over from the old Admin module.
        self.presence_panel = manager_game_presence.GamePresencePanel(
            self._all_bots, self._config_api, self.restart_instance
        )
        return self.presence_panel

    def _build_kairo_page(self) -> QWidget:
        # Admin (Kairo) bots: language and Social Awareness.
        self.kairo_panel = manager_kairo.KairoPanel(self._all_bots, self._config_api, self.restart_instance)
        return self.kairo_panel

    def _build_filter_page(self) -> QWidget:
        # Kairo's content filter: settings, who is filtered, the mute log.
        self.filter_panel = manager_content_filter.ContentFilterPanel(self._all_bots, self._config_api, self.restart_instance)
        return self.filter_panel

    def _build_stream_page(self) -> QWidget:
        self.stream_panel = manager_stream_director.StreamDirectorPanel(
            self._all_bots, self._config_api, self.restart_instance, self._start_worker
        )
        return self.stream_panel

    def _stream_summary(self, info: manager_core.InstanceInfo) -> str | None:
        """One line for the dashboard: what the Stream Director bot is doing."""
        if info.bot_type != STREAM_DIRECTOR_BOT_TYPE_ID:
            return None
        if info.state == manager_core.STATE_RUNNING:
            status = admin_terminal.read_runtime_json(
                admin_terminal.runtime_dir_for_logs(info.logs_dir), manager_stream_director.STATUS_FILE_NAME
            )
            if isinstance(status, dict):
                diagnosis = status.get("diagnosis") or {}
                twitch_title, _color, _details = manager_stream_director.twitch_lines(status.get("twitch"))
                if diagnosis.get("text"):
                    return f"{diagnosis['text']} · {twitch_title}"
        try:
            data = stream_director_config.normalize_config(self._config_api.get_config_snapshot(info.instance_id).effective)
        except Exception:
            return "Config problem: open the Stream Director page."
        if not stream_director_config.is_configured(data):
            return "Not configured: choose the stream channel on the Stream Director page."
        return None if info.state == manager_core.STATE_RUNNING else "Stopped."

    def _presence_summary(self, info: manager_core.InstanceInfo) -> str | None:
        """One line for the dashboard: the Game Presence bot's current reason/state."""
        if info.bot_type != GAME_PRESENCE_BOT_TYPE_ID:
            return None
        if info.state == manager_core.STATE_RUNNING:
            status = admin_terminal.read_runtime_json(
                admin_terminal.runtime_dir_for_logs(info.logs_dir), manager_game_presence.STATUS_FILE_NAME
            )
            diagnosis = status.get("diagnosis") if isinstance(status, dict) else None
            if isinstance(diagnosis, dict) and diagnosis.get("text"):
                return str(diagnosis["text"])
        try:
            data = game_presence.normalize_bot_config(self._config_api.get_config_snapshot(info.instance_id).effective)
        except Exception:
            return "Config problem: open the Game Presence page."
        if not game_presence.is_configured(data):
            return "Not configured: choose a server and a channel on the Game Presence page."
        return None if info.state == manager_core.STATE_RUNNING else "Stopped."

    def restart_instance(self, instance_id: str) -> None:
        """Restart one bot through the normal lifecycle path (used by module pages)."""
        if self.current_group() is not None:
            self._reload_group_tabs(select=None)
            self.group_tabs.setCurrentIndex(0)
            self.refresh_instances()
        if self._select_instance_by_id(instance_id):
            self.restart_selected()

    def _all_bots(self) -> list[tuple[str, str, manager_core.InstanceInfo]]:
        bots = []
        for info in self._last_infos:
            group = self.group_store.group_of(info.instance_id)
            bots.append((info.instance_id, f"{group} · {info.display_name}", info))
        return bots

    def _terminal_bots(self) -> list[tuple[str, str, manager_core.InstanceInfo]]:
        """The AI terminal talks to Admin bots only (Game Presence has no AI tools)."""
        return [bot for bot in self._all_bots() if bot[2].bot_type == ADMIN_BOT_TYPE_ID]

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
            "Connections are provider accounts (key + model). The base set is what every bot uses by default; any bot "
            "can get its own connections instead. Bots on the same connection share its key and limits. Keys stay on "
            "this PC. Planning writes the plan from a short tool catalog; execution runs it with only the planned tools."
        )
        description.setWordWrap(True)

        connections_panel = dash.Panel("\U0001f50c", "Connections")
        connections_panel.header.addWidget(self.add_connection_button)
        self.connections_status = dash.muted("")
        self.connections_status.setWordWrap(True)
        connections_panel.body.addWidget(self.connections_status)
        self.connections_box = QVBoxLayout()
        self.connections_box.setSpacing(10)
        connections_panel.body.addLayout(self.connections_box)
        self.connection_rows: dict[str, dash.ProviderRow] = {}

        base_panel = dash.Panel("⭐", "Base set (default for every bot)")
        self.base_planner_combo = QComboBox()
        self.base_executor_combo = QComboBox()
        self.base_fallback_combo = QComboBox()
        self.base_cross_checkbox = QCheckBox("If planning or execution fails, the other one takes over")
        self.base_save_button = dash.styled_button("Save Base Set", "primary")
        self.base_save_button.clicked.connect(self.save_base_set)
        self.base_status_label = dash.muted("")
        self.base_status_label.setWordWrap(True)
        base_form = QFormLayout()
        base_form.addRow("Planning (thinks):", self.base_planner_combo)
        base_form.addRow("Execution (acts):", self.base_executor_combo)
        base_form.addRow("Extra fallback:", self.base_fallback_combo)
        base_form.addRow("", self.base_cross_checkbox)
        base_panel.body.addLayout(base_form)
        base_buttons = QHBoxLayout()
        base_buttons.addWidget(self.base_save_button)
        base_buttons.addStretch(1)
        base_panel.body.addLayout(base_buttons)
        base_panel.body.addWidget(self.base_status_label)

        bot_panel = dash.Panel("\U0001f916", "AI of a bot")
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Bot:"))
        bot_row.addWidget(self.ai_bot_combo, 1)
        bot_panel.body.addLayout(bot_row)
        self.ai_bot_hint = dash.muted("")
        self.ai_bot_hint.setWordWrap(True)
        bot_panel.body.addWidget(self.ai_bot_hint)
        self.bot_mode_base_radio = QRadioButton("Use the base set")
        self.bot_mode_custom_radio = QRadioButton("Choose connections for this bot")
        self.bot_mode_group = QButtonGroup(self)
        self.bot_mode_group.addButton(self.bot_mode_base_radio)
        self.bot_mode_group.addButton(self.bot_mode_custom_radio)
        self.bot_mode_base_radio.toggled.connect(lambda _checked: self._update_bot_controls())
        self.bot_planner_combo = QComboBox()
        self.bot_executor_combo = QComboBox()
        self.bot_fallback_combo = QComboBox()
        self.bot_cross_checkbox = QCheckBox("If planning or execution fails, the other one takes over")
        self.bot_save_button = dash.styled_button("Save for this bot", "primary")
        self.bot_save_button.clicked.connect(self.save_bot_ai)
        self.bot_status_label = dash.muted("")
        self.bot_status_label.setWordWrap(True)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.bot_mode_base_radio)
        mode_row.addWidget(self.bot_mode_custom_radio)
        mode_row.addStretch(1)
        bot_panel.body.addLayout(mode_row)
        bot_form = QFormLayout()
        bot_form.addRow("Planning (thinks):", self.bot_planner_combo)
        bot_form.addRow("Execution (acts):", self.bot_executor_combo)
        bot_form.addRow("Extra fallback:", self.bot_fallback_combo)
        bot_form.addRow("", self.bot_cross_checkbox)
        bot_panel.body.addLayout(bot_form)
        bot_buttons = QHBoxLayout()
        bot_buttons.addWidget(self.bot_save_button)
        bot_buttons.addStretch(1)
        bot_panel.body.addLayout(bot_buttons)
        self.ai_routing_label = QLabel("")
        self.ai_routing_label.setWordWrap(True)
        bot_panel.body.addWidget(self.ai_routing_label)
        bot_panel.body.addWidget(self.bot_status_label)

        safety_panel = dash.Panel("\U0001f6e1", "How the AI acts on your server")
        safety = QLabel(
            "• Only Discord actions through the bot's tools: no Windows commands, files or browser.\n"
            "• Access: AI allowed user/role IDs in Bot Setup (Discord admins do not get AI automatically).\n"
            "• Confirmations: 'plan once' (default) or 'every change' in Bot Setup; deletions, bans, kicks "
            "and permission changes always ask with exact data.\n"
            "• Nobody can use the bot to act on roles or members at or above their own highest role, "
            "and Administrator is never granted.\n"
            "• A bot only ever uses the connections of its base set or of its own choice."
        )
        safety.setWordWrap(True)
        safety_panel.body.addWidget(safety)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        for panel in (connections_panel, base_panel, bot_panel, safety_panel):
            panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            layout.addWidget(panel)
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
        # Showing a page again keeps unsaved edits of the same bot (still marked unsaved).
        if name == "presence":
            self.presence_panel.refresh(keep_edits=True)
        if name == "stream":
            self.stream_panel.refresh(keep_edits=True)
        if name == "kairo":
            self.kairo_panel.refresh(keep_edits=True)
        if name == "filter":
            self.filter_panel.refresh(keep_edits=True)

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
            ]
            if info.bot_type == ADMIN_BOT_TYPE_ID:
                facts.append(f"Commands: {len(dash.SLASH_COMMANDS)} slash · {self.tool_count} AI tools")
            capabilities = BOT_TYPE_CAPABILITIES.get(info.bot_type)
            if capabilities:
                facts.append(f"Can do: {capabilities}")
            presence = self._presence_summary(info)
            if presence:
                # "Online" alone said nothing about whether suggestions can happen.
                facts.append(f"Game Presence: {presence}")
            stream = self._stream_summary(info)
            if stream:
                facts.append(f"Stream Director: {stream}")
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

    # -- per-bot AI storage ----------------------------------------------------

    def _refresh_ai_choices(self) -> None:
        current = self.ai_bot_combo.currentData()
        ai_infos = self._ai_infos()
        wanted = [(info.instance_id, f"{info.display_name} ({info.instance_id}) · {info.bot_type_display_name}") for info in ai_infos]
        existing = [(self.ai_bot_combo.itemData(index), self.ai_bot_combo.itemText(index)) for index in range(self.ai_bot_combo.count())]
        if existing == wanted:
            return
        self.ai_bot_combo.blockSignals(True)
        self.ai_bot_combo.clear()
        for instance_id, label in wanted:
            self.ai_bot_combo.addItem(label, instance_id)
        index = self.ai_bot_combo.findData(current)
        if index < 0:
            # Default: the first Admin bot (the one that uses AI the most).
            index = next((i for i, info in enumerate(ai_infos) if info.bot_type == ADMIN_BOT_TYPE_ID), 0)
        self.ai_bot_combo.setCurrentIndex(index if self.ai_bot_combo.count() else -1)
        self.ai_bot_combo.blockSignals(False)
        self.refresh_ai_overview()

    def _ai_info(self) -> manager_core.InstanceInfo | None:
        instance_id = self.ai_bot_combo.currentData()
        return next((info for info in self._last_infos if info.instance_id == instance_id), None)

    def ai_stores_for(self, instance_id: str) -> ai_storage.InstanceAIStores:
        """AI of ONE bot: its selection, its usage and the shared connections."""
        return ai_storage.for_instance_id(instance_id, must_exist=False)

    def _ai_stores(self) -> ai_storage.InstanceAIStores | None:
        info = self._ai_info()
        if info is None:
            return None
        try:
            return self.ai_stores_for(info.instance_id)
        except (ai_storage.AIStorageError, instance_store.InstanceStoreError):
            return None

    # -- connections: data ------------------------------------------------------

    def _load_connections(self) -> ai_connections.ConnectionsConfig | None:
        try:
            return self.connection_store.load()
        except ai_platform.AIPlatformError:
            return None

    def _bot_selection(self, instance_id: str) -> ai_connections.BotSelection | None:
        try:
            return self.ai_stores_for(instance_id).selection.load()
        except (ai_platform.AIPlatformError, instance_store.InstanceStoreError):
            return None

    def _usage_stores(self) -> list[ai_usage.AIUsageStore]:
        """Every bot's usage + the Manager's (Test Connection): limits are per key."""
        stores = []
        for info in self._ai_infos():
            try:
                stores.append(self.ai_stores_for(info.instance_id).usage)
            except (ai_storage.AIStorageError, instance_store.InstanceStoreError):
                continue
        stores.append(self.manager_usage)
        return stores

    def _ai_infos(self) -> list[manager_core.InstanceInfo]:
        """Instances of bot types that use AI connections (Stream Director does not)."""
        return [info for info in self._last_infos if info.bot_type in AI_BOT_TYPES]

    def _bots_using(self, connection_id: str, config: ai_connections.ConnectionsConfig) -> list[str]:
        """Bots whose current route (base set or own choice) includes the connection."""
        names = []
        for info in self._ai_infos():
            selection = self._bot_selection(info.instance_id) or ai_connections.BotSelection()
            if connection_id in selection.route(config).connection_ids():
                names.append(info.display_name)
        return names

    def users_of(self, connection_id: str) -> list[str]:
        """Who uses a connection: the base set and bots with their own choice."""
        config = self._load_connections()
        users = []
        base_bots = []
        custom_bots = []
        for info in self._ai_infos():
            selection = self._bot_selection(info.instance_id) or ai_connections.BotSelection()
            if selection.mode == ai_connections.MODE_BASE:
                base_bots.append(info.display_name)
            elif connection_id in selection.custom.connection_ids():
                custom_bots.append(info.display_name)
        if config is not None and connection_id in config.base.connection_ids():
            users.append(f"Base set ({', '.join(base_bots)})" if base_bots else "Base set")
        return users + custom_bots

    def _connection_label(self, config: ai_connections.ConnectionsConfig, connection_id: str | None) -> str:
        connection = config.get(connection_id) if connection_id else None
        if connection is None:
            return "not set"
        return f"{connection.name} ({ai_providers.model_display_name(connection.provider_id, connection.model_id)})"

    @staticmethod
    def _fill_combo(combo: QComboBox, config: ai_connections.ConnectionsConfig, none_label: str) -> None:
        signature = (none_label,) + tuple((item.connection_id, item.name) for item in config.connections)
        if combo.property("signature") == signature:
            return
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(none_label, None)
        for item in config.connections:
            combo.addItem(item.name, item.connection_id)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        combo.setProperty("signature", signature)

    @staticmethod
    def _select(combo: QComboBox, connection_id: str | None) -> None:
        index = combo.findData(connection_id)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _sync_rows(
        self,
        box: QVBoxLayout,
        rows: dict[str, dash.ProviderRow],
        connections: list[ai_connections.Connection],
    ) -> None:
        wanted = [(item.connection_id, item.name, item.provider_id) for item in connections]
        current = [(cid, row.property("connection_name"), row.provider_id) for cid, row in rows.items()]
        if wanted == current:
            return
        for row in rows.values():
            box.removeWidget(row)
            # Hide and detach now: deleteLater alone leaves the old row painted
            # over the panel until the event loop runs.
            row.hide()
            row.setParent(None)
            row.deleteLater()
        rows.clear()
        for connection in connections:
            spec = ai_providers.get_spec(connection.provider_id)
            row = dash.ProviderRow(connection.provider_id, connection.name, spec.logo)
            row.connection_id = connection.connection_id
            row.setProperty("connection_name", connection.name)
            row.settings_button.setText("⚙  Edit")
            row.test_button.clicked.connect(lambda _checked=False, cid=connection.connection_id: self.test_connection(cid))
            row.settings_button.clicked.connect(lambda _checked=False, cid=connection.connection_id: self.edit_connection(cid))
            box.addWidget(row)
            rows[connection.connection_id] = row

    def _row_state(self, config: ai_connections.ConnectionsConfig, connection: ai_connections.Connection) -> tuple[str, str]:
        if connection.connection_id in self._connection_tests_running:
            return "Testing...", "warn"
        tested = self._connection_tests.get(connection.connection_id)
        if tested is not None:
            return (tested[1], "ok") if tested[0] else (tested[1], "bad")
        if self.connection_store.has_key(connection):
            return "Configured", "info"
        return "No key saved", "muted"

    # -- connections: overview -----------------------------------------------------

    def refresh_ai_overview(self) -> None:
        config = self._load_connections()
        info = self._ai_info()
        bot_text = f"Showing AI of: {info.display_name} ({info.instance_id})" if info is not None else "Add a bot first."
        self.dashboard_ai_bot_label.setText(bot_text)
        if config is None:
            self.connections_status.setText(
                f"{ai_connections.CONNECTIONS_FILE_NAME} is unreadable. AI is unavailable for every bot until it is "
                "restored (the file is kept as it is)."
            )
            dash.colored(self.connections_status, "bad")
            self.ai_card.update_card("Settings invalid", "bad", "Connections file unreadable")
            self.routing_card.update_card("plan: not set", "muted", "run: not set", "muted")
            for button in (self.add_connection_button, self.base_save_button, self.bot_save_button):
                button.setEnabled(False)
            return
        for button in (self.add_connection_button, self.base_save_button):
            button.setEnabled(True)
        connections = list(config.connections)
        self.connections_status.setText(
            "" if connections else "No connections yet. Add one: provider, API key and model."
        )
        dash.colored(self.connections_status, "muted")
        names = {item.connection_id: item.name for item in connections}

        # All connections: total usage of every bot + Manager tests (limits belong to the key).
        self._sync_rows(self.connections_box, self.connection_rows, connections)
        stores = self._usage_stores()
        for connection in connections:
            row = self.connection_rows[connection.connection_id]
            text, color = self._row_state(config, connection)
            model = ai_providers.model_display_name(connection.provider_id, connection.model_id)
            row.set_state(text, color, f"{model} · {ai_providers.get_spec(connection.provider_id).display_name}", [])
            users = self.users_of(connection.connection_id)
            row.role_label.setText(f"Used by: {', '.join(users)}" if users else "Not used by any bot")
            usage_rows = ai_usage.merged_usage(stores, connection.provider_id, connection.connection_id)
            row.set_usage(ai_usage.rows_text(usage_rows, {connection.model_id: model}), dash.CONNECTION_USAGE_TOOLTIP)
            row.test_button.setEnabled(self.connection_store.has_key(connection) and connection.connection_id not in self._connection_tests_running)

        # Base set editor (only reset to the saved state when that state changes).
        for combo, none_label in (
            (self.base_planner_combo, "— same as execution —"),
            (self.base_executor_combo, "— not set —"),
            (self.base_fallback_combo, "— none —"),
        ):
            self._fill_combo(combo, config, none_label)
        if self._loaded_base != config.base:
            self._loaded_base = config.base
            self._select(self.base_planner_combo, config.base.planner)
            self._select(self.base_executor_combo, config.base.executor)
            self._select(self.base_fallback_combo, config.base.fallback)
            self.base_cross_checkbox.setChecked(config.base.cross_fallback)
        base_users = [item.display_name for item in self._last_infos if (self._bot_selection(item.instance_id) or ai_connections.BotSelection()).mode == ai_connections.MODE_BASE]
        self.base_status_label.setText(
            f"Planning: {self._connection_label(config, config.base.planner or config.base.executor)} · "
            f"Execution: {self._connection_label(config, config.base.executor or config.base.planner)}\n"
            f"Bots on the base set: {', '.join(base_users) if base_users else 'none'}"
        )

        self._refresh_bot_ai(config, info)
        self._refresh_ai_cards(config, info)
        self._refresh_dashboard_connections(config, info, stores)

    def _refresh_bot_ai(self, config: ai_connections.ConnectionsConfig, info: manager_core.InstanceInfo | None) -> None:
        if info is not None and info.bot_type == GAME_PRESENCE_BOT_TYPE_ID:
            self.ai_bot_hint.setText(
                "Game Presence uses only the Execution connection, to vary suggestion wording (no tools). "
                "Without a working connection it posts the built-in templates."
            )
        else:
            self.ai_bot_hint.setText("")
        self.ai_bot_hint.setVisible(bool(self.ai_bot_hint.text()))
        for combo, none_label in (
            (self.bot_planner_combo, "— same as execution —"),
            (self.bot_executor_combo, "— not set —"),
            (self.bot_fallback_combo, "— none —"),
        ):
            self._fill_combo(combo, config, none_label)
        if info is None:
            self.bot_save_button.setEnabled(False)
            self.bot_mode_base_radio.setEnabled(False)
            self.bot_mode_custom_radio.setEnabled(False)
            self.ai_routing_label.setText("Add a bot first.")
            self.bot_status_label.setText("")
            self._loaded_bot = None
            self._update_bot_controls()
            return
        self.bot_mode_base_radio.setEnabled(True)
        self.bot_mode_custom_radio.setEnabled(True)
        selection = self._bot_selection(info.instance_id)
        if selection is None:
            self.ai_routing_label.setText(
                f"{ai_connections.SELECTION_FILE_NAME} of this bot is unreadable: its AI is unavailable (fail closed). "
                "Save a choice below to replace it."
            )
            dash.colored(self.ai_routing_label, "bad")
            selection = ai_connections.BotSelection()
            state = (info.instance_id, None)
        else:
            dash.colored(self.ai_routing_label, "muted")
            state = (info.instance_id, selection)
            route = selection.route(config).restricted_to(config.ids())
            fallback = []
            if route.cross_fallback:
                fallback.append("the other one")
            if route.fallback:
                fallback.append(self._connection_label(config, route.fallback))
            source = "base set" if selection.mode == ai_connections.MODE_BASE else "own choice"
            self.ai_routing_label.setText(
                f"In use ({source}):\n"
                f"Planning (thinks): {self._connection_label(config, route.planner or route.executor)}\n"
                f"Execution (acts): {self._connection_label(config, route.executor or route.planner)}\n"
                f"Fallback: {', '.join(fallback) if fallback else 'none'}"
            )
        if self._loaded_bot != state:
            self._loaded_bot = state
            (self.bot_mode_custom_radio if selection.mode == ai_connections.MODE_CUSTOM else self.bot_mode_base_radio).setChecked(True)
            self._select(self.bot_planner_combo, selection.custom.planner)
            self._select(self.bot_executor_combo, selection.custom.executor)
            self._select(self.bot_fallback_combo, selection.custom.fallback)
            self.bot_cross_checkbox.setChecked(selection.custom.cross_fallback)
            self.bot_status_label.setText("")
        self.bot_save_button.setEnabled(True)
        self._update_bot_controls()

    def _update_bot_controls(self) -> None:
        custom = self.bot_mode_custom_radio.isChecked() and self.bot_mode_custom_radio.isEnabled()
        for widget in (self.bot_planner_combo, self.bot_executor_combo, self.bot_fallback_combo, self.bot_cross_checkbox):
            widget.setEnabled(custom)

    def _refresh_ai_cards(self, config: ai_connections.ConnectionsConfig, info: manager_core.InstanceInfo | None) -> None:
        keyed = [item.name for item in config.connections if self.connection_store.has_key(item)]
        if not config.connections:
            self.ai_card.update_card("No connections", "muted", "Add a Groq or Gemini connection")
        else:
            self.ai_card.update_card(f"{len(config.connections)} connection(s)", "ok" if keyed else "muted", ", ".join(keyed) or "No key saved yet")
        selection = self._bot_selection(info.instance_id) if info is not None else None
        if selection is None:
            self.routing_card.update_card("plan: not set", "muted", "run: not set", "muted")
            return
        route = selection.route(config).restricted_to(config.ids())
        planner = config.get(route.planner or route.executor)
        executor = config.get(route.executor or route.planner)
        routed = planner is not None and executor is not None
        self.routing_card.update_card(
            f"plan: {planner.name if planner else 'not set'}",
            "accent" if routed else "muted",
            f"run: {executor.name if executor else 'not set'}",
            "ok" if routed else "muted",
        )

    def _refresh_dashboard_connections(
        self,
        config: ai_connections.ConnectionsConfig,
        info: manager_core.InstanceInfo | None,
        stores: list[ai_usage.AIUsageStore],
    ) -> None:
        """Dashboard: the selected bot's connections with THIS bot's usage."""
        selection = self._bot_selection(info.instance_id) if info is not None else None
        route = selection.route(config).restricted_to(config.ids()) if selection is not None else ai_connections.RouteSelection()
        roles: dict[str, list[str]] = {}
        for name, connection_id in (
            ("planning", route.planner or route.executor),
            ("execution", route.executor or route.planner),
            ("fallback", route.fallback),
        ):
            if connection_id:
                roles.setdefault(connection_id, []).append(name)
        if route.cross_fallback and route.planner and route.executor and route.planner != route.executor:
            for connection_id in (route.planner, route.executor):
                roles.setdefault(connection_id, []).append("fallback")
        used = [config.get(connection_id) for connection_id in roles]
        self._sync_rows(self.dashboard_connections_box, self.dashboard_connection_rows, [item for item in used if item is not None])
        bot_usage = self.ai_stores_for(info.instance_id).usage if info is not None else None
        for connection in [item for item in used if item is not None]:
            row = self.dashboard_connection_rows[connection.connection_id]
            text, color = self._row_state(config, connection)
            model = ai_providers.model_display_name(connection.provider_id, connection.model_id)
            row.set_state(text, color, model, roles[connection.connection_id])
            others = [name for name in self._bots_using(connection.connection_id, config) if info is None or name != info.display_name]
            if others:
                # Shared key: the other bots spend the same provider limits.
                row.role_label.setText(f"{row.role_label.text()} · also used by {', '.join(others)}")
            try:
                own_rows = bot_usage.day_usage(connection.provider_id, None, connection.connection_id) if bot_usage else []
            except (OSError, ValueError):
                own_rows = None
            if own_rows is None:
                row.set_usage("Usage data unreadable", dash.BOT_USAGE_TOOLTIP)
            else:
                freshest = {item.model_id: item.rate_limits for item in ai_usage.merged_usage(stores, connection.provider_id, connection.connection_id)}
                own_rows = [replace(item, rate_limits=freshest.get(item.model_id, item.rate_limits)) for item in own_rows]
                row.set_usage(ai_usage.rows_text(own_rows, {connection.model_id: model}), dash.BOT_USAGE_TOOLTIP)
            row.test_button.setEnabled(self.connection_store.has_key(connection) and connection.connection_id not in self._connection_tests_running)
        self.dashboard_no_ai_label.setVisible(not used)

    # -- connections: actions -------------------------------------------------------------

    def _connection_dialog(self, connection: ai_connections.Connection | None) -> ConnectionDialog:
        return ConnectionDialog(
            self.connection_store,
            connection,
            usage_store=self.manager_usage,
            users_of=self.users_of,
            parent=self,
        )

    def add_connection(self) -> None:
        dialog = self._connection_dialog(None)
        dialog.exec()
        self._after_connection_dialog(dialog)

    def edit_connection(self, connection_id: str) -> None:
        config = self._load_connections()
        connection = config.get(connection_id) if config is not None else None
        if connection is None:
            self._set_light_error("This connection no longer exists.")
            self.refresh_ai_overview()
            return
        dialog = self._connection_dialog(connection)
        dialog.exec()
        self._after_connection_dialog(dialog)

    def _after_connection_dialog(self, dialog: ConnectionDialog) -> None:
        if getattr(dialog, "changed", False) and dialog.connection is not None:
            # Key or model may have changed: an earlier test result is stale.
            self._connection_tests.pop(dialog.connection.connection_id, None)
            config = self._load_connections()
            if config is not None and not config.base.connection_ids() and not dialog.removed:
                # The first working connection becomes the base set, so bots can use AI at once.
                self.connection_store.set_base(ai_connections.RouteSelection(executor=dialog.connection.connection_id))
            self.activity.add("AI", f"Connection {'removed' if dialog.removed else 'saved'}: {dialog.connection.name}.")
        self.refresh_ai_overview()

    def save_base_set(self) -> None:
        route = ai_connections.RouteSelection(
            self.base_planner_combo.currentData(),
            self.base_executor_combo.currentData(),
            self.base_fallback_combo.currentData(),
            self.base_cross_checkbox.isChecked(),
        )
        if route.connection_ids() and not (route.executor or route.planner):
            self.base_status_label.setText("Choose the execution connection.")
            return
        try:
            self.connection_store.set_base(route)
        except ai_platform.AIPlatformError as exc:
            self.base_status_label.setText(f"Not saved: {exc}")
            return
        self.activity.add("AI", "Base AI set saved.")
        self.refresh_ai_overview()

    def save_bot_ai(self) -> None:
        info = self._ai_info()
        if info is None:
            return
        if self.bot_mode_custom_radio.isChecked():
            route = ai_connections.RouteSelection(
                self.bot_planner_combo.currentData(),
                self.bot_executor_combo.currentData(),
                self.bot_fallback_combo.currentData(),
                self.bot_cross_checkbox.isChecked(),
            )
            if not (route.executor or route.planner):
                self.bot_status_label.setText("Choose at least the execution connection, or use the base set.")
                return
            selection = ai_connections.BotSelection(ai_connections.MODE_CUSTOM, route)
        else:
            # Keep the custom route stored, so switching back restores it.
            previous = self._bot_selection(info.instance_id) or ai_connections.BotSelection()
            selection = ai_connections.BotSelection(ai_connections.MODE_BASE, previous.custom)
        try:
            self.ai_stores_for(info.instance_id).selection.save(selection)
        except (ai_platform.AIPlatformError, OSError) as exc:
            self.bot_status_label.setText(f"Not saved: {exc}")
            return
        self._loaded_bot = None
        self.activity.add("AI", f"AI of {info.display_name}: {'own choice' if selection.mode == ai_connections.MODE_CUSTOM else 'base set'}.")
        self.refresh_ai_overview()
        self.bot_status_label.setText("Saved. Running bots use it from their next AI request.")

    def test_connection(self, connection_id: str) -> None:
        config = self._load_connections()
        connection = config.get(connection_id) if config is not None else None
        if connection is None:
            return
        if connection_id in self._connection_tests_running:
            return
        if not self.connection_store.has_key(connection):
            self._set_light_error(f"{connection.name}: no API key saved. Edit the connection to add one.")
            return
        self._connection_tests_running.add(connection_id)
        self.refresh_ai_overview()
        credentials = self.connection_store.credentials
        # A real API request on this key: counted as Manager usage of the connection.
        recorder = self.manager_usage.recorder()

        def run_action() -> ai_platform.Availability:
            import asyncio

            provider = create_provider(connection.provider_id, credentials, usage_recorder=recorder)
            return asyncio.run(provider.test_connection(connection_id))

        self._start_worker(run_action, lambda result: self._finish_connection_test(connection, result))

    def test_all_providers(self) -> None:
        """Quick action: test every connection the selected bot uses."""
        info = self._ai_info()
        config = self._load_connections()
        if info is None or config is None:
            self._set_light_error("Add a bot first.")
            return
        selection = self._bot_selection(info.instance_id)
        route = selection.route(config).restricted_to(config.ids()) if selection is not None else ai_connections.RouteSelection()
        started = False
        for connection_id in route.connection_ids():
            connection = config.get(connection_id)
            if connection is not None and self.connection_store.has_key(connection):
                self.test_connection(connection_id)
                started = True
        if not started:
            self._set_light_error("This bot has no connection with a saved key yet. Open AI Providers to add one.")

    def _finish_connection_test(self, connection: ai_connections.Connection, result: ActionResult) -> None:
        self._connection_tests_running.discard(connection.connection_id)
        text = availability_text(result)
        self._connection_tests[connection.connection_id] = (text == "Connected", text)
        if text == "Connected":
            self.activity.add("AI", f"{connection.name}: connection test successful.")
        else:
            self.activity.add("Error", f"{connection.name}: connection test failed: {text}.")
        self.refresh_ai_overview()

    def open_ai_providers(self, tab: str | None = None) -> None:
        self.show_page("ai")

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
        self._refresh_ai_choices()
        # Keys, models, routing and usage of the selected bot change outside the
        # Manager too (the bot records usage): re-read them on every refresh.
        self.refresh_ai_overview()

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
        dialog = BotSetupDialog(
            instance_id,
            self._instance_api,
            self._config_api,
            self,
            is_running=lambda: any(info.instance_id == instance_id and info.state == manager_core.STATE_RUNNING for info in self._last_infos),
        )
        accepted = dialog.exec()
        self.refresh_instances()
        if accepted == QDialog.Accepted and dialog.start_requested:
            self._select_instance_by_id(instance_id)
            self.start_selected()

    def create_bot_instance(self) -> None:
        """New bot instance of any registered type (own token, config, keys, data)."""
        dialog = CreateBotInstanceDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        bot_type, instance_id, display_name = dialog.values()
        try:
            self._instance_api.create_instance(bot_type, instance_id, display_name=display_name)
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

    # -- self-update -------------------------------------------------------

    def _refresh_update_widgets(self) -> None:
        if not hasattr(self, "update_check_button"):
            return
        update = self.available_update
        busy = self._update_check_running or self._update_installing
        self.update_install_button.setVisible(update is not None)
        self.update_install_button.setEnabled(update is not None and not busy)
        if update is not None:
            self.update_install_button.setText("Updating..." if self._update_installing else f"⬆  Update to v{update.version}")
            self.update_install_button.setToolTip(update.url)
        self.update_check_button.setEnabled(self.installed_app is not None and not busy)
        self.update_check_button.setText("Checking..." if self._update_check_running else "Check for updates")
        if self.installed_app is None:
            self.update_check_button.setToolTip(UPDATES_SOURCE_TEXT)
            text, tooltip = "Updates: packaged app only", UPDATES_SOURCE_TEXT
        elif self._update_installing:
            text, tooltip = "Installing the update: bots restart automatically.", ""
        elif self._update_error:
            text, tooltip = "Update check failed (retrying later).", self._update_error
        elif update is not None:
            text, tooltip = f"v{update.version} is available.", update.url
        elif self._update_checked_at is not None:
            text, tooltip = f"Up to date · checked {self._update_checked_at:%H:%M}", app_updates.RELEASES_URL
        else:
            text, tooltip = "", ""
        self.update_label.setText(text)
        self.update_label.setToolTip(tooltip)

    def check_for_updates(self, silent: bool = False) -> None:
        """Ask GitHub for a newer release in the background (installed app only)."""
        if self.installed_app is None:
            if not silent:
                self._set_light_error(UPDATES_SOURCE_TEXT)
            return
        if self._update_check_running or self._update_installing:
            return
        self._update_check_running = True
        self._refresh_update_widgets()
        current = self.installed_app.version
        self._start_worker(
            lambda: app_updates.check_for_update(current),
            lambda result: self._finish_update_check(result, silent),
        )

    def _finish_update_check(self, result: ActionResult, silent: bool) -> None:
        self._update_check_running = False
        self._update_checked_at = datetime.now()
        if not result.ok:
            self._update_error = result.message
            self._refresh_update_widgets()
            if not silent:
                self._show_error(result.message)
            return
        self._update_error = ""
        previous = self.available_update
        self.available_update = result.value if isinstance(result.value, app_updates.AvailableUpdate) else None
        update = self.available_update
        if update is not None and (previous is None or previous.version != update.version):
            self.activity.add("Update", f"Version {update.version} is available: use '⬆ Update' in the sidebar.")
        if not silent:
            self._set_status(f"Version {update.version} is available." if update else "You have the newest version.")
        self._refresh_update_widgets()

    def _running_instance_ids(self) -> list[str]:
        return [info.instance_id for info in self.manager.list_instance_info() if info.state == manager_core.STATE_RUNNING]

    def install_available_update(self) -> None:
        update, installed = self.available_update, self.installed_app
        if update is None or installed is None or self._update_installing:
            return
        if self._operation_in_progress():
            self._set_light_error("Wait for the current operation to finish, then update.")
            return
        try:
            running = self._running_instance_ids()
        except manager_core.ManagerCoreError as exc:
            self._show_error(f"Unable to check running bots before the update: {exc}")
            return
        bots_line = (
            f"Running bots ({', '.join(running)}) are stopped for the switch and started again by the new version."
            if running
            else "No bots are running."
        )
        answer = QMessageBox.question(
            self,
            "Update DarkAbyss Bot Manager",
            f"Install v{update.version}? You have v{installed.version}.\n\n"
            "The Manager downloads it from GitHub, verifies it, installs it next to the current version "
            "and restarts.\n"
            f"{bots_line}\n\n"
            "Bots, tokens, AI connections and keys, settings and logs are kept: they live in "
            f"{app_paths.DATA_ROOT}, and the update contains program files only.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            return
        self._update_installing = True
        self._refresh_update_widgets()
        self._set_status(f"Downloading and installing v{update.version}...")
        self.activity.add("Update", f"Installing v{update.version}...")

        def run_update() -> dict[str, str]:
            # Nothing changes until the new version is verified and active; only
            # then are the bots stopped (they restart under the new version).
            app_updates.install_update(update, installed.install_root, keep_versions=(installed.version,))
            try:
                running_now = self._running_instance_ids()
            except manager_core.ManagerCoreError:
                running_now = list(running)  # the new version must start regardless
            app_updates.save_resume(running_now, update.version)
            stopped = self.manager.shutdown_all()
            return {instance_id: str(value) for instance_id, value in stopped.items() if isinstance(value, Exception)}

        self._start_worker(run_update, lambda result: self._finish_update_install(update, result))

    def _finish_update_install(self, update: app_updates.AvailableUpdate, result: ActionResult) -> None:
        installed = self.installed_app
        if not result.ok:
            self._update_installing = False
            self._refresh_update_widgets()
            self.refresh_instances()
            if isinstance(result.value, app_updates.AppUpdateError):
                self._show_error(f"{result.message}\n\nNothing was changed: this version keeps running.")
            else:
                self._show_error(
                    f"v{update.version} was installed, but the switch did not finish: {result.message}\n"
                    "Close the Manager and start Launcher.exe to open the new version."
                )
            return
        failures = result.value or {}
        if failures:
            self._update_installing = False
            self._refresh_update_widgets()
            self.refresh_instances()
            self._show_error(
                f"v{update.version} is installed, but some bots did not stop:\n"
                + "\n".join(f"{instance_id}: {message}" for instance_id, message in failures.items())
                + "\n\nStop them, then close the Manager and start Launcher.exe."
            )
            return
        try:
            app_updates.start_launcher(installed)
        except (app_updates.AppUpdateError, OSError) as exc:
            self._update_installing = False
            self._refresh_update_widgets()
            self.refresh_instances()
            self._show_error(f"v{update.version} is installed. Start Launcher.exe to open it ({exc}).")
            return
        self._allow_close = True
        self.close()

    def resume_bots_after_update(self) -> list[str]:
        """Start the bots that were running when the previous version installed this one."""
        resume = app_updates.take_resume()
        known = {info.instance_id for info in self._last_infos}
        started = [instance_id for instance_id in resume if instance_id in known and instance_id not in self._busy_instances]
        if resume:
            self.activity.add("Update", f"Updated to {self._version_text}." + (f" Starting again: {', '.join(started)}." if started else ""))
        for instance_id in started:
            self._busy_instances.add(instance_id)
            self._start_worker(
                lambda instance_id=instance_id: self.manager.start(instance_id),
                lambda result, instance_id=instance_id: self._finish_lifecycle_action(instance_id, "start", result),
            )
        self._update_buttons()
        return started

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
        info = next((item for item in self._last_infos if item.instance_id == instance_id), None)
        if action_name in ("start", "restart") and getattr(info, "bot_type", None) == ADMIN_BOT_TYPE_ID:
            # Kairo that never saved a Social Awareness choice: ask once, then remember.
            if not manager_kairo.ensure_social_awareness_choice(self, instance_id, self._config_api, self.ask_social_awareness):
                self._set_status(f"{action_name.capitalize()} cancelled for {instance_id}.")
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
        text = "" if info is None else instance_info_details(info)
        if info is not None:
            text += "".join(f"\n{line}" for line in self._saved_bot_settings_lines(info))
        self.details_view.setPlainText(text)
        self._update_buttons()

    def _saved_bot_settings_lines(self, info: manager_core.InstanceInfo) -> list[str]:
        """The bot's saved language (and Kairo's Social Awareness choice) for the details view."""
        try:
            effective = self._config_api.get_config_snapshot(info.instance_id).effective
            language = bot_i18n.normalize_language(effective.get("language"), default_bot_language(info.bot_type))
        except Exception:
            return ["Bot language: config problem (open Advanced JSON)"]
        lines = [f"Bot language: {bot_i18n.LANGUAGES.get(language, language)}"]
        if info.bot_type == ADMIN_BOT_TYPE_ID:
            choice = effective.get(manager_kairo.sa.CONFIG_ENABLED)
            lines.append(
                "Social Awareness: " + ("on" if choice is True else "off" if choice is False else "not chosen yet (asked at start)")
            )
        return lines

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
    # One-time conversion of older AI layouts into connections (copy only,
    # idempotent): the single bot's keys become the base set.
    for message in ai_storage.run_migrations():
        print(message)


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
    window.resume_bots_after_update()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
