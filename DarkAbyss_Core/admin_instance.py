from __future__ import annotations

import app_paths
import bot_registry
import instance_store

ADMIN_BOT_TYPE_ID = "admin"
DEFAULT_ADMIN_INSTANCE_ID = "admin-main"


class AdminInstanceError(RuntimeError):
    pass


def _read_migration_config_or_default() -> bytes:
    for config_path in (
        app_paths.PHASE1_ADMIN_CONFIG_PATH,
        app_paths.LEGACY_SOURCE_ADMIN_CONFIG_PATH,
    ):
        if config_path.is_file():
            return config_path.read_bytes()
    return app_paths.DEFAULT_ADMIN_CONFIG_PATH.read_bytes()


def _token_bytes_if_real(path) -> bytes | None:
    if not path.is_file():
        return None

    token_bytes = path.read_bytes()
    token_text = token_bytes.decode("utf-8", errors="replace").strip()
    if not token_text or token_text == app_paths.TOKEN_PLACEHOLDER:
        return None
    return token_bytes


def _read_migration_token_or_placeholder() -> bytes:
    for token_path in (
        app_paths.PHASE1_ADMIN_TOKEN_PATH,
        app_paths.LEGACY_SOURCE_ADMIN_TOKEN_PATH,
    ):
        token_bytes = _token_bytes_if_real(token_path)
        if token_bytes is not None:
            return token_bytes
    return (app_paths.TOKEN_PLACEHOLDER + "\n").encode("utf-8")


def _ensure_admin_bot_type(instance: instance_store.BotInstance, instance_id: str) -> instance_store.BotInstance:
    if instance.bot_type != ADMIN_BOT_TYPE_ID:
        raise AdminInstanceError(
            f"Instance {instance_id!r} has bot_type {instance.bot_type!r}; expected {ADMIN_BOT_TYPE_ID!r}."
        )
    return instance


def ensure_admin_instance(instance_id: str = DEFAULT_ADMIN_INSTANCE_ID) -> instance_store.BotInstance:
    if instance_id != DEFAULT_ADMIN_INSTANCE_ID:
        return _ensure_admin_bot_type(instance_store.load_instance(instance_id), instance_id)

    if instance_store.instance_exists(instance_id):
        return _ensure_admin_bot_type(instance_store.load_instance(instance_id), instance_id)

    bot_type = bot_registry.get_bot_type(ADMIN_BOT_TYPE_ID)
    return instance_store._create_instance_atomic(
        bot_type=bot_type,
        instance_id=instance_id,
        display_name=bot_type.display_name,
        config_bytes=_read_migration_config_or_default(),
        token_bytes=_read_migration_token_or_placeholder(),
    )


def main() -> int:
    instance = ensure_admin_instance()
    print("Default Admin Bot instance is ready.")
    print(f"Instance: {instance.id}")
    print(f"Instance root: {instance.paths.root}")
    print(f"Config: {instance.paths.config}")
    print(f"Token: {instance.paths.token}")
    print(f"Put your Discord bot token into: {instance.paths.token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
