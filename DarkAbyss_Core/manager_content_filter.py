"""Manager page: Kairo's content filter (Admin bot instances).

Settings (config): the filter on/off and which kinds of messages are muted at
once even as a joke. Members (data/content_filter.json, shared with the bot):
who is filtered on which server - added here by Discord user ID, with
``/filter add`` in Discord, or by asking Kairo. The log shows every mute (and
every mute that failed, with the reason). The page always shows what is
stored, reloads after Save and refuses to write over an unreadable config or
list.
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
    QVBoxLayout,
    QWidget,
)

import admin_terminal
import content_filter as cf
import message_policy as mp
import instance_store
import manager_dashboard as dash

BOT_TYPE_ID = "admin"
HELP_TEXT = (
    "Only the members on the list below are checked. Each of their messages is judged by this bot's AI (normal effort): "
    "who it is aimed at, joke or not, how bad. The only punishment is a Discord mute (timeout) of 30 minutes to 3 hours "
    "— longer for worse messages and for repeats within a week. Kairo replies to the message, pinging only that "
    "member (\"Ew, how rude. @member, that's 30 minutes of mute. Need some soap?\"), in the bot's language.\n"
    "Kinds ticked below are muted at once even as a joke; the others only when they are meant (friendly banter is "
    "fine). Swearing at nothing (a game, oneself) is never punished. Owner, administrators and members with a role "
    "above the bot cannot be muted. Needs the Message Content Intent and the Moderate Members permission."
)


class ContentFilterPanel(QWidget):
    def __init__(
        self,
        list_bots: Callable[[], list[tuple[str, str, Any]]],
        config_api: Any,
        restart_bot: Callable[[str], None],
        parent: QWidget | None = None,
        store_for: Callable[[str], cf.FilterStore] | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_bots = list_bots
        self._config_api = config_api
        self._restart_bot = restart_bot
        self._store_for = store_for or (lambda instance_id: cf.FilterStore(instance_store.get_instance_paths(instance_id).data_dir / cf.FILE_NAME))
        self._loaded: dict[str, Any] = {}
        self._loading = False
        self._config_ok = False
        self._current_bot: str | None = None

        title = QLabel("Content Filter")
        title.setObjectName("heroSubtitle")
        description = dash.muted("Kairo mutes named members for insults and hostility (30 minutes to 3 hours, it decides how long).")
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.bot_combo.currentIndexChanged.connect(lambda _index: self._on_bot_changed())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(lambda: self.refresh())
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Kairo bot"))
        bot_row.addWidget(self.bot_combo, 1)
        bot_row.addWidget(refresh)

        status_panel = dash.Panel("\U0001f6e1", "Status")
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
        self.enabled_checkbox = QCheckBox("Filter the messages of the members on the list")
        self.immediate_boxes: dict[str, QCheckBox] = {}
        form = QFormLayout()
        form.addRow("", self.enabled_checkbox)
        form.addRow(QLabel("Mute at once, even as a joke:"))
        for category in cf.CATEGORIES:
            box = QCheckBox(cf.CATEGORY_LABELS[category])
            self.immediate_boxes[category] = box
            form.addRow("", box)
        help_label = dash.muted(HELP_TEXT)
        help_label.setWordWrap(True)
        settings_panel.body.addLayout(form)
        settings_panel.body.addWidget(help_label)
        self.policy_checkbox = QCheckBox("Custom message rules (defined locally in Bots → Advanced JSON)")
        self.policy_checkbox.setToolTip(
            "Turns on any custom \"message_policies\" rules kept in this bot's config. They are not part of the program or "
            "its releases — you define them yourself in Advanced JSON. Off here means they never run."
        )
        settings_panel.body.addWidget(self.policy_checkbox)
        self.save_button = dash.styled_button("Save", "primary")
        self.save_restart_button = dash.styled_button("Save && Restart Bot", "secondary")
        self.save_button.clicked.connect(lambda: self.save())
        self.save_restart_button.clicked.connect(lambda: self.save(restart=True))
        self.result_label = dash.SaveIndicator()
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.save_restart_button)
        buttons.addStretch(1)
        settings_panel.body.addLayout(buttons)
        settings_panel.body.addWidget(self.result_label)

        members_panel = dash.Panel("\U0001f465", "Filtered members")
        self.guild_combo = QComboBox()
        self.guild_combo.setEditable(True)
        self.guild_combo.setToolTip("A server the bot is in, or type a server ID.")
        self.guild_combo.currentIndexChanged.connect(lambda _index: self.load_members())
        guild_row = QHBoxLayout()
        guild_row.addWidget(QLabel("Server"))
        guild_row.addWidget(self.guild_combo, 1)
        self.store_problem_label = dash.muted("")
        self.store_problem_label.setWordWrap(True)
        self.reset_store_button = QPushButton("Reset the list (keeps a copy of the broken file)")
        self.reset_store_button.clicked.connect(lambda: self.reset_store())
        self.members_list = QListWidget()
        self.members_list.setMinimumHeight(120)
        self.member_id_edit = QLineEdit()
        self.member_id_edit.setPlaceholderText("Discord user ID (Developer Mode → right click → Copy User ID)")
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Optional note")
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(lambda: self.add_member())
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(lambda: self.remove_selected())
        add_row = QHBoxLayout()
        add_row.addWidget(self.member_id_edit, 2)
        add_row.addWidget(self.note_edit, 2)
        add_row.addWidget(self.add_button)
        self.members_result = dash.muted("")
        self.members_result.setWordWrap(True)
        members_hint = dash.muted("Also in Discord: /filter add @member, /filter remove, /filter list — or ask Kairo (\"start filtering @member\").")
        members_hint.setWordWrap(True)
        self.log_list = QListWidget()
        self.log_list.setMinimumHeight(120)
        members_panel.body.addLayout(guild_row)
        members_panel.body.addWidget(self.store_problem_label)
        members_panel.body.addWidget(self.reset_store_button)
        members_panel.body.addWidget(self.members_list)
        members_panel.body.addLayout(add_row)
        members_panel.body.addWidget(self.remove_button, 0, Qt.AlignLeft)
        members_panel.body.addWidget(self.members_result)
        members_panel.body.addWidget(members_hint)
        members_panel.body.addWidget(QLabel("Recent mutes"))
        members_panel.body.addWidget(self.log_list)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(bot_row)
        for panel in (status_panel, members_panel, settings_panel):
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

        self.enabled_checkbox.toggled.connect(lambda _checked: self._update_dirty())
        self.policy_checkbox.toggled.connect(lambda _checked: self._update_dirty())
        for box in self.immediate_boxes.values():
            box.toggled.connect(lambda _checked: self._update_dirty())
        self.refresh()

    # -- helpers --------------------------------------------------------------------------

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

    def _store(self) -> cf.FilterStore | None:
        bot = self._bot()
        if bot is None:
            return None
        try:
            return self._store_for(bot[0])
        except Exception:
            return None

    def _status(self) -> dict[str, Any] | None:
        bot = self._bot()
        runtime = self._runtime()
        if bot is None or runtime is None or not self._running(bot[1]):
            return None
        return admin_terminal.read_runtime_json(runtime, cf.STATUS_FILE_NAME)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.save_button, self.save_restart_button, self.enabled_checkbox, self.policy_checkbox, *self.immediate_boxes.values()):
            widget.setEnabled(enabled)

    # -- loading ---------------------------------------------------------------------------

    def refresh(self, keep_edits: bool = False) -> None:
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
            self.refresh_members()
            return
        self.load()

    def _confirm_reload(self) -> bool:
        answer = QMessageBox.question(
            self, "Unsaved changes", "Reload the saved settings and discard the unsaved changes?", QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel
        )
        return answer == QMessageBox.Discard

    def load(self) -> None:
        """Fill every control from the selected bot's saved config and list."""
        bot = self._bot()
        self._current_bot = bot[0] if bot is not None else None
        self._set_enabled(bot is not None)
        if bot is None:
            self._config_ok = False
            self.result_label.show_disabled("Settings appear here once a Kairo (Admin) bot exists.")
            self.status_title.setText("No Kairo bot")
            dash.set_dot_color(self.status_dot, "muted")
            self.status_details.setText("")
            self.refresh_members()
            return
        problem = None
        try:
            effective = dict(self._config_api.get_config_snapshot(bot[0]).effective)
            checked = {key: effective[key] for key in (cf.CONFIG_ENABLED, cf.CONFIG_IMMEDIATE) if key in effective}
            cf.validate_config_fields(checked)
            checked[mp.CONFIG_ENABLED] = effective.get(mp.CONFIG_ENABLED) is True
            self._loaded = checked
        except Exception as exc:
            self._loaded = {}
            problem = str(exc)
        self._loading = True
        try:
            self.enabled_checkbox.setChecked(self._loaded.get(cf.CONFIG_ENABLED, True) is not False)
            immediate = set(self._loaded.get(cf.CONFIG_IMMEDIATE, cf.DEFAULT_IMMEDIATE))
            for category, box in self.immediate_boxes.items():
                box.setChecked(category in immediate)
            self.policy_checkbox.setChecked(self._loaded.get(mp.CONFIG_ENABLED, False) is True)
        finally:
            self._loading = False
        self._config_ok = problem is None
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
        self.refresh_members()

    def current_settings(self) -> dict[str, Any]:
        return {
            cf.CONFIG_ENABLED: self.enabled_checkbox.isChecked(),
            cf.CONFIG_IMMEDIATE: [category for category, box in self.immediate_boxes.items() if box.isChecked()],
            mp.CONFIG_ENABLED: self.policy_checkbox.isChecked(),
        }

    @property
    def dirty(self) -> bool:
        if self._current_bot is None or not self._config_ok:
            return False
        saved = {
            cf.CONFIG_ENABLED: self._loaded.get(cf.CONFIG_ENABLED, True),
            cf.CONFIG_IMMEDIATE: list(self._loaded.get(cf.CONFIG_IMMEDIATE, cf.DEFAULT_IMMEDIATE)),
            mp.CONFIG_ENABLED: self._loaded.get(mp.CONFIG_ENABLED, False),
        }
        return self.current_settings() != saved

    def _update_dirty(self) -> None:
        if self._loading or self._current_bot is None or not self._config_ok:
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

    # -- status ------------------------------------------------------------------------------

    def refresh_status(self) -> None:
        bot = self._bot()
        if bot is None:
            return
        store = self._store()
        watched = 0
        store_problem = None
        if store is not None:
            store_problem = store.check()
            if store_problem is None:
                watched = sum(len(store.guild(guild_id).watched) for guild_id in store.guild_ids())
        enabled = self._loaded.get(cf.CONFIG_ENABLED, True) is not False
        running = self._running(bot[1])
        status = self._status()
        details: list[str] = []
        if not self._config_ok:
            title, color = "Config problem", "bad"
        elif store_problem:
            title, color = "List problem", "bad"
            details.append(store_problem)
        elif not enabled:
            title, color = "Off", "muted"
        elif not watched:
            title, color = "On — nobody on the list", "muted"
        elif not running:
            title, color = f"On — {watched} member(s) on the list; starts with the bot", "muted"
        elif isinstance(status, dict) and status.get("problem"):
            title, color = f"Filtering {watched} member(s) — needs attention", "bad"
            details.append(str(status["problem"]))
        elif isinstance(status, dict) and not status.get("active"):
            title, color = "Not active yet — restart the bot", "warn"
        else:
            title, color = f"Filtering {watched} member(s)", "ok"
        if isinstance(status, dict) and running:
            counters = status.get("counters") or {}
            details.append(f"Since the bot started: {counters.get('checked', 0)} messages checked, {counters.get('muted', 0)} mutes, {counters.get('failed', 0)} failed.")
        self.status_title.setText(title)
        dash.colored(self.status_title, color)
        dash.set_dot_color(self.status_dot, color)
        self.status_details.setText("\n".join(details))

    # -- members -------------------------------------------------------------------------------

    def _guild_names(self) -> dict[int, str]:
        runtime = self._runtime()
        status = admin_terminal.read_bot_status(runtime) if runtime is not None else None
        names = {}
        for guild in (status or {}).get("guilds") or []:
            if str(guild.get("id", "")).isdigit():
                names[int(guild["id"])] = str(guild.get("name") or guild["id"])
        return names

    def _selected_guild(self) -> int | None:
        data = self.guild_combo.currentData()
        if isinstance(data, int):
            return data
        text = self.guild_combo.currentText().strip()
        return int(text) if text.isdigit() else None

    def refresh_members(self) -> None:
        store = self._store()
        current = self._selected_guild()
        self.guild_combo.blockSignals(True)
        self.guild_combo.clear()
        problem = store.check() if store is not None else None
        guild_ids = set(self._guild_names())
        if store is not None and problem is None:
            guild_ids |= set(store.guild_ids())
        names = self._guild_names()
        for guild_id in sorted(guild_ids):
            self.guild_combo.addItem(names.get(guild_id, f"Server {guild_id}"), guild_id)
        index = self.guild_combo.findData(current)
        self.guild_combo.setCurrentIndex(index if index >= 0 else 0)
        self.guild_combo.blockSignals(False)
        self.store_problem_label.setText(problem or "")
        dash.colored(self.store_problem_label, "bad" if problem else "muted")
        self.reset_store_button.setVisible(problem is not None)
        self.load_members()

    def load_members(self) -> None:
        store = self._store()
        guild_id = self._selected_guild()
        self.members_list.clear()
        self.log_list.clear()
        usable = store is not None and store.check() is None
        for widget in (self.add_button, self.remove_button, self.member_id_edit, self.note_edit):
            widget.setEnabled(usable)
        if not usable or guild_id is None:
            return
        guild = store.guild(guild_id)
        for member in sorted(guild.watched.values(), key=lambda item: item.added_at):
            mutes = sum(1 for action in guild.actions if action.user_id == member.user_id and action.result == "muted")
            label = f"{member.name or 'member'} ({member.user_id})" + (f" · {member.note}" if member.note else "") + f" · mutes: {mutes}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, member.user_id)
            self.members_list.addItem(item)
        for action in reversed(guild.actions[-50:]):
            when = time.strftime("%d.%m %H:%M", time.localtime(action.at))
            result = f"{action.minutes} min" if action.result == "muted" else action.result
            text = f"{when} · {action.name} · {result} · {cf.CATEGORY_LABELS.get(action.category, action.category)}"
            if action.reason:
                text += f" · {action.reason}"
            if action.excerpt:
                text += f" · “{action.excerpt}”"
            self.log_list.addItem(QListWidgetItem(text))

    def add_member(self) -> bool:
        store = self._store()
        guild_id = self._selected_guild()
        member_id = self.member_id_edit.text().strip()
        if store is None or guild_id is None:
            self._members_message("Choose a server (or type its ID) first.", "bad")
            return False
        if not member_id.isdigit():
            self._members_message("The member ID must be a number (Developer Mode → right click the member → Copy User ID).", "bad")
            return False
        try:
            added = store.watch(guild_id, int(member_id), added_by="manager", note=self.note_edit.text().strip())
        except (ValueError, cf.FilterStoreError) as exc:
            self._members_message(str(exc), "bad")
            return False
        self.member_id_edit.clear()
        self.note_edit.clear()
        note = "Added: their next messages are checked (live)." if added else "Already on the list (note updated)."
        status = self._status()
        if isinstance(status, dict) and status.get("message_content") is False:
            note += " The running bot does not read messages yet: restart it once (Message Content Intent)."
        self._members_message(note, "ok")
        self.refresh_members()
        self.refresh_status()
        return True

    def remove_selected(self) -> bool:
        store = self._store()
        item = self.members_list.currentItem()
        guild_id = self._selected_guild()
        if store is None or item is None or guild_id is None:
            return False
        try:
            store.unwatch(guild_id, item.data(Qt.UserRole))
        except cf.FilterStoreError as exc:
            self._members_message(str(exc), "bad")
            return False
        self._members_message("Removed from the list.", "ok")
        self.refresh_members()
        self.refresh_status()
        return True

    def reset_store(self) -> None:
        store = self._store()
        if store is None:
            return
        answer = QMessageBox.question(
            self, "Content filter", "The list cannot be read. Start with an empty list? The broken file is kept next to it.", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel
        )
        if answer == QMessageBox.Yes:
            store.reset()
        self.refresh_members()
        self.refresh_status()

    def _members_message(self, text: str, color: str) -> None:
        self.members_result.setText(text)
        dash.colored(self.members_result, color)

    # -- saving ----------------------------------------------------------------------------------

    def save(self, restart: bool = False) -> bool:
        bot = self._bot()
        if bot is None:
            return False
        if not self._config_ok:
            self.result_label.show_error("the stored config has a problem; fix it in Bots → Advanced JSON first.")
            return False
        try:
            settings = self.current_settings()
            checked = dict(settings)
            cf.validate_config_fields(checked)
            snapshot = self._config_api.get_config_snapshot(bot[0])
            self._config_api.save_config_overrides(bot[0], {**snapshot.overrides, **checked})
        except Exception as exc:
            self.result_label.show_error(str(exc))
            return False
        running = self._running(bot[1])
        if restart and running:
            self._restart_bot(bot[0])
            note = "Restarting the bot now."
        elif running:
            note = "Applied live: the running bot picks it up within 15 seconds (no restart needed)."
            status = self._status()
            if settings[cf.CONFIG_ENABLED] and isinstance(status, dict) and status.get("message_content") is False:
                note = "Saved. The running bot does not read messages yet: press Save & Restart Bot (Message Content Intent)."
        else:
            note = "The bot uses it when it starts."
        self.load()
        self.result_label.show_saved(note)
        return True
