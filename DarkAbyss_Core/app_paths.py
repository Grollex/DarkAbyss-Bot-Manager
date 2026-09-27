import os
import shutil
from pathlib import Path

TOKEN_PLACEHOLDER = "PUT_DISCORD_BOT_TOKEN_HERE"

PROGRAM_ROOT = Path(__file__).resolve().parent
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

ADMIN_CONFIG_PATH = CONFIG_DIR / "admin.json"
ADMIN_TOKEN_PATH = SECRETS_DIR / "admin_bot_token.txt"
ADMIN_LOCK_PATH = RUNTIME_DIR / "admin_bot.lock"

LEGACY_ADMIN_CONFIG_PATH = PROGRAM_ROOT / "admin_config.json"
LEGACY_ADMIN_TOKEN_PATH = PROGRAM_ROOT / "admin_bot_token.txt"


def ensure_user_directories() -> None:
    for directory in (CONFIG_DIR, SECRETS_DIR, RUNTIME_DIR, LOGS_DIR, INSTANCES_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _copy_if_destination_missing(source: Path, destination: Path, label: str) -> bool:
    if destination.exists() or not source.exists():
        return False

    shutil.copyfile(source, destination)
    print(f"Imported legacy {label} from {source} to {destination}")
    return True


def _legacy_token_is_importable(path: Path) -> bool:
    if not path.exists():
        return False
    token = path.read_text(encoding="utf-8").strip()
    return bool(token and token != TOKEN_PLACEHOLDER)


def ensure_admin_config() -> None:
    if _copy_if_destination_missing(LEGACY_ADMIN_CONFIG_PATH, ADMIN_CONFIG_PATH, "admin config"):
        return

    if not ADMIN_CONFIG_PATH.exists():
        shutil.copyfile(DEFAULT_ADMIN_CONFIG_PATH, ADMIN_CONFIG_PATH)


def ensure_admin_token() -> None:
    if ADMIN_TOKEN_PATH.exists():
        return

    if _legacy_token_is_importable(LEGACY_ADMIN_TOKEN_PATH):
        shutil.copyfile(LEGACY_ADMIN_TOKEN_PATH, ADMIN_TOKEN_PATH)
        print(f"Imported legacy admin bot token from {LEGACY_ADMIN_TOKEN_PATH} to {ADMIN_TOKEN_PATH}")
        return

    ADMIN_TOKEN_PATH.write_text(TOKEN_PLACEHOLDER + "\n", encoding="utf-8")


def ensure_user_data() -> None:
    ensure_user_directories()
    ensure_admin_config()
    ensure_admin_token()


def main() -> int:
    ensure_user_data()
    print("DarkAbyss Bot Manager user data initialized.")
    print(f"DATA_ROOT: {DATA_ROOT}")
    print(f"Admin config: {ADMIN_CONFIG_PATH}")
    print(f"Admin token: {ADMIN_TOKEN_PATH}")
    print(f"Put your Discord bot token into: {ADMIN_TOKEN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
