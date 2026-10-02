"""Manager page for the Game Presence capability of a bot instance.

Settings live in the instance config section ``game_presence`` (ConfigStore
overrides, same validation as the bot). Servers/channels come from the
bot's ``bot_status.json``; operational status from
``game_presence_status.json`` written by the running bot.
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import admin_terminal
import game_presence
import manager_dashboard as dash

STATUS_FILE_NAME = "game_presence_status.json"
REQUIRED_INTENTS_TEXT = (
    "Discord Developer Portal → your application → Bot → Privileged Gateway Intents:\n"
    "• Presence Intent — required while Game Presence is enabled (who plays what).\n"
    "• Server Members Intent — always required by this bot.\n"
    "Message Content Intent is not needed for Game Presence. Intent changes apply after a bot restart; "
    "turning Game Presence off removes the Presence Intent requirement."
)
SPIN_FIELDS = (
    ("delay_minutes", "Suggestion delay", "min", "Players must be in the same game this long before a suggestion."),
    ("group_cooldown_minutes", "Same group + game cooldown", "min", "No repeat for the same people and game."),
    ("user_cooldown_minutes", "Per-user cooldown", "min", "A person is mentioned at most once in this time."),
    ("guild_cooldown_minutes", "Server cooldown", "min", "At most one suggestion per server in this time."),
)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


class GamePresencePanel(QWidget):
    def __init__(
        self,
        list_bots: Callable[[], list[tuple[str, str, Any]]],
        config_api: Any,
        restart_bot: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_bots = list_bots
        self._config_api = config_api
        self._restart_bot = restart_bot
        self._loaded: dict[str, Any] = dict(game_presence.DEFAULT_CONFIG)

        title = QLabel("Game Presence")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "When two or more members play the same game but are not together in one voice channel, the bot "
            "posts one public suggestion in your channel and pings them. Members can mute or allow these pings "
            "with buttons under every suggestion. Uses only Discord presence and voice state."
        )
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.bot_combo.currentIndexChanged.connect(lambda _index: self.load())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh)
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Bot"))
        bot_row.addWidget(self.bot_combo, 1)
        bot_row.addWidget(refresh)

        status_panel = dash.Panel("\U0001f4e1", "Status")
        self.status_dot = dash.dot("muted")
        self.status_title = QLabel("Disabled")
        self.status_title.setObjectName("cardTitle")
        status_head = QHBoxLayout()
        status_head.addWidget(self.status_dot)
        status_head.addWidget(self.status_title, 1)
        self.status_details = dash.muted("")
        self.status_details.setWordWrap(True)
        self.status_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status_panel.body.addLayout(status_head)
        status_panel.body.addWidget(self.status_details)

        intents_panel = dash.Panel("\U0001f511", "Required Discord intents")
        intents = QLabel(REQUIRED_INTENTS_TEXT)
        intents.setWordWrap(True)
        intents_panel.body.addWidget(intents)

        settings_panel = dash.Panel("⚙", "Settings")
        self.enabled_checkbox = QCheckBox("Enable Game Presence for this bot")
        self.guild_combo = QComboBox()
        self.guild_combo.currentIndexChanged.connect(lambda _index: self._load_channels())
        self.channel_combo = QComboBox()
        self.spins: dict[str, QSpinBox] = {}
        form = QFormLayout()
        form.addRow("", self.enabled_checkbox)
        form.addRow("Server", self.guild_combo)
        form.addRow("Suggestion channel", self.channel_combo)
        for key, label, suffix, tip in SPIN_FIELDS:
            spin = QSpinBox()
            low, high = game_presence.LIMITS[key]
            spin.setRange(low, high)
            spin.setSuffix(f" {suffix}")
            spin.setToolTip(tip)
            self.spins[key] = spin
            form.addRow(label, spin)
        self.voice_checkbox = QCheckBox("Voice-aware: invite people to a voice channel where others already play")
        self.ai_checkbox = QCheckBox("Let the AI vary the wording (mentions and timing stay rule-based)")
        form.addRow("", self.voice_checkbox)
        form.addRow("", self.ai_checkbox)
        self.allowlist_edit = QPlainTextEdit()
        self.allowlist_edit.setPlaceholderText("Only these games (one per line). Empty = every game.")
        self.allowlist_edit.setFixedHeight(80)
        self.ignore_edit = QPlainTextEdit()
        self.ignore_edit.setPlaceholderText("Never suggest these games (one per line).")
        self.ignore_edit.setFixedHeight(80)
        form.addRow("Game allowlist", self.allowlist_edit)
        form.addRow("Ignore list", self.ignore_edit)
        settings_panel.body.addLayout(form)
        self.save_button = dash.styled_button("Save", "primary")
        self.save_restart_button = dash.styled_button("Save && Restart Bot", "secondary")
        self.save_button.clicked.connect(self.save)
        self.save_restart_button.clicked.connect(lambda: self.save(restart=True))
        self.result_label = dash.muted("")
        self.result_label.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.save_restart_button)
        buttons.addStretch(1)
        settings_panel.body.addLayout(buttons)
        settings_panel.body.addWidget(self.result_label)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(bot_row)
        layout.addWidget(status_panel)
        layout.addWidget(settings_panel)
        layout.addWidget(intents_panel)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.refresh()

    # -- helpers ---------------------------------------------------------------

    def _bot(self) -> tuple[str, Any] | None:
        instance_id = self.bot_combo.currentData()
        for bot_id, _label, info in self._list_bots():
            if bot_id == instance_id:
                return bot_id, info
        return None

    def _runtime(self) -> Any:
        bot = self._bot()
        return None if bot is None else admin_terminal.runtime_dir_for_logs(bot[1].logs_dir)

    @staticmethod
    def _running(info: Any) -> bool:
        return str(getattr(info, "state", "")).upper() == "RUNNING"

    # -- loading -------------------------------------------------------------------

    def refresh(self) -> None:
        current = self.bot_combo.currentData()
        self.bot_combo.blockSignals(True)
        self.bot_combo.clear()
        for instance_id, label, _info in self._list_bots():
            self.bot_combo.addItem(label, instance_id)
        index = self.bot_combo.findData(current)
        self.bot_combo.setCurrentIndex(index if index >= 0 else 0)
        self.bot_combo.blockSignals(False)
        self.load()

    def load(self) -> None:
        bot = self._bot()
        self.save_button.setEnabled(bot is not None)
        self.save_restart_button.setEnabled(bot is not None)
        if bot is None:
            self.status_title.setText("No bots yet")
            return
        try:
            effective = self._config_api.get_config_snapshot(bot[0]).effective
            self._loaded = game_presence.normalize_config_dict(effective.get("game_presence"))
            problem = None
        except Exception as exc:
            self._loaded = dict(game_presence.DEFAULT_CONFIG)
            problem = str(exc)
        data = self._loaded
        self.enabled_checkbox.setChecked(data["enabled"])
        for key, spin in self.spins.items():
            spin.setValue(int(data[key]))
        self.voice_checkbox.setChecked(data["voice_aware"])
        self.ai_checkbox.setChecked(data["ai_rewrite"])
        self.allowlist_edit.setPlainText("\n".join(data["allowlist"]))
        self.ignore_edit.setPlainText("\n".join(data["ignore_list"]))
        self._load_guilds(data["guild_id"], data["channel_id"])
        self.result_label.setText(f"Config problem: {problem}" if problem else "")
        self.refresh_status()

    def _load_guilds(self, guild_id: str | None, channel_id: str | None) -> None:
        status = admin_terminal.read_bot_status(self._runtime()) if self._runtime() is not None else None
        self._channel_wanted = channel_id
        self.guild_combo.blockSignals(True)
        self.guild_combo.clear()
        guilds = list((status or {}).get("guilds") or [])
        for guild in guilds:
            self.guild_combo.addItem(str(guild.get("name")), guild)
        if guild_id and not any(str(guild.get("id")) == guild_id for guild in guilds):
            # Bot offline or not in that server any more: keep the saved choice visible.
            self.guild_combo.addItem(f"Server {guild_id} (bot offline or not in it)", {"id": guild_id, "channels": []})
        index = next((i for i in range(self.guild_combo.count()) if str(self.guild_combo.itemData(i).get("id")) == guild_id), 0)
        self.guild_combo.setCurrentIndex(index)
        self.guild_combo.blockSignals(False)
        self._load_channels()

    def _load_channels(self) -> None:
        # Saved channel only while loading; a server switched by hand starts fresh.
        wanted = getattr(self, "_channel_wanted", None)
        self.channel_combo.clear()
        guild = self.guild_combo.currentData() or {}
        for channel in guild.get("channels") or []:
            if channel.get("type") in ("text", "news"):
                self.channel_combo.addItem(f"#{channel.get('name')}", str(channel.get("id")))
        if wanted and self.channel_combo.findData(wanted) < 0:
            self.channel_combo.addItem(f"Channel {wanted} (not in the bot's list)", wanted)
        index = self.channel_combo.findData(wanted)
        self.channel_combo.setCurrentIndex(index if index >= 0 else 0)
        self._channel_wanted = None

    def refresh_status(self) -> None:
        bot = self._bot()
        if bot is None:
            return
        enabled = self._loaded.get("enabled") is True
        running = self._running(bot[1])
        status = admin_terminal.read_runtime_json(self._runtime(), STATUS_FILE_NAME) if running else None
        details = []
        if not enabled:
            title, color = "Disabled", "muted"
            details.append("Turn it on below, choose a server and a channel, save and restart the bot.")
        elif not running:
            title, color = "Enabled · bot stopped", "warn"
            details.append("Start the bot to begin tracking.")
        elif status is None or status.get("enabled") is not True or not status.get("presence_intent"):
            title, color = "Enabled · restart needed", "warn"
            details.append("The running bot started before Game Presence was enabled. Restart it (Presence Intent is requested at startup).")
        elif status.get("problem"):
            title, color = "Enabled · problem", "bad"
            details.append(str(status["problem"]))
        else:
            title, color = "Active", "ok"
        if status:
            details.append(
                f"Server: {status.get('guild_name') or status.get('guild_id') or '-'} · "
                f"channel: #{status.get('channel_name') or status.get('channel_id') or '-'}"
            )
            details.append(
                f"Players in games now: {status.get('tracked_players', 0)} · groups waiting: {status.get('pending_groups', 0)}"
            )
            games = status.get("top_games") or []
            if games:
                details.append("Top games: " + ", ".join(f"{item.get('game')} ({item.get('players')})" for item in games))
            last = status.get("last_suggestion")
            if isinstance(last, dict) and isinstance(last.get("at"), (int, float)):
                minutes = int((time.time() - last["at"]) // 60)
                details.append(
                    f"Last suggestion: {last.get('name') or last.get('game')} · "
                    f"{len(last.get('users') or [])} people · {minutes} min ago"
                )
        self.status_title.setText(title)
        dash.colored(self.status_title, color)
        dash.set_dot_color(self.status_dot, color)
        self.status_details.setText("\n".join(details))

    # -- saving -----------------------------------------------------------------------

    def current_settings(self) -> dict[str, Any]:
        guild = self.guild_combo.currentData() or {}
        return {
            "enabled": self.enabled_checkbox.isChecked(),
            "guild_id": str(guild.get("id")) if guild.get("id") else None,
            "channel_id": self.channel_combo.currentData() or None,
            **{key: spin.value() for key, spin in self.spins.items()},
            "voice_aware": self.voice_checkbox.isChecked(),
            "allowlist": _lines(self.allowlist_edit.toPlainText()),
            "ignore_list": _lines(self.ignore_edit.toPlainText()),
            "ai_rewrite": self.ai_checkbox.isChecked(),
        }

    def save(self, restart: bool = False) -> bool:
        bot = self._bot()
        if bot is None:
            return False
        try:
            settings = game_presence.normalize_config_dict(self.current_settings())
            overrides = dict(self._config_api.get_config_snapshot(bot[0]).overrides)
            overrides["game_presence"] = settings
            self._config_api.save_config_overrides(bot[0], overrides)
        except Exception as exc:
            self.result_label.setText(f"Not saved: {exc}")
            dash.colored(self.result_label, "bad")
            return False
        intent_change = settings["enabled"] != self._loaded.get("enabled")
        self._loaded = settings
        if restart and self._running(bot[1]):
            self._restart_bot(bot[0])
            message = "Saved. Restarting the bot..."
        elif intent_change and self._running(bot[1]):
            message = "Saved. Restart the bot to apply (Presence Intent is requested at startup)."
        else:
            message = "Saved. Other settings apply within 15 seconds while the bot runs."
        self.result_label.setText(message)
        dash.colored(self.result_label, "ok")
        self.refresh_status()
        return True
