from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import runtime_layout
import update_engine


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class LauncherTarget:
    install_root: Path
    version: str
    version_dir: Path
    app_executable: Path
    command: tuple[str, ...]


def resolve_current_app(install_root: Path | str) -> LauncherTarget:
    root = Path(install_root).resolve()
    health = update_engine.check_install_health(root)
    if health.state != update_engine.HEALTHY or health.version is None:
        raise LauncherError(f"Current installed version is not healthy: {health.state}: {health.error or ''}".strip())

    version_candidate = root / update_engine.VERSIONS_DIR_NAME / health.version
    if version_candidate.is_symlink():
        raise LauncherError(f"Current version directory must not be a symlink: {version_candidate}")
    version_dir = version_candidate.resolve()
    try:
        version_dir.relative_to(root)
    except ValueError as exc:
        raise LauncherError(f"Current version directory escapes install root: {version_dir}") from exc

    try:
        app_executable = runtime_layout.resolve_packaged_app_executable(version_dir)
    except runtime_layout.RuntimeLayoutError as exc:
        raise LauncherError(str(exc)) from exc

    return LauncherTarget(
        install_root=root,
        version=health.version,
        version_dir=version_dir,
        app_executable=app_executable,
        command=(str(app_executable), "--manager"),
    )


def start_manager(
    install_root: Path | str | None = None,
    *,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    root = runtime_layout.stable_launcher_install_root() if install_root is None else Path(install_root)
    target = resolve_current_app(root)
    return popen_factory(
        list(target.command),
        cwd=target.version_dir,
        stdin=subprocess.DEVNULL,
        shell=False,
    )


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("Launcher accepts no command-line arguments.", file=sys.stderr)
        return 2
    try:
        start_manager()
    except Exception as exc:
        print(f"DarkAbyss launcher failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
