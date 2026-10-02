"""DarkAbyss Manager look: dark theme, dashboard widgets and small data helpers.

Pure presentation for ``manager_gui``: no process control, no network, no
config writes. Data helpers only read what the Manager already owns (instance
log tails, the device-local AI settings file and credential existence).
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

APP_TAGLINE = "Control. Automate. Evolve."
HERO_QUOTE = "“From the depths\nwe build greater things.”"
FEATURE_CHIPS = ("\U0001f3ae  Discord Bots", "✨  AI Integration", "\U0001f9e9  Modular System", "\U0001f6e1  Local & Secure")
LOGO_GLYPH = "\U0001f419"
SLASH_COMMANDS = ("/execute", "/ai", "/ai_reset")
LOG_TAIL_BYTES = 64 * 1024
CONNECTION_TAIL_BYTES = 16 * 1024
MAX_ACTIVITY = 60

COLORS = {
    "ok": "#34d399",
    "info": "#60a5fa",
    "warn": "#fbbf24",
    "bad": "#f87171",
    "muted": "#8f88b0",
    "accent": "#a78bfa",
}

THEME_QSS = """
QWidget { background-color: #0d0b18; color: #e8e4f7; font-family: "Segoe UI", "Inter", sans-serif; font-size: 10pt; }
QMainWindow, QDialog { background-color: #0d0b18; }
QLabel { background: transparent; }
QFrame#sidebar { background-color: #110e20; border: none; border-right: 1px solid #241f3d; }
QFrame#sideStatus { background-color: #15122a; border: 1px solid #2a2448; border-radius: 12px; }
QLabel#brandTitle { font-size: 14pt; font-weight: 600; color: #f4f1ff; }
QLabel#brandLogo { font-size: 26pt; }
QPushButton#navButton { text-align: left; padding: 11px 14px; border: none; border-radius: 10px; background: transparent; color: #cfc9e8; font-size: 11pt; }
QPushButton#navButton:hover { background-color: #1c1733; }
QPushButton#navButton:checked { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b2a6b, stop:1 #1d1838); color: #ffffff; border-left: 3px solid #a78bfa; }
QFrame#hero { border-radius: 16px; border: 1px solid #2f2752;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1a1433, stop:0.55 #2b1d55, stop:1 #0f0c1f); }
QLabel#heroTitle { font-family: Georgia, "Times New Roman", serif; font-size: 34pt; color: #f4f1ff; }
QLabel#heroSubtitle { font-family: Georgia, "Times New Roman", serif; font-size: 19pt; color: #b9a6ff; }
QLabel#heroTagline { font-size: 13pt; color: #cbbdf5; }
QLabel#heroQuote { font-size: 11pt; color: #d9cffc; }
QLabel#chip { background-color: rgba(20, 16, 40, 0.85); border: 1px solid #3a3163; border-radius: 9px; padding: 7px 12px; color: #ddd6fb; }
QFrame#card, QFrame#panel { background-color: #15122a; border: 1px solid #2a2448; border-radius: 14px; }
QFrame#card:hover { border-color: #4b3f80; }
QFrame#row { background-color: #110e20; border: 1px solid #241f3d; border-radius: 12px; }
QLabel#cardTitle { font-size: 12pt; font-weight: 600; color: #f1eefc; }
QLabel#panelTitle { font-size: 13pt; font-weight: 600; color: #f1eefc; }
QLabel#cardIcon { font-size: 20pt; background-color: #221c40; border-radius: 12px; padding: 8px; }
QLabel#providerLogo { font-size: 15pt; font-weight: 700; color: #ffffff; background-color: #0a0a12; border-radius: 12px; padding: 10px; }
QLabel#avatar { font-size: 34pt; background-color: #221c40; border: 2px solid #4b3f80; border-radius: 38px; padding: 6px; }
QLabel#muted { color: #9a93ba; }
QLabel#mono { font-family: Consolas, "Cascadia Mono", monospace; color: #9a93ba; }
QPushButton { background-color: #1d1838; border: 1px solid #352d5c; border-radius: 9px; padding: 7px 14px; color: #e8e4f7; }
QPushButton:hover { background-color: #272047; border-color: #4b3f80; }
QPushButton:pressed { background-color: #15122a; }
QPushButton:disabled { color: #6b6588; background-color: #15122a; border-color: #241f3d; }
QPushButton#primary { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8b5cf6, stop:1 #a855f7); border: none; color: #ffffff; font-weight: 600; padding: 14px; font-size: 11pt; }
QPushButton#primary:hover { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9d74f8, stop:1 #b874f8); }
QPushButton#secondary { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2f4bd8, stop:1 #3b5bdb); border: none; color: #ffffff; font-weight: 600; padding: 14px; font-size: 11pt; }
QPushButton#secondary:hover { background-color: #4a68e6; }
QPushButton#quick { padding: 14px; font-size: 11pt; text-align: left; }
QPushButton#link { background: transparent; border: none; color: #a78bfa; padding: 2px 4px; }
QPushButton#link:hover { color: #c4b5fd; }
QLineEdit, QPlainTextEdit, QComboBox, QTableWidget, QListWidget, QTreeWidget, QSpinBox {
    background-color: #110e20; border: 1px solid #2a2448; border-radius: 9px; padding: 6px; selection-background-color: #4c3a8f; }
QComboBox QAbstractItemView { background-color: #15122a; border: 1px solid #2a2448; selection-background-color: #4c3a8f; }
QHeaderView::section { background-color: #1a1630; color: #bfb7e0; border: none; padding: 7px; font-weight: 600; }
QTableWidget { gridline-color: #241f3d; }
QTableWidget::item:selected, QTreeWidget::item:selected { background-color: #3b2a6b; }
QTabWidget::pane { border: 1px solid #2a2448; border-radius: 10px; top: -1px; }
QTabBar::tab { background-color: #15122a; padding: 8px 18px; border-top-left-radius: 9px; border-top-right-radius: 9px; color: #bfb7e0; margin-right: 2px; }
QTabBar::tab:selected { background-color: #2b2150; color: #ffffff; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #6b5ca8; border-radius: 4px; background-color: #110e20; }
QCheckBox::indicator:hover { border-color: #a78bfa; }
QCheckBox::indicator:checked { background-color: #8b5cf6; border-color: #c4b5fd; }
QCheckBox::indicator:disabled { border-color: #3a3163; background-color: #15122a; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #110e20; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2f2752; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar { background-color: #110e20; color: #9a93ba; }
QToolTip { background-color: #1d1838; color: #e8e4f7; border: 1px solid #3a3163; padding: 4px; }
QMessageBox { background-color: #15122a; }
"""


# --------------------------------------------------------------------------
# data helpers
# --------------------------------------------------------------------------


def app_version_text(runtime_layout: Any) -> str:
    """Version from the packaged release.json; "dev" when running from source."""
    try:
        if runtime_layout.is_frozen():
            manifest = json.loads((runtime_layout.current_version_dir() / "release.json").read_text(encoding="utf-8"))
            version = manifest.get("version")
            if isinstance(version, str) and version:
                return f"v{version}"
    except Exception:
        pass
    return "dev"


def read_log_tail(path: Path | None, max_bytes: int = LOG_TAIL_BYTES) -> str:
    """Last ``max_bytes`` of a Manager-owned log file ("" when missing)."""
    if path is None:
        return ""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(data) == max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def bot_connection_state(info: Any) -> tuple[str, str]:
    """(label, color key) for one bot from its process state and log tail.

    "Online" only when the running process logged a gateway connection after
    the last restart marker; Discord is never contacted by the Manager.
    """
    state = str(getattr(info, "state", "") or "").lower()
    if state != "running":
        # EXITED with code 0/None is a normal stop; only a non-zero code is an error.
        if getattr(info, "exit_code", None) not in (None, 0):
            return "Stopped (error)", "bad"
        return "Offline", "muted"
    tail = read_log_tail(getattr(info, "stderr_log_path", None), CONNECTION_TAIL_BYTES)
    last_login = tail.rfind("logging in using static token")
    connected = tail.rfind("has connected to Gateway")
    resumed = tail.rfind("has successfully RESUMED")
    refused = max(tail.rfind("PrivilegedIntentsRequired"), tail.rfind("LoginFailure"))
    if refused > last_login >= 0 or (refused >= 0 and last_login < 0):
        return "Login refused", "bad"
    if max(connected, resumed) > last_login:
        return "Online", "ok"
    return "Connecting...", "warn"


def provider_overview(settings_store: Any, credential_store: Any, rows: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    """Local AI provider facts: key saved, model, routing role. No network."""
    try:
        settings = settings_store.load()
    except Exception:
        settings = None
    profiles = {profile.profile_id: profile for profile in getattr(settings, "profiles", ())}
    routing = getattr(settings, "routing", None)
    overview: dict[str, Any] = {"providers": {}, "valid": settings is not None}
    for provider_id, profile_id, credential_ref in rows:
        try:
            has_key = bool(credential_store.exists(provider_id, credential_ref))
        except Exception:
            has_key = False
        profile = profiles.get(profile_id)
        roles = []
        if routing is not None:
            if getattr(routing, "planner_profile_id", None) == profile_id:
                roles.append("planning")
            if getattr(routing, "routine_profile_id", None) == profile_id:
                roles.append("execution")
        overview["providers"][provider_id] = {
            "configured": has_key,
            "model": getattr(profile, "model_id", None),
            "roles": roles,
        }
    planner = getattr(routing, "planner_profile_id", None) if routing is not None else None
    executor = getattr(routing, "routine_profile_id", None) if routing is not None else None
    overview["planner"] = planner
    overview["executor"] = executor
    return overview


def ai_tool_summary() -> tuple[int, dict[str, list[tuple[str, str, str]]]]:
    """(tool count, {category: [(name, risk, description)]}) from the Admin Tool registry."""
    try:
        import admin_tools
    except Exception:
        return 0, {}
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for definition in admin_tools.list_tool_definitions():
        grouped.setdefault(definition.category, []).append((definition.name, definition.risk, definition.description))
    ordered = {category: grouped[category] for category in admin_tools.TOOL_CATEGORIES if category in grouped}
    return len(admin_tools.TOOL_DEFINITIONS), ordered


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------


def dot(color_key: str, size_pt: int = 11) -> QLabel:
    label = QLabel("●")
    set_dot_color(label, color_key, size_pt)
    return label


def set_dot_color(label: QLabel, color_key: str, size_pt: int = 11) -> None:
    label.setStyleSheet(f"color: {COLORS.get(color_key, color_key)}; font-size: {size_pt}pt; background: transparent;")


def colored(label: QLabel, color_key: str) -> QLabel:
    label.setStyleSheet(f"color: {COLORS.get(color_key, color_key)}; background: transparent;")
    return label


def muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    return label


class ClickableFrame(QFrame):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StatCard(ClickableFrame):
    """Dashboard status card: icon, title, status line with dot, detail, chevron."""

    def __init__(self, icon: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(104)
        icon_label = QLabel(icon)
        icon_label.setObjectName("cardIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setFixedSize(58, 58)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.status_dot = dot("muted")
        self.value_label = QLabel("")
        self.detail_label = muted("")
        # Wrap instead of widening the card row past the window width.
        for label in (self.value_label, self.detail_label):
            label.setWordWrap(True)
            label.setMinimumWidth(40)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        chevron = muted("›")
        chevron.setStyleSheet("font-size: 18pt; color: #8f88b0; background: transparent;")

        title_row = QHBoxLayout()
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.status_dot)
        text = QVBoxLayout()
        text.setSpacing(3)
        text.addLayout(title_row)
        text.addWidget(self.value_label)
        detail_row = QHBoxLayout()
        detail_row.addWidget(self.detail_label, 1)
        detail_row.addWidget(chevron)
        text.addLayout(detail_row)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 14, 14)
        layout.setSpacing(14)
        layout.addWidget(icon_label, 0, Qt.AlignTop)
        layout.addLayout(text, 1)

    def update_card(self, value: str, value_color: str, detail: str, dot_color: str | None = None) -> None:
        self.value_label.setText(value)
        colored(self.value_label, value_color)
        self.detail_label.setText(detail)
        set_dot_color(self.status_dot, dot_color or value_color)


class Panel(QFrame):
    """Rounded panel with a title row (icon + title + optional header button)."""

    def __init__(self, icon: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.header_button: QPushButton | None = None
        title_label = QLabel(f"{icon}  {title}")
        title_label.setObjectName("panelTitle")
        self.header = QHBoxLayout()
        self.header.addWidget(title_label)
        self.header.addStretch(1)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addLayout(self.header)
        layout.addLayout(self.body)

    def add_header_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        self.header.addWidget(button)
        self.header_button = button
        return button


class ProviderRow(QFrame):
    """One AI provider: logo, name, status, model/role, Test Connection + Settings."""

    def __init__(self, provider_id: str, display_name: str, logo: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.provider_id = provider_id
        logo_label = QLabel(logo)
        logo_label.setObjectName("providerLogo")
        logo_label.setAlignment(Qt.AlignCenter)
        logo_label.setFixedSize(64, 64)
        name = QLabel(display_name)
        name.setObjectName("cardTitle")
        self.status_dot = dot("muted", 9)
        self.status_label = QLabel("Not configured")
        self.model_label = muted("")
        self.role_label = muted("")
        for label in (self.model_label, self.role_label):
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(name)
        text.addLayout(status_row)
        text.addWidget(self.model_label)
        text.addWidget(self.role_label)
        self.test_button = QPushButton("▷  Test Connection")
        self.settings_button = QPushButton("⚙  Settings")
        buttons = QVBoxLayout()
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.settings_button)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        layout.addWidget(logo_label)
        layout.addLayout(text, 1)
        layout.addLayout(buttons)

    def set_state(self, text: str, color_key: str, model: str | None, roles: list[str]) -> None:
        self.status_label.setText(text)
        colored(self.status_label, color_key)
        set_dot_color(self.status_dot, color_key, 9)
        self.model_label.setText(f"Model: {model}" if model else "Model: not set")
        self.role_label.setText(("Role: " + " + ".join(roles)) if roles else "Role: not routed")


class ActivityFeed(QWidget):
    """Recent Manager events (newest first): time, dot, kind, text."""

    KIND_COLORS = {"Bot": "ok", "AI": "info", "Error": "bad", "Manager": "accent"}

    def __init__(self, visible_rows: int = 6, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.visible_rows = visible_rows
        self.entries: deque[tuple[str, str, str]] = deque(maxlen=MAX_ACTIVITY)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(8)
        self._render()

    def add(self, kind: str, text: str, *, now: datetime | None = None) -> None:
        stamp = (now or datetime.now()).strftime("%H:%M:%S")
        self.entries.appendleft((stamp, kind, text))
        self._render()

    def _render(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not self.entries:
            self._grid.addWidget(muted("No activity yet."), 0, 0)
            return
        rows = list(self.entries)[: self.visible_rows]
        for row, (stamp, kind, text) in enumerate(rows):
            time_label = QLabel(stamp)
            time_label.setObjectName("mono")
            kind_label = colored(QLabel(kind), self.KIND_COLORS.get(kind, "info"))
            text_label = QLabel(text)
            text_label.setWordWrap(True)
            text_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self._grid.addWidget(time_label, row, 0)
            self._grid.addWidget(dot(self.KIND_COLORS.get(kind, "info"), 9), row, 1)
            self._grid.addWidget(kind_label, row, 2)
            self._grid.addWidget(text_label, row, 3)
            self._grid.setRowStretch(row, 0)
        # Keep rows compact at the top instead of spreading over the panel.
        self._grid.setRowStretch(len(rows), 1)
        self._grid.setColumnStretch(3, 1)


def nav_button(text: str, on_click: Callable[[], None]) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("navButton")
    button.setCheckable(True)
    button.setCursor(Qt.PointingHandCursor)
    button.clicked.connect(on_click)
    return button


def styled_button(text: str, object_name: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    button.setCursor(Qt.PointingHandCursor)
    return button
