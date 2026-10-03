"""Shared AI provider connections, the base set, and each bot's choice.

    <DATA_ROOT>/config/ai_connections.json               connections + base set (no keys)
    <DATA_ROOT>/secrets/ai_connections/<provider>/<id>.secret   one API key per connection
    <DATA_ROOT>/instances/<id>/data/ai_selection.json    the bot's choice: base set or custom

A connection is one provider account: provider + key + model + options, e.g.
"Groq main" (GPT-OSS 120B) or "Groq backup". Several connections may use the
same provider. The base set names the connections for planning, execution and
fallback; every bot uses it unless it has its own (custom) choice. Bots that
use the same connection share its key and its provider limits.

``BotSettingsView`` turns (connections + the bot's choice) into the
``AISettings`` the orchestrator reads on every request, containing ONLY the
connections that bot may use, so a bot can never fall back to a connection it
was not given. Changes in the Manager apply to running bots immediately.
Unreadable files fail closed (AI unavailable, file kept).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import ai_platform
import ai_providers
import app_paths

CONNECTIONS_FILE_NAME = "ai_connections.json"
CREDENTIALS_DIR_NAME = "ai_connections"
SELECTION_FILE_NAME = "ai_selection.json"
MANAGER_USAGE_FILE_NAME = "ai_usage_manager.json"
SCHEMA_VERSION = 1
MAX_NAME = 60
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODE_BASE = "base"
MODE_CUSTOM = "custom"


class ConnectionsError(ai_platform.AIPlatformError):
    """Connection settings are invalid or could not be stored."""


def connections_path() -> Path:
    return app_paths.CONFIG_DIR / CONNECTIONS_FILE_NAME


def credentials_root() -> Path:
    return app_paths.SECRETS_DIR / CREDENTIALS_DIR_NAME


def manager_usage_path() -> Path:
    """Usage of Manager Test Connection calls (they spend real provider quota)."""
    return app_paths.CONFIG_DIR / MANAGER_USAGE_FILE_NAME


def validate_connection_id(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ConnectionsError(f"Invalid connection id: {value!r}")
    return value


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    connection_id: str
    provider_id: str
    name: str
    model_id: str
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_connection_id(self.connection_id)
        spec = ai_providers.get_spec(self.provider_id)
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > MAX_NAME:
            raise ConnectionsError(f"Connection name must be 1-{MAX_NAME} characters.")
        if not isinstance(self.model_id, str) or not self.model_id.strip() or len(self.model_id) > 128:
            raise ConnectionsError("Connection model is invalid.")
        options = dict(self.options or {})
        for key, value in options.items():
            if key not in spec.options:
                raise ConnectionsError(f"{spec.display_name} does not support option {key!r}.")
            if key == "reasoning_effort" and value not in spec.reasoning_levels:
                raise ConnectionsError("Unsupported reasoning level.")
        object.__setattr__(self, "name", " ".join(self.name.split()))
        object.__setattr__(self, "options", options)

    def profile(self) -> ai_platform.AIProfile:
        # The connection ID is both the profile ID and the credential reference.
        return ai_platform.AIProfile(
            profile_id=self.connection_id,
            provider_id=self.provider_id,
            model_id=self.model_id,
            credential_ref=self.connection_id,
            options=dict(self.options),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.connection_id,
            "provider_id": self.provider_id,
            "name": self.name,
            "model_id": self.model_id,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class RouteSelection:
    """Which connections plan, execute (also used for wording) and back them up.

    ``cross_fallback``: if the planning or the execution connection fails, the
    other one takes over. ``fallback``: an extra connection tried afterwards.
    """

    planner: str | None = None
    executor: str | None = None
    fallback: str | None = None
    cross_fallback: bool = False

    def __post_init__(self) -> None:
        for name in ("planner", "executor", "fallback"):
            value = getattr(self, name)
            if value is not None:
                validate_connection_id(value)
        if not isinstance(self.cross_fallback, bool):
            raise ConnectionsError("cross_fallback must be true or false.")

    def connection_ids(self) -> tuple[str, ...]:
        ordered = []
        for value in (self.planner, self.executor, self.fallback):
            if value and value not in ordered:
                ordered.append(value)
        return tuple(ordered)

    def restricted_to(self, available: Iterable[str]) -> "RouteSelection":
        keep = set(available)
        return replace(
            self,
            planner=self.planner if self.planner in keep else None,
            executor=self.executor if self.executor in keep else None,
            fallback=self.fallback if self.fallback in keep else None,
        )

    def routing(self) -> ai_platform.RoutingConfig:
        executor = self.executor or self.planner
        planner = self.planner or executor

        def fallbacks(primary: str | None, other: str | None) -> tuple[str, ...]:
            items: list[str] = []
            if self.cross_fallback and other and other != primary:
                items.append(other)
            if self.fallback and self.fallback != primary and self.fallback not in items:
                items.append(self.fallback)
            return tuple(items)

        return ai_platform.RoutingConfig(
            routine_profile_id=executor,
            planner_profile_id=planner,
            creative_profile_id=executor,
            routine_fallback_profile_ids=fallbacks(executor, planner),
            planner_fallback_profile_ids=fallbacks(planner, executor),
            creative_fallback_profile_ids=fallbacks(executor, planner),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "executor": self.executor,
            "fallback": self.fallback,
            "cross_fallback": self.cross_fallback,
        }

    @staticmethod
    def from_dict(raw: Any) -> "RouteSelection":
        if raw is None:
            return RouteSelection()
        if not isinstance(raw, dict):
            raise ConnectionsError("Route selection must be an object.")
        return RouteSelection(raw.get("planner"), raw.get("executor"), raw.get("fallback"), raw.get("cross_fallback", False))


@dataclass(frozen=True)
class ConnectionsConfig:
    connections: tuple[Connection, ...] = ()
    base: RouteSelection = RouteSelection()
    # Bot instances whose old per-bot AI settings were already converted.
    migrated_instances: tuple[str, ...] = ()

    def get(self, connection_id: str | None) -> Connection | None:
        return next((item for item in self.connections if item.connection_id == connection_id), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.connection_id for item in self.connections)


@dataclass(frozen=True)
class BotSelection:
    mode: str = MODE_BASE
    custom: RouteSelection = RouteSelection()

    def __post_init__(self) -> None:
        if self.mode not in (MODE_BASE, MODE_CUSTOM):
            raise ConnectionsError("AI source must be 'base' or 'custom'.")

    def route(self, config: ConnectionsConfig) -> RouteSelection:
        return config.base if self.mode == MODE_BASE else self.custom


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ConnectionsError(f"Could not write {path.name}.") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectionsError(f"{path.name} is unreadable.") from exc


class ConnectionStore:
    """Connections + base set (shared by every bot) and their API keys."""

    _lock = threading.RLock()

    def __init__(self, path: Path | str | None = None, credentials_dir: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else connections_path()
        self.credentials = ai_platform.CredentialStore(credentials_dir if credentials_dir is not None else credentials_root())

    def load(self) -> ConnectionsConfig:
        if not self.path.exists():
            return ConnectionsConfig()
        raw = _read_json(self.path)
        try:
            if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
                raise ConnectionsError("unsupported schema")
            items = raw.get("connections", [])
            if not isinstance(items, list):
                raise ConnectionsError("connections must be a list")
            connections = []
            for item in items:
                if not isinstance(item, dict):
                    raise ConnectionsError("connection must be an object")
                connections.append(
                    Connection(item.get("id"), item.get("provider_id"), item.get("name"), item.get("model_id"), item.get("options") or {})
                )
            ids = [item.connection_id for item in connections]
            if len(ids) != len(set(ids)):
                raise ConnectionsError("duplicate connection id")
            migrated = raw.get("migrated_instances", [])
            if not isinstance(migrated, list) or not all(isinstance(item, str) for item in migrated):
                raise ConnectionsError("migrated_instances must be a list")
            return ConnectionsConfig(tuple(connections), RouteSelection.from_dict(raw.get("base")), tuple(migrated))
        except (ConnectionsError, ai_platform.AIPlatformError, ValueError, TypeError) as exc:
            raise ConnectionsError(f"{self.path.name} is invalid: {exc}") from exc

    def save(self, config: ConnectionsConfig) -> None:
        known = set(config.ids())
        for connection_id in config.base.connection_ids():
            if connection_id not in known:
                raise ConnectionsError(f"Base set uses an unknown connection: {connection_id}")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "connections": [item.public_dict() for item in config.connections],
            "base": config.base.public_dict(),
            "migrated_instances": list(config.migrated_instances),
        }
        with self._lock:
            _atomic_write_json(self.path, payload)

    # -- editing helpers (Manager) ----------------------------------------------------

    def new_connection_id(self, provider_id: str, config: ConnectionsConfig | None = None) -> str:
        ai_providers.get_spec(provider_id)
        existing = set((config or self.load()).ids())
        number = 1
        while f"{provider_id}-{number}" in existing:
            number += 1
        return f"{provider_id}-{number}"

    def upsert(self, connection: Connection, api_key: str | None = None) -> ConnectionsConfig:
        """Add or update one connection; a blank ``api_key`` keeps the saved key."""
        with self._lock:
            config = self.load()
            existing = config.get(connection.connection_id)
            if existing is not None and existing.provider_id != connection.provider_id:
                raise ConnectionsError("The provider of an existing connection cannot change.")
            if api_key is not None and api_key.strip():
                self.credentials.write_secret(connection.provider_id, connection.connection_id, api_key.strip())
            if existing is None:
                connections = config.connections + (connection,)
            else:
                connections = tuple(connection if item.connection_id == connection.connection_id else item for item in config.connections)
            updated = replace(config, connections=connections)
            self.save(updated)
            return updated

    def remove(self, connection_id: str) -> ConnectionsConfig:
        """Remove a connection and its key. Refused while the base set uses it."""
        with self._lock:
            config = self.load()
            connection = config.get(connection_id)
            if connection is None:
                return config
            if connection_id in config.base.connection_ids():
                raise ConnectionsError("The base set uses this connection; choose another one first.")
            updated = replace(config, connections=tuple(item for item in config.connections if item.connection_id != connection_id))
            self.save(updated)
            try:
                self.credentials.delete_secret(connection.provider_id, connection.connection_id)
            except ai_platform.CredentialStoreError:
                pass
            return updated

    def set_base(self, base: RouteSelection) -> ConnectionsConfig:
        with self._lock:
            updated = replace(self.load(), base=base)
            self.save(updated)
            return updated

    def has_key(self, connection: Connection) -> bool:
        try:
            return self.credentials.exists(connection.provider_id, connection.connection_id)
        except (ValueError, ai_platform.CredentialStoreError):
            return False


class SelectionStore:
    """One bot's AI source: the base set (default) or its own route."""

    def __init__(self, path: Path | str) -> None:
        if path is None:
            raise ConnectionsError("Selection path is required.")
        self.path = Path(path).resolve()

    def load(self) -> BotSelection:
        if not self.path.exists():
            return BotSelection()
        raw = _read_json(self.path)
        try:
            if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
                raise ConnectionsError("unsupported schema")
            return BotSelection(raw.get("mode", MODE_BASE), RouteSelection.from_dict(raw.get("custom")))
        except (ConnectionsError, ValueError, TypeError) as exc:
            raise ConnectionsError(f"{self.path.name} is invalid: {exc}") from exc

    def save(self, selection: BotSelection) -> None:
        _atomic_write_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, "mode": selection.mode, "custom": selection.custom.public_dict()},
        )


class BotSettingsView:
    """Read-only ``AISettings`` of one bot: only the connections it may use."""

    def __init__(self, connections: ConnectionStore, selection: SelectionStore) -> None:
        self.connections = connections
        self.selection = selection

    def route(self) -> tuple[ConnectionsConfig, RouteSelection]:
        config = self.connections.load()
        route = self.selection.load().route(config)
        return config, route.restricted_to(config.ids())

    def load(self) -> ai_platform.AISettings:
        try:
            config, route = self.route()
        except ConnectionsError as exc:
            raise ai_platform.AIPlatformError("AI settings are invalid.") from exc
        used = route.connection_ids()
        profiles = tuple(config.get(connection_id).profile() for connection_id in used)
        return ai_platform.AISettings(profiles=profiles, routing=route.routing())

    def save(self, settings: Any) -> None:
        raise ai_platform.AIPlatformError("Bot AI settings are edited through connections and the bot's AI source.")
