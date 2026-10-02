"""Short conversation memory for the Discord AI transport (in RAM only).

One conversation per (guild, channel, user): the last few user requests and
the bot's final answers, so follow-ups such as "now delete it" work. Nothing
is written to disk; a bot restart, ``/ai_reset`` or the TTL clears it. The
text is bounded so it never dominates a low-TPM provider request.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

MEMORY_TTL_SECONDS = 1800.0
MAX_EXCHANGES = 6
MAX_TOTAL_CHARS = 3000
MAX_ENTRY_CHARS = 800
MAX_CONVERSATIONS = 500

ConversationKey = tuple[int, int, int]  # guild_id, channel_id, user_id


@dataclass(frozen=True)
class Exchange:
    user_text: str
    assistant_text: str
    at: float


def _clip(text: str, limit: int) -> str:
    text = text if isinstance(text, str) else ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


class ConversationMemory:
    def __init__(self, *, clock: Callable[[], float] | None = None, ttl_seconds: float = MEMORY_TTL_SECONDS) -> None:
        self._clock = clock or time.monotonic
        self._ttl = float(ttl_seconds)
        # Least recently used first; bounded by MAX_CONVERSATIONS.
        self._conversations: OrderedDict[ConversationKey, list[Exchange]] = OrderedDict()

    def _purge(self) -> None:
        cutoff = self._clock() - self._ttl
        for key in [key for key, items in self._conversations.items() if not items or items[-1].at < cutoff]:
            self._conversations.pop(key, None)

    def history(self, key: ConversationKey) -> list[tuple[str, str]]:
        """Recent (user_text, assistant_text) pairs, oldest first, within the char budget."""
        self._purge()
        items = self._conversations.get(key) or []
        selected: list[tuple[str, str]] = []
        used = 0
        for item in reversed(items):
            size = len(item.user_text) + len(item.assistant_text)
            if selected and used + size > MAX_TOTAL_CHARS:
                break
            selected.append((item.user_text, item.assistant_text))
            used += size
        selected.reverse()
        return selected

    def record(self, key: ConversationKey, user_text: str, assistant_text: str) -> None:
        user_text = _clip(user_text.strip() if isinstance(user_text, str) else "", MAX_ENTRY_CHARS)
        assistant_text = _clip(assistant_text.strip() if isinstance(assistant_text, str) else "", MAX_ENTRY_CHARS)
        if not user_text:
            return
        self._purge()
        items = self._conversations.pop(key, [])
        items.append(Exchange(user_text, assistant_text or "(no text reply)", self._clock()))
        self._conversations[key] = items[-MAX_EXCHANGES:]
        while len(self._conversations) > MAX_CONVERSATIONS:
            self._conversations.popitem(last=False)

    def clear(self, key: ConversationKey) -> bool:
        return self._conversations.pop(key, None) is not None

    def __len__(self) -> int:
        self._purge()
        return len(self._conversations)
