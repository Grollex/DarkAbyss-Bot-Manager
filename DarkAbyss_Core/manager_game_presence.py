"""Manager page for Game Presence Bot instances (bot type ``game_presence``).

A Game Presence bot is its own Discord application with its own token,
process, config, data and (optional) AI keys. Settings are the instance's
top-level config (ConfigStore overrides, same validation as the bot).
Servers/channels come from the bot's own ``bot_status.json``; operational
status from its ``game_presence_status.json``.

Older versions ran Game Presence inside the Admin bot (config section
``game_presence``). Such settings are ignored by the Admin bot now; the page
shows where they are and imports them into a Game Presence bot only when the
user asks (nothing is deleted).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
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
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import admin_terminal
import game_presence
import instance_store
import manager_dashboard as dash

BOT_TYPE_ID = "game_presence"
LEGACY_BOT_TYPE_ID = "admin"
LEGACY_SECTION = "game_presence"
STATUS_FILE_NAME = "game_presence_status.json"
STATE_FILE_NAME = "game_presence_state.json"
LEGACY_STATE_FILE_NAME = "admin_features.json"
REQUIRED_INTENTS_TEXT = (
    "Discord Developer Portal → your Game Presence application → Bot → Privileged Gateway Intents:\n"
    "• Presence Intent — required (who plays what).\n"
    "• Server Members Intent — required.\n"
    "Message Content Intent is not needed. Intent changes apply after a bot restart. If one is missing, "
    "the bot stops and its log names the intent to enable."
)
SPIN_FIELDS = (
    ("delay_minutes", "Suggestion delay", "min", "Players must be in the same game this long before a suggestion."),
    ("group_cooldown_minutes", "Same group + game cooldown", "min", "No repeat for the same people and game."),
    ("user_cooldown_minutes", "Per-user cooldown", "min", "A person is mentioned at most once in this time."),
    ("guild_cooldown_minutes", "Server cooldown", "min", "At most one suggestion per server in this time."),
)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _legacy_section(config_api: Any, instance_id: str) -> dict[str, Any] | None:
    """The old Admin ``game_presence`` section, if the user ever saved one."""
    try:
        overrides = config_api.get_config_snapshot(instance_id).overrides
    except Exception:
        return None
    section = overrides.get(LEGACY_SECTION) if isinstance(overrides, dict) else None
    return section if isinstance(section, dict) else None


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
        self._loaded: dict[str, Any] = dict(game_presence.DEFAULT_CONFIG, enabled=True)

        title = QLabel("Game Presence")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "A separate bot with its own Discord application and token. When two or more members play the same "
            "game but are not together in one voice channel, it posts one public suggestion in your channel and "
            "pings them. Members mute or allow these pings with buttons under every suggestion. Add one with "
            "Bots → Add Bot → Game Presence Bot."
        )
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.bot_combo.currentIndexChanged.connect(lambda _index: self.load())
        refresh = QPushButton("⟳  Refresh")
        refresh.clicked.connect(self.refresh)
        bot_row = QHBoxLayout()
        bot_row.addWidget(QLabel("Game Presence bot"))
        bot_row.addWidget(self.bot_combo, 1)
        bot_row.addWidget(refresh)

        self.legacy_panel = dash.Panel("ℹ", "Settings from the old Admin module")
        # Explicit line breaks instead of word wrap: wrapped labels inside a
        # panel in a scroll area get squeezed below their real height.
        self.legacy_label = QLabel("")
        self.legacy_combo = QComboBox()
        self.import_button = QPushButton("Import into this bot")
        self.import_button.clicked.connect(self.import_legacy)
        legacy_row = QHBoxLayout()
        legacy_row.addWidget(QLabel("From"))
        legacy_row.addWidget(self.legacy_combo, 1)
        legacy_row.addWidget(self.import_button)
        self.legacy_panel.body.addWidget(self.legacy_label)
        self.legacy_panel.body.addLayout(legacy_row)

        status_panel = dash.Panel("\U0001f4e1", "Status")
        self.status_dot = dash.dot("muted")
        self.status_title = QLabel("No Game Presence bot")
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
        self.enabled_checkbox = QCheckBox("Post suggestions (uncheck to pause this bot)")
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
        self.ai_checkbox = QCheckBox("Let the AI vary the wording (this bot's own AI keys; mentions and timing stay rule-based)")
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
        # Panels never shrink below their natural height: the page scrolls instead.
        for panel in (self.legacy_panel, status_panel, settings_panel, intents_panel):
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
        self.refresh()

    # -- helpers ---------------------------------------------------------------

    def _presence_bots(self) -> list[tuple[str, str, Any]]:
        return [bot for bot in self._list_bots() if getattr(bot[2], "bot_type", None) == BOT_TYPE_ID]

    def _legacy_bots(self) -> list[tuple[str, str, Any]]:
        """Admin bots that still carry an old ``game_presence`` section (ignored by them)."""
        return [
            bot
            for bot in self._list_bots()
            if getattr(bot[2], "bot_type", None) == LEGACY_BOT_TYPE_ID and _legacy_section(self._config_api, bot[0]) is not None
        ]

    def _bot(self) -> tuple[str, Any] | None:
        instance_id = self.bot_combo.currentData()
        for bot_id, _label, info in self._presence_bots():
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
        for instance_id, label, _info in self._presence_bots():
            self.bot_combo.addItem(label, instance_id)
        index = self.bot_combo.findData(current)
        self.bot_combo.setCurrentIndex(index if index >= 0 else 0)
        self.bot_combo.blockSignals(False)
        self._refresh_legacy()
        self.load()

    def _refresh_legacy(self) -> None:
        legacy = self._legacy_bots()
        self.legacy_combo.clear()
        for instance_id, label, _info in legacy:
            self.legacy_combo.addItem(label, instance_id)
        self.legacy_panel.setVisible(bool(legacy))
        if legacy:
            names = ", ".join(label for _id, label, _info in legacy)
            self.legacy_label.setText(
                f"{names}: Game Presence settings from an older version (Admin bots ignore them now).\n"
                "Import copies them into the selected Game Presence bot; opt-outs and cooldowns too while it is stopped.\n"
                "Nothing is deleted from the Admin bot."
            )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.save_button,
            self.save_restart_button,
            self.enabled_checkbox,
            self.guild_combo,
            self.channel_combo,
            self.voice_checkbox,
            self.ai_checkbox,
            self.allowlist_edit,
            self.ignore_edit,
            *self.spins.values(),
        ):
            widget.setEnabled(enabled)
        self.import_button.setEnabled(enabled)

    def load(self) -> None:
        bot = self._bot()
        self._set_controls_enabled(bot is not None)
        if bot is None:
            self.status_title.setText("No Game Presence bot")
            dash.colored(self.status_title, "muted")
            dash.set_dot_color(self.status_dot, "muted")
            self.status_details.setText("Add one with Bots → Add Bot → Game Presence Bot (it needs its own Discord application and token).")
            self.result_label.setText("")
            return
        try:
            effective = self._config_api.get_config_snapshot(bot[0]).effective
            self._loaded = game_presence.normalize_bot_config(effective)
            problem = None
        except Exception as exc:
            # Fail closed: show the problem, keep defaults in the form, save
            # only what the user explicitly confirms.
            self._loaded = dict(game_presence.DEFAULT_CONFIG, enabled=True)
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
        self.refresh_status(config_problem=problem)

    def _load_guilds(self, guild_id: str | None, channel_id: str | None) -> None:
        runtime = self._runtime()
        status = admin_terminal.read_bot_status(runtime) if runtime is not None else None
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

    def refresh_status(self, config_problem: str | None = None) -> None:
        bot = self._bot()
        if bot is None:
            return
        data = self._loaded
        running = self._running(bot[1])
        runtime = self._runtime()
        status = admin_terminal.read_runtime_json(runtime, STATUS_FILE_NAME) if running and runtime is not None else None
        details = []
        if config_problem:
            title, color = "Config problem", "bad"
            details.append(f"{config_problem} The bot posts nothing until this is fixed.")
        elif not running:
            title, color = "Stopped", "warn"
            details.append("Start this bot on the Bots page (its own token is set in Bot Setup).")
        elif data.get("enabled") is not True:
            title, color = "Paused", "muted"
            details.append("Posting is paused. Check 'Post suggestions' and save to resume.")
        elif not game_presence.is_configured(data):
            title, color = "Not configured", "warn"
            details.append("Choose the server and the suggestion channel below, then Save.")
        elif status is None:
            title, color = "Starting...", "warn"
            details.append("Waiting for the bot to report its status.")
        elif status.get("problem"):
            title, color = "Problem", "bad"
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
            settings = game_presence.normalize_bot_config(self.current_settings())
            # The whole config of a Game Presence bot is these settings.
            self._config_api.save_config_overrides(bot[0], dict(settings))
        except Exception as exc:
            self.result_label.setText(f"Not saved: {exc}")
            dash.colored(self.result_label, "bad")
            return False
        self._loaded = settings
        if restart and self._running(bot[1]):
            self._restart_bot(bot[0])
            message = "Saved. Restarting the bot..."
        elif not game_presence.is_configured(settings):
            message = "Saved. Choose a server and a channel so the bot can post."
        else:
            message = "Saved. Changes apply within 15 seconds while the bot runs."
        self.result_label.setText(message)
        dash.colored(self.result_label, "ok")
        self.refresh_status()
        return True

    # -- import from the old Admin module ------------------------------------------------

    def import_legacy(self) -> bool:
        """Copy an Admin bot's old Game Presence settings (and, if the Game
        Presence bot is stopped, opt-outs/history/cooldowns) into the selected
        Game Presence bot. Never deletes anything; never overwrites existing
        per-server state of the Game Presence bot."""
        bot = self._bot()
        source_id = self.legacy_combo.currentData()
        if bot is None or not source_id:
            return False
        section = _legacy_section(self._config_api, source_id)
        if section is None:
            self.result_label.setText("Nothing to import.")
            return False
        try:
            settings = game_presence.normalize_bot_config(section)
            self._config_api.save_config_overrides(bot[0], dict(settings))
        except Exception as exc:
            self.result_label.setText(f"Not imported: {exc}")
            dash.colored(self.result_label, "bad")
            return False
        message = f"Imported settings from {source_id}."
        if self._running(bot[1]):
            message += " Opt-outs and cooldowns were not copied because the Game Presence bot is running; stop it and import again to copy them."
        else:
            try:
                copied = self._copy_legacy_state(source_id, bot[0])
            except Exception as exc:
                message += f" Opt-outs/cooldowns were not copied: {type(exc).__name__}."
            else:
                message += f" Copied opt-outs and cooldowns for {copied} server(s)."
        message += f" The old section in {source_id} is kept (ignored); remove it with Advanced JSON if you like."
        self.load()
        self.result_label.setText(message)
        dash.colored(self.result_label, "ok")
        return True

    def _copy_legacy_state(self, source_id: str, target_id: str) -> int:
        source_path = instance_store.get_instance_paths(source_id).data_dir / LEGACY_STATE_FILE_NAME
        target_path = instance_store.get_instance_paths(target_id).data_dir / STATE_FILE_NAME
        # Read-only on the Admin side; an unreadable file on either side stops
        # the copy (nothing is renamed, reset or overwritten).
        source = _read_state_file(source_path)
        target = _read_state_file(target_path)
        copied = 0
        for guild_id, guild in source["guilds"].items():
            state = guild.get(game_presence.STORE_KEY) if isinstance(guild, dict) else None
            if not isinstance(state, dict) or not state:
                continue
            target_guild = target["guilds"].setdefault(str(guild_id), {})
            if not isinstance(target_guild, dict) or target_guild.get(game_presence.STORE_KEY):
                continue  # never overwrite the Game Presence bot's own state
            target_guild[game_presence.STORE_KEY] = state
            copied += 1
        if copied:
            _write_state_file(target_path, target)
        return copied


def _read_state_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "guilds": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("guilds"), dict):
        raise ValueError(f"{path.name} has an invalid shape")
    return {"version": 1, "guilds": raw["guilds"]}


def _write_state_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)
