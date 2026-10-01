from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import app_paths
import bot_registry

INSTANCE_SCHEMA_VERSION = 1
SAFE_INSTANCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STAGING_DIR_NAME = ".staging"


class InstanceStoreError(ValueError):
    pass


class InstanceAlreadyExistsError(InstanceStoreError):
    pass


@dataclass(frozen=True)
class InstancePaths:
    root: Path
    metadata: Path
    config: Path
    config_meta: Path
    token: Path
    secrets_dir: Path
    runtime_dir: Path
    logs_dir: Path
    data_dir: Path


@dataclass(frozen=True)
class BotInstance:
    schema_version: int
    id: str
    bot_type: str
    display_name: str
    paths: InstancePaths


def validate_instance_id(instance_id: object) -> str:
    if not isinstance(instance_id, str) or not SAFE_INSTANCE_ID_PATTERN.fullmatch(instance_id):
        raise InstanceStoreError(f"Invalid instance id: {instance_id!r}")
    return instance_id


def get_instance_paths(instance_id: str) -> InstancePaths:
    valid_id = validate_instance_id(instance_id)
    root = app_paths.INSTANCES_DIR / valid_id
    return InstancePaths(
        root=root,
        metadata=root / "instance.json",
        config=root / "config.json",
        config_meta=root / "config.meta.json",
        token=root / "secrets" / "token.txt",
        secrets_dir=root / "secrets",
        runtime_dir=root / "runtime",
        logs_dir=root / "logs",
        data_dir=root / "data",
    )


def instance_exists(instance_id: str) -> bool:
    return get_instance_paths(instance_id).root.exists()


def get_staging_root() -> Path:
    return app_paths.INSTANCES_DIR / STAGING_DIR_NAME


def _ensure_instance_root_is_safe(paths: InstancePaths) -> None:
    try:
        paths.root.resolve().relative_to(app_paths.INSTANCES_DIR.resolve())
    except ValueError as exc:
        raise InstanceStoreError(f"Instance path escapes instances directory: {paths.root}") from exc


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _atomic_write_json(path: Path, payload: dict) -> None:
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstanceStoreError(f"Invalid {label} JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstanceStoreError(f"Invalid {label} at {path}: root value must be an object.")
    return payload


def _validate_display_name(display_name: str | None, default_display_name: str) -> str:
    if display_name is None:
        return default_display_name
    if not isinstance(display_name, str) or not display_name.strip():
        raise InstanceStoreError("display_name must be a non-empty string when provided.")
    return display_name


def _validate_updated_display_name(display_name: object) -> str:
    if not isinstance(display_name, str) or not display_name.strip():
        raise InstanceStoreError("display_name must be a non-empty string.")
    return display_name.strip()


def _paths_for_root(root: Path) -> InstancePaths:
    return InstancePaths(
        root=root,
        metadata=root / "instance.json",
        config=root / "config.json",
        config_meta=root / "config.meta.json",
        token=root / "secrets" / "token.txt",
        secrets_dir=root / "secrets",
        runtime_dir=root / "runtime",
        logs_dir=root / "logs",
        data_dir=root / "data",
    )


def _validate_loaded_layout(instance_id: str, paths: InstancePaths) -> None:
    required_files = (
        (paths.config, "config.json"),
        (paths.token, "secrets/token.txt"),
    )
    required_dirs = (
        (paths.secrets_dir, "secrets/"),
        (paths.runtime_dir, "runtime/"),
        (paths.logs_dir, "logs/"),
        (paths.data_dir, "data/"),
    )

    for path, label in required_files:
        if not path.is_file():
            raise InstanceStoreError(f"Instance {instance_id}: required file missing or not a file: {label}")

    for path, label in required_dirs:
        if not path.is_dir():
            raise InstanceStoreError(f"Instance {instance_id}: required directory missing or not a directory: {label}")


def _ensure_metadata_file_is_safe(instance_id: str, paths: InstancePaths) -> None:
    expected_paths = get_instance_paths(instance_id)
    if paths.metadata != expected_paths.metadata:
        raise InstanceStoreError(f"Instance {instance_id}: unexpected metadata path: {paths.metadata}")
    _ensure_instance_root_is_safe(paths)
    if paths.metadata.is_symlink():
        raise InstanceStoreError(f"Instance {instance_id}: refusing to write symlinked metadata: {paths.metadata}")
    try:
        paths.metadata.resolve(strict=False).relative_to(paths.root.resolve(strict=False))
    except ValueError as exc:
        raise InstanceStoreError(f"Instance {instance_id}: metadata path escapes instance root: {paths.metadata}") from exc
    if not paths.metadata.is_file():
        raise InstanceStoreError(f"Instance {instance_id}: metadata missing or not a file: {paths.metadata}")


def _write_initial_instance_files(
    paths: InstancePaths,
    metadata: dict,
    config_bytes: bytes,
    token_bytes: bytes,
    config_meta_bytes: bytes | None = None,
) -> None:
    paths.secrets_dir.mkdir()
    paths.runtime_dir.mkdir()
    paths.logs_dir.mkdir()
    paths.data_dir.mkdir()
    _write_json(paths.metadata, metadata)
    paths.config.write_bytes(config_bytes)
    if config_meta_bytes is not None:
        paths.config_meta.write_bytes(config_meta_bytes)
    paths.token.write_bytes(token_bytes)


def _create_instance_atomic(
    bot_type: bot_registry.BotType,
    instance_id: str,
    display_name: str | None,
    config_bytes: bytes,
    token_bytes: bytes,
    config_meta_bytes: bytes | None = None,
) -> BotInstance:
    valid_instance_id = validate_instance_id(instance_id)
    resolved_display_name = _validate_display_name(display_name, bot_type.display_name)
    paths = get_instance_paths(valid_instance_id)
    _ensure_instance_root_is_safe(paths)

    if paths.root.exists():
        raise InstanceAlreadyExistsError(f"Instance already exists: {valid_instance_id}")

    app_paths.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    staging_parent = get_staging_root()
    staging_parent.mkdir(exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f"{valid_instance_id}.", suffix=".tmp", dir=staging_parent))
    staging_paths = _paths_for_root(staging_root)
    try:
        metadata = {
            "schema_version": INSTANCE_SCHEMA_VERSION,
            "id": valid_instance_id,
            "bot_type": bot_type.id,
            "display_name": resolved_display_name,
        }
        _write_initial_instance_files(staging_paths, metadata, config_bytes, token_bytes, config_meta_bytes)
        if paths.root.exists():
            raise InstanceAlreadyExistsError(f"Instance already exists: {valid_instance_id}")
        staging_root.rename(paths.root)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

    return load_instance(valid_instance_id)


def create_instance(bot_type_id: str, instance_id: str, display_name: str | None = None) -> BotInstance:
    bot_type = bot_registry.get_bot_type(bot_type_id)
    config_bytes = b"{}\n"
    config_meta_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "config_version": bot_type.config_version,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    token_bytes = (app_paths.TOKEN_PLACEHOLDER + "\n").encode("utf-8")
    return _create_instance_atomic(bot_type, instance_id, display_name, config_bytes, token_bytes, config_meta_bytes)


def load_instance(instance_id: str) -> BotInstance:
    valid_instance_id = validate_instance_id(instance_id)
    paths = get_instance_paths(valid_instance_id)
    _ensure_instance_root_is_safe(paths)

    if not paths.metadata.is_file():
        raise InstanceStoreError(f"Missing instance metadata: {paths.metadata}")

    metadata = _read_json(paths.metadata, "instance metadata")
    schema_version = metadata.get("schema_version")
    if not isinstance(schema_version, int) or schema_version != INSTANCE_SCHEMA_VERSION:
        raise InstanceStoreError(f"Instance {valid_instance_id}: unsupported schema_version {schema_version!r}.")

    metadata_id = metadata.get("id")
    if metadata_id != valid_instance_id:
        raise InstanceStoreError(f"Instance {valid_instance_id}: metadata id must match directory name.")

    bot_type_id = metadata.get("bot_type")
    try:
        bot_registry.get_bot_type(bot_type_id)
    except bot_registry.BotRegistryError as exc:
        raise InstanceStoreError(f"Instance {valid_instance_id}: unknown bot_type {bot_type_id!r}.") from exc

    display_name = metadata.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise InstanceStoreError(f"Instance {valid_instance_id}: display_name must be a non-empty string.")

    _validate_loaded_layout(valid_instance_id, paths)

    return BotInstance(
        schema_version=schema_version,
        id=valid_instance_id,
        bot_type=bot_type_id,
        display_name=display_name,
        paths=paths,
    )


def update_instance_display_name(instance_id: str, display_name: str) -> BotInstance:
    valid_instance_id = validate_instance_id(instance_id)
    updated_display_name = _validate_updated_display_name(display_name)
    instance = load_instance(valid_instance_id)
    _ensure_metadata_file_is_safe(valid_instance_id, instance.paths)
    metadata = _read_json(instance.paths.metadata, "instance metadata")

    if metadata.get("schema_version") != instance.schema_version:
        raise InstanceStoreError(f"Instance {valid_instance_id}: metadata schema changed unexpectedly.")
    if metadata.get("id") != instance.id:
        raise InstanceStoreError(f"Instance {valid_instance_id}: metadata id changed unexpectedly.")
    if metadata.get("bot_type") != instance.bot_type:
        raise InstanceStoreError(f"Instance {valid_instance_id}: metadata bot_type changed unexpectedly.")

    updated_metadata = dict(metadata)
    updated_metadata["display_name"] = updated_display_name

    try:
        _atomic_write_json(instance.paths.metadata, updated_metadata)
    except OSError as exc:
        raise InstanceStoreError(f"Instance {valid_instance_id}: failed to update display_name: {exc}") from exc

    return load_instance(valid_instance_id)


def list_instances() -> list[BotInstance]:
    if not app_paths.INSTANCES_DIR.exists():
        return []
    instances: list[BotInstance] = []
    for instance_dir in sorted(path for path in app_paths.INSTANCES_DIR.iterdir() if path.is_dir()):
        if instance_dir.name == STAGING_DIR_NAME:
            continue
        instances.append(load_instance(instance_dir.name))
    return instances


def main() -> int:
    parser = argparse.ArgumentParser(description="Small development helper for bot instances.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("bot_type_id")
    create_parser.add_argument("instance_id")
    create_parser.add_argument("--display-name")
    args = parser.parse_args()

    if args.command == "list":
        for instance in list_instances():
            print(f"{instance.id}\t{instance.bot_type}\t{instance.display_name}")
        return 0

    if args.command == "create":
        instance = create_instance(args.bot_type_id, args.instance_id, args.display_name)
        print(f"Created instance: {instance.id}")
        print(f"Instance root: {instance.paths.root}")
        print(f"Config: {instance.paths.config}")
        print(f"Token: {instance.paths.token}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
