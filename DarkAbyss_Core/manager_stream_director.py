"""Manager page for Stream Director Bot instances (bot type ``stream_director``).

The settings are the instance's whole config (ConfigStore overrides, same
normalization as the bot). The Twitch account is connected here with the
OAuth Device Code flow of a *public* Twitch application: the page shows a
code, the streamer approves it on twitch.tv/activate, and the tokens go to
the instance's secrets folder (never into the config, never shown back).
Servers/channels come from the bot's ``bot_status.json``; the operational
state, the inbox and the last recap from its ``stream_director_status.json``.
Nothing here writes the bot's state file.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
import stream_director as sd
import stream_director_config as sdc
import stream_director_store as sds
import stream_director_twitch as sdt

BOT_TYPE_ID = "stream_director"
STATUS_FILE_NAME = "stream_director_status.json"
CHOOSE_SERVER = "— choose a server —"
CHOOSE_CHANNEL = "— choose a channel —"
TWITCH_CONSOLE_URL = "https://dev.twitch.tv/console/apps/create"
PROBLEM_CODES = frozenset({"config_problem", "state_problem", "channel_unavailable", "permission_denied"})
TWITCH_STATES = {
    "not_configured": ("Twitch: no Client ID yet", "warn"),
    "not_connected": ("Twitch: account not connected", "warn"),
    "connecting": ("Twitch: connecting…", "warn"),
    "connected": ("Twitch: connected", "ok"),
    "auth_failed": ("Twitch: connect the account again", "bad"),
    "error": ("Twitch: reconnecting", "warn"),
}
TWITCH_HELP = (
    "One-time setup on Twitch (free):\n"
    "1. dev.twitch.tv/console → Register Your Application.\n"
    "2. Name: anything · OAuth Redirect URL: http://localhost · Category: Application Integration.\n"
    "3. Client Type: Public (important — Stream Director keeps no client secret).\n"
    "4. Copy the Client ID here and press Connect Twitch account: a code appears, approve it on twitch.tv/activate."
)
SPIN_FIELDS = (
    ("moment_cooldown_seconds", "Moment cooldown per person", "s"),
    ("suggestion_cooldown_seconds", "Suggestion cooldown per person", "s"),
    ("max_open_challenges_per_user", "Open challenges per person", ""),
    ("moment_cluster_seconds", "Moments closer than this are one moment", "s"),
    ("moment_reaction_lag_seconds", "Shift marks back by (reaction time)", "s"),
    ("notable_moment_min_users", "Notable moment: at least", "people"),
    ("poll_default_minutes", "Default poll duration", "min"),
    ("end_grace_minutes", "Same session if the stream returns within", "min"),
)


def _ids(text: str) -> list[str]:
    return [part for part in text.replace(",", " ").split() if part]


def twitch_lines(twitch: dict[str, Any] | None) -> tuple[str, str, list[str]]:
    """(title, color, details) of the Twitch connection for the status panel."""
    twitch = twitch or {}
    title, color = TWITCH_STATES.get(str(twitch.get("state")), ("Twitch: unknown", "muted"))
    details = []
    account = twitch.get("account") or {}
    if account.get("login"):
        title = f"{title} as {account.get('display_name') or account['login']}" if twitch.get("state") == "connected" else title
    if twitch.get("detail"):
        details.append(str(twitch["detail"]))
    if account.get("missing_scopes"):
        details.append("Not granted: " + ", ".join(f"{scope} ({sdt.SCOPES.get(scope, '')})" for scope in account["missing_scopes"]))
    if twitch.get("failed_subscriptions"):
        details.append("Twitch events not available: " + ", ".join(twitch["failed_subscriptions"]))
    if twitch.get("live") is not None:
        details.append("Channel is LIVE now" if twitch["live"] else "Channel is offline")
    return title, color, details


class StreamDirectorPanel(QWidget):
    def __init__(
        self,
        list_bots: Callable[[], list[tuple[str, str, Any]]],
        config_api: Any,
        restart_bot: Callable[[str], None],
        run_in_background: Callable[[Callable[[], Any], Callable[[Any], None]], None],
        token_store_for: Callable[[str], sdt.TokenStore] | None = None,
        transport: sdt.HttpTransport = sdt.urllib_transport,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_bots = list_bots
        self._config_api = config_api
        self._restart_bot = restart_bot
        self._run = run_in_background
        self._token_store_for = token_store_for or (lambda instance_id: sdt.TokenStore(instance_store.get_instance_paths(instance_id).secrets_dir))
        self._transport = transport
        self._loaded: dict[str, Any] = sdc.normalize_config({})
        self._loading = False
        self._config_ok = False
        self._current_bot: str | None = None
        self._connecting = False
        self._cancel = False
        self.device: sdt.DeviceCode | None = None

        title = QLabel("Stream Director")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "Turns every Twitch stream into a community session in Discord: a live card with a thread, moments marked "
            "by viewers, challenges, polls and predictions, a streamer inbox, community level and goals, and a recap "
            "after the stream. Its own Discord application and token; Twitch is optional (/stream start works without it)."
        )
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.bot_combo.currentIndexChanged.connect(lambda _index: self._on_bot_changed())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh)
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Stream Director bot"))
        bot_row.addWidget(self.bot_combo, 1)
        bot_row.addWidget(refresh)

        status_panel = dash.Panel("\U0001f4e1", "Status")
        self.status_dot = dash.dot("muted")
        self.status_title = QLabel("No Stream Director bot")
        self.status_title.setObjectName("cardTitle")
        head = QHBoxLayout()
        head.addWidget(self.status_dot)
        head.addWidget(self.status_title, 1)
        self.status_details = dash.muted("")
        self.status_details.setWordWrap(True)
        self.status_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_panel.body.addLayout(head)
        status_panel.body.addWidget(self.status_details)

        twitch_panel = dash.Panel("\U0001f7e3", "Twitch connection")
        self.twitch_title = QLabel("Twitch: —")
        self.twitch_title.setObjectName("cardTitle")
        self.twitch_details = dash.muted("")
        self.twitch_details.setWordWrap(True)
        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Client ID of your Twitch application (Public client)")
        self.connect_button = dash.styled_button("Connect Twitch account", "primary")
        self.connect_button.clicked.connect(self.connect_twitch)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_twitch)
        self.open_console_button = QPushButton("Open Twitch developer console")
        self.open_console_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(TWITCH_CONSOLE_URL)))
        self.device_label = QLabel("")
        self.device_label.setWordWrap(True)
        self.device_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_activation_button = QPushButton("Open twitch.tv/activate")
        self.open_activation_button.clicked.connect(self.open_activation)
        twitch_help = dash.muted(TWITCH_HELP)
        twitch_help.setWordWrap(True)
        twitch_form = QFormLayout()
        twitch_form.addRow("Twitch Client ID", self.client_id_edit)
        twitch_buttons = QHBoxLayout()
        for button in (self.connect_button, self.cancel_button, self.disconnect_button, self.open_console_button):
            twitch_buttons.addWidget(button)
        twitch_buttons.addStretch(1)
        twitch_panel.body.addWidget(self.twitch_title)
        twitch_panel.body.addWidget(self.twitch_details)
        twitch_panel.body.addLayout(twitch_form)
        twitch_panel.body.addLayout(twitch_buttons)
        twitch_panel.body.addWidget(self.device_label)
        twitch_panel.body.addWidget(self.open_activation_button)
        twitch_panel.body.addWidget(twitch_help)

        settings_panel = dash.Panel("⚙", "Discord and community")
        self.enabled_checkbox = QCheckBox("Post in Discord (uncheck to pause: sessions are still tracked)")
        self.guild_combo = QComboBox()
        self.guild_combo.currentIndexChanged.connect(lambda _index: self._load_channels())
        self.channel_combo = QComboBox()
        self.team_roles_edit = QLineEdit()
        self.team_roles_edit.setPlaceholderText("Role IDs of moderators (optional; server managers are always team)")
        self.go_live_edit = QLineEdit()
        self.go_live_edit.setPlaceholderText("Optional role ID pinged when the stream starts")
        form = QFormLayout()
        form.addRow("", self.enabled_checkbox)
        form.addRow("Server", self.guild_combo)
        form.addRow("Stream channel", self.channel_combo)
        self.language_combo = QComboBox()
        for code, label in bot_i18n.LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setToolTip("Language of everything this bot writes in Discord. Slash command descriptions change after a restart.")
        form.addRow("Bot language", self.language_combo)
        form.addRow("Stream team roles", self.team_roles_edit)
        form.addRow("Go-live ping role", self.go_live_edit)
        self.feature_boxes: dict[str, QCheckBox] = {}
        for name in sdc.FEATURES:
            box = QCheckBox(sdc.FEATURE_LABELS[name])
            self.feature_boxes[name] = box
            form.addRow("", box)
        self.spins: dict[str, QSpinBox] = {}
        for key, label, suffix in SPIN_FIELDS:
            spin = QSpinBox()
            low, high = sdc.LIMITS[key]
            spin.setRange(low, high)
            if suffix:
                spin.setSuffix(f" {suffix}")
            self.spins[key] = spin
            form.addRow(label, spin)
        settings_panel.body.addLayout(form)
        self.save_button = dash.styled_button("Save", "primary")
        self.save_restart_button = dash.styled_button("Save && Restart Bot", "secondary")
        self.save_button.clicked.connect(self.save)
        self.save_restart_button.clicked.connect(lambda: self.save(restart=True))
        # Saved / unsaved changes / saved now (and when it applies) / not saved.
        self.result_label = dash.SaveIndicator()
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.save_restart_button)
        buttons.addStretch(1)
        settings_panel.body.addLayout(buttons)
        settings_panel.body.addWidget(self.result_label)

        inbox_panel = dash.Panel("\U0001f4e5", "Streamer inbox")
        self.inbox_label = dash.muted("")
        self.inbox_label.setWordWrap(True)
        self.inbox_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        inbox_panel.body.addWidget(self.inbox_label)
        recap_panel = dash.Panel("\U0001f4fc", "Last stream")
        self.recap_label = dash.muted("")
        self.recap_label.setWordWrap(True)
        self.recap_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        recap_panel.body.addWidget(self.recap_label)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(bot_row)
        for panel in (status_panel, twitch_panel, settings_panel, inbox_panel, recap_panel):
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
        for box in (self.enabled_checkbox, *self.feature_boxes.values()):
            box.toggled.connect(lambda _checked: self._update_dirty())
        for combo in (self.guild_combo, self.channel_combo, self.language_combo):
            combo.currentIndexChanged.connect(lambda _index: self._update_dirty())
        for spin in self.spins.values():
            spin.valueChanged.connect(lambda _value: self._update_dirty())
        for edit in (self.team_roles_edit, self.go_live_edit, self.client_id_edit):
            edit.textChanged.connect(lambda _text: self._update_dirty())
        self.refresh()

    # -- helpers -----------------------------------------------------------------------------

    def _bots(self) -> list[tuple[str, str, Any]]:
        return [bot for bot in self._list_bots() if getattr(bot[2], "bot_type", None) == BOT_TYPE_ID]

    def _bot(self) -> tuple[str, Any] | None:
        instance_id = self.bot_combo.currentData()
        for bot_id, _label, info in self._bots():
            if bot_id == instance_id:
                return bot_id, info
        return None

    def _runtime(self) -> Any:
        bot = self._bot()
        return None if bot is None else admin_terminal.runtime_dir_for_logs(bot[1].logs_dir)

    @staticmethod
    def _running(info: Any) -> bool:
        return str(getattr(info, "state", "")).upper() == "RUNNING"

    def _status(self) -> dict[str, Any] | None:
        bot = self._bot()
        runtime = self._runtime()
        if bot is None or runtime is None or not self._running(bot[1]):
            return None
        return admin_terminal.read_runtime_json(runtime, STATUS_FILE_NAME)

    # -- loading -------------------------------------------------------------------------------

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

    def _set_enabled(self, enabled: bool) -> None:
        widgets = [
            self.save_button,
            self.save_restart_button,
            self.enabled_checkbox,
            self.guild_combo,
            self.channel_combo,
            self.team_roles_edit,
            self.go_live_edit,
            self.client_id_edit,
            self.connect_button,
            self.disconnect_button,
            self.language_combo,
            *self.feature_boxes.values(),
            *self.spins.values(),
        ]
        for widget in widgets:
            widget.setEnabled(enabled)

    def load(self) -> None:
        """Fill every control from the selected bot's saved config (nothing from the previous bot stays)."""
        bot = self._bot()
        self._current_bot = bot[0] if bot is not None else None
        self._set_enabled(bot is not None)
        self._update_connect_buttons()
        self.guild_combo.setToolTip("")
        if bot is None:
            self._config_ok = False
            self.result_label.show_disabled("Settings appear here once a Stream Director bot exists.")
            self.status_title.setText("No Stream Director bot")
            dash.colored(self.status_title, "muted")
            dash.set_dot_color(self.status_dot, "muted")
            self.status_details.setText("Add one with Bots → Add Bot → Stream Director Bot (it needs its own Discord application and token).")
            self.twitch_title.setText("Twitch: —")
            self.twitch_details.setText("")
            self.inbox_label.setText("")
            self.recap_label.setText("")
            return
        problem = None
        try:
            self._loaded = sdc.normalize_config(self._config_api.get_config_snapshot(bot[0]).effective)
        except Exception as exc:
            self._loaded = sdc.normalize_config({})
            problem = str(exc)
        data = self._loaded
        self._loading = True
        try:
            self.enabled_checkbox.setChecked(data["enabled"])
            self.client_id_edit.setText(data["twitch_client_id"])
            self.team_roles_edit.setText(", ".join(data["team_role_ids"]))
            self.go_live_edit.setText(data["go_live_role_id"] or "")
            index = self.language_combo.findData(data["language"])
            self.language_combo.setCurrentIndex(index if index >= 0 else 0)
            for name, box in self.feature_boxes.items():
                box.setChecked(data["features"][name])
            for key, spin in self.spins.items():
                spin.setValue(int(data[key]))
            self._load_guilds(data["guild_id"], data["channel_id"])
        finally:
            self._loading = False
        self._config_ok = problem is None
        if problem:
            # Fail closed: defaults are neither shown as saved nor written over the real config.
            self._set_enabled(False)
            self.result_label.show_disabled(
                f"Config problem: {problem} The form is locked so nothing is saved over it — fix or reset the config in "
                "Bots → Advanced JSON, then press Refresh."
            )
            dash.colored(self.result_label, "bad")
        else:
            self.result_label.show_clean(f"Showing the saved settings of {self.bot_combo.currentText() or 'this bot'}.")
        self.refresh_status(config_problem=problem)

    @property
    def dirty(self) -> bool:
        if self._current_bot is None or not self._config_ok:
            return False
        try:
            return sdc.normalize_config(self.current_settings()) != self._loaded
        except Exception:
            return True

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

    def _load_guilds(self, guild_id: str | None, channel_id: str | None) -> None:
        runtime = self._runtime()
        status = admin_terminal.read_bot_status(runtime) if runtime is not None else None
        self._channel_wanted = channel_id
        self.guild_combo.blockSignals(True)
        self.guild_combo.clear()
        guilds = list((status or {}).get("guilds") or [])
        if not guilds:
            self.guild_combo.setToolTip("Start this bot once: the Manager lists the servers and channels the bot can see.")
        if not guild_id:
            self.guild_combo.addItem(CHOOSE_SERVER, {})
        for guild in guilds:
            self.guild_combo.addItem(str(guild.get("name")), guild)
        if guild_id and not any(str(guild.get("id")) == guild_id for guild in guilds):
            self.guild_combo.addItem(f"Server {guild_id} (bot offline or not in it)", {"id": guild_id, "channels": []})
        index = next((i for i in range(self.guild_combo.count()) if guild_id and str(self.guild_combo.itemData(i).get("id")) == guild_id), 0)
        self.guild_combo.setCurrentIndex(index)
        self.guild_combo.blockSignals(False)
        self._load_channels()

    def _load_channels(self) -> None:
        wanted = getattr(self, "_channel_wanted", None)
        self.channel_combo.clear()
        guild = self.guild_combo.currentData() or {}
        if not wanted:
            self.channel_combo.addItem(CHOOSE_CHANNEL, None)
        for channel in guild.get("channels") or []:
            if channel.get("type") in ("text", "news"):
                self.channel_combo.addItem(f"#{channel.get('name')}", str(channel.get("id")))
        if wanted and self.channel_combo.findData(wanted) < 0:
            self.channel_combo.addItem(f"Channel {wanted} (not in the bot's list)", wanted)
        index = self.channel_combo.findData(wanted)
        self.channel_combo.setCurrentIndex(index if index >= 0 else 0)
        self._channel_wanted = None

    def state_file_problem(self, instance_id: str) -> str | None:
        try:
            return sds.StateStore(instance_store.get_instance_paths(instance_id).data_dir).verify()
        except Exception as exc:
            return str(exc)

    def refresh_status(self, config_problem: str | None = None) -> None:
        bot = self._bot()
        if bot is None:
            return
        data = self._loaded
        running = self._running(bot[1])
        status = self._status()
        state_problem = self.state_file_problem(bot[0])
        details: list[str] = []
        diagnosis = (status or {}).get("diagnosis") or {}
        if state_problem:
            title, color = "State file problem", "bad"
            details.append(state_problem)
        elif config_problem:
            title, color = "Config problem", "bad"
            details.append(f"{config_problem} Nothing is posted until this is fixed.")
        elif not running:
            title, color = "Stopped", "warn"
            details.append("Start this bot on the Bots page (its own Discord token is set in Bot Setup).")
        elif not sdc.is_configured(data):
            title, color = "Not configured — nothing is posted", "bad"
            details.append("Choose the server and the stream channel below and press Save; the bot picks it up within 15 seconds.")
        elif data.get("enabled") is not True:
            title, color = "Paused", "muted"
            details.append("Posting is paused. Sessions are still tracked.")
        elif status is None:
            title, color = "Starting…", "warn"
            details.append("Waiting for the bot to report its status.")
        elif diagnosis.get("code") in PROBLEM_CODES:
            title, color = "Problem", "bad"
            details.append(str(diagnosis.get("text")))
        elif diagnosis.get("code") == "live":
            title, color = "LIVE — session running", "ok"
        elif diagnosis.get("code") == "ending":
            title, color = "Stream offline — recap in a few minutes", "warn"
        else:
            title, color = "Ready — waiting for the stream", "ok"
        if status:
            if status.get("problem"):
                details.append(f"Last Discord problem: {status['problem']}")
            if status.get("guild_name") or status.get("channel_name"):
                details.append(f"Server: {status.get('guild_name') or '-'} · channel: #{status.get('channel_name') or '-'}")
            if status.get("missing_permissions"):
                details.append("Missing in the stream channel: " + ", ".join(status["missing_permissions"]))
            if sdc.is_configured(data) and not status.get("commands_synced"):
                details.append("Slash commands are not registered in that server yet (they appear a few seconds after the bot connects).")
            director = status.get("director") or {}
            session = director.get("session")
            if session:
                minutes = int((time.time() - float(session.get("started_at") or time.time())) // 60)
                details.append(f"Session #{session['id']} ({session['source']}) · {sd.plain(session.get('title') or '')} · {minutes} min · {session.get('moments', 0)} moments")
            community = director.get("community")
            if community:
                details.append(
                    f"Community level {community['level']} ({community['level_progress']}/{community['level_span']} XP) · "
                    f"season {community['season']}: {community['season_points']} XP"
                )
        self.status_title.setText(title)
        dash.colored(self.status_title, color)
        dash.set_dot_color(self.status_dot, color)
        self.status_details.setText("\n".join(details))
        self._refresh_twitch(status)
        self._refresh_inbox(status)

    def _refresh_twitch(self, status: dict[str, Any] | None) -> None:
        bot = self._bot()
        if bot is None:
            return
        token = self._token_store_for(bot[0]).load()
        if status and status.get("twitch"):
            title, color, details = twitch_lines(status["twitch"])
        elif token is not None:
            title, color, details = f"Twitch: account {token.display_name} connected", "ok", []
            missing = token.public()["missing_scopes"]
            if missing:
                details.append("Not granted: " + ", ".join(missing))
        elif self._loaded.get("twitch_client_id"):
            title, color, details = "Twitch: account not connected", "warn", ["Press Connect Twitch account."]
        else:
            title, color, details = "Twitch: not set up (optional)", "muted", ["Without Twitch, start and end sessions with /stream start and /stream end."]
        if token is not None and self._loaded.get("twitch_client_id") and token.client_id != self._loaded["twitch_client_id"]:
            details.append("The saved account belongs to another Client ID: connect again.")
        self.twitch_title.setText(title)
        dash.colored(self.twitch_title, color)
        self.twitch_details.setText("\n".join(details))
        self.disconnect_button.setEnabled(token is not None and not self._connecting)

    def _refresh_inbox(self, status: dict[str, Any] | None) -> None:
        director = (status or {}).get("director") or {}
        items = director.get("inbox") or []
        if not status:
            self.inbox_label.setText("Shown while the bot runs. In Discord the stream team opens it with /inbox.")
        elif not items:
            self.inbox_label.setText("Nothing waiting. Viewers send things with 💬 Suggest, /suggest, /challenge or !q / !game in Twitch chat.")
        else:
            lines = []
            for item in items:
                label = sd.INBOX_LABELS.get(item["kind"], item["kind"])
                link = f" {item['link']}" if item.get("link") else ""
                lines.append(f"#{item['id']} {label}: {sd.plain(item['text'])}{link} — {sd.plain(item['author'])} ({item['platform']}) · 👍 {item['supporters']}")
            accepted = director.get("accepted_challenges") or []
            if accepted:
                lines.append("Accepted challenges: " + "; ".join(f"#{item['id']} {sd.plain(item['text'])}" for item in accepted))
            lines.append("Mark items done or dismissed in Discord with /inbox.")
            self.inbox_label.setText("\n".join(lines))
        recap = director.get("last_recap") or {}
        if recap:
            lines = []
            for section, section_lines in recap.items():
                lines.append(f"{section}:")
                lines.extend(f"  {sd.plain(line)}" for line in section_lines[:6])
            self.recap_label.setText("\n".join(lines))
        else:
            self.recap_label.setText("No finished stream yet." if status else "")

    # -- saving --------------------------------------------------------------------------------

    def current_settings(self) -> dict[str, Any]:
        guild = self.guild_combo.currentData() or {}
        return {
            "enabled": self.enabled_checkbox.isChecked(),
            "language": self.language_combo.currentData() or sdc.DEFAULT_CONFIG["language"],
            "guild_id": str(guild.get("id")) if guild.get("id") else None,
            "channel_id": self.channel_combo.currentData() or None,
            "twitch_client_id": self.client_id_edit.text().strip(),
            "team_role_ids": _ids(self.team_roles_edit.text()),
            "go_live_role_id": self.go_live_edit.text().strip() or None,
            "features": {name: box.isChecked() for name, box in self.feature_boxes.items()},
            **{key: spin.value() for key, spin in self.spins.items()},
        }

    def save(self, restart: bool = False) -> bool:
        bot = self._bot()
        if bot is None:
            return False
        if not self._config_ok:
            self.result_label.show_error("the stored config has a problem; fix it in Bots → Advanced JSON first.")
            return False
        previous = dict(self._loaded)
        try:
            settings = sdc.normalize_config(self.current_settings())
            self._config_api.save_config_overrides(bot[0], dict(settings))
        except Exception as exc:
            self.result_label.show_error(str(exc))
            return False
        running = self._running(bot[1])
        notes = []
        if restart and running:
            self._restart_bot(bot[0])
            notes.append("Restarting the bot now.")
        elif not sdc.is_configured(settings):
            notes.append("Choose a server and the stream channel so the bot can post.")
        elif running:
            notes.append("Applied live: the running bot picks it up within 15 seconds (no restart needed).")
        else:
            notes.append("The bot uses it when it starts.")
        if running and not restart and settings["language"] != previous.get("language"):
            notes.append("Slash command descriptions switch language after a restart.")
        if settings["twitch_client_id"] != previous.get("twitch_client_id"):
            notes.append("The Twitch Client ID changed: connect the Twitch account again.")
        # Show what is stored now (a round trip, not the form as typed).
        self.load()
        self.result_label.show_saved(" ".join(notes))
        return True

    # -- Twitch connection (Device Code flow) -----------------------------------------------------

    def _update_connect_buttons(self) -> None:
        has_bot = self._bot() is not None
        self.connect_button.setEnabled(has_bot and not self._connecting)
        self.cancel_button.setVisible(self._connecting)
        self.open_activation_button.setVisible(self._connecting and self.device is not None)
        if not self._connecting:
            self.device_label.setText("")

    def connect_twitch(self) -> None:
        bot = self._bot()
        if bot is None or self._connecting:
            return
        client_id = self.client_id_edit.text().strip().lower()
        try:
            sdc.normalize_config({"twitch_client_id": client_id})
        except sdc.ConfigError as exc:
            self.device_label.setText(str(exc))
            return
        if not client_id:
            self.device_label.setText("Paste the Client ID of your Twitch application first (see the steps below).")
            return
        self._connecting = True
        self._cancel = False
        self.device = None
        self._update_connect_buttons()
        self.device_label.setText("Asking Twitch for a login code…")
        store = self._token_store_for(bot[0])
        instance_id = bot[0]

        def start() -> sdt.DeviceCode:
            return sdt.start_device_flow(client_id, self._transport)

        def started(result: Any) -> None:
            if not getattr(result, "ok", False):
                self._finish_connect(False, str(getattr(result, "message", "Twitch login failed.")))
                return
            self.device = result.value
            self.device_label.setText(
                f"Open {self.device.verification_uri.split('?')[0]} and enter the code:\n\n    {self.device.user_code}\n\n"
                "Waiting for your approval on Twitch…"
            )
            self._update_connect_buttons()
            self.open_activation()

            def wait() -> sdt.TwitchToken:
                return sdt.complete_device_flow(client_id, self.device, store, transport=self._transport, cancelled=lambda: self._cancel)

            self._run(wait, lambda outcome: self._connected(instance_id, client_id, outcome))

        self._run(start, started)

    def _connected(self, instance_id: str, client_id: str, result: Any) -> None:
        if not getattr(result, "ok", False):
            self._finish_connect(False, str(getattr(result, "message", "Twitch login failed.")))
            return
        token: sdt.TwitchToken = result.value
        # The Client ID that produced the token is saved with it.
        try:
            snapshot = self._config_api.get_config_snapshot(instance_id)
            settings = sdc.normalize_config({**snapshot.effective, "twitch_client_id": client_id})
            self._config_api.save_config_overrides(instance_id, dict(settings))
            self._loaded = settings
        except Exception as exc:
            self._finish_connect(False, f"Connected, but the Client ID was not saved: {exc}")
            return
        missing = token.public()["missing_scopes"]
        note = f" Not granted: {', '.join(missing)}." if missing else ""
        self._finish_connect(True, f"Connected to Twitch as {token.display_name}.{note} The bot picks it up within a few seconds.")

    def _finish_connect(self, ok: bool, message: str) -> None:
        self._connecting = False
        self.device = None
        self._update_connect_buttons()
        self.device_label.setText(message)
        dash.colored(self.device_label, "ok" if ok else "bad")
        self.refresh_status()

    def cancel_connect(self) -> None:
        self._cancel = True
        self.device_label.setText("Cancelling…")

    def open_activation(self) -> None:
        if self.device is not None:
            QDesktopServices.openUrl(QUrl(self.device.verification_uri))

    def disconnect_twitch(self) -> None:
        bot = self._bot()
        if bot is None:
            return
        store = self._token_store_for(bot[0])
        token = store.load()
        store.clear()
        if token is not None:
            self._run(lambda: sdt.revoke(token, self._transport), lambda _result: None)
        self.device_label.setText("Twitch account disconnected. The bot keeps working with Discord only.")
        self.refresh_status()
