from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

import app_paths
import runtime_layout

SUPPORTED_SCHEMA_VERSION = 1
SAFE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

PROJECT_ROOT = runtime_layout.resource_root()
BOTS_DIR = runtime_layout.bots_root()


class BotRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class BotType:
    schema_version: int
    id: str
    display_name: str
    version: str
    entrypoint: Path
    default_config: Path
    config_schema: Path
    config_version: int
    manifest_path: Path


def validate_bot_type_id(bot_type_id: object) -> str:
    if not isinstance(bot_type_id, str) or not SAFE_ID_PATTERN.fullmatch(bot_type_id):
        raise BotRegistryError(f"Invalid bot type id: {bot_type_id!r}")
    return bot_type_id


def _path_is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _relative_parts(value: object, field_name: str, bot_type_id: str) -> Iterable[str]:
    if not isinstance(value, str) or not value.strip():
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} must be a non-empty relative path.")

    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.is_absolute() or posix_path.is_absolute() or windows_path.drive:
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} must be relative.")

    parts = windows_path.parts if "\\" in value else posix_path.parts
    if any(part in ("", ".", "..") for part in parts):
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} must not contain path traversal.")
    return parts


def _resolve_program_path(value: object, field_name: str, bot_type_id: str) -> Path:
    candidate = (PROJECT_ROOT.joinpath(*_relative_parts(value, field_name, bot_type_id))).resolve()
    if not _path_is_inside(candidate, PROJECT_ROOT):
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} resolves outside the program tree.")
    if _path_is_inside(candidate, app_paths.DATA_ROOT):
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} must not resolve into DATA_ROOT.")
    if not candidate.exists():
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} does not exist: {candidate}")
    if not candidate.is_file():
        raise BotRegistryError(f"Bot type {bot_type_id}: {field_name} must be a file: {candidate}")
    return candidate


def load_bot_type(bot_type_dir: Path) -> BotType:
    manifest_path = bot_type_dir / "manifest.json"
    if not manifest_path.exists():
        raise BotRegistryError(f"Bot type directory {bot_type_dir.name}: missing manifest.json")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BotRegistryError(f"Bot type directory {bot_type_dir.name}: malformed manifest.json: {exc}") from exc

    if not isinstance(manifest, dict):
        raise BotRegistryError(f"Bot type directory {bot_type_dir.name}: manifest root must be an object.")

    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise BotRegistryError(f"Bot type directory {bot_type_dir.name}: unsupported schema_version {schema_version!r}.")

    bot_type_id = validate_bot_type_id(manifest.get("id"))
    if bot_type_id != bot_type_dir.name:
        raise BotRegistryError(f"Bot type {bot_type_id}: id must match directory name {bot_type_dir.name!r}.")

    display_name = manifest.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise BotRegistryError(f"Bot type {bot_type_id}: display_name must be a non-empty string.")

    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise BotRegistryError(f"Bot type {bot_type_id}: version must be a non-empty string.")

    entrypoint = _resolve_program_path(manifest.get("entrypoint"), "entrypoint", bot_type_id)
    default_config = _resolve_program_path(manifest.get("default_config"), "default_config", bot_type_id)
    config_schema = _resolve_program_path(manifest.get("config_schema"), "config_schema", bot_type_id)
    config_version = manifest.get("config_version")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 1:
        raise BotRegistryError(f"Bot type {bot_type_id}: config_version must be a positive integer.")

    return BotType(
        schema_version=schema_version,
        id=bot_type_id,
        display_name=display_name,
        version=version,
        entrypoint=entrypoint,
        default_config=default_config,
        config_schema=config_schema,
        config_version=config_version,
        manifest_path=manifest_path.resolve(),
    )


def discover_bot_types(bots_dir: Path = BOTS_DIR) -> dict[str, BotType]:
    bot_types: dict[str, BotType] = {}
    if not bots_dir.exists():
        return bot_types

    for bot_type_dir in sorted(path for path in bots_dir.iterdir() if path.is_dir()):
        bot_type = load_bot_type(bot_type_dir)
        if bot_type.id in bot_types:
            raise BotRegistryError(f"Duplicate bot type id: {bot_type.id}")
        bot_types[bot_type.id] = bot_type
    return bot_types


def get_bot_type(bot_type_id: str, bots_dir: Path = BOTS_DIR) -> BotType:
    validate_bot_type_id(bot_type_id)
    bot_types = discover_bot_types(bots_dir)
    try:
        return bot_types[bot_type_id]
    except KeyError as exc:
        raise BotRegistryError(f"Unknown bot type: {bot_type_id}") from exc
