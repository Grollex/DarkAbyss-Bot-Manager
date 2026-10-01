from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

import github_updates
import launcher
import runtime_layout
import update_engine


UPDATE_ASSET_PREFIX = "darkabyss-release"
FRESH_INSTALL_PREFIX = "DarkAbyssBotManager"
FRESH_INSTALL_ROOT_NAME = "DarkAbyssBotManager"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FORBIDDEN_ARTIFACT_ROOTS = update_engine.RESERVED_USER_ROOTS | {
    ".git",
    "__pycache__",
    "build",
    "dist",
}


class ReleaseArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseArtifacts:
    update_zip: Path
    update_sha256: Path
    fresh_install_zip: Path
    fresh_install_sha256: Path


def version_from_tag(tag: str) -> str:
    if not isinstance(tag, str) or not tag.startswith("v") or len(tag) <= 1:
        raise ReleaseArtifactError(f"Release tag must use v<version> format: {tag!r}")
    return update_engine._validate_version(tag[1:], error_type=ReleaseArtifactError)


def update_asset_name(version: str) -> str:
    valid_version = update_engine._validate_version(version, error_type=ReleaseArtifactError)
    return f"{UPDATE_ASSET_PREFIX}-{valid_version}.zip"


def fresh_install_asset_name(version: str) -> str:
    valid_version = update_engine._validate_version(version, error_type=ReleaseArtifactError)
    return f"{FRESH_INSTALL_PREFIX}-{valid_version}-windows.zip"


def build_release_artifacts(
    *,
    version: str,
    distribution: Path | str,
    output: Path | str,
    tag: str | None = None,
) -> ReleaseArtifacts:
    valid_version = _validate_version_inputs(version, tag)
    distribution_root = Path(distribution)
    output_root = Path(output)
    _ensure_distribution_output_disjoint(distribution_root, output_root)
    version_dir = _validate_distribution(distribution_root, valid_version)

    _prepare_output_root(output_root)
    update_zip = output_root / update_asset_name(valid_version)
    fresh_zip = output_root / fresh_install_asset_name(valid_version)

    _write_update_zip(version_dir, update_zip, valid_version)
    _verify_update_zip_size(update_zip)
    _write_fresh_install_zip(distribution_root, fresh_zip, valid_version)
    _write_sha256_file(update_zip)
    _write_sha256_file(fresh_zip)

    _verify_update_zip(update_zip, valid_version)
    _verify_fresh_install_zip(fresh_zip, valid_version)

    return ReleaseArtifacts(
        update_zip=update_zip.resolve(),
        update_sha256=(update_zip.with_name(update_zip.name + ".sha256")).resolve(),
        fresh_install_zip=fresh_zip.resolve(),
        fresh_install_sha256=(fresh_zip.with_name(fresh_zip.name + ".sha256")).resolve(),
    )


def verify_release_artifacts(
    *,
    version: str,
    artifacts_dir: Path | str,
) -> ReleaseArtifacts:
    valid_version = update_engine._validate_version(version, error_type=ReleaseArtifactError)
    root = Path(artifacts_dir).resolve()
    update_zip = root / update_asset_name(valid_version)
    fresh_zip = root / fresh_install_asset_name(valid_version)
    for path in (update_zip, fresh_zip, update_zip.with_name(update_zip.name + ".sha256"), fresh_zip.with_name(fresh_zip.name + ".sha256")):
        if path.is_symlink() or not path.is_file():
            raise ReleaseArtifactError(f"Expected artifact is missing or unsafe: {path}")
    _verify_sha256_file(update_zip)
    _verify_sha256_file(fresh_zip)
    _verify_update_zip(update_zip, valid_version)
    _verify_fresh_install_zip(fresh_zip, valid_version)
    return ReleaseArtifacts(
        update_zip=update_zip,
        update_sha256=update_zip.with_name(update_zip.name + ".sha256"),
        fresh_install_zip=fresh_zip,
        fresh_install_sha256=fresh_zip.with_name(fresh_zip.name + ".sha256"),
    )


def _validate_version_inputs(version: str, tag: str | None) -> str:
    valid_version = update_engine._validate_version(version, error_type=ReleaseArtifactError)
    if tag is not None:
        tag_version = version_from_tag(tag)
        if tag_version != valid_version:
            raise ReleaseArtifactError(f"Tag version {tag_version!r} does not match requested version {valid_version!r}.")
    return valid_version


def _validate_distribution(distribution: Path | str, version: str) -> Path:
    root = Path(distribution)
    if root.is_symlink():
        raise ReleaseArtifactError(f"Distribution root must not be a symlink: {root}")
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise ReleaseArtifactError(f"Distribution root does not exist: {resolved_root}")
    _reject_symlinks(resolved_root)

    launcher_path = resolved_root / runtime_layout.launcher_executable_name()
    current_path = resolved_root / update_engine.CURRENT_POINTER_NAME
    version_dir = resolved_root / update_engine.VERSIONS_DIR_NAME / version
    for required_file in (launcher_path, current_path):
        if required_file.is_symlink() or not required_file.is_file():
            raise ReleaseArtifactError(f"Distribution required file is missing or unsafe: {required_file}")
    if version_dir.is_symlink() or not version_dir.is_dir():
        raise ReleaseArtifactError(f"Distribution version directory is missing or unsafe: {version_dir}")

    target = launcher.resolve_current_app(resolved_root)
    if target.version != version:
        raise ReleaseArtifactError(f"current.json selects {target.version!r}, expected {version!r}.")
    expected_app = (version_dir / runtime_layout.app_executable_name()).resolve()
    if target.app_executable != expected_app:
        raise ReleaseArtifactError("Launcher resolution does not select the expected versioned app.")

    release = update_engine.inspect_release(version_dir)
    if release.version != version:
        raise ReleaseArtifactError(f"release.json version {release.version!r} does not match {version!r}.")
    _validate_exact_distribution_files(resolved_root, version, release)
    return version_dir.resolve()


def _prepare_output_root(output_root: Path) -> None:
    if output_root.is_symlink():
        raise ReleaseArtifactError(f"Artifact output root must not be a symlink: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    for artifact in output_root.iterdir():
        if artifact.is_symlink():
            raise ReleaseArtifactError(f"Refusing to remove symlinked existing artifact: {artifact}")
        if artifact.is_dir():
            shutil.rmtree(artifact)
        else:
            artifact.unlink()


def _ensure_distribution_output_disjoint(distribution_root: Path, output_root: Path) -> None:
    if output_root.is_symlink():
        raise ReleaseArtifactError(f"Artifact output root must not be a symlink: {output_root}")
    resolved_distribution = distribution_root.resolve()
    resolved_output = output_root.resolve(strict=False)
    if resolved_distribution == resolved_output:
        raise ReleaseArtifactError("Artifact output root must be separate from the distribution root.")
    try:
        resolved_output.relative_to(resolved_distribution)
    except ValueError:
        pass
    else:
        raise ReleaseArtifactError("Artifact output root must not be inside the distribution root.")
    try:
        resolved_distribution.relative_to(resolved_output)
    except ValueError:
        pass
    else:
        raise ReleaseArtifactError("Artifact output root must not contain the distribution root.")


def _write_update_zip(version_dir: Path, destination: Path, version: str) -> None:
    release = update_engine.inspect_release(version_dir)
    if release.version != version:
        raise ReleaseArtifactError(f"release.json version {release.version!r} does not match {version!r}.")
    entries = [(release.manifest_path, update_engine.RELEASE_MANIFEST_NAME)]
    for item in release.files:
        relative = item.path
        _validate_update_archive_path(relative)
        entries.append((version_dir / PurePosixPath(relative), relative))
    _write_zip(destination, entries)
    _assert_zip_members(destination, update=True, version=version)


def _write_fresh_install_zip(distribution_root: Path | str, destination: Path, version: str) -> None:
    root = Path(distribution_root).resolve()
    version_dir = _validate_distribution(root, version)
    release = update_engine.inspect_release(version_dir)
    stable_entries = [
        (root / runtime_layout.launcher_executable_name(), runtime_layout.launcher_executable_name()),
        (root / update_engine.CURRENT_POINTER_NAME, update_engine.CURRENT_POINTER_NAME),
        (release.manifest_path, f"{update_engine.VERSIONS_DIR_NAME}/{version}/{update_engine.RELEASE_MANIFEST_NAME}"),
    ]
    entries = []
    for path, relative in stable_entries:
        _validate_fresh_archive_path(relative)
        entries.append((path, f"{FRESH_INSTALL_ROOT_NAME}/{relative}"))
    for item in release.files:
        relative = f"{update_engine.VERSIONS_DIR_NAME}/{version}/{item.path}"
        _validate_fresh_archive_path(relative)
        entries.append((version_dir / PurePosixPath(item.path), f"{FRESH_INSTALL_ROOT_NAME}/{relative}"))
    _write_zip(destination, entries)
    _assert_zip_members(destination, update=False, version=version)


def _reject_symlinks(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise ReleaseArtifactError(f"Release artifact input must not contain symlinks: {path}")


def _validate_exact_distribution_files(root: Path, version: str, release: update_engine.ReleaseInfo) -> None:
    expected_root_entries = {
        runtime_layout.launcher_executable_name(),
        update_engine.CURRENT_POINTER_NAME,
        update_engine.VERSIONS_DIR_NAME,
    }
    actual_root_entries = {path.name for path in root.iterdir()}
    missing_root_entries = sorted(expected_root_entries - actual_root_entries)
    unexpected_root_entries = sorted(actual_root_entries - expected_root_entries)
    if missing_root_entries:
        raise ReleaseArtifactError(f"Distribution is missing expected root entries: {', '.join(missing_root_entries)}")
    if unexpected_root_entries:
        raise ReleaseArtifactError(f"Distribution contains unexpected root entries: {', '.join(unexpected_root_entries)}")

    versions_dir = root / update_engine.VERSIONS_DIR_NAME
    if versions_dir.is_symlink() or not versions_dir.is_dir():
        raise ReleaseArtifactError(f"Distribution versions path is missing or unsafe: {versions_dir}")
    version_entries = list(versions_dir.iterdir())
    if len(version_entries) != 1 or version_entries[0].name != version or not version_entries[0].is_dir():
        names = ", ".join(sorted(path.name for path in version_entries))
        raise ReleaseArtifactError(
            f"Distribution versions directory must contain exactly {version!r}; found: {names or '<empty>'}"
        )

    expected = {
        runtime_layout.launcher_executable_name(),
        update_engine.CURRENT_POINTER_NAME,
        f"{update_engine.VERSIONS_DIR_NAME}/{version}/{update_engine.RELEASE_MANIFEST_NAME}",
    }
    for item in release.files:
        _validate_update_archive_path(item.path)
        expected.add(f"{update_engine.VERSIONS_DIR_NAME}/{version}/{item.path}")

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if missing:
        raise ReleaseArtifactError(f"Distribution is missing expected files: {', '.join(missing)}")
    if unexpected:
        raise ReleaseArtifactError(f"Distribution contains unexpected files: {', '.join(unexpected)}")


def _validate_update_archive_path(relative: str) -> None:
    _validate_common_archive_path(relative)
    first = PurePosixPath(relative).parts[0]
    if first in FORBIDDEN_ARTIFACT_ROOTS:
        raise ReleaseArtifactError(f"Update artifact must not contain user/build root: {relative}")
    if first == runtime_layout.launcher_executable_name():
        raise ReleaseArtifactError("Update artifact must not contain the stable launcher.")
    if first == update_engine.CURRENT_POINTER_NAME:
        raise ReleaseArtifactError("Update artifact must not contain current.json.")
    if first == update_engine.VERSIONS_DIR_NAME:
        raise ReleaseArtifactError("Update artifact must not contain a versions/ wrapper.")


def _validate_fresh_archive_path(relative: str) -> None:
    _validate_common_archive_path(relative)
    first = PurePosixPath(relative).parts[0]
    if first in FORBIDDEN_ARTIFACT_ROOTS:
        raise ReleaseArtifactError(f"Fresh install artifact must not contain user/build root: {relative}")


def _validate_common_archive_path(relative: str) -> None:
    if "\\" in relative:
        raise ReleaseArtifactError(f"Archive path must use forward slashes: {relative!r}")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseArtifactError(f"Archive path must be normalized and relative: {relative!r}")
    lowered_parts = {part.lower() for part in path.parts}
    if ".git" in lowered_parts:
        raise ReleaseArtifactError(f"Release artifact must not contain .git content: {relative}")
    if path.name == "admin_bot_token.txt":
        raise ReleaseArtifactError(f"Release artifact must not contain source-adjacent runtime file: {relative}")
    if path.name == "admin_config.json" and "/DarkAbyss_Core/admin_config.json" in f"/{path.as_posix()}":
        raise ReleaseArtifactError(f"Release artifact must not contain source-adjacent runtime file: {relative}")
    lowered_name = path.name.lower()
    if lowered_name in {"config.json", "token.txt"} or lowered_name.endswith((".sqlite", ".db", ".log")):
        raise ReleaseArtifactError(f"Release artifact must not contain mutable user/runtime file: {relative}")


def _write_zip(destination: Path, entries: list[tuple[Path, str]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise ReleaseArtifactError(f"Destination ZIP must not be a symlink: {destination}")
        destination.unlink()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, archive_name in sorted(entries, key=lambda item: item[1]):
            if source.is_symlink() or not source.is_file():
                raise ReleaseArtifactError(f"Release artifact source is missing or unsafe: {source}")
            info = zipfile.ZipInfo(archive_name, FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())


def _assert_zip_members(zip_path: Path, *, update: bool, version: str) -> list[str]:
    infos = _validated_zip_infos(zip_path)
    members = [info.filename for info in infos]
    if members != sorted(members):
        raise ReleaseArtifactError(f"ZIP members must be sorted deterministically: {zip_path}")
    for member in members:
        if "\\" in member:
            raise ReleaseArtifactError(f"ZIP member must use forward slashes: {member!r}")
        parts = PurePosixPath(member).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ReleaseArtifactError(f"ZIP member must be normalized: {member!r}")
    if update:
        if update_engine.RELEASE_MANIFEST_NAME not in members:
            raise ReleaseArtifactError("Update ZIP must contain release.json at archive root.")
        if any(member.startswith(f"{FRESH_INSTALL_ROOT_NAME}/") for member in members):
            raise ReleaseArtifactError("Update ZIP must not contain a fresh-install parent folder.")
        forbidden = {
            runtime_layout.launcher_executable_name(),
            update_engine.CURRENT_POINTER_NAME,
        }
        if forbidden & set(members) or any(member.startswith(f"{update_engine.VERSIONS_DIR_NAME}/") for member in members):
            raise ReleaseArtifactError("Update ZIP contains stable install files instead of a version payload.")
    else:
        prefix = f"{FRESH_INSTALL_ROOT_NAME}/"
        required = {
            f"{prefix}{runtime_layout.launcher_executable_name()}",
            f"{prefix}{update_engine.CURRENT_POINTER_NAME}",
            f"{prefix}{update_engine.VERSIONS_DIR_NAME}/{version}/{runtime_layout.app_executable_name()}",
            f"{prefix}{update_engine.VERSIONS_DIR_NAME}/{version}/{update_engine.RELEASE_MANIFEST_NAME}",
        }
        missing = sorted(required - set(members))
        if missing:
            raise ReleaseArtifactError(f"Fresh install ZIP is missing required files: {', '.join(missing)}")
        if any(not member.startswith(prefix) for member in members):
            raise ReleaseArtifactError("Fresh install ZIP entries must live under DarkAbyssBotManager/.")
    return members


def _validated_zip_infos(zip_path: Path) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ReleaseArtifactError(f"Malformed ZIP artifact: {zip_path}") from exc
    seen = set()
    for info in infos:
        name = info.filename
        if not isinstance(name, str) or not name:
            raise ReleaseArtifactError("ZIP member path must be non-empty.")
        if "\\" in name:
            raise ReleaseArtifactError(f"ZIP member must use forward slashes: {name!r}")
        posix_path = PurePosixPath(name)
        windows_path = PureWindowsPath(name)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ReleaseArtifactError(f"ZIP member path must be relative: {name!r}")
        if any(part in {"", ".", ".."} for part in posix_path.parts):
            raise ReleaseArtifactError(f"ZIP member path must not contain traversal: {name!r}")
        key = name.lower()
        if key in seen:
            raise ReleaseArtifactError(f"Duplicate or case-colliding ZIP member: {name!r}")
        seen.add(key)
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode):
            raise ReleaseArtifactError(f"ZIP member must not be a symlink: {name!r}")
        if file_type and not info.is_dir() and not stat.S_ISREG(mode):
            raise ReleaseArtifactError(f"ZIP member must be a regular file: {name!r}")
    return infos


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_sha256_file(path: Path) -> Path:
    checksum_path = path.with_name(path.name + ".sha256")
    checksum_path.write_text(f"{_sha256_file(path)}  {path.name}\n", encoding="utf-8")
    return checksum_path


def _verify_sha256_file(path: Path) -> None:
    checksum_path = path.with_name(path.name + ".sha256")
    text = checksum_path.read_text(encoding="utf-8")
    expected = f"{_sha256_file(path)}  {path.name}\n"
    if text != expected:
        raise ReleaseArtifactError(f"Checksum file does not match artifact: {checksum_path}")


def _verify_update_zip(zip_path: Path, version: str) -> None:
    _verify_update_zip_size(zip_path)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        install_root = temp_root / "install"
        release = github_updates.GitHubReleaseInfo(
            tag_name=f"v{version}",
            version=version,
            name=f"DarkAbyss {version}",
            draft=False,
            prerelease=False,
            assets=(
                github_updates.GitHubReleaseAsset(
                    name=zip_path.name,
                    download_url=f"https://github.com/example/project/releases/download/v{version}/{zip_path.name}",
                    size=zip_path.stat().st_size,
                    sha256=_sha256_file(zip_path),
                ),
            ),
        )
        downloaded = github_updates.DownloadedRelease(
            release=release,
            asset=release.assets[0],
            archive_path=zip_path,
            byte_count=zip_path.stat().st_size,
            sha256=_sha256_file(zip_path),
        )
        prepared = github_updates.prepare_downloaded_release(downloaded, install_root)
        staged = update_engine.stage_release(prepared.release_root, install_root)
        if staged.version != version:
            raise ReleaseArtifactError(f"Prepared update ZIP staged {staged.version!r}, expected {version!r}.")


def _verify_update_zip_size(zip_path: Path) -> None:
    size = zip_path.stat().st_size
    limit = github_updates.DEFAULT_MAX_ARTIFACT_BYTES
    if size > limit:
        raise ReleaseArtifactError(
            f"Update ZIP exceeds Phase 7 download size limit: {size} bytes > {limit} bytes."
        )


def _verify_fresh_install_zip(zip_path: Path, version: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        extract_root = Path(temp_dir)
        with zipfile.ZipFile(zip_path) as archive:
            infos = _validated_zip_infos(zip_path)
            members = [info.filename for info in infos if not info.is_dir()]
            prefix = f"{FRESH_INSTALL_ROOT_NAME}/"
            if any(not member.startswith(prefix) for member in members):
                raise ReleaseArtifactError("Fresh install ZIP entries must live under DarkAbyssBotManager/.")
            for info in infos:
                if info.is_dir():
                    continue
                target = extract_root / PurePosixPath(info.filename)
                _ensure_inside(extract_root, target, "fresh ZIP extraction target")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
        install_root = extract_root / FRESH_INSTALL_ROOT_NAME
        _validate_distribution(install_root, version)
        target = launcher.resolve_current_app(install_root)
        if target.version != version:
            raise ReleaseArtifactError(f"Fresh install launcher resolved {target.version!r}, expected {version!r}.")
        expected_app = (install_root / update_engine.VERSIONS_DIR_NAME / version / runtime_layout.app_executable_name()).resolve()
        if target.app_executable != expected_app:
            raise ReleaseArtifactError("Fresh install launcher resolution selected an unexpected executable.")


def _ensure_inside(root: Path, target: Path, label: str) -> None:
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseArtifactError(f"{label} escapes root {root}: {target}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build DarkAbyss GitHub release artifacts from an assembled distribution.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.verify_only:
            artifacts = verify_release_artifacts(version=args.version, artifacts_dir=args.output)
        else:
            artifacts = build_release_artifacts(
                version=args.version,
                distribution=args.distribution,
                output=args.output,
                tag=args.tag,
            )
    except (
        ReleaseArtifactError,
        github_updates.GitHubUpdateError,
        update_engine.UpdateEngineError,
        launcher.LauncherError,
        zipfile.BadZipFile,
        OSError,
    ) as exc:
        print(f"Release artifact build failed: {exc}")
        return 1

    print(f"Update artifact: {artifacts.update_zip}")
    print(f"Update checksum: {artifacts.update_sha256}")
    print(f"Fresh install artifact: {artifacts.fresh_install_zip}")
    print(f"Fresh install checksum: {artifacts.fresh_install_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
