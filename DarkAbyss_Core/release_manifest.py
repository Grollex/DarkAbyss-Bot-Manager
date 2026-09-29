from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import update_engine


class ReleaseManifestGenerationError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_payload_files(release_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(release_root.rglob("*"), key=lambda item: item.relative_to(release_root).as_posix()):
        if path.is_symlink():
            raise ReleaseManifestGenerationError(f"Release payload must not contain symlinks: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseManifestGenerationError(f"Release payload path is not a regular file: {path}")
        relative = path.relative_to(release_root).as_posix()
        if relative == update_engine.RELEASE_MANIFEST_NAME:
            continue
        try:
            update_engine._normalize_release_path(relative)
            update_engine._ensure_release_source_path_safe(release_root, path, relative)
        except update_engine.UpdateEngineError as exc:
            raise ReleaseManifestGenerationError(str(exc)) from exc
        files.append(path)
    return files


def build_manifest_payload(release_root: Path | str, version: str) -> dict:
    root = Path(release_root).resolve()
    if not root.is_dir():
        raise ReleaseManifestGenerationError(f"Release root must be a directory: {root}")
    valid_version = update_engine._validate_version(version, error_type=update_engine.ReleaseManifestError)
    manifest_files = []
    for path in _iter_payload_files(root):
        relative = path.relative_to(root).as_posix()
        manifest_files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    if not manifest_files:
        raise ReleaseManifestGenerationError("Release payload must contain at least one file.")
    return {
        "schema_version": update_engine.RELEASE_SCHEMA_VERSION,
        "version": valid_version,
        "files": manifest_files,
    }


def write_release_manifest(
    release_root: Path | str,
    version: str,
    output_path: Path | str | None = None,
) -> Path:
    root = Path(release_root).resolve()
    destination = Path(output_path).resolve() if output_path is not None else root / update_engine.RELEASE_MANIFEST_NAME
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ReleaseManifestGenerationError(f"release.json output must stay inside release root: {destination}") from exc
    if destination.name != update_engine.RELEASE_MANIFEST_NAME:
        raise ReleaseManifestGenerationError("release manifest output must be named release.json.")
    payload = build_manifest_payload(root, version)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    update_engine.inspect_release(root)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic DarkAbyss release.json.")
    parser.add_argument("release_root")
    parser.add_argument("version")
    args = parser.parse_args(argv)
    try:
        manifest_path = write_release_manifest(args.release_root, args.version)
    except (ReleaseManifestGenerationError, update_engine.UpdateEngineError) as exc:
        print(f"release manifest generation failed: {exc}")
        return 1
    print(f"Generated {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
