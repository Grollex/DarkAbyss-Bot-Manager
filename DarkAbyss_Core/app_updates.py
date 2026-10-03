"""Self-update of an installed DarkAbyss Bot Manager from GitHub Releases.

Program code only. Everything the user owns lives in DATA_ROOT
(``%LOCALAPPDATA%\\DarkAbyssBotManager``: instances, tokens, AI connections and
keys, configs, logs) and is never touched; update_engine refuses an install root
that overlaps it.

Flow (installed builds only, i.e. started through Launcher.exe):

    check_for_update(current)     newest non-draft, non-prerelease GitHub release
                                  of UPDATE_OWNER/UPDATE_REPO that is newer (semver)
    install_update(update, root)  download the "darkabyss-release-<v>.zip" asset
                                  (SHA-256 checked), extract into updates/, verify
                                  release.json, stage as versions/<v> next to the
                                  running version, then switch current.json
    save_resume(running bots)     the new Manager starts those bots again
    restart via Launcher.exe      which starts versions/<current>

Old versions stay installed (rollback = pointer switch, see update_engine).
New bot types come with the program (bots/<type>/manifest.json); existing
instances keep their data, and config migrations run on first use.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import app_paths
import github_updates
import runtime_layout
import update_engine

UPDATE_OWNER = "Grollex"
UPDATE_REPO = "DarkAbyss-Bot-Manager"
RELEASES_URL = f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}/releases"
RESUME_FILE_NAME = "resume_after_update.json"
TRASH_DIR_NAME = "trash"
RESUME_MAX_AGE_SECONDS = 15 * 60
CHECK_INTERVAL_SECONDS = 6 * 3600
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


class AppUpdateError(RuntimeError):
    """The update could not be checked or installed (nothing was changed)."""


@dataclass(frozen=True)
class AvailableUpdate:
    version: str
    tag: str
    name: str | None = None

    @property
    def url(self) -> str:
        return f"{RELEASES_URL}/tag/{self.tag}"


@dataclass(frozen=True)
class InstalledApp:
    install_root: Path
    version: str
    launcher: Path


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------


def version_key(version: str) -> tuple:
    """Semantic-version order: 1.0.0 > 1.0.0-rc2 > 0.9.0-ai7-rc4; unknown text sorts lowest."""
    match = _VERSION_RE.fullmatch(str(version).strip().lstrip("v"))
    if match is None:
        return (-1,)
    major, minor, patch, pre = match.groups()
    numbers = (int(major), int(minor or 0), int(patch or 0))
    if not pre:
        return numbers + (1, ())
    identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"[.-]", pre))
    return numbers + (0, identifiers)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


# --------------------------------------------------------------------------
# the running installation
# --------------------------------------------------------------------------


def current_install() -> InstalledApp | None:
    """The installed (packaged) app this process runs from; None from source."""
    if not runtime_layout.is_frozen():
        return None
    try:
        version_dir = runtime_layout.current_version_dir()
    except runtime_layout.RuntimeLayoutError:
        return None
    install_root = version_dir.parent.parent
    if version_dir.parent.name != update_engine.VERSIONS_DIR_NAME:
        return None
    launcher = install_root / runtime_layout.launcher_executable_name()
    return InstalledApp(install_root=install_root, version=version_dir.name, launcher=launcher)


# --------------------------------------------------------------------------
# check / install
# --------------------------------------------------------------------------


def check_for_update(
    current_version: str,
    *,
    transport: github_updates.HttpTransport | None = None,
    timeout: float = github_updates.DEFAULT_TIMEOUT_SECONDS,
) -> AvailableUpdate | None:
    """Newest published release if it is newer than ``current_version``."""
    try:
        release = github_updates.fetch_latest_release(UPDATE_OWNER, UPDATE_REPO, timeout=timeout, transport=transport)
    except github_updates.ReleaseSelectionError:
        return None  # no published release yet
    except github_updates.GitHubUpdateError as exc:
        raise AppUpdateError(f"Update check failed: {exc}") from exc
    if not is_newer(release.version, current_version):
        return None
    return AvailableUpdate(release.version, release.tag_name, release.name)


def install_update(
    update: AvailableUpdate,
    install_root: Path | str,
    *,
    keep_versions: tuple[str, ...] = (),
    transport: github_updates.HttpTransport | None = None,
) -> update_engine.ActivationResult:
    """Download, verify, stage and activate ``update``. The running version and
    DATA_ROOT are not modified; on any failure the current version stays active.
    ``keep_versions`` (the running version) are never pruned."""
    root = Path(install_root)
    try:
        state = update_engine.get_activation_state(root)
        if state is not None and state.version == update.version:
            # Activated by an earlier attempt that did not restart: keep the
            # pointer (and its previous version) as it is.
            health = update_engine.check_install_health(root)
            if health.state != update_engine.HEALTHY:
                raise AppUpdateError(f"Update to {update.version} failed: installed version is {health.state}: {health.error}")
            result = update_engine.ActivationResult(
                version=state.version,
                previous_version=state.previous_version,
                current_path=root / update_engine.CURRENT_POINTER_NAME,
                version_dir=root / update_engine.VERSIONS_DIR_NAME / state.version,
            )
        else:
            if not (root / update_engine.VERSIONS_DIR_NAME / update.version).is_dir():
                github_updates.download_and_stage_release(
                    UPDATE_OWNER, UPDATE_REPO, root, tag=update.tag, transport=transport
                )
            # Already staged by an earlier attempt: activation re-verifies it.
            result = update_engine.activate_staged_release(update.version, root)
    except (github_updates.GitHubUpdateError, update_engine.UpdateEngineError, OSError) as exc:
        raise AppUpdateError(f"Update to {update.version} failed: {exc}") from exc
    try:
        clean_update_files(root)
        prune_old_versions(root, keep=keep_versions)
    except (update_engine.UpdateEngineError, OSError):
        pass  # housekeeping only; the new version is already active
    return result


def clean_update_files(install_root: Path | str) -> None:
    """Remove downloaded/extracted update archives (only below <install>/updates)."""
    updates = Path(install_root) / github_updates.UPDATES_DIR_NAME
    for name in (github_updates.DOWNLOADS_DIR_NAME, github_updates.PREPARED_DIR_NAME):
        folder = updates / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink(missing_ok=True)
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)


def prune_old_versions(install_root: Path | str, keep: tuple[str, ...] = ()) -> list[str]:
    """Delete installed program versions other than the current, the previous
    one (rollback) and ``keep`` (the running version). Program files only.

    A version is first renamed out of versions/ and only then deleted: Windows
    refuses the rename while a program or bot still runs from that folder, so
    a version in use is skipped instead of being left half deleted."""
    root = Path(install_root)
    state = update_engine.get_activation_state(root)
    if state is None:
        return []
    keep_set = {state.version, state.previous_version, *keep}
    trash = root / github_updates.UPDATES_DIR_NAME / TRASH_DIR_NAME
    if trash.parent.is_symlink() or trash.is_symlink():
        return []
    trash.mkdir(parents=True, exist_ok=True)
    for leftover in trash.iterdir():
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
    removed = []
    for version in update_engine.list_installed_versions(root):
        if version in keep_set:
            continue
        folder = root / update_engine.VERSIONS_DIR_NAME / version
        if folder.is_symlink() or not folder.is_dir():
            continue
        target = trash / f"{version}.{uuid.uuid4().hex}"
        try:
            folder.rename(target)
        except OSError:
            continue  # in use: try again after the next update
        shutil.rmtree(target, ignore_errors=True)
        removed.append(version)
    return removed


def start_launcher(installed: InstalledApp, popen: Callable[..., Any] = subprocess.Popen) -> Any:
    """Start the stable Launcher, which runs the version in current.json."""
    if not installed.launcher.is_file():
        raise AppUpdateError(f"Launcher not found: {installed.launcher}")
    return popen([str(installed.launcher)], cwd=str(installed.install_root), stdin=subprocess.DEVNULL, shell=False)


# --------------------------------------------------------------------------
# bots running before the update are started again by the new Manager
# --------------------------------------------------------------------------


def resume_path() -> Path:
    return app_paths.CONFIG_DIR / RESUME_FILE_NAME


def save_resume(instance_ids: list[str], version: str) -> None:
    path = resume_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"instances": sorted(set(instance_ids)), "version": version, "at": time.time()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def take_resume(now: float | None = None) -> list[str]:
    """Bots to start again after an update (read once, then removed; stale = ignored)."""
    path = resume_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    try:
        path.unlink()
    except OSError:
        pass
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not isinstance(payload.get("at"), (int, float)):
        return []
    if now - float(payload["at"]) > RESUME_MAX_AGE_SECONDS:
        return []
    instances = payload.get("instances")
    return [item for item in instances if isinstance(item, str)] if isinstance(instances, list) else []
