from __future__ import annotations

import sys
from pathlib import Path


APP_EXECUTABLE_BASENAME = "DarkAbyssApp"
LAUNCHER_EXECUTABLE_BASENAME = "Launcher"


class RuntimeLayoutError(RuntimeError):
    pass


def line_buffered_output() -> None:
    """Bot processes write to log files: flush every line so the Manager's Logs
    page shows messages while the bot runs (block buffering hid them until exit)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(line_buffering=True)
            except (ValueError, OSError):
                pass


def _exe_name(base_name: str) -> str:
    return f"{base_name}.exe" if sys.platform == "win32" else base_name


def app_executable_name() -> str:
    return _exe_name(APP_EXECUTABLE_BASENAME)


def launcher_executable_name() -> str:
    return _exe_name(LAUNCHER_EXECUTABLE_BASENAME)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    if is_frozen():
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root).resolve()
        return Path(sys.executable).resolve().parent
    return source_project_root()


def core_root() -> Path:
    return resource_root() / "DarkAbyss_Core"


def bots_root() -> Path:
    return resource_root() / "bots"


def executable_version_dir(executable: Path | str) -> Path:
    return Path(executable).resolve().parent


def _current_app_paths() -> tuple[Path, Path]:
    lexical_executable = Path(sys.executable)
    lexical_version_dir = lexical_executable.parent
    expected_name = app_executable_name()
    if lexical_executable.name != expected_name:
        raise RuntimeLayoutError(f"Current executable must be {expected_name}: {lexical_executable}")
    if lexical_executable.is_symlink():
        raise RuntimeLayoutError(f"Current app executable must not be a symlink: {lexical_executable}")
    if lexical_version_dir.is_symlink():
        raise RuntimeLayoutError(f"Current version directory must not be a symlink: {lexical_version_dir}")

    resolved_version_dir = lexical_version_dir.resolve()
    resolved_executable = lexical_executable.resolve()
    if not resolved_version_dir.is_dir():
        raise RuntimeLayoutError(f"Current version directory must be a real directory: {resolved_version_dir}")
    if not resolved_executable.is_file():
        raise RuntimeLayoutError(f"Current app executable is missing or not a regular file: {resolved_executable}")
    if resolved_executable.parent != resolved_version_dir:
        raise RuntimeLayoutError(f"Current app executable must be directly inside version directory: {resolved_executable}")
    return resolved_executable, resolved_version_dir


def current_app_executable() -> Path:
    executable, _version_dir = _current_app_paths()
    return executable


def current_version_dir() -> Path:
    _executable, version_dir = _current_app_paths()
    return version_dir


def stable_launcher_install_root(executable: Path | str | None = None) -> Path:
    selected = Path(executable) if executable is not None else Path(sys.executable)
    return selected.resolve().parent


def path_is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def resolve_packaged_app_executable(version_dir: Path | str) -> Path:
    root = Path(version_dir).resolve()
    if Path(version_dir).is_symlink() or not root.is_dir():
        raise RuntimeLayoutError(f"Packaged version directory must be a real directory: {root}")
    candidate = root / app_executable_name()
    if candidate.is_symlink():
        raise RuntimeLayoutError(f"Packaged app executable must not be a symlink: {candidate}")
    executable = candidate.resolve()
    if not executable.is_file():
        raise RuntimeLayoutError(f"Packaged app executable is missing or not a regular file: {executable}")
    if not path_is_inside(executable, root):
        raise RuntimeLayoutError(f"Packaged app executable escapes version directory: {executable}")
    if executable.parent != root:
        raise RuntimeLayoutError(f"Packaged app executable must be directly inside version directory: {executable}")
    return executable
