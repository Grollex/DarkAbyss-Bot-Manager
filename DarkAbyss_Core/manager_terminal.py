"""Manager AI terminal page: type requests to a running bot from the app.

Talks to the bot only through the admin_terminal file mailbox. Replies,
plans, confirmations and engine-switch offers arrive as events and are shown
as chat bubbles / cards with the same Approve and Cancel choices as Discord.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import admin_terminal
import manager_dashboard as dash

POLL_INTERVAL_MS = 400
NO_CHANNEL = "__none__"


def _bubble(text: str, kind: str) -> QFrame:
    """kind: user | bot | error | note"""
    frame = QFrame()
    styles = {
        "user": "background-color: #3b2a6b; border: 1px solid #5b4699; border-radius: 12px;",
        "bot": "background-color: #15122a; border: 1px solid #2a2448; border-radius: 12px;",
        "error": "background-color: #2a1220; border: 1px solid #7f1d1d; border-radius: 12px;",
        "note": "background: transparent; border: none;",
    }
    # Selector by object name: QLabel is a QFrame too and must not inherit the box.
    frame.setObjectName("bubble")
    frame.setStyleSheet(f"QFrame#bubble {{ {styles.get(kind, styles['bot'])} }}")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 9, 12, 9)
    body, footer = _split_footer(text)
    # Bot replies use Discord markdown (**bold**, `code`, lists); user text stays plain.
    label = QLabel(body if kind == "user" else discord_markdown(body))
    label.setTextFormat(Qt.PlainText if kind == "user" else Qt.MarkdownText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    if kind == "note":
        label.setObjectName("muted")
    if kind == "error":
        label.setStyleSheet("color: #fca5a5; background: transparent;")
    layout.addWidget(label)
    if footer:
        footer_label = dash.muted(footer)
        footer_label.setWordWrap(True)
        layout.addWidget(footer_label)
    return frame


_LIST_PREFIXES = ("- ", "* ", "• ")


def _is_list_line(line: str) -> bool:
    stripped = line.lstrip()
    head = stripped.split(".", 1)[0]
    return stripped.startswith(_LIST_PREFIXES) or (head.isdigit() and stripped[len(head) : len(head) + 2] == ". ")


def discord_markdown(text: str) -> str:
    """Discord keeps every newline; Markdown joins lines. Keep them as hard breaks."""
    out: list[str] = []
    previous_list = False
    for line in str(text or "").split("\n"):
        is_list = _is_list_line(line)
        if previous_list and not is_list and line.strip():
            out.append("")  # end the list before ordinary text
        out.append(line)
        previous_list = is_list
    return "  \n".join(out)


def _split_footer(text: str) -> tuple[str, str]:
    """Discord "-# engine" footer lines become a small muted footer."""
    lines = str(text or "").split("\n")
    footer = [line[3:] for line in lines if line.startswith("-# ")]
    body = [line for line in lines if not line.startswith("-# ")]
    return "\n".join(body).strip(), " · ".join(footer)


class ApprovalCard(QFrame):
    def __init__(self, text: str, buttons: list[dict[str, Any]], on_decide: Callable[[bool], None]) -> None:
        super().__init__()
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        body, footer = _split_footer(text)
        self.text_label = QLabel(discord_markdown(body))
        self.text_label.setTextFormat(Qt.MarkdownText)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.text_label)
        if footer:
            layout.addWidget(dash.muted(footer))
        self.status_label = dash.muted("")
        self.status_label.setWordWrap(True)
        self.buttons: list[QPushButton] = []
        row = QHBoxLayout()
        for spec in buttons:
            approved = spec.get("approved") is True
            button = dash.styled_button(str(spec.get("label") or ("Approve" if approved else "Cancel")), "primary" if approved else "quick")
            button.clicked.connect(lambda _checked=False, value=approved: self._decide(value, on_decide))
            self.buttons.append(button)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self.status_label)

    def _decide(self, approved: bool, on_decide: Callable[[bool], None]) -> None:
        for button in self.buttons:
            button.setEnabled(False)
        self.status_label.setText("Sent to the bot...")
        on_decide(approved)

    def resolve(self, text: str) -> None:
        for button in self.buttons:
            button.hide()
        body, _footer = _split_footer(text)
        if body:
            self.text_label.setText(discord_markdown(body))
        self.status_label.setText("")

    def expire(self) -> None:
        for button in self.buttons:
            button.hide()
        self.status_label.setText("Expired: nothing from it was executed.")


class TerminalPanel(QWidget):
    """``list_bots`` returns [(instance_id, label, info)] for all bots."""

    def __init__(self, list_bots: Callable[[], list[tuple[str, str, Any]]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._list_bots = list_bots
        # request_id -> {"runtime": Path, "offset": int, "cards": {token: ApprovalCard}, "status": QLabel|None}
        self._requests: dict[str, dict[str, Any]] = {}

        title = QLabel("AI Terminal")
        title.setObjectName("heroSubtitle")
        description = dash.muted(
            "Give the running bot tasks right from here. You act as the bot's operator: no Discord role "
            "limits, but plans and destructive actions still need your approval below."
        )
        description.setWordWrap(True)

        self.bot_combo = QComboBox()
        self.guild_combo = QComboBox()
        self.channel_combo = QComboBox()
        self.mode_combo = QComboBox()
        for mode in admin_terminal.TASK_MODES:
            self.mode_combo.addItem(mode, mode)
        refresh = QPushButton("⟳")
        refresh.setToolTip("Reload bots, servers and channels")
        refresh.clicked.connect(self.refresh_targets)
        self.bot_combo.currentIndexChanged.connect(lambda _index: self._load_guilds())
        self.guild_combo.currentIndexChanged.connect(lambda _index: self._load_channels())
        targets = QHBoxLayout()
        for label, widget, stretch in (
            ("Bot", self.bot_combo, 3),
            ("Server", self.guild_combo, 3),
            ("Channel (“here”)", self.channel_combo, 3),
            ("Mode", self.mode_combo, 1),
        ):
            targets.addWidget(QLabel(label))
            targets.addWidget(widget, stretch)
        targets.addWidget(refresh)
        self.hint_label = dash.muted("")
        self.hint_label.setWordWrap(True)

        self.transcript = QVBoxLayout()
        self.transcript.setSpacing(10)
        self.transcript.addStretch(1)
        transcript_widget = QWidget()
        transcript_widget.setLayout(self.transcript)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(transcript_widget)
        self.scroll.setObjectName("panel")

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Write a task for the bot, e.g. “create a category Events with a text and a voice channel”. Ctrl+Enter sends.")
        self.input.setFixedHeight(84)
        self.send_button = dash.styled_button("Send  ➤", "primary")
        self.send_button.clicked.connect(self.send)
        QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self.send)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(targets)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(input_row)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.poll)
        self.refresh_targets()

    # -- targets -------------------------------------------------------------

    def _selected_bot(self) -> tuple[str, Any] | None:
        instance_id = self.bot_combo.currentData()
        for bot_id, _label, info in self._list_bots():
            if bot_id == instance_id:
                return bot_id, info
        return None

    def _runtime(self) -> Any:
        selected = self._selected_bot()
        return None if selected is None else admin_terminal.runtime_dir_for_logs(selected[1].logs_dir)

    def refresh_targets(self) -> None:
        current = self.bot_combo.currentData()
        self.bot_combo.blockSignals(True)
        self.bot_combo.clear()
        for instance_id, label, info in self._list_bots():
            running = str(getattr(info, "state", "")).upper() == "RUNNING"
            self.bot_combo.addItem(f"{label}{'' if running else '  (stopped)'}", instance_id)
        index = self.bot_combo.findData(current)
        self.bot_combo.setCurrentIndex(index if index >= 0 else 0)
        self.bot_combo.blockSignals(False)
        self._load_guilds()

    def _load_guilds(self) -> None:
        current = self.guild_combo.currentData()
        self.guild_combo.blockSignals(True)
        self.guild_combo.clear()
        selected = self._selected_bot()
        status = None
        if selected is None:
            self.hint_label.setText("No bots yet. Add one on the Bots page.")
        elif str(getattr(selected[1], "state", "")).upper() != "RUNNING":
            self.hint_label.setText("This bot is not running. Start it to send requests.")
        else:
            status = admin_terminal.read_bot_status(self._runtime())
            if status is None:
                self.hint_label.setText("Waiting for the bot to report its servers (a few seconds after it connects)...")
            else:
                self.hint_label.setText(f"Connected as {status.get('bot_name') or 'the bot'}.")
        for guild in (status or {}).get("guilds", []):
            self.guild_combo.addItem(str(guild.get("name")), guild)
        index = next(
            (i for i in range(self.guild_combo.count()) if (self.guild_combo.itemData(i) or {}).get("id") == (current or {}).get("id")),
            0,
        )
        self.guild_combo.setCurrentIndex(index)
        self.guild_combo.blockSignals(False)
        self._load_channels()
        self.send_button.setEnabled(self.guild_combo.count() > 0)

    def _load_channels(self) -> None:
        current = self.channel_combo.currentData()
        self.channel_combo.clear()
        self.channel_combo.addItem("No specific channel", NO_CHANNEL)
        guild = self.guild_combo.currentData() or {}
        for channel in guild.get("channels", []):
            prefix = "#" if channel.get("type") in ("text", "news", "forum") else "\U0001f50a "
            self.channel_combo.addItem(f"{prefix}{channel.get('name')}", channel.get("id"))
        index = self.channel_combo.findData(current)
        self.channel_combo.setCurrentIndex(index if index >= 0 else 0)

    # -- requests ----------------------------------------------------------------

    def _add(self, widget: QWidget, align_right: bool = False) -> None:
        row = QHBoxLayout()
        if align_right:
            row.addStretch(1)
            row.addWidget(widget, 4)
        else:
            row.addWidget(widget, 6)
            row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self.transcript.insertWidget(self.transcript.count() - 1, holder)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def send(self) -> None:
        prompt = self.input.toPlainText().strip()
        runtime = self._runtime()
        guild = self.guild_combo.currentData() or {}
        channel = self.channel_combo.currentData()
        if runtime is None or not guild:
            self.hint_label.setText("Choose a running bot and a server first.")
            return
        try:
            request_id = admin_terminal.submit_request(
                runtime,
                guild_id=guild.get("id"),
                channel_id=None if channel in (None, NO_CHANNEL) else channel,
                prompt=prompt,
                mode=str(self.mode_combo.currentData() or "routine"),
            )
        except admin_terminal.TerminalError as exc:
            self.hint_label.setText(str(exc))
            return
        except OSError as exc:
            self.hint_label.setText(f"Could not reach the bot: {exc}")
            return
        self.input.clear()
        target = str(guild.get("name") or "server")
        if channel not in (None, NO_CHANNEL):
            target = f"{target} · {self.channel_combo.currentText()}"
        self._add(_bubble(f"{prompt}\n-# {target}", "user"), align_right=True)
        status = dash.muted("Sent. Waiting for the bot...")
        self._add(status)
        self._requests[request_id] = {"runtime": runtime, "offset": 0, "cards": {}, "status": status}
        if not self.timer.isActive():
            self.timer.start()

    def poll(self) -> None:
        for request_id, request in list(self._requests.items()):
            events, request["offset"] = admin_terminal.read_events(request["runtime"], request_id, request["offset"])
            for event in events:
                self._apply(request_id, request, event)

    def _apply(self, request_id: str, request: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("type")
        text = str(event.get("text") or "")
        status = request.get("status")
        if kind == "status":
            if status is not None:
                status.setText(text or "Working...")
                status.show()
            return
        if kind == "idle":
            if status is not None:
                status.setText("")
                status.hide()
            return
        if kind == "message":
            self._add(_bubble(text, "bot"))
        elif kind == "error":
            self._add(_bubble(text, "error"))
        elif kind == "approval":
            token = str(event.get("token") or "")

            def decide(approved: bool, token: str = token) -> None:
                try:
                    admin_terminal.submit_decision(request["runtime"], request_id=request_id, token=token, approved=approved)
                except (admin_terminal.TerminalError, OSError) as exc:
                    self.hint_label.setText(f"Could not send the decision: {exc}")

            card = ApprovalCard(text, list(event.get("buttons") or []), decide)
            request["cards"][token] = card
            self._add(card)
        elif kind == "resolved":
            card = request["cards"].get(str(event.get("token") or ""))
            if card is not None:
                card.resolve(text)
            elif text:
                self._add(_bubble(text, "note"))
        elif kind == "expired":
            card = request["cards"].get(str(event.get("token") or ""))
            if card is not None:
                card.expire()
