"""Manager page for Kairo (Admin bot instances): bot language and Social Awareness.

Both live in the instance config (ConfigStore overrides, validated like the
bot does). The page always shows what is stored: it reloads after Save, says
whether a change applies live or needs a restart, refuses to save over a
config it cannot read, and asks before switching bots with unsaved edits.
The bot's ``social_awareness_status.json`` (runtime folder) shows what Social
Awareness is doing right now.

``ensure_social_awareness_choice`` is the one-time question when a Kairo
instance that never saved a Social Awareness choice is started.

The "Server memory" panel shows Kairo's long-term social memory of each
server (``data/social_memory.json``): Server Lore (forget one item, clear a
server), the quiet wishes in force (lift them), and how people took Kairo's
autonomous actions. An unreadable memory file can be reset (a copy is kept).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import admin_terminal
import bot_i18n
import instance_store
import manager_dashboard as dash
import social_awareness as sa
import social_memory as sm

BOT_TYPE_ID = "admin"
DEFAULT_LANGUAGE = "en"
CONFIG_KEYS = ("language", sa.CONFIG_ENABLED, sa.CONFIG_CHANNELS, sa.CONFIG_REPLIES, sa.CONFIG_LORE)
SOCIAL_AWARENESS_HELP = (
    "Kairo follows the conversation in the channels below (in memory only, nothing is stored) and understands it in "
    "context, including messages that refer to something earlier without a reply or mention — for example to a Group "
    "Up invitation. It decides by itself whether to answer now, come back later, or stay silent; silence is the usual "
    "choice. It never pings anyone and never runs actions.\n"
    "Only some messages are analysed (Kairo's name, replies to Kairo, people from a recent Group Up, rarely a lively "
    "chat). Each analysis is one AI request with HIGH reasoning effort on this bot's AI connection; the reply itself "
    "uses the normal effort. Commands, /ai and @mentions are not affected. Needs the Message Content Intent.\n"
    "Kairo may answer with a reaction instead of words, learns from how people take its interventions (answers, "
    "laughs, being ignored or told off: it steps back for a while after repeated negative signals) and keeps quiet "
    "when asked (\"Kairo, be quiet\", \"not here\", \"don't reply to me\", \"shut up for an hour\") — only these "
    "autonomous interventions; commands and /ai always work."
)
LORE_HELP = (
    "Server Lore: Kairo slowly remembers durable things about each server — local memes, nicknames people use, running "
    "jokes, social patterns, notable events — to understand later conversations. Not a chat log: an item needs to be "
    "noticed twice before it counts, the list per server is small, and you can see, forget or clear it below."
)
QUESTION_TEXT = (
    "Start Kairo with Social Awareness?\n\n"
    "Kairo will follow the conversation (in memory only), understand references without replies or mentions, and "
    "decide by itself whether to answer, come back later, or stay silent — usually silent. It never pings anyone.\n\n"
    "It uses this bot's AI connection with high reasoning effort for the analysis (more AI usage) and needs the "
    "Message Content Intent enabled in the Discord Developer Portal.\n\n"
    "You can change this any time on the Kairo page. This question is asked only once."
)


def _id_list(text: str) -> list[str]:
    parts = [part for part in text.replace(",", " ").split() if part]
    for part in parts:
        if not part.isdigit():
            raise ValueError(f"Channel IDs must be numbers (got {part!r}).")
    return parts


def kairo_settings(effective: dict[str, Any]) -> dict[str, Any]:
    """The page's part of the effective config, validated the way the bot validates it."""
    data = {key: effective.get(key) for key in CONFIG_KEYS if key in effective}
    data["language"] = bot_i18n.normalize_language(effective.get("language"), DEFAULT_LANGUAGE)
    checked = dict(data)
    sa.validate_config_fields(checked, lambda value, name: [int(item) for item in _id_list(" ".join(str(entry) for entry in (value or [])))])
    return {
        "language": data["language"],
        sa.CONFIG_ENABLED: checked[sa.CONFIG_ENABLED],
        sa.CONFIG_CHANNELS: [str(item) for item in checked[sa.CONFIG_CHANNELS]],
        sa.CONFIG_REPLIES: checked[sa.CONFIG_REPLIES],
        sa.CONFIG_LORE: checked[sa.CONFIG_LORE],
    }


def needs_social_awareness_choice(config_api: Any, instance_id: str) -> bool:
    """True when the effective config says "never chosen" (null from the program defaults)."""
    try:
        effective = config_api.get_config_snapshot(instance_id).effective
    except Exception:
        return False  # unreadable config: the start shows that problem, no question
    return sa.CONFIG_ENABLED in effective and effective[sa.CONFIG_ENABLED] is None


def save_social_awareness_choice(config_api: Any, instance_id: str, enabled: bool) -> None:
    snapshot = config_api.get_config_snapshot(instance_id)
    config_api.save_config_overrides(instance_id, {**snapshot.overrides, sa.CONFIG_ENABLED: bool(enabled)})


def ask_social_awareness(parent: QWidget | None) -> bool | None:
    """True / False = start with / without it, None = do not start now."""
    box = QMessageBox(parent)
    box.setWindowTitle("Social Awareness for Kairo")
    box.setText(QUESTION_TEXT)
    with_button = box.addButton("Start with Social Awareness", QMessageBox.YesRole)
    without_button = box.addButton("Start without it", QMessageBox.NoRole)
    box.addButton(QMessageBox.Cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is with_button:
        return True
    if clicked is without_button:
        return False
    return None


def ensure_social_awareness_choice(
    parent: QWidget | None,
    instance_id: str,
    config_api: Any,
    ask: Callable[[QWidget | None], bool | None] = ask_social_awareness,
) -> bool:
    """Before starting a Kairo instance: ask once if no choice was ever saved.

    Returns False when the user cancelled the start."""
    if not needs_social_awareness_choice(config_api, instance_id):
        return True
    answer = ask(parent)
    if answer is None:
        return False
    try:
        save_social_awareness_choice(config_api, instance_id, answer)
    except Exception as exc:
        QMessageBox.warning(parent, "Social Awareness", f"The choice could not be saved ({exc}). The bot starts without it.")
    return True


class KairoPanel(QWidget):
    def __init__(
        self,
        list_bots: Callable[[], list[tuple[str, str, Any]]],
        config_api: Any,
        restart_bot: Callable[[str], None],
        parent: QWidget | None = None,
        memory_for: Callable[[str], sm.SocialMemory] | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_bots = list_bots
        self._config_api = config_api
        self._restart_bot = restart_bot
        self._memory_for = memory_for or (lambda instance_id: sm.SocialMemory(instance_store.get_instance_paths(instance_id).data_dir / sm.FILE_NAME))
        self._loaded: dict[str, Any] = {}
        self._effective: dict[str, Any] = {}
        self._loading = False
        self._config_ok = False
        self._current_bot: str | None = None

        title = QLabel("Kairo")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "Settings of the Admin bot (Kairo) that are not part of the setup wizard: the language Kairo writes in and "
            "Social Awareness. Access, AI permissions and the control channel stay in Bot Setup."
        )
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.bot_combo.currentIndexChanged.connect(lambda _index: self._on_bot_changed())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh)
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Kairo bot"))
        bot_row.addWidget(self.bot_combo, 1)
        bot_row.addWidget(refresh)

        status_panel = dash.Panel("\U0001f9ed", "Social Awareness now")
        self.status_dot = dash.dot("muted")
        self.status_title = QLabel("—")
        self.status_title.setObjectName("cardTitle")
        head = QHBoxLayout()
        head.addWidget(self.status_dot)
        head.addWidget(self.status_title, 1)
        self.status_details = dash.muted("")
        self.status_details.setWordWrap(True)
        self.status_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_panel.body.addLayout(head)
        status_panel.body.addWidget(self.status_details)

        settings_panel = dash.Panel("⚙", "Settings")
        self.language_combo = QComboBox()
        for code, label in bot_i18n.LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setToolTip(
            "Everything Kairo writes in Discord: replies, errors, confirmations, buttons, role menus, welcome and AI answers. "
            "Applies within 15 s; slash command descriptions change after a restart."
        )
        self.social_checkbox = QCheckBox("Social Awareness: understand the conversation and decide when to speak")
        self.choice_note = dash.muted("")
        self.choice_note.setWordWrap(True)
        self.channels_edit = QLineEdit()
        self.channels_edit.setPlaceholderText("Channel IDs, comma separated; empty = every channel Kairo can read")
        self.channels_note = dash.muted("")
        self.channels_note.setWordWrap(True)
        self.replies_spin = QSpinBox()
        self.replies_spin.setRange(*sa.REPLIES_RANGE)
        self.replies_spin.setSuffix(" per hour")
        self.replies_spin.setToolTip("At most this many Social Awareness messages per hour (analyses are capped as well).")
        self.lore_checkbox = QCheckBox("Server Lore: remember durable things about the server (memes, nicknames, running jokes)")
        self.lore_checkbox.setToolTip(LORE_HELP)
        help_label = dash.muted(SOCIAL_AWARENESS_HELP)
        help_label.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Bot language", self.language_combo)
        form.addRow("", self.social_checkbox)
        form.addRow("", self.choice_note)
        form.addRow("Watched channels", self.channels_edit)
        form.addRow("", self.channels_note)
        form.addRow("Social replies at most", self.replies_spin)
        form.addRow("", self.lore_checkbox)
        settings_panel.body.addLayout(form)
        settings_panel.body.addWidget(help_label)
        self.save_button = dash.styled_button("Save", "primary")
        self.save_restart_button = dash.styled_button("Save && Restart Bot", "secondary")
        self.save_button.clicked.connect(self.save)
        self.save_restart_button.clicked.connect(lambda: self.save(restart=True))
        self.result_label = dash.SaveIndicator()
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.save_restart_button)
        buttons.addStretch(1)
        settings_panel.body.addLayout(buttons)
        settings_panel.body.addWidget(self.result_label)

        memory_panel = dash.Panel("\U0001f4dc", "Server memory")
        self.memory_guild_combo = QComboBox()
        self.memory_guild_combo.currentIndexChanged.connect(lambda _index: self.load_memory())
        memory_row = QHBoxLayout()
        memory_row.addWidget(QLabel("Server"))
        memory_row.addWidget(self.memory_guild_combo, 1)
        self.memory_problem_label = dash.muted("")
        self.memory_problem_label.setWordWrap(True)
        self.reset_memory_button = QPushButton("Reset memory (keeps a copy of the broken file)")
        self.reset_memory_button.clicked.connect(lambda: self.reset_memory())
        self.lore_list = QListWidget()
        self.lore_list.setMinimumHeight(110)
        self.forget_lore_button = QPushButton("Forget selected")
        self.forget_lore_button.clicked.connect(lambda: self.forget_selected_lore())
        self.clear_lore_button = QPushButton("Clear this server's lore")
        self.clear_lore_button.clicked.connect(lambda: self.clear_lore())
        self.quiet_list = QListWidget()
        self.quiet_list.setMaximumHeight(90)
        self.lift_quiet_button = QPushButton("Lift selected")
        self.lift_quiet_button.clicked.connect(lambda: self.lift_selected_quiet())
        self.lift_all_button = QPushButton("Lift all")
        self.lift_all_button.clicked.connect(lambda: self.lift_all_quiet())
        self.feedback_label = dash.muted("")
        self.feedback_label.setWordWrap(True)
        lore_buttons = QHBoxLayout()
        lore_buttons.addWidget(self.forget_lore_button)
        lore_buttons.addWidget(self.clear_lore_button)
        lore_buttons.addStretch(1)
        quiet_buttons = QHBoxLayout()
        quiet_buttons.addWidget(self.lift_quiet_button)
        quiet_buttons.addWidget(self.lift_all_button)
        quiet_buttons.addStretch(1)
        lore_help = dash.muted(LORE_HELP)
        lore_help.setWordWrap(True)
        memory_panel.body.addLayout(memory_row)
        memory_panel.body.addWidget(self.memory_problem_label)
        memory_panel.body.addWidget(self.reset_memory_button)
        memory_panel.body.addWidget(QLabel("Server Lore"))
        memory_panel.body.addWidget(self.lore_list)
        memory_panel.body.addLayout(lore_buttons)
        memory_panel.body.addWidget(lore_help)
        memory_panel.body.addWidget(QLabel("Kairo keeps quiet"))
        memory_panel.body.addWidget(self.quiet_list)
        memory_panel.body.addLayout(quiet_buttons)
        memory_panel.body.addWidget(self.feedback_label)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(bot_row)
        for panel in (status_panel, settings_panel, memory_panel):
            panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            layout.addWidget(panel)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.language_combo.currentIndexChanged.connect(lambda _index: self._update_dirty())
        self.social_checkbox.toggled.connect(lambda _checked: self._update_dirty())
        self.channels_edit.textChanged.connect(lambda _text: self._update_dirty())
        self.replies_spin.valueChanged.connect(lambda _value: self._update_dirty())
        self.lore_checkbox.toggled.connect(lambda _checked: self._update_dirty())
        self.refresh()

    # -- helpers ---------------------------------------------------------------------

    def _bots(self) -> list[tuple[str, str, Any]]:
        return [bot for bot in self._list_bots() if getattr(bot[2], "bot_type", None) == BOT_TYPE_ID]

    def _bot(self) -> tuple[str, Any] | None:
        instance_id = self.bot_combo.currentData()
        for bot_id, _label, info in self._bots():
            if bot_id == instance_id:
                return bot_id, info
        return None

    @staticmethod
    def _running(info: Any) -> bool:
        return str(getattr(info, "state", "")).upper() == "RUNNING"

    def _runtime(self) -> Any:
        bot = self._bot()
        return None if bot is None else admin_terminal.runtime_dir_for_logs(bot[1].logs_dir)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.save_button, self.save_restart_button, self.language_combo, self.social_checkbox, self.channels_edit, self.replies_spin, self.lore_checkbox):
            widget.setEnabled(enabled)

    # -- loading ---------------------------------------------------------------------

    def refresh(self, keep_edits: bool = False) -> None:
        """Re-read bots and the saved settings. ``keep_edits`` (the page is shown
        again): unsaved edits of the same bot stay on screen, still marked unsaved."""
        current = self.bot_combo.currentData()
        dirty = self.dirty
        if dirty and not keep_edits and not self._confirm_reload():
            return
        self.bot_combo.blockSignals(True)
        self.bot_combo.clear()
        for instance_id, label, _info in self._bots():
            self.bot_combo.addItem(label, instance_id)
        index = self.bot_combo.findData(current)
        self.bot_combo.setCurrentIndex(index if index >= 0 else 0)
        self.bot_combo.blockSignals(False)
        if dirty and keep_edits and current is not None and self.bot_combo.currentData() == current == self._current_bot:
            self.refresh_status()
            return
        self.load()

    def _confirm_reload(self) -> bool:
        """Refresh with unsaved edits: reload the saved settings only if the user agrees."""
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "Reload the saved settings and discard the unsaved changes?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Discard

    def load(self) -> None:
        """Fill every control from the selected bot's saved config (nothing from the previous bot stays)."""
        bot = self._bot()
        self._current_bot = bot[0] if bot is not None else None
        self._set_enabled(bot is not None)
        if bot is None:
            self._config_ok = False
            self.result_label.show_disabled("Settings appear here once a Kairo (Admin) bot exists.")
            self.status_title.setText("No Kairo bot")
            dash.set_dot_color(self.status_dot, "muted")
            self.status_details.setText("")
            return
        problem = None
        try:
            self._effective = dict(self._config_api.get_config_snapshot(bot[0]).effective)
            self._loaded = kairo_settings(self._effective)
        except Exception as exc:
            self._effective = {}
            self._loaded = {}
            problem = str(exc)
        self._loading = True
        try:
            data = self._loaded or {
                "language": DEFAULT_LANGUAGE,
                sa.CONFIG_ENABLED: None,
                sa.CONFIG_CHANNELS: [],
                sa.CONFIG_REPLIES: sa.DEFAULT_REPLIES_PER_HOUR,
                sa.CONFIG_LORE: True,
            }
            index = self.language_combo.findData(data["language"])
            self.language_combo.setCurrentIndex(index if index >= 0 else 0)
            self.social_checkbox.setChecked(data[sa.CONFIG_ENABLED] is True)
            self.channels_edit.setText(", ".join(data[sa.CONFIG_CHANNELS]))
            self.replies_spin.setValue(int(data[sa.CONFIG_REPLIES]))
            self.lore_checkbox.setChecked(data[sa.CONFIG_LORE] is not False)
        finally:
            self._loading = False
        self._config_ok = problem is None
        self._update_choice_note()
        self._update_channels_note()
        if problem:
            self._set_enabled(False)
            self.result_label.show_disabled(
                f"Config problem: {problem} The form is locked so nothing is saved over it — fix or reset the config in "
                "Bots → Advanced JSON, then press Refresh."
            )
            dash.colored(self.result_label, "bad")
        else:
            self.result_label.show_clean(f"Showing the saved settings of {self.bot_combo.currentText() or 'this bot'}.")
        self.refresh_status()
        self.refresh_memory()

    def _update_choice_note(self) -> None:
        if not self._config_ok:
            self.choice_note.setText("")
        elif self._loaded.get(sa.CONFIG_ENABLED) is None:
            self.choice_note.setText("Not chosen yet: off for now; you are asked once when this bot is started. Save here to decide now.")
        else:
            self.choice_note.setText("")

    def _update_channels_note(self) -> None:
        ids = []
        try:
            ids = _id_list(self.channels_edit.text())
        except ValueError as exc:
            self.channels_note.setText(str(exc))
            return
        if not ids:
            self.channels_note.setText("Every channel Kairo can read.")
            return
        runtime = self._runtime()
        status = admin_terminal.read_bot_status(runtime) if runtime is not None else None
        names = {}
        for guild in (status or {}).get("guilds") or []:
            for channel in guild.get("channels") or []:
                names[str(channel.get("id"))] = f"#{channel.get('name')}"
        self.channels_note.setText("Watching: " + ", ".join(names.get(item, f"{item} (unknown to the bot)") for item in ids))

    def current_settings(self) -> dict[str, Any]:
        return {
            "language": self.language_combo.currentData() or DEFAULT_LANGUAGE,
            sa.CONFIG_ENABLED: self.social_checkbox.isChecked(),
            sa.CONFIG_CHANNELS: _id_list(self.channels_edit.text()),
            sa.CONFIG_REPLIES: self.replies_spin.value(),
            sa.CONFIG_LORE: self.lore_checkbox.isChecked(),
        }

    @property
    def dirty(self) -> bool:
        if self._current_bot is None or not self._config_ok:
            return False
        try:
            current = self.current_settings()
        except ValueError:
            return True
        saved = dict(self._loaded)
        # "Never chosen" shows as unchecked: only a real change of the box counts.
        saved[sa.CONFIG_ENABLED] = saved.get(sa.CONFIG_ENABLED) is True
        return current != saved

    def _update_dirty(self) -> None:
        if self._loading:
            return
        self._update_channels_note()
        if self._current_bot is None or not self._config_ok:
            return
        if self.dirty:
            self.result_label.show_dirty()
        elif self.result_label.state == "dirty":
            self.result_label.show_clean(f"Showing the saved settings of {self.bot_combo.currentText() or 'this bot'}.")

    def _on_bot_changed(self) -> None:
        new_id = self.bot_combo.currentData()
        if self._current_bot is not None and new_id != self._current_bot and self.dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "The settings of the previous bot have unsaved changes. Switch and discard them?",
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                index = self.bot_combo.findData(self._current_bot)
                self.bot_combo.blockSignals(True)
                self.bot_combo.setCurrentIndex(index)
                self.bot_combo.blockSignals(False)
                return
        self.load()

    # -- status ----------------------------------------------------------------------

    def refresh_status(self) -> None:
        bot = self._bot()
        if bot is None:
            return
        running = self._running(bot[1])
        enabled = self._loaded.get(sa.CONFIG_ENABLED) is True
        runtime = self._runtime()
        status = admin_terminal.read_runtime_json(runtime, sa.STATUS_FILE_NAME) if running and runtime is not None else None
        details: list[str] = []
        if not self._config_ok:
            title, color = "Config problem", "bad"
        elif not enabled:
            title, color = "Off", "muted"
            if self._loaded.get(sa.CONFIG_ENABLED) is None:
                details.append("No choice saved yet.")
        elif not running:
            title, color = "On — starts with the bot", "muted"
        elif not isinstance(status, dict):
            title, color = "On — waiting for the bot's first report", "warn"
        elif status.get("problem"):
            title, color = "On — needs attention", "bad"
            details.append(str(status["problem"]))
        elif not status.get("active"):
            title, color = "On — not active yet", "warn"
        else:
            title, color = "Listening (usually silent)", "ok"
        if isinstance(status, dict) and running:
            details.append(
                f"Last hour: {status.get('analyses_last_hour', 0)} analyses, {status.get('replies_last_hour', 0)} replies, "
                f"{status.get('reactions_last_hour', 0)} reactions (limit {status.get('replies_per_hour', '?')} replies/h); "
                f"waiting thoughts: {status.get('waiting_thoughts', 0)}."
            )
            if status.get("quiet"):
                details.append(f"Keeping quiet in {len(status['quiet'])} place(s) — see Server memory below.")
            if status.get("darkabyss_bots"):
                details.append(f"Knows {status['darkabyss_bots']} other DarkAbyss bot(s).")
            last = status.get("last_decision") or {}
            if last.get("decision"):
                about = f" — {last['about']}" if last.get("about") else ""
                details.append(f"Last decision: {last['decision']} ({last.get('reason', '')}){about}")
        self.status_title.setText(title)
        dash.colored(self.status_title, color)
        dash.set_dot_color(self.status_dot, color)
        self.status_details.setText("\n".join(details))

    # -- server memory --------------------------------------------------------------------

    def _memory(self) -> sm.SocialMemory | None:
        bot = self._bot()
        if bot is None:
            return None
        try:
            return self._memory_for(bot[0])
        except Exception:
            return None

    def _guild_names(self) -> dict[int, str]:
        runtime = self._runtime()
        status = admin_terminal.read_bot_status(runtime) if runtime is not None else None
        names = {}
        for guild in (status or {}).get("guilds") or []:
            if str(guild.get("id", "")).isdigit():
                names[int(guild["id"])] = str(guild.get("name") or guild["id"])
        return names

    def refresh_memory(self) -> None:
        """Re-read the memory file: the server list, then the selected server."""
        memory = self._memory()
        current = self.memory_guild_combo.currentData()
        self.memory_guild_combo.blockSignals(True)
        self.memory_guild_combo.clear()
        problem = None
        guild_ids: list[int] = []
        if memory is not None:
            problem = memory.check()
            if problem is None:
                guild_ids = memory.guild_ids()
        names = self._guild_names()
        for guild_id in guild_ids:
            self.memory_guild_combo.addItem(names.get(guild_id, f"Server {guild_id}"), guild_id)
        index = self.memory_guild_combo.findData(current)
        self.memory_guild_combo.setCurrentIndex(index if index >= 0 else 0)
        self.memory_guild_combo.blockSignals(False)
        self.memory_problem_label.setText(problem or "")
        dash.colored(self.memory_problem_label, "bad" if problem else "muted")
        self.reset_memory_button.setVisible(problem is not None)
        self.load_memory()

    def load_memory(self) -> None:
        memory = self._memory()
        guild_id = self.memory_guild_combo.currentData()
        self.lore_list.clear()
        self.quiet_list.clear()
        has_guild = memory is not None and guild_id is not None
        for widget in (self.forget_lore_button, self.clear_lore_button, self.lift_quiet_button, self.lift_all_button):
            widget.setEnabled(has_guild)
        if not has_guild:
            self.feedback_label.setText("Nothing remembered yet." if memory is not None and memory.check() is None else "")
            return
        try:
            guild = memory.guild(guild_id)
        except sm.SocialMemoryError as exc:
            self.memory_problem_label.setText(str(exc))
            return
        now = time.time()
        for entry in sorted(guild.lore, key=lambda item: (item.status != "active", -item.confirmations, -item.updated_at)):
            label = f"[{entry.kind}] {entry.text}" + (f"  (noticed {entry.confirmations}×)" if entry.status == "active" else "  (candidate: noticed once)")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry.id)
            self.lore_list.addItem(item)
        for mute in guild.mutes:
            if mute.until <= now:
                continue
            where = {"guild": "the whole server", "channel": f"channel {mute.channel_id}", "user": f"member {mute.user_id}"}[mute.scope]
            why = "asked" if mute.reason == "asked" else "stepped back by itself after negative reactions"
            item = QListWidgetItem(f"{where} · {why} · {max(1, int((mute.until - now) // 60))} min left")
            item.setData(Qt.UserRole, mute.id)
            self.quiet_list.addItem(item)
        counts = sm.feedback_counts(guild.outcomes, last=20)
        total = sum(counts.values())
        self.feedback_label.setText(
            "How people took Kairo's last autonomous actions: no data yet."
            if not total
            else (
                f"How people took Kairo's last {total} autonomous actions: {counts['engaged']} answered, {counts['positive']} liked, "
                f"{counts['neutral']} no reaction, {counts['ignored']} ignored, {counts['negative']} negative."
            )
        )

    def _memory_action(self, action: Callable[[sm.SocialMemory, int], Any]) -> None:
        memory = self._memory()
        guild_id = self.memory_guild_combo.currentData()
        if memory is None or guild_id is None:
            return
        try:
            action(memory, guild_id)
        except sm.SocialMemoryError as exc:
            QMessageBox.warning(self, "Server memory", str(exc))
        self.refresh_memory()

    def forget_selected_lore(self) -> None:
        item = self.lore_list.currentItem()
        if item is not None:
            entry_id = item.data(Qt.UserRole)
            self._memory_action(lambda memory, guild_id: memory.forget(guild_id, entry_id))

    def clear_lore(self, confirm: bool = True) -> None:
        if confirm and self.lore_list.count():
            answer = QMessageBox.question(self, "Server Lore", "Forget everything Kairo remembers about this server?", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return
        self._memory_action(lambda memory, guild_id: memory.clear_lore(guild_id))

    def lift_selected_quiet(self) -> None:
        item = self.quiet_list.currentItem()
        if item is not None:
            mute_id = item.data(Qt.UserRole)
            self._memory_action(lambda memory, guild_id: memory.lift(guild_id, lambda mute: mute.id == mute_id))

    def lift_all_quiet(self) -> None:
        self._memory_action(lambda memory, guild_id: memory.lift(guild_id, lambda mute: True))

    def reset_memory(self) -> None:
        memory = self._memory()
        if memory is None:
            return
        answer = QMessageBox.question(
            self,
            "Server memory",
            "The memory file cannot be read. Start with an empty memory? The broken file is kept next to it.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            memory.reset()
        self.refresh_memory()

    # -- saving ----------------------------------------------------------------------

    def save(self, restart: bool = False) -> bool:
        bot = self._bot()
        if bot is None:
            return False
        if not self._config_ok:
            self.result_label.show_error("the stored config has a problem; fix it in Bots → Advanced JSON first.")
            return False
        previous = dict(self._effective)
        try:
            settings = self.current_settings()
            snapshot = self._config_api.get_config_snapshot(bot[0])
            kairo_settings({**snapshot.effective, **settings})  # same validation as the bot
            self._config_api.save_config_overrides(bot[0], {**snapshot.overrides, **settings})
        except Exception as exc:
            self.result_label.show_error(str(exc))
            return False
        running = self._running(bot[1])
        notes = []
        if restart and running:
            self._restart_bot(bot[0])
            notes.append("Restarting the bot now.")
        elif running:
            notes.append("Applied live: the running bot picks it up within 15 seconds (no restart needed).")
            if settings["language"] != previous.get("language"):
                notes.append("Slash command descriptions switch language after a restart.")
            turned_on = settings[sa.CONFIG_ENABLED] and previous.get(sa.CONFIG_ENABLED) is not True
            if turned_on and not _message_content_requested(previous):
                notes = [
                    "Restart required: Social Awareness reads messages and needs the Message Content Intent, which the "
                    "bot requests only when it starts. Press Save & Restart Bot."
                ]
        else:
            notes.append("The bot uses it when it starts.")
        if settings[sa.CONFIG_ENABLED]:
            notes.append("Enable Message Content Intent in the Discord Developer Portal if it is not on yet.")
        self.load()
        self.result_label.show_saved(" ".join(notes))
        return True


def _message_content_requested(config: dict[str, Any]) -> bool:
    """Same rule as Admin.message_content_requested (what the running bot asked for at start)."""
    return (
        config.get("ai_control_channel_id") is not None
        or config.get("ai_read_message_content") is True
        or config.get("ai_mention_enabled") is True
        or config.get(sa.CONFIG_ENABLED) is True
    )
