"""Which texts of a module go through translation (used by tests and to keep
the Russian catalogs complete). Not imported by the bots at run time."""

from __future__ import annotations

import ast
from pathlib import Path

CALL_NAMES = {"_t", "t", "_", "d", "tr"}


def literal_keys(path: Path | str) -> set[str]:
    """String literals passed as the text argument of a translation call:
    ``_t("…")``, ``t("…")``, ``self.t("…")``, ``_(lang, "…")``, ``tr(lang, "…")``,
    ``bot_i18n.t("…")`` — including both branches of ``"a" if x else "b"``."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    keys: set[str] = set()

    def strings(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return strings(node.body) + strings(node.orelse)
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in CALL_NAMES or not node.args:
            continue
        # tr(lang, text) / _(lang, text): the text is the second argument.
        index = 1 if name in ("tr", "_") and len(node.args) >= 2 else 0
        keys.update(strings(node.args[index]))
    return keys
