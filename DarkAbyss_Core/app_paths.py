import os
from pathlib import Path

import runtime_layout

TOKEN_PLACEHOLDER = "PUT_DISCORD_BOT_TOKEN_HERE"

PROGRAM_ROOT = runtime_layout.core_root()
DEFAULTS_DIR = PROGRAM_ROOT / "defaults"
DEFAULT_ADMIN_CONFIG_PATH = DEFAULTS_DIR / "admin_config.json"


def _data_root() -> Path:
    override = os.environ.get("DARKABYSS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "DarkAbyssBotManager"

    return Path.home() / ".darkabyss_bot_manager"


DATA_ROOT = _data_root()
CONFIG_DIR = DATA_ROOT / "config"
SECRETS_DIR = DATA_ROOT / "secrets"
RUNTIME_DIR = DATA_ROOT / "runtime"
LOGS_DIR = DATA_ROOT / "logs"
INSTANCES_DIR = DATA_ROOT / "instances"
BACKUPS_DIR = DATA_ROOT / "backups"

PHASE1_ADMIN_CONFIG_PATH = CONFIG_DIR / "admin.json"
PHASE1_ADMIN_TOKEN_PATH = SECRETS_DIR / "admin_bot_token.txt"
ADMIN_LOCK_PATH = RUNTIME_DIR / "admin_bot.lock"

LEGACY_SOURCE_ADMIN_CONFIG_PATH = PROGRAM_ROOT / "admin_config.json"
LEGACY_SOURCE_ADMIN_TOKEN_PATH = PROGRAM_ROOT / "admin_bot_token.txt"

ADMIN_CONFIG_PATH = PHASE1_ADMIN_CONFIG_PATH
ADMIN_TOKEN_PATH = PHASE1_ADMIN_TOKEN_PATH
LEGACY_ADMIN_CONFIG_PATH = LEGACY_SOURCE_ADMIN_CONFIG_PATH
LEGACY_ADMIN_TOKEN_PATH = LEGACY_SOURCE_ADMIN_TOKEN_PATH


def ensure_user_directories() -> None:
    for directory in (CONFIG_DIR, SECRETS_DIR, RUNTIME_DIR, LOGS_DIR, INSTANCES_DIR, BACKUPS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def ensure_user_data() -> None:
    ensure_user_directories()


def main() -> int:
    ensure_user_data()
    print("DarkAbyss Bot Manager user data initialized.")
    print(f"DATA_ROOT: {DATA_ROOT}")
    print(f"Instances root: {INSTANCES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
