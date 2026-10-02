"""Manager-local bot groups (tabs), e.g. one group per person or per server.

Stored in ``<DATA_ROOT>/config/manager_groups.json``; purely a Manager view
setting - bot instances, tokens and configs are not touched. Every bot
without an assignment belongs to DEFAULT_GROUP.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

DEFAULT_GROUP = "Main"
MAX_GROUP_NAME = 40
MAX_GROUPS = 50
FILE_NAME = "manager_groups.json"


class GroupError(ValueError):
    """Invalid group name or operation."""


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise GroupError("Group name must be text.")
    cleaned = " ".join(name.split())
    if not cleaned:
        raise GroupError("Group name must not be empty.")
    if len(cleaned) > MAX_GROUP_NAME:
        raise GroupError(f"Group name must be {MAX_GROUP_NAME} characters or fewer.")
    if not all(char.isprintable() for char in cleaned):
        raise GroupError("Group name contains invalid characters.")
    return cleaned


class GroupStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        groups = [DEFAULT_GROUP]
        for name in raw.get("groups", []) if isinstance(raw, dict) else []:
            try:
                cleaned = _clean_name(name)
            except GroupError:
                continue
            if cleaned.lower() not in {item.lower() for item in groups}:
                groups.append(cleaned)
        assignments = {}
        raw_assignments = raw.get("assignments", {}) if isinstance(raw, dict) else {}
        for instance_id, group in raw_assignments.items() if isinstance(raw_assignments, dict) else []:
            if isinstance(instance_id, str) and group in groups:
                assignments[instance_id] = group
        return {"groups": groups[: MAX_GROUPS + 1], "assignments": assignments}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        payload = {"groups": [name for name in data["groups"] if name != DEFAULT_GROUP], "assignments": data["assignments"]}
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def groups(self) -> list[str]:
        return self.load()["groups"]

    def group_of(self, instance_id: str) -> str:
        return self.load()["assignments"].get(instance_id, DEFAULT_GROUP)

    def add_group(self, name: Any) -> str:
        cleaned = _clean_name(name)
        data = self.load()
        if cleaned.lower() in {item.lower() for item in data["groups"]}:
            raise GroupError(f"Group {cleaned!r} already exists.")
        if len(data["groups"]) > MAX_GROUPS:
            raise GroupError(f"At most {MAX_GROUPS} groups.")
        data["groups"].append(cleaned)
        self._save(data)
        return cleaned

    def assign(self, instance_id: str, group: str) -> None:
        data = self.load()
        if group not in data["groups"]:
            raise GroupError(f"Group {group!r} does not exist.")
        if group == DEFAULT_GROUP:
            data["assignments"].pop(instance_id, None)
        else:
            data["assignments"][instance_id] = group
        self._save(data)

    def rename_group(self, old: str, new: Any) -> str:
        if old == DEFAULT_GROUP:
            raise GroupError(f"The {DEFAULT_GROUP!r} group cannot be renamed.")
        cleaned = _clean_name(new)
        data = self.load()
        if old not in data["groups"]:
            raise GroupError(f"Group {old!r} does not exist.")
        if cleaned.lower() != old.lower() and cleaned.lower() in {item.lower() for item in data["groups"]}:
            raise GroupError(f"Group {cleaned!r} already exists.")
        data["groups"] = [cleaned if item == old else item for item in data["groups"]]
        data["assignments"] = {key: (cleaned if value == old else value) for key, value in data["assignments"].items()}
        self._save(data)
        return cleaned

    def remove_group(self, name: str) -> None:
        """Delete a group; its bots move back to the default group (nothing else changes)."""
        if name == DEFAULT_GROUP:
            raise GroupError(f"The {DEFAULT_GROUP!r} group cannot be removed.")
        data = self.load()
        data["groups"] = [item for item in data["groups"] if item != name]
        data["assignments"] = {key: value for key, value in data["assignments"].items() if value != name}
        self._save(data)
