from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

import launcher
import release_manifest
import runtime_layout
import update_engine


class DistributionAssemblyError(RuntimeError):
    pass


def _reject_symlinks(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise DistributionAssemblyError(f"Distribution input must not contain symlinks: {path}")


def _prepare_clean_output_root(output_root: Path) -> None:
    if output_root.exists() or output_root.is_symlink():
        if output_root.is_symlink():
            raise DistributionAssemblyError(f"Output distribution root must not be a symlink: {output_root}")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)


def _copy_directory_contents(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False)
        else:
            shutil.copy2(item, target)


def assemble_distribution(
    *,
    version: str,
    app_bundle_dir: Path | str,
    launcher_executable: Path | str,
    output_dir: Path | str,
) -> Path:
    valid_version = update_engine._validate_version(version, error_type=update_engine.ReleaseManifestError)
    lexical_app_root = Path(app_bundle_dir)
    lexical_launcher_path = Path(launcher_executable)
    lexical_output_root = Path(output_dir)

    if lexical_app_root.is_symlink():
        raise DistributionAssemblyError(f"DarkAbyssApp onedir must not be a symlink: {lexical_app_root}")
    if lexical_launcher_path.is_symlink():
        raise DistributionAssemblyError(f"Launcher executable must not be a symlink: {lexical_launcher_path}")
    if lexical_output_root.is_symlink():
        raise DistributionAssemblyError(f"Output distribution root must not be a symlink: {lexical_output_root}")

    app_root = lexical_app_root.resolve()
    launcher_path = lexical_launcher_path.resolve()
    output_root = lexical_output_root.resolve()

    if not app_root.is_dir():
        raise DistributionAssemblyError(f"DarkAbyssApp onedir does not exist: {app_root}")
    _reject_symlinks(app_root)

    app_executable = app_root / runtime_layout.app_executable_name()
    if not app_executable.is_file():
        raise DistributionAssemblyError(f"Missing DarkAbyssApp executable: {app_executable}")

    if launcher_path.is_symlink() or not launcher_path.is_file():
        raise DistributionAssemblyError(f"Launcher executable is missing or not a regular file: {launcher_path}")
    if launcher_path.name != runtime_layout.launcher_executable_name():
        raise DistributionAssemblyError(
            f"Launcher executable must be named {runtime_layout.launcher_executable_name()}: {launcher_path}"
        )

    _prepare_clean_output_root(output_root)
    _copy_directory_contents(app_root, output_root / update_engine.VERSIONS_DIR_NAME / valid_version)
    shutil.copy2(launcher_path, output_root / runtime_layout.launcher_executable_name())

    version_dir = output_root / update_engine.VERSIONS_DIR_NAME / valid_version
    release_manifest.write_release_manifest(version_dir, valid_version)
    release = update_engine.inspect_release(version_dir)
    if release.version != valid_version:
        raise DistributionAssemblyError(f"Generated release version mismatch: {release.version!r}")

    current_path = output_root / update_engine.CURRENT_POINTER_NAME
    current_path.write_text(
        json.dumps(
            {
                "schema_version": update_engine.CURRENT_SCHEMA_VERSION,
                "version": valid_version,
                "previous_version": None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = launcher.resolve_current_app(output_root)
    expected_app = (version_dir / runtime_layout.app_executable_name()).resolve()
    if target.version != valid_version or target.app_executable != expected_app:
        raise DistributionAssemblyError("Launcher resolution did not select the assembled versioned app.")

    return output_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a bootable DarkAbyss Windows distribution tree.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--app-bundle", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    try:
        output_root = assemble_distribution(
            version=args.version,
            app_bundle_dir=args.app_bundle,
            launcher_executable=args.launcher,
            output_dir=args.output,
        )
    except (DistributionAssemblyError, update_engine.UpdateEngineError, release_manifest.ReleaseManifestGenerationError) as exc:
        print(f"Distribution assembly failed: {exc}")
        return 1

    print(f"Assembled distribution: {output_root}")
    print(f"Launcher: {output_root / runtime_layout.launcher_executable_name()}")
    print(f"Current pointer: {output_root / update_engine.CURRENT_POINTER_NAME}")
    print(f"Versioned app: {output_root / update_engine.VERSIONS_DIR_NAME / args.version / runtime_layout.app_executable_name()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
