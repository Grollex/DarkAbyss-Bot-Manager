from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import app_paths
import bot_registry
import instance_store

CONFIG_META_SCHEMA_VERSION = 1
BACKUP_SCHEMA_VERSION = 1
LEGACY_CONFIG_VERSION = 0


class ConfigStoreError(RuntimeError):
    pass


class ConfigMigrationError(ConfigStoreError):
    pass


class ConfigValidationError(ConfigStoreError):
    pass


@dataclass(frozen=True)
class ConfigSnapshot:
    instance_id: str
    bot_type: str
    config_version: int
    overrides: dict
    defaults: dict
    effective: dict


def _load_json_file(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigStoreError(f"Invalid {label} text encoding at {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigStoreError(f"Unable to read {label} at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigStoreError(f"Invalid {label} JSON at {path}: {exc}") from exc


def _load_json_object(path: Path, label: str) -> dict:
    loaded = _load_json_file(path, label)
    if not isinstance(loaded, dict):
        raise ConfigStoreError(f"Invalid {label} at {path}: root value must be an object.")
    return loaded


def _ensure_instance_owned_file_path(instance: instance_store.BotInstance, path: Path, label: str) -> None:
    expected_paths = instance_store.get_instance_paths(instance.id)
    expected_path = getattr(expected_paths, label)
    if path != expected_path:
        raise ConfigStoreError(f"Instance {instance.id}: unexpected {label} path: {path}")
    if path.is_symlink():
        raise ConfigStoreError(f"Instance {instance.id}: {path.name} must not be a symlink.")

    instance_root = instance.paths.root.resolve()
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(instance_root)
    except ValueError as exc:
        raise ConfigStoreError(f"Instance {instance.id}: {path.name} must stay inside instance root.") from exc

    resolved_parent = resolved_path.parent
    try:
        resolved_parent.relative_to(instance_root)
    except ValueError as exc:
        raise ConfigStoreError(f"Instance {instance.id}: {path.name} parent must stay inside instance root.") from exc


def _ensure_config_file_safe(instance: instance_store.BotInstance) -> None:
    _ensure_instance_owned_file_path(instance, instance.paths.config, "config")


def _ensure_config_meta_file_safe(instance: instance_store.BotInstance) -> None:
    if instance.paths.config_meta.exists() or instance.paths.config_meta.is_symlink():
        _ensure_instance_owned_file_path(instance, instance.paths.config_meta, "config_meta")


def _merge_config(defaults: dict, overrides: dict) -> dict:
    effective = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(effective.get(key), dict):
            effective[key] = _merge_config(effective[key], value)
        else:
            effective[key] = copy.deepcopy(value)
    return effective


def _schema_type_matches(value: object, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "null":
        return value is None
    return False


def _validate_schema_node(value: object, schema: dict, path: str) -> None:
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                _validate_schema_node(value, option, path)
                return
            except ConfigValidationError as exc:
                errors.append(str(exc))
        raise ConfigValidationError(f"{path} does not match any allowed schema: {'; '.join(errors)}")

    expected_type = schema.get("type")
    if expected_type is not None:
        if not isinstance(expected_type, str) or not _schema_type_matches(value, expected_type):
            raise ConfigValidationError(f"{path} must be {expected_type}.")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ConfigValidationError(f"{path} schema has invalid required list.")
        for key in required:
            if key not in value:
                raise ConfigValidationError(f"{path}.{key} is required.")

        properties = schema.get("properties", {})
        if properties is not None and not isinstance(properties, dict):
            raise ConfigValidationError(f"{path} schema properties must be an object.")
        additional = schema.get("additionalProperties", True)
        for key, child_value in value.items():
            child_schema = properties.get(key) if isinstance(properties, dict) else None
            if child_schema is None:
                if additional is False:
                    raise ConfigValidationError(f"{path}.{key} is not allowed.")
                continue
            if not isinstance(child_schema, dict):
                raise ConfigValidationError(f"{path}.{key} schema must be an object.")
            _validate_schema_node(child_value, child_schema, f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, dict):
            raise ConfigValidationError(f"{path} items schema must be an object.")
        for index, item in enumerate(value):
            _validate_schema_node(item, item_schema, f"{path}[{index}]")

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not isinstance(value, str):
            raise ConfigValidationError(f"{path} pattern requires a string value.")
        if re.fullmatch(pattern, value) is None:
            raise ConfigValidationError(f"{path} does not match pattern {pattern!r}.")


def _load_schema(bot_type: bot_registry.BotType) -> dict:
    schema = _load_json_object(bot_type.config_schema, "config schema")
    schema_version = schema.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != CONFIG_META_SCHEMA_VERSION:
        raise ConfigStoreError(
            f"Unsupported config schema version {schema_version!r} in {bot_type.config_schema}."
        )
    return schema


def _load_default_config(bot_type: bot_registry.BotType) -> dict:
    return _load_json_object(bot_type.default_config, "default config")


def _load_user_overrides(instance: instance_store.BotInstance) -> dict:
    _ensure_config_file_safe(instance)
    return _load_json_object(instance.paths.config, "instance config")


def _load_instance_and_bot_type(instance_id: str) -> tuple[instance_store.BotInstance, bot_registry.BotType]:
    try:
        instance = instance_store.load_instance(instance_id)
        bot_type = bot_registry.get_bot_type(instance.bot_type)
    except (instance_store.InstanceStoreError, bot_registry.BotRegistryError, OSError) as exc:
        raise ConfigStoreError(f"Invalid config target {instance_id!r}: {exc}") from exc
    return instance, bot_type


def _read_config_meta(instance: instance_store.BotInstance, bot_type: bot_registry.BotType) -> int:
    _ensure_config_meta_file_safe(instance)
    if not instance.paths.config_meta.exists():
        return LEGACY_CONFIG_VERSION

    meta = _load_json_object(instance.paths.config_meta, "config metadata")
    schema_version = meta.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != CONFIG_META_SCHEMA_VERSION:
        raise ConfigStoreError(
            f"Instance {instance.id}: unsupported config metadata schema_version {schema_version!r}."
        )

    config_version = meta.get("config_version")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 0:
        raise ConfigStoreError(f"Instance {instance.id}: invalid config_version {config_version!r}.")
    if config_version > bot_type.config_version:
        raise ConfigStoreError(
            f"Instance {instance.id}: config version {config_version} is newer than supported "
            f"{bot_type.config_version}."
        )
    return config_version


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _meta_bytes(config_version: int) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": CONFIG_META_SCHEMA_VERSION,
                "config_version": config_version,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _overrides_bytes(overrides: dict) -> bytes:
    return (json.dumps(overrides, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _ensure_backup_path_is_safe(path: Path) -> None:
    try:
        path.resolve().relative_to(app_paths.BACKUPS_DIR.resolve())
    except ValueError as exc:
        raise ConfigMigrationError(f"Backup path escapes BACKUPS_DIR: {path}") from exc


def _create_config_backup(
    instance: instance_store.BotInstance,
    config_bytes: bytes,
    from_version: int,
    to_version: int,
) -> Path:
    if instance.paths.config != instance_store.get_instance_paths(instance.id).config:
        raise ConfigMigrationError(f"Refusing to back up unexpected config path for instance {instance.id!r}.")

    backup_parent = app_paths.BACKUPS_DIR / "instances" / instance.id / "config"
    _ensure_backup_path_is_safe(backup_parent)
    backup_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".backup.", suffix=".tmp", dir=backup_parent))
    final_root = backup_parent / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}"
    _ensure_backup_path_is_safe(staging_root)
    _ensure_backup_path_is_safe(final_root)
    try:
        (staging_root / "config.json").write_bytes(config_bytes)
        metadata = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "instance_id": instance.id,
            "from_config_version": from_version,
            "to_config_version": to_version,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        }
        (staging_root / "backup.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if final_root.exists():
            raise ConfigMigrationError(f"Backup destination already exists: {final_root}")
        staging_root.rename(final_root)
        return final_root
    except Exception as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        if isinstance(exc, ConfigMigrationError):
            raise
        raise ConfigMigrationError(f"Failed to create config backup for instance {instance.id!r}: {exc}") from exc


def _validate_effective_config(effective: dict, bot_type: bot_registry.BotType) -> None:
    try:
        _validate_schema_node(effective, _load_schema(bot_type), "$")
    except ConfigValidationError:
        raise
    except ConfigStoreError:
        raise
    except Exception as exc:
        raise ConfigValidationError(f"Config schema validation failed: {exc}") from exc


def _load_effective_config_current(
    instance: instance_store.BotInstance,
    bot_type: bot_registry.BotType,
) -> dict:
    defaults = _load_default_config(bot_type)
    overrides = _load_user_overrides(instance)
    effective = _merge_config(defaults, overrides)
    _validate_effective_config(effective, bot_type)
    return effective


def get_config_version(instance_id: str) -> int:
    instance, bot_type = _load_instance_and_bot_type(instance_id)
    return _read_config_meta(instance, bot_type)


def load_effective_config(instance_id: str) -> dict:
    ensure_config_current(instance_id)
    instance, bot_type = _load_instance_and_bot_type(instance_id)
    current_version = _read_config_meta(instance, bot_type)
    if current_version != bot_type.config_version:
        raise ConfigMigrationError(
            f"Instance {instance.id}: config version {current_version} is not current after migration."
        )
    return _load_effective_config_current(instance, bot_type)


def load_config_overrides(instance_id: str) -> dict:
    ensure_config_current(instance_id)
    instance, _bot_type = _load_instance_and_bot_type(instance_id)
    return copy.deepcopy(_load_user_overrides(instance))


def get_config_snapshot(instance_id: str) -> ConfigSnapshot:
    ensure_config_current(instance_id)
    instance, bot_type = _load_instance_and_bot_type(instance_id)
    config_version = _read_config_meta(instance, bot_type)
    if config_version != bot_type.config_version:
        raise ConfigMigrationError(
            f"Instance {instance.id}: config version {config_version} is not current after migration."
        )
    defaults = _load_default_config(bot_type)
    overrides = _load_user_overrides(instance)
    effective = _merge_config(defaults, overrides)
    _validate_effective_config(effective, bot_type)
    return ConfigSnapshot(
        instance_id=instance.id,
        bot_type=instance.bot_type,
        config_version=config_version,
        overrides=copy.deepcopy(overrides),
        defaults=copy.deepcopy(defaults),
        effective=copy.deepcopy(effective),
    )


def save_config_overrides(instance_id: str, overrides: dict) -> dict:
    ensure_config_current(instance_id)
    instance, bot_type = _load_instance_and_bot_type(instance_id)
    current_version = _read_config_meta(instance, bot_type)
    if current_version != bot_type.config_version:
        raise ConfigMigrationError(
            f"Instance {instance.id}: config version {current_version} is not current after migration."
        )
    _ensure_config_file_safe(instance)
    if not isinstance(overrides, dict):
        raise ConfigValidationError("Config overrides must be a JSON object.")

    proposed_overrides = copy.deepcopy(overrides)
    defaults = _load_default_config(bot_type)
    effective = _merge_config(defaults, proposed_overrides)
    _validate_effective_config(effective, bot_type)

    try:
        _atomic_write_bytes(instance.paths.config, _overrides_bytes(proposed_overrides))
    except Exception as exc:
        raise ConfigStoreError(f"Failed to write config overrides for instance {instance.id!r}: {exc}") from exc
    return copy.deepcopy(proposed_overrides)


def ensure_config_current(instance_id: str) -> int:
    instance, bot_type = _load_instance_and_bot_type(instance_id)
    current_version = _read_config_meta(instance, bot_type)
    target_version = bot_type.config_version
    if current_version == target_version:
        _load_effective_config_current(instance, bot_type)
        return current_version

    if current_version > target_version:
        raise ConfigMigrationError(
            f"Instance {instance.id}: config version {current_version} is newer than supported {target_version}."
        )
    if current_version != LEGACY_CONFIG_VERSION or target_version != 1:
        raise ConfigMigrationError(
            f"Instance {instance.id}: no migration path from config version {current_version} to {target_version}."
        )

    try:
        _ensure_config_file_safe(instance)
        original_config_bytes = instance.paths.config.read_bytes()
    except OSError as exc:
        raise ConfigMigrationError(f"Unable to read config before migration: {instance.paths.config}") from exc

    _load_effective_config_current(instance, bot_type)

    _create_config_backup(instance, original_config_bytes, current_version, target_version)
    try:
        _atomic_write_bytes(instance.paths.config_meta, _meta_bytes(target_version))
    except Exception as exc:
        raise ConfigMigrationError(f"Failed to write config metadata for instance {instance.id!r}: {exc}") from exc
    return target_version
