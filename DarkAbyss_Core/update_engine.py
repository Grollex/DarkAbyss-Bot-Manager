from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import app_paths


RELEASE_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 1
RELEASE_MANIFEST_NAME = "release.json"
CURRENT_POINTER_NAME = "current.json"
VERSIONS_DIR_NAME = "versions"
UPDATES_DIR_NAME = "updates"
STAGING_DIR_NAME = "staging"
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
RESERVED_USER_ROOTS = {
    "backups",
    "config",
    "configs",
    "data",
    "database",
    "databases",
    "downloads",
    "instances",
    "logs",
    "runtime",
    "secrets",
    "tokens",
    "updates",
    "user_data",
}


class UpdateEngineError(RuntimeError):
    pass


class ReleaseManifestError(UpdateEngineError):
    pass


class ReleaseVerificationError(UpdateEngineError):
    pass


class ReleaseStageError(UpdateEngineError):
    pass


class ActivationError(UpdateEngineError):
    pass


class CurrentPointerError(UpdateEngineError):
    pass


class RollbackError(UpdateEngineError):
    pass


class RecoveryError(UpdateEngineError):
    pass


HEALTHY = "HEALTHY"
NO_CURRENT_POINTER = "NO_CURRENT_POINTER"
INVALID_CURRENT_POINTER = "INVALID_CURRENT_POINTER"
CURRENT_VERSION_MISSING = "CURRENT_VERSION_MISSING"
CURRENT_VERSION_CORRUPT = "CURRENT_VERSION_CORRUPT"


@dataclass(frozen=True)
class ReleaseFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseInfo:
    release_root: Path
    version: str
    files: tuple[ReleaseFile, ...]
    manifest_path: Path


@dataclass(frozen=True)
class StageResult:
    version: str
    version_dir: Path


@dataclass(frozen=True)
class ActivationState:
    version: str
    previous_version: str | None


@dataclass(frozen=True)
class ActivationResult:
    version: str
    previous_version: str | None
    current_path: Path
    version_dir: Path


@dataclass(frozen=True)
class RollbackResult:
    version: str
    previous_version: str | None
    current_path: Path
    version_dir: Path
    changed: bool


@dataclass(frozen=True)
class RecoveryResult:
    version: str
    previous_version: str | None
    current_path: Path
    version_dir: Path


@dataclass(frozen=True)
class InstallHealth:
    state: str
    version: str | None
    previous_version: str | None
    error: str | None = None


def inspect_release(release_root: Path | str) -> ReleaseInfo:
    root = Path(release_root).resolve()
    manifest_path = root / RELEASE_MANIFEST_NAME
    manifest = _load_manifest(manifest_path)
    version = _validate_version(manifest.get("version"))
    files = _validate_manifest_files(manifest.get("files"), root)
    release = ReleaseInfo(
        release_root=root,
        version=version,
        files=tuple(files),
        manifest_path=manifest_path,
    )
    _verify_release_files(release, root)
    return release


def stage_release(release_root: Path | str, install_root: Path | str) -> StageResult:
    release = inspect_release(release_root)
    root = _prepare_install_root(install_root)
    versions_dir = _ensure_structural_directory(root, VERSIONS_DIR_NAME, create=True)
    target_dir = versions_dir / release.version
    _ensure_target_inside(root, target_dir, "version directory")
    if target_dir.exists():
        raise ReleaseStageError(f"Installed version already exists: {release.version}")

    staging_parent = _ensure_structural_directory(root, UPDATES_DIR_NAME, STAGING_DIR_NAME, create=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f"{release.version}.",
            suffix=f".{uuid.uuid4().hex}.tmp",
            dir=staging_parent,
        )
    )
    _ensure_target_inside(root, staging_root, "staging directory")

    try:
        _copy_release_to_staging(release, staging_root)
        _verify_release_files(release, staging_root)
        _ensure_structural_directory(root, VERSIONS_DIR_NAME, create=True)
        if target_dir.exists():
            raise ReleaseStageError(f"Installed version already exists: {release.version}")
        staging_root.rename(target_dir)
    except Exception as exc:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        if isinstance(exc, UpdateEngineError):
            raise
        raise ReleaseStageError(f"Failed to stage release {release.version!r}: {exc}") from exc

    return StageResult(version=release.version, version_dir=target_dir)


def activate_staged_release(version: str, install_root: Path | str) -> ActivationResult:
    valid_version = _validate_version(version)
    root = _prepare_install_root(install_root)
    version_dir = _verify_installed_version(root, valid_version, ActivationError)
    previous_version = get_current_version(root)
    current_path = root / CURRENT_POINTER_NAME
    try:
        _atomic_write_current_pointer(
            current_path,
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "version": valid_version,
                "previous_version": previous_version,
            },
        )
    except Exception as exc:
        raise ActivationError(f"Failed to activate version {valid_version!r}: {exc}") from exc

    return ActivationResult(
        version=valid_version,
        previous_version=previous_version,
        current_path=current_path,
        version_dir=version_dir,
    )


def get_current_version(install_root: Path | str) -> str | None:
    root = _prepare_install_root(install_root, create=False)
    state = _read_activation_state(root)
    if state is None:
        return None
    _ensure_installed_version_directory(root, state.version, CurrentPointerError)
    return state.version


def get_activation_state(install_root: Path | str) -> ActivationState | None:
    root = _prepare_install_root(install_root, create=False)
    return _read_activation_state(root)


def rollback_to_version(target_version: str, install_root: Path | str) -> RollbackResult:
    valid_version = _validate_version(target_version, error_type=RollbackError)
    root = _prepare_install_root(install_root, create=False)
    state = _read_activation_state(root)
    if state is None:
        raise RollbackError("Cannot rollback without a valid current pointer.")
    version_dir = _verify_installed_version(root, valid_version, RollbackError)
    current_path = root / CURRENT_POINTER_NAME
    if state.version == valid_version:
        return RollbackResult(
            version=state.version,
            previous_version=state.previous_version,
            current_path=current_path,
            version_dir=version_dir,
            changed=False,
        )

    try:
        _atomic_write_current_pointer(
            current_path,
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "version": valid_version,
                "previous_version": state.version,
            },
        )
    except Exception as exc:
        raise RollbackError(f"Failed to rollback to version {valid_version!r}: {exc}") from exc

    return RollbackResult(
        version=valid_version,
        previous_version=state.version,
        current_path=current_path,
        version_dir=version_dir,
        changed=True,
    )


def rollback_to_previous(install_root: Path | str) -> RollbackResult:
    root = _prepare_install_root(install_root, create=False)
    state = _read_activation_state(root)
    if state is None:
        raise RollbackError("Cannot rollback to previous without a current pointer.")
    if state.previous_version is None:
        raise RollbackError("No previous_version is recorded in current.json.")
    return rollback_to_version(state.previous_version, root)


def recover_current_pointer(target_version: str, install_root: Path | str) -> RecoveryResult:
    valid_version = _validate_version(target_version, error_type=RecoveryError)
    root = _prepare_install_root(install_root)
    health = check_install_health(root)
    if health.state == HEALTHY:
        raise RecoveryError("Current pointer is already healthy; recovery is unnecessary.")
    version_dir = _verify_installed_version(root, valid_version, RecoveryError)
    current_path = root / CURRENT_POINTER_NAME
    try:
        _atomic_write_current_pointer(
            current_path,
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "version": valid_version,
                "previous_version": None,
            },
        )
    except Exception as exc:
        raise RecoveryError(f"Failed to recover current pointer to version {valid_version!r}: {exc}") from exc
    return RecoveryResult(
        version=valid_version,
        previous_version=None,
        current_path=current_path,
        version_dir=version_dir,
    )


def check_install_health(install_root: Path | str) -> InstallHealth:
    root = _prepare_install_root(install_root, create=False)
    try:
        state = _read_activation_state(root)
    except CurrentPointerError as exc:
        return InstallHealth(
            state=INVALID_CURRENT_POINTER,
            version=None,
            previous_version=None,
            error=str(exc),
        )
    if state is None:
        return InstallHealth(
            state=NO_CURRENT_POINTER,
            version=None,
            previous_version=None,
        )
    try:
        _verify_installed_version(root, state.version, CurrentPointerError)
    except CurrentPointerError as exc:
        message = str(exc)
        health_state = (
            CURRENT_VERSION_MISSING
            if "does not exist" in message or "Required update structural directory is missing" in message
            else CURRENT_VERSION_CORRUPT
        )
        return InstallHealth(
            state=health_state,
            version=state.version,
            previous_version=state.previous_version,
            error=message,
        )
    return InstallHealth(
        state=HEALTHY,
        version=state.version,
        previous_version=state.previous_version,
    )


def _read_activation_state(root: Path) -> ActivationState | None:
    current_path = root / CURRENT_POINTER_NAME
    if current_path.is_symlink():
        raise CurrentPointerError(f"Current pointer is not a regular file: {current_path}")
    if not current_path.exists():
        return None
    if not current_path.is_file():
        raise CurrentPointerError(f"Current pointer is not a regular file: {current_path}")
    try:
        payload = json.loads(current_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CurrentPointerError(f"Malformed current.json at {current_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CurrentPointerError(f"Invalid current.json encoding at {current_path}: {exc}") from exc
    except OSError as exc:
        raise CurrentPointerError(f"Unable to read current.json at {current_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CurrentPointerError(f"Invalid current.json at {current_path}: root value must be an object.")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != CURRENT_SCHEMA_VERSION:
        raise CurrentPointerError(f"Unsupported current.json schema_version {schema_version!r}.")
    version = _validate_version(payload.get("version"), error_type=CurrentPointerError)
    previous_value = payload.get("previous_version")
    if previous_value is None:
        previous_version = None
    elif isinstance(previous_value, str):
        previous_version = _validate_version(previous_value, error_type=CurrentPointerError)
    else:
        raise CurrentPointerError(f"Invalid current.json previous_version {previous_value!r}.")
    return ActivationState(version=version, previous_version=previous_version)


def list_installed_versions(install_root: Path | str) -> list[str]:
    root = _prepare_install_root(install_root, create=False)
    versions_dir = root / VERSIONS_DIR_NAME
    if not versions_dir.exists():
        return []
    versions_dir = _ensure_structural_directory(root, VERSIONS_DIR_NAME, create=False)
    versions = []
    for child in versions_dir.iterdir():
        if child.is_dir():
            versions.append(_validate_version(child.name))
    return sorted(versions)


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise ReleaseManifestError(f"Missing release manifest: {manifest_path}")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseManifestError(f"Release manifest is not a regular file: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReleaseManifestError(f"Malformed release manifest JSON at {manifest_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ReleaseManifestError(f"Invalid release manifest encoding at {manifest_path}: {exc}") from exc
    except OSError as exc:
        raise ReleaseManifestError(f"Unable to read release manifest at {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseManifestError(f"Release manifest root must be an object: {manifest_path}")
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != RELEASE_SCHEMA_VERSION:
        raise ReleaseManifestError(f"Unsupported release manifest schema_version {schema_version!r}.")
    return manifest


def _validate_manifest_files(files: object, release_root: Path) -> list[ReleaseFile]:
    if not isinstance(files, list) or not files:
        raise ReleaseManifestError("Release manifest files must be a non-empty array.")
    seen_paths = set()
    validated = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ReleaseManifestError(f"Release manifest file entry {index} must be an object.")
        path = _normalize_release_path(entry.get("path"))
        path_key = path.lower()
        if path_key in seen_paths:
            raise ReleaseManifestError(f"Duplicate release file path: {path}")
        seen_paths.add(path_key)

        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            raise ReleaseManifestError(f"Invalid sha256 for release file {path!r}.")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseManifestError(f"Invalid size for release file {path!r}.")

        source_path = release_root / path
        _ensure_release_source_path_safe(release_root, source_path, path)
        validated.append(ReleaseFile(path=path, sha256=sha256.lower(), size=size))
    return validated


def _validate_version(value: object, error_type: type[UpdateEngineError] = ReleaseManifestError) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None or value in {".", ".."}:
        raise error_type(f"Invalid version: {value!r}")
    if "/" in value or "\\" in value or PureWindowsPath(value).drive:
        raise error_type(f"Invalid version path component: {value!r}")
    return value


def _normalize_release_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseManifestError(f"Release file path must be a non-empty string: {value!r}")
    if "\\" in value:
        raise ReleaseManifestError(f"Release file path must use normalized '/' separators: {value!r}")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.is_absolute() or posix_path.is_absolute() or windows_path.drive:
        raise ReleaseManifestError(f"Release file path must be relative: {value!r}")
    parts = posix_path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseManifestError(f"Release file path must not contain traversal: {value!r}")
    normalized = "/".join(parts)
    if not normalized or normalized == RELEASE_MANIFEST_NAME:
        raise ReleaseManifestError(f"Release file path is reserved: {value!r}")
    root_name = parts[0].lower()
    if root_name in RESERVED_USER_ROOTS:
        raise ReleaseManifestError(f"Release file path targets reserved user-owned root: {value!r}")
    return normalized


def _ensure_release_source_path_safe(release_root: Path, source_path: Path, relative_path: str) -> None:
    root = release_root.resolve()
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ReleaseVerificationError(f"Release payload path must not be a symlink: {relative_path}")
    try:
        source_path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ReleaseVerificationError(f"Release payload path escapes release root: {relative_path}") from exc


def _verify_release_files(release: ReleaseInfo, payload_root: Path) -> None:
    root = payload_root.resolve()
    for release_file in release.files:
        source_path = root / release_file.path
        _ensure_release_source_path_safe(root, source_path, release_file.path)
        if not source_path.exists():
            raise ReleaseVerificationError(f"Missing release file: {release_file.path}")
        if not source_path.is_file():
            raise ReleaseVerificationError(f"Release file is not a regular file: {release_file.path}")
        size = source_path.stat().st_size
        if size != release_file.size:
            raise ReleaseVerificationError(
                f"Release file size mismatch for {release_file.path}: expected {release_file.size}, got {size}"
            )
        sha256 = _sha256_file(source_path)
        if sha256 != release_file.sha256:
            raise ReleaseVerificationError(
                f"Release file sha256 mismatch for {release_file.path}: expected {release_file.sha256}, got {sha256}"
            )


def _copy_release_to_staging(release: ReleaseInfo, staging_root: Path) -> None:
    _ensure_target_inside(staging_root, staging_root / RELEASE_MANIFEST_NAME, "staged manifest")
    shutil.copyfile(release.manifest_path, staging_root / RELEASE_MANIFEST_NAME)
    for release_file in release.files:
        source_path = release.release_root / release_file.path
        destination_path = staging_root / release_file.path
        _ensure_target_inside(staging_root, destination_path, "staged release file")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)


def _ensure_installed_version_directory(
    root: Path,
    version: str,
    error_type: type[UpdateEngineError],
) -> Path:
    try:
        versions_dir = _ensure_structural_directory(root, VERSIONS_DIR_NAME, create=False)
        version_dir = versions_dir / version
        if version_dir.is_symlink():
            raise error_type(f"Installed version directory must not be a symlink: {version_dir}")
        _ensure_target_inside(root, version_dir, "version directory")
        if not version_dir.is_dir():
            raise error_type(f"Installed version does not exist: {version}")
        return version_dir
    except UpdateEngineError as exc:
        if isinstance(exc, error_type):
            raise
        raise error_type(f"Failed to resolve installed version {version!r}: {exc}") from exc


def _verify_installed_version(
    root: Path,
    version: str,
    error_type: type[UpdateEngineError],
) -> Path:
    version_dir = _ensure_installed_version_directory(root, version, error_type)
    try:
        release = inspect_release(version_dir)
    except UpdateEngineError as exc:
        raise error_type(f"Installed version {version!r} failed verification: {exc}") from exc
    if release.version != version:
        raise error_type(
            f"Installed release metadata mismatch: requested {version!r}, found {release.version!r}."
        )
    return version_dir


def _prepare_install_root(install_root: Path | str, create: bool = True) -> Path:
    root = Path(install_root).resolve()
    _ensure_install_root_disjoint_from_data_root(root)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_install_root_disjoint_from_data_root(install_root: Path) -> None:
    data_root = app_paths.DATA_ROOT.resolve()
    try:
        install_root.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise UpdateEngineError(f"Install root must be disjoint from DATA_ROOT: {install_root}")
    try:
        data_root.relative_to(install_root)
    except ValueError:
        pass
    else:
        raise UpdateEngineError(f"DATA_ROOT must not be inside install root: {data_root}")


def _ensure_structural_directory(root: Path, *relative_parts: str, create: bool) -> Path:
    current = root
    for part in relative_parts:
        current = current / part
        # Symlink first: resolving it would only report "escapes root".
        if current.is_symlink():
            raise UpdateEngineError(f"Update structural directory must not be a symlink: {current}")
        _ensure_target_inside(root, current, "update structural directory")
        if current.exists():
            if not current.is_dir():
                raise UpdateEngineError(f"Update structural path must be a directory: {current}")
            _ensure_target_inside(root, current.resolve(), "update structural directory")
            continue
        if not create:
            raise UpdateEngineError(f"Required update structural directory is missing: {current}")
        current.mkdir()
        if current.is_symlink() or not current.is_dir():
            raise UpdateEngineError(f"Update structural directory was not created safely: {current}")
        _ensure_target_inside(root, current.resolve(), "update structural directory")
    return current


def _ensure_target_inside(root: Path, target: Path, label: str) -> None:
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise UpdateEngineError(f"{label} escapes root {root}: {target}") from exc


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
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


def _atomic_write_current_pointer(path: Path, payload: dict) -> None:
    if path.is_symlink():
        raise CurrentPointerError(f"Current pointer is not a regular file: {path}")
    _atomic_write_json(path, payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
