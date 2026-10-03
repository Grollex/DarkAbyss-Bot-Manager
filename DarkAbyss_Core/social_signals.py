"""Deterministic social signals for Kairo's Social Awareness (no AI involved).

* ``parse_quiet_request``: "Кайро, помолчи", "не лезь сюда", "заткнись на час",
  "не отвечай в этом канале", "мне не отвечай", "shut up for 10 minutes" ...
  and the way back ("можешь снова говорить", "you can talk again"). Called only
  for messages that address Kairo (its name, a reply to it, or a mention), so
  everyday words do not silence it.
* ``classify_reaction`` / ``classify_text``: how people took one of Kairo's
  autonomous messages (laughing, thanks, "no one asked", 👎 ...).
* ``KAIRO_REACTIONS``: the only emoji Social Awareness may react with.

Parsing is deliberately conservative: an unclear message is not a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MINUTE = 60
HOUR = 3600
DAY = 86400
MAX_QUIET_SECONDS = 30 * DAY
DEFAULT_QUIET_SECONDS = HOUR  # "помолчи" with no time: one hour, then Kairo may speak again
LONG_QUIET_SECONDS = 30 * DAY  # "не отвечай в этом канале", "мне не отвечай": a lasting wish

# Kairo reacts only with these (no custom emoji, no text, no flags).
KAIRO_REACTIONS = (
    "😂", "💀", "👍", "❤️", "🔥", "👀", "🎉", "😅", "🤔", "😮", "😢", "🫡", "💯", "👏", "🙏", "🤝", "😭", "😎",
)
QUIET_ACK_REACTION = "🤐"
RESUME_ACK_REACTION = "👌"

POSITIVE_REACTIONS = frozenset(
    {"😂", "🤣", "💀", "😆", "😄", "😁", "😹", "👍", "❤️", "♥️", "🔥", "💯", "👏", "🙌", "😍", "🥰", "✅", "🫡", "🙏", "😎", "🎉", "😭", "🤝", "⭐", "🏆", "👌"}
)
NEGATIVE_REACTIONS = frozenset({"👎", "🤡", "🙄", "💩", "😡", "🤬", "🖕", "😒", "😑", "🚫", "❌", "🤮", "😤", "🥱", "🤨"})

_VARIATION = "️"


def normalize_emoji(value: object) -> str:
    return str(value or "").replace(_VARIATION, "").strip()


_KAIRO_REACTIONS_NORMALIZED = {normalize_emoji(item): item for item in KAIRO_REACTIONS}


def kairo_reaction(value: object) -> str | None:
    """The allowed emoji for ``value`` (as Kairo would send it), else None."""
    return _KAIRO_REACTIONS_NORMALIZED.get(normalize_emoji(value))


def classify_reaction(emoji: object) -> str | None:
    value = normalize_emoji(emoji)
    if value in {normalize_emoji(item) for item in POSITIVE_REACTIONS}:
        return "positive"
    if value in {normalize_emoji(item) for item in NEGATIVE_REACTIONS}:
        return "negative"
    return None


# -- what people write back ---------------------------------------------------------

_NEGATIVE_TEXT = re.compile(
    r"(тебя\s+(никто\s+)?не\s+спрашивал|никто\s+не\s+спрашивал|не\s+спрашивали|кринж|тупой\s+бот|бот\s+тупой|"
    r"отвали|отстань|бесишь|задолбал|достал|фу\b|no\s*one\s+asked|nobody\s+asked|didn'?t\s+ask|cringe|stupid\s+bot|"
    r"bad\s+bot|dumb\s+bot|annoying|go\s+away|shut\s+it)",
    re.IGNORECASE,
)
_POSITIVE_TEXT = re.compile(
    r"(спасибо|спс|благодарю|красава|молодец|хорош\b|ахах|хаха|лол\b|ору\b|топ\b|good\s+bot|thanks|thank\s+you|thx|"
    r"\blol\b|lmao|haha|nice\b|based\b|love\s+it)",
    re.IGNORECASE,
)


def classify_text(text: object) -> str:
    """"negative" / "positive" / "neutral" for a message answering Kairo."""
    value = str(text or "")
    if _NEGATIVE_TEXT.search(value):
        return "negative"
    if _POSITIVE_TEXT.search(value):
        return "positive"
    return "neutral"


# -- quiet requests ---------------------------------------------------------------------


@dataclass(frozen=True)
class QuietRequest:
    action: str  # "quiet" | "resume"
    scope: str  # "channel" | "guild" | "user"
    seconds: int | None = None  # quiet only


_QUIET = re.compile(
    r"(помолчи|замолчи|замолкни|умолкни|заткнись|завали(\s+рот|сь)?\b|не\s+лезь|не\s+вмешивайся|не\s+встревай|не\s+влезай|"
    r"не\s+отвечай|не\s+пиши|не\s+мешай|уймись|отстань|отвали|потише|хватит\s+(болтать|писать|отвечать|влезать)|"
    r"shut\s+up|be\s+quiet|stay\s+quiet|keep\s+quiet|stop\s+(talking|replying|answering|chiming\s+in)|go\s+away|"
    r"don'?t\s+(reply|answer|respond|talk)|do\s+not\s+(reply|answer|respond|talk)|stay\s+out|butt\s+out|leave\s+(us|me)\s+alone|"
    r"(please\s+)?mute\s+yourself)",
    re.IGNORECASE,
)
_RESUME = re.compile(
    r"(можешь\s+(снова\s+|опять\s+)?(говорить|писать|отвечать|вмешиваться)|снова\s+можешь|говори\s+снова|вернись|"
    r"размуть|размучиваю|отвечай\s+снова|ты\s+снова\s+можешь|you\s+can\s+(talk|speak|reply|chat)(\s+again)?|"
    r"talk\s+again|speak\s+again|unmute|come\s+back)",
    re.IGNORECASE,
)
_NEGATED_QUIET = re.compile(r"(не\s+(надо\s+)?(молчи|молчать)|don'?t\s+be\s+quiet|не\s+стесняйся)", re.IGNORECASE)
_USER_SCOPE = re.compile(
    r"(мне\s+не\s+(отвечай|пиши)|не\s+(отвечай|пиши)\s+мне|отстань\s+от\s+меня|не\s+трогай\s+меня|игнорь\s+меня|"
    r"don'?t\s+(reply|answer|respond|talk)\s+to\s+me|leave\s+me\s+alone|ignore\s+me|stop\s+replying\s+to\s+me)",
    re.IGNORECASE,
)
_GUILD_SCOPE = re.compile(r"(везде|на\s+(всём\s+|всем\s+)?сервере|во\s+всех\s+каналах|everywhere|on\s+(the|this)\s+server|in\s+all\s+channels)", re.IGNORECASE)
_CHANNEL_SCOPE = re.compile(r"(сюда|здесь|тут|в\s+этом\s+канале|в\s+этот\s+канал|in\s+(this|here)\s+channel|in\s+here|\bhere\b|this\s+channel)", re.IGNORECASE)
_FOREVER = re.compile(r"(навсегда|больше\s+никогда|вообще\s+никогда|forever|ever\s+again|for\s+good|permanently)", re.IGNORECASE)
_DURATION = re.compile(
    r"(\d{1,4})\s*(минут\w*|мин\b|м\b|minutes?|mins?\b|m\b|час\w*|ч\b|hours?|hrs?\b|h\b|дн\w*|день|сут\w*|д\b|days?|d\b|недел\w*|weeks?|w\b)",
    re.IGNORECASE,
)
_WORD_DURATIONS = (
    (re.compile(r"(полчаса|half\s+an\s+hour)", re.IGNORECASE), 30 * MINUTE),
    (re.compile(r"(на\s+час|часок|an\s+hour|one\s+hour|for\s+a\s+while\b)", re.IGNORECASE), HOUR),
    (re.compile(r"(до\s+завтра|на\s+сегодня|until\s+tomorrow|for\s+today|for\s+the\s+day)", re.IGNORECASE), 12 * HOUR),
    (re.compile(r"(на\s+день|a\s+day|one\s+day)", re.IGNORECASE), DAY),
    (re.compile(r"(на\s+неделю|a\s+week|one\s+week)", re.IGNORECASE), 7 * DAY),
    (re.compile(r"(пару\s+минут|минутку|a\s+few\s+minutes|a\s+minute)", re.IGNORECASE), 10 * MINUTE),
)


def _unit_seconds(unit: str) -> int:
    unit = unit.casefold()
    if unit.startswith(("мин", "minute", "min")) or unit in ("м", "m"):
        return MINUTE
    if unit.startswith(("час", "hour", "hr")) or unit in ("ч", "h"):
        return HOUR
    if unit.startswith(("нед", "week")) or unit == "w":
        return 7 * DAY
    return DAY


def parse_duration(text: str) -> int | None:
    if _FOREVER.search(text):
        return MAX_QUIET_SECONDS
    match = _DURATION.search(text)
    if match:
        return max(MINUTE, min(MAX_QUIET_SECONDS, int(match.group(1)) * _unit_seconds(match.group(2))))
    for pattern, seconds in _WORD_DURATIONS:
        if pattern.search(text):
            return seconds
    return None


def parse_quiet_request(text: object) -> QuietRequest | None:
    """A request to Kairo to stay quiet (or to speak again), else None.

    Only call it for messages addressed to Kairo."""
    value = " ".join(str(text or "").split())
    if not value:
        return None
    if _RESUME.search(value) and not _QUIET.search(_RESUME.sub(" ", value)):
        scope = "user" if re.search(r"(мне|me\b)", value, re.IGNORECASE) and not _CHANNEL_SCOPE.search(value) else "channel"
        if _GUILD_SCOPE.search(value):
            scope = "guild"
        return QuietRequest("resume", scope)
    if _NEGATED_QUIET.search(value) or not _QUIET.search(value):
        return None
    duration = parse_duration(value)
    if _USER_SCOPE.search(value):
        return QuietRequest("quiet", "user", duration or LONG_QUIET_SECONDS)
    if _GUILD_SCOPE.search(value):
        return QuietRequest("quiet", "guild", duration or DEFAULT_QUIET_SECONDS)
    explicit_channel = bool(_CHANNEL_SCOPE.search(value))
    if duration is None:
        # "не отвечай в этом канале" is a lasting wish; a bare "помолчи" is for a while.
        lasting = explicit_channel and re.search(r"(не\s+(отвечай|пиши|лезь)|don'?t\s+(reply|answer|respond)|stay\s+out)", value, re.IGNORECASE)
        duration = LONG_QUIET_SECONDS if lasting else DEFAULT_QUIET_SECONDS
    return QuietRequest("quiet", "channel", duration)


def describe_seconds(seconds: int) -> str:
    if seconds >= DAY:
        return f"{round(seconds / DAY)}d"
    if seconds >= HOUR:
        return f"{round(seconds / HOUR)}h"
    return f"{max(1, round(seconds / MINUTE))}m"
