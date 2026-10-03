"""Manager-side memory of the Bot Setup wizard, per bot instance.

The wizard has steps that happen outside the program (the Discord Application
ID used for the invite link, "I enabled the intents", "I invited the bot").
They are not bot settings, so they live here and not in the instance config:

    config/manager_setup.json   {"<instance_id>": {"application_id": "…", "intents_confirmed": true, "invited": true}}

No secrets: the Application ID is public (it is part of every invite link).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import app_paths

FILE_NAME = "manager_setup.json"


@dataclass(frozen=True)
class SetupSteps:
    application_id: str = ""
    intents_confirmed: bool = False
    invited: bool = False


def path() -> Path:
    return app_paths.CONFIG_DIR / FILE_NAME


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(instance_id: str) -> SetupSteps:
    raw = _read().get(instance_id)
    if not isinstance(raw, dict):
        return SetupSteps()
    application_id = raw.get("application_id")
    return SetupSteps(
        application_id=application_id if isinstance(application_id, str) and application_id.isdigit() else "",
        intents_confirmed=raw.get("intents_confirmed") is True,
        invited=raw.get("invited") is True,
    )


def save(instance_id: str, steps: SetupSteps) -> None:
    data = _read()
    data[instance_id] = {
        "application_id": steps.application_id if steps.application_id.isdigit() else "",
        "intents_confirmed": bool(steps.intents_confirmed),
        "invited": bool(steps.invited),
    }
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
