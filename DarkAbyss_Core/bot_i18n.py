"""Language of everything a bot shows in Discord, chosen per bot instance.

Each instance config has ``language`` ("en" or "ru"). Nothing here is global
for the whole program: a bot process serves exactly one instance, and code
that has the instance config at hand passes its language explicitly
(``tr(lang, ...)``, ``Translator``). The Admin bot, whose output is spread over
many modules, sets its process language whenever it loads its config
(``set_bot_language``) and uses ``t(...)``.

Texts are written in English in the code and act as keys; the Russian
catalogs live in ``bot_i18n_ru_*`` modules. Placeholders use ``str.format``
names (``"Challenge #{id} accepted."``). A missing translation falls back to
English (tests check that every wrapped text has one).

For AI the language is a rule inside the request (``ai_language_rule``), added
by the orchestrator to every provider call, not a hope that the model guesses.
"""

from __future__ import annotations

import string
from typing import Any

LANGUAGES: dict[str, str] = {"en": "English", "ru": "Русский"}
LANGUAGE_NAMES_EN: dict[str, str] = {"en": "English", "ru": "Russian"}
DEFAULT_LANGUAGE = "en"

_catalogs: dict[str, dict[str, str]] = {"ru": {}}
_loaded = False
_bot_language = DEFAULT_LANGUAGE


class LanguageError(ValueError):
    pass


def normalize_language(value: Any, default: str = DEFAULT_LANGUAGE) -> str:
    """"en" / "ru"; missing -> ``default``; anything else is an error."""
    if value is None or value == "":
        return default
    if not isinstance(value, str) or value.strip().lower() not in LANGUAGES:
        raise LanguageError(f"language must be one of: {', '.join(LANGUAGES)}.")
    return value.strip().lower()


def register(language: str, entries: dict[str, str]) -> None:
    _catalogs.setdefault(language, {}).update(entries)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    import bot_i18n_ru_admin
    import bot_i18n_ru_game_presence
    import bot_i18n_ru_stream_director

    for module in (bot_i18n_ru_admin, bot_i18n_ru_game_presence, bot_i18n_ru_stream_director):
        register("ru", module.RU)


def catalog(language: str) -> dict[str, str]:
    _ensure_loaded()
    return dict(_catalogs.get(language, {}))


def tr(language: str | None, text: str, /, **params: Any) -> str:
    """``text`` (English) in ``language``, with ``params`` filled in."""
    template = text
    if language and language != "en":
        _ensure_loaded()
        template = _catalogs.get(language, {}).get(text, text)
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return text.format(**params)


class Translator:
    def __init__(self, language: str | None = None) -> None:
        self.language = normalize_language(language)

    def __call__(self, text: str, /, **params: Any) -> str:
        return tr(self.language, text, **params)


def set_bot_language(language: Any) -> str:
    """Language of this bot process (one process = one bot instance)."""
    global _bot_language
    try:
        _bot_language = normalize_language(language)
    except LanguageError:
        _bot_language = DEFAULT_LANGUAGE
    return _bot_language


def bot_language() -> str:
    return _bot_language


def t(text: str, /, **params: Any) -> str:
    return tr(_bot_language, text, **params)


def placeholders(template: str) -> set[str]:
    return {name for _literal, name, _spec, _conv in string.Formatter().parse(template) if name}


def plural(language: str | None, count: int, en_one: str, en_many: str, ru_one: str, ru_few: str, ru_many: str) -> str:
    if language != "ru":
        return en_one if count == 1 else en_many
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return ru_many
    last = tail % 10
    if last == 1:
        return ru_one
    if 2 <= last <= 4:
        return ru_few
    return ru_many


AI_LANGUAGE_RULES = {
    "en": (
        "LANGUAGE RULE (mandatory): write every text meant for Discord users — replies, plans, questions, "
        "confirmations, summaries and error explanations — in English, even when the user writes in another "
        "language. Do not translate names the user gave (channels, roles, members, games, server names)."
    ),
    "ru": (
        "ПРАВИЛО ЯЗЫКА (обязательно): весь текст для пользователей Discord — ответы, планы, вопросы, "
        "подтверждения, итоги и объяснения ошибок — пиши только на русском языке, даже если пользователь "
        "пишет на другом. Не переводи названия, которые дал пользователь (каналы, роли, участники, игры, "
        "название сервера)."
    ),
}


def ai_language_rule(language: str | None) -> str:
    return AI_LANGUAGE_RULES.get(normalize_language(language), AI_LANGUAGE_RULES[DEFAULT_LANGUAGE])
