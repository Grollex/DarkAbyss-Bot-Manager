"""Per-instance AI storage: settings and API keys belong to ONE bot instance.

    instances/<id>/data/ai.json                         profiles, models, routing, fallbacks
    instances/<id>/secrets/ai/<provider>/<ref>.secret   Groq / Gemini API keys

Provider adapters, orchestrator and safety code are shared by every bot; only
the storage is scoped. Two bots therefore use separate keys and quotas unless
the user deliberately pastes the same key into both.

``migrate_legacy_global_ai`` moves the old global configuration
(``config/ai.json`` + ``secrets/ai``) into the single Admin instance of an
existing installation: copy only, never overwrite instance data, never delete
the old files, never guess when the destination is ambiguous, and never
give keys to a Game Presence bot.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import ai_platform
import app_paths
import instance_store

SETTINGS_FILE_NAME = "ai.json"
SECRETS_SUBDIR = "ai"
MIGRATION_MARKER_NAME = "ai.migrated.json"
MIGRATION_BOT_TYPE = "admin"


class AIStorageError(ai_platform.AIPlatformError):
    """Per-instance AI storage could not be resolved safely."""


@dataclass(frozen=True)
class InstanceAIStores:
    instance_id: str
    settings: ai_platform.AISettingsStore
    credentials: ai_platform.CredentialStore


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def for_instance(instance: Any) -> InstanceAIStores:
    """Stores of one bot instance (an instance_store.BotInstance or equivalent).

    The ID is re-validated and both locations must resolve inside
    instances/<id>, so a crafted ID or path cannot reach another instance.
    """
    instance_id = instance_store.validate_instance_id(getattr(instance, "id", None))
    expected = instance_store.get_instance_paths(instance_id)
    paths = getattr(instance, "paths", None)
    if paths is not None and Path(paths.root).resolve() != expected.root.resolve():
        raise AIStorageError(f"Instance {instance_id} does not live in its own instance directory.")
    settings_path = expected.data_dir / SETTINGS_FILE_NAME
    credentials_root = expected.secrets_dir / SECRETS_SUBDIR
    if not _inside(expected.root, app_paths.INSTANCES_DIR):
        raise AIStorageError(f"Instance {instance_id} escapes the instances directory.")
    for path in (settings_path, credentials_root):
        if not _inside(path, expected.root):
            raise AIStorageError(f"AI storage path escapes instance {instance_id}.")
    return InstanceAIStores(
        instance_id=instance_id,
        settings=ai_platform.AISettingsStore(settings_path),
        credentials=ai_platform.CredentialStore(credentials_root),
    )


def for_instance_id(instance_id: str, *, must_exist: bool = True) -> InstanceAIStores:
    """Stores by instance ID. ``must_exist=False`` skips loading the instance
    metadata (the Manager lists only real instances); the ID is still validated
    and every path stays inside instances/<id>."""
    if must_exist:
        return for_instance(instance_store.load_instance(instance_id))
    return for_instance(_InstanceRef(instance_store.validate_instance_id(instance_id)))


@dataclass(frozen=True)
class _InstanceRef:
    id: str
    paths: Any = None


# --------------------------------------------------------------------------
# one-time migration of the old global AI configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationResult:
    status: str  # nothing | migrated | already | ambiguous | no_destination | failed
    instance_id: str | None = None
    copied_settings: bool = False
    copied_keys: tuple[str, ...] = ()  # "provider/ref" names only, never values
    message: str = ""


def migration_marker_path() -> Path:
    return app_paths.CONFIG_DIR / MIGRATION_MARKER_NAME


def _legacy_key_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = []
    for provider_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()):
        for key_file in sorted(provider_dir.glob("*.secret")):
            if key_file.is_file() and not key_file.is_symlink():
                files.append(key_file)
    return files


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(6)}.tmp"
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, destination)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def migrate_legacy_global_ai(instances: Iterable[Any] | None = None) -> MigrationResult:
    """Copy the old global AI settings/keys into the single Admin instance.

    Idempotent (a marker records completion). Copies only what the
    destination does not have yet; the old files stay where they were.
    """
    legacy_settings = ai_platform.LEGACY_SETTINGS_PATH
    legacy_root = ai_platform.LEGACY_CREDENTIALS_ROOT
    key_files = _legacy_key_files(legacy_root)
    if not legacy_settings.is_file() and not key_files:
        return MigrationResult("nothing")
    marker_path = migration_marker_path()
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        return MigrationResult("already", marker.get("instance_id") if isinstance(marker, dict) else None)

    try:
        candidates = [item for item in (instances if instances is not None else instance_store.list_instances())]
    except Exception as exc:
        return MigrationResult("failed", message=f"Instances could not be listed: {type(exc).__name__}.")
    admins = [item for item in candidates if getattr(item, "bot_type", None) == MIGRATION_BOT_TYPE]
    if not admins:
        return MigrationResult("no_destination", message="No Admin Bot instance exists yet; old AI settings kept as they are.")
    if len(admins) > 1:
        return MigrationResult(
            "ambiguous",
            message="Several Admin Bot instances exist; old global AI settings were not copied. Enter keys per bot in AI Providers.",
        )

    destination = admins[0]
    try:
        stores = for_instance(destination)
        copied_settings = False
        if legacy_settings.is_file() and not stores.settings.path.exists():
            # Validate before copying: a corrupted global file is not propagated.
            ai_platform.AISettingsStore(legacy_settings).load()
            _copy_file_atomic(legacy_settings, stores.settings.path)
            copied_settings = True
        copied_keys = []
        for key_file in key_files:
            provider_id = key_file.parent.name
            credential_ref = key_file.stem
            try:
                target = stores.credentials.path_for(provider_id, credential_ref)
            except (ValueError, ai_platform.CredentialStoreError):
                continue  # unsafe legacy name: skip it, never write outside the instance
            if target.exists():
                continue
            _copy_file_atomic(key_file, target)
            copied_keys.append(f"{provider_id}/{credential_ref}")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"instance_id": stores.instance_id, "at": time.time(), "keys": copied_keys}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        # Nothing is deleted on failure; the next start tries again.
        return MigrationResult("failed", getattr(destination, "id", None), message=f"AI settings migration failed: {type(exc).__name__}.")
    return MigrationResult(
        "migrated",
        stores.instance_id,
        copied_settings,
        tuple(copied_keys),
        f"Old global AI settings were copied to bot instance {stores.instance_id}.",
    )
