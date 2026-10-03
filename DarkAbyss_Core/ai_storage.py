"""AI storage of a bot instance: shared connections + the bot's own choice.

    <DATA_ROOT>/config/ai_connections.json              connections + base set (ai_connections)
    <DATA_ROOT>/secrets/ai_connections/<provider>/<id>.secret   one key per connection
    instances/<id>/data/ai_selection.json              this bot: base set or its own route
    instances/<id>/data/ai_usage.json                  this bot's provider-reported usage

``for_instance`` returns what the orchestrator of ONE bot needs: a read-only
settings view with only the connections that bot may use, the connection
credential store, and the bot's usage store. Provider adapters and the
orchestrator are shared code.

Older layouts are converted once (``run_migrations``), copy-only, never
deleting anything and never overwriting what already exists:

1. ``migrate_legacy_global_ai``: the very first global store
   (``config/ai.json`` + ``secrets/ai``) -> the single Admin instance.
2. ``migrate_instances_to_connections``: per-bot settings
   (``instances/<id>/data/ai.json`` + ``instances/<id>/secrets/ai``) ->
   connections. If exactly one bot had keys, they become the base set (every
   bot uses it by default); otherwise each bot keeps its keys as its own
   connections and its own route.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import ai_connections
import ai_platform
import ai_providers
import ai_usage
import app_paths
import instance_store

OWN_SETTINGS_FILE_NAME = "ai.json"
OWN_SECRETS_SUBDIR = "ai"
MIGRATION_MARKER_NAME = "ai.migrated.json"
MIGRATION_BOT_TYPE = "admin"


class AIStorageError(ai_platform.AIPlatformError):
    """AI storage of an instance could not be resolved safely."""


@dataclass(frozen=True)
class InstanceAIStores:
    instance_id: str
    # Read-only view: only the connections this bot may use (ai_connections).
    settings: ai_connections.BotSettingsView
    # Keys of the connections (credential_ref = connection ID).
    credentials: ai_platform.CredentialStore
    # Provider-reported requests/tokens/rate limits of THIS bot.
    usage: ai_usage.AIUsageStore
    selection: ai_connections.SelectionStore
    connections: ai_connections.ConnectionStore


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _instance_paths(instance: Any) -> tuple[str, Any]:
    instance_id = instance_store.validate_instance_id(getattr(instance, "id", None))
    expected = instance_store.get_instance_paths(instance_id)
    paths = getattr(instance, "paths", None)
    if paths is not None and Path(paths.root).resolve() != expected.root.resolve():
        raise AIStorageError(f"Instance {instance_id} does not live in its own instance directory.")
    if not _inside(expected.root, app_paths.INSTANCES_DIR):
        raise AIStorageError(f"Instance {instance_id} escapes the instances directory.")
    return instance_id, expected


def for_instance(instance: Any, connections: ai_connections.ConnectionStore | None = None) -> InstanceAIStores:
    """Stores of one bot instance (an instance_store.BotInstance or equivalent).

    The ID is re-validated and the bot's own files must resolve inside
    instances/<id>, so a crafted ID or path cannot reach another instance.
    """
    instance_id, expected = _instance_paths(instance)
    selection_path = expected.data_dir / ai_connections.SELECTION_FILE_NAME
    usage_path = expected.data_dir / ai_usage.USAGE_FILE_NAME
    for path in (selection_path, usage_path):
        if not _inside(path, expected.root):
            raise AIStorageError(f"AI storage path escapes instance {instance_id}.")
    store = connections or ai_connections.ConnectionStore()
    selection = ai_connections.SelectionStore(selection_path)
    return InstanceAIStores(
        instance_id=instance_id,
        settings=ai_connections.BotSettingsView(store, selection),
        credentials=store.credentials,
        usage=ai_usage.AIUsageStore(usage_path),
        selection=selection,
        connections=store,
    )


def for_instance_id(instance_id: str, *, must_exist: bool = True) -> InstanceAIStores:
    """Stores by instance ID. ``must_exist=False`` skips loading the instance
    metadata (the Manager lists only real instances); the ID is still validated
    and every per-bot path stays inside instances/<id>."""
    if must_exist:
        return for_instance(instance_store.load_instance(instance_id))
    return for_instance(_InstanceRef(instance_store.validate_instance_id(instance_id)))


@dataclass(frozen=True)
class _InstanceRef:
    id: str
    paths: Any = None


def own_stores(instance: Any) -> tuple[ai_platform.AISettingsStore, ai_platform.CredentialStore]:
    """The old per-bot AI files (read by the migrations only)."""
    instance_id, expected = _instance_paths(instance)
    settings_path = expected.data_dir / OWN_SETTINGS_FILE_NAME
    credentials_root = expected.secrets_dir / OWN_SECRETS_SUBDIR
    for path in (settings_path, credentials_root):
        if not _inside(path, expected.root):
            raise AIStorageError(f"AI storage path escapes instance {instance_id}.")
    return ai_platform.AISettingsStore(settings_path), ai_platform.CredentialStore(credentials_root)


# --------------------------------------------------------------------------
# migrations
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
    """Copy the very first global AI settings/keys into the single Admin instance.

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
            message="Several Admin Bot instances exist; old global AI settings were not copied. Add connections in AI Providers.",
        )

    destination = admins[0]
    try:
        settings_store, credentials = own_stores(destination)
        copied_settings = False
        if legacy_settings.is_file() and not settings_store.path.exists():
            # Validate before copying: a corrupted global file is not propagated.
            ai_platform.AISettingsStore(legacy_settings).load()
            _copy_file_atomic(legacy_settings, settings_store.path)
            copied_settings = True
        copied_keys = []
        for key_file in key_files:
            provider_id = key_file.parent.name
            credential_ref = key_file.stem
            try:
                target = credentials.path_for(provider_id, credential_ref)
            except (ValueError, ai_platform.CredentialStoreError):
                continue  # unsafe legacy name: skip it, never write outside the instance
            if target.exists():
                continue
            _copy_file_atomic(key_file, target)
            copied_keys.append(f"{provider_id}/{credential_ref}")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"instance_id": destination.id, "at": time.time(), "keys": copied_keys}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        # Nothing is deleted on failure; the next start tries again.
        return MigrationResult("failed", getattr(destination, "id", None), message=f"AI settings migration failed: {type(exc).__name__}.")
    return MigrationResult(
        "migrated",
        destination.id,
        copied_settings,
        tuple(copied_keys),
        f"Old global AI settings were copied to bot instance {destination.id}.",
    )


@dataclass(frozen=True)
class _OwnAI:
    instance: Any
    profiles: tuple[ai_platform.AIProfile, ...]  # only profiles whose key exists
    routing: ai_platform.RoutingConfig
    credentials: ai_platform.CredentialStore


def _own_ai(instance: Any) -> _OwnAI:
    """Old per-bot settings with the profiles that actually have a key."""
    settings_store, credentials = own_stores(instance)
    settings = settings_store.load()  # AIPlatformError when unreadable -> caller skips the bot
    profiles = list(settings.profiles)
    if not profiles:
        # Keys saved without settings: one profile per known provider key.
        for provider_id in ai_providers.provider_ids():
            ref = f"{provider_id}-default"
            if credentials.exists(provider_id, ref):
                spec = ai_providers.get_spec(provider_id)
                profiles.append(ai_platform.AIProfile(ref, provider_id, spec.default_model, ref, {"reasoning_effort": "medium"}))
    keyed = []
    for profile in profiles:
        if profile.provider_id not in ai_providers.PROVIDERS or not profile.credential_ref or not profile.enabled:
            continue
        try:
            if credentials.exists(profile.provider_id, profile.credential_ref):
                keyed.append(profile)
        except (ValueError, ai_platform.CredentialStoreError):
            continue
    return _OwnAI(instance, tuple(keyed), settings.routing, credentials)


def _unique_id(base: str, taken: set[str]) -> str:
    candidate = base[:64].rstrip("-_") or "connection"
    number = 2
    while candidate in taken:
        suffix = f"-{number}"
        candidate = base[: 64 - len(suffix)].rstrip("-_") + suffix
        number += 1
    taken.add(candidate)
    return candidate


def _connection_options(profile: ai_platform.AIProfile) -> dict[str, Any]:
    spec = ai_providers.get_spec(profile.provider_id)
    return {key: value for key, value in dict(profile.options).items() if key in spec.options}


def migrate_instances_to_connections(
    instances: Iterable[Any] | None = None,
    store: ai_connections.ConnectionStore | None = None,
) -> MigrationResult:
    """Convert per-bot AI settings + keys into shared connections (copy only).

    Exactly one bot with keys and an empty base set -> its keys become the base
    set (every bot uses it by default). Otherwise each bot's keys become its own
    connections and the bot keeps its route as a custom choice.
    """
    store = store or ai_connections.ConnectionStore()
    try:
        candidates = sorted(instances if instances is not None else instance_store.list_instances(), key=lambda item: item.id)
        config = store.load()
    except Exception as exc:
        return MigrationResult("failed", message=f"Old per-bot AI settings were not converted: {type(exc).__name__}.")
    pending = [item for item in candidates if item.id not in config.migrated_instances]
    if not pending:
        return MigrationResult("already" if config.migrated_instances else "nothing")

    sources: list[_OwnAI] = []
    done: list[str] = []
    skipped: list[str] = []
    for instance in pending:
        try:
            own = _own_ai(instance)
        except Exception:
            skipped.append(instance.id)  # unreadable: retried on the next start
            continue
        done.append(instance.id)
        if own.profiles:
            sources.append(own)

    taken = set(config.ids())
    connections = list(config.connections)
    base = config.base
    selections: list[tuple[Any, ai_connections.RouteSelection]] = []
    names: list[str] = []
    to_base = len(sources) == 1 and not config.base.connection_ids()
    try:
        for own in sources:
            mapping: dict[str, str] = {}
            for profile in own.profiles:
                spec = ai_providers.get_spec(profile.provider_id)
                if to_base:
                    connection_id = _unique_id(f"{profile.provider_id}-main", taken)
                    name = f"{spec.display_name} main"
                else:
                    connection_id = _unique_id(f"{profile.provider_id}-{own.instance.id}", taken)
                    label = getattr(own.instance, "display_name", own.instance.id) or own.instance.id
                    name = f"{spec.display_name} ({label})"[: ai_connections.MAX_NAME]
                if not store.credentials.exists(profile.provider_id, connection_id):
                    store.credentials.write_secret(
                        profile.provider_id, connection_id, own.credentials.read_secret(profile.provider_id, profile.credential_ref)
                    )
                connections.append(
                    ai_connections.Connection(connection_id, profile.provider_id, name, profile.model_id, _connection_options(profile))
                )
                mapping[profile.profile_id] = connection_id
                names.append(name)
            routing = own.routing
            first = next(iter(mapping.values()))
            executor = mapping.get(routing.routine_profile_id or "") or mapping.get(routing.creative_profile_id or "") or first
            planner = mapping.get(routing.planner_profile_id or "") or executor
            routine_fallbacks = [mapping[item] for item in routing.routine_fallback_profile_ids if item in mapping]
            planner_fallbacks = [mapping[item] for item in routing.planner_fallback_profile_ids if item in mapping]
            # "If the selected engine fails, try the other one" -> cross fallback.
            cross = planner != executor and (planner in routine_fallbacks or executor in planner_fallbacks)
            fallback = next((item for item in routine_fallbacks + planner_fallbacks if item not in (planner, executor)), None)
            route = ai_connections.RouteSelection(planner, executor, fallback, cross)
            if to_base:
                base = route
            else:
                selections.append((own.instance, route))
        for instance, route in selections:
            selection = for_instance(instance, store).selection
            if not selection.path.exists():  # never overwrite a choice made in the Manager
                selection.save(ai_connections.BotSelection(ai_connections.MODE_CUSTOM, route))
        store.save(
            replace(
                config,
                connections=tuple(connections),
                base=base,
                migrated_instances=tuple(config.migrated_instances) + tuple(done),
            )
        )
    except Exception as exc:
        # Nothing is marked converted, so the next start tries again; old files stay.
        return MigrationResult("failed", message=f"Old per-bot AI settings were not converted: {type(exc).__name__}.")
    if not sources:
        return MigrationResult("nothing" if not skipped else "failed", message="" if not skipped else f"Unreadable AI settings kept for: {', '.join(skipped)}.")
    if to_base:
        message = f"AI keys of {sources[0].instance.id} are now the base set ({', '.join(names)}); every bot uses it unless it has its own choice."
    else:
        message = f"Per-bot AI keys converted to connections: {', '.join(names)}."
    if skipped:
        message += f" Unreadable AI settings kept for: {', '.join(skipped)}."
    return MigrationResult("migrated", sources[0].instance.id if to_base else None, message=message)


def run_migrations() -> list[str]:
    """All AI storage migrations in order; returns messages worth logging."""
    messages = []
    first = migrate_legacy_global_ai()
    if first.status in ("migrated", "ambiguous", "failed") and first.message:
        messages.append(first.message)
    second = migrate_instances_to_connections()
    if second.status in ("migrated", "failed") and second.message:
        messages.append(second.message)
    return messages
