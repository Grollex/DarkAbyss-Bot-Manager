from __future__ import annotations

import argparse
import sys
from typing import Callable

import manager_core
import runtime_layout


class AppEntryError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DarkAbyss packaged application entrypoint.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manager", action="store_true", help="Start the Manager GUI.")
    mode.add_argument("--bot-runner", metavar="BOT_TYPE", help="Run one bot instance in this process.")
    parser.add_argument("--instance", help="Bot instance id for --bot-runner mode.")
    return parser.parse_args(argv)


def build_manager_for_runtime() -> manager_core.BotProcessManager:
    if runtime_layout.is_frozen():
        executable = runtime_layout.current_app_executable()
        version_dir = runtime_layout.current_version_dir()
        return manager_core.BotProcessManager(
            launch_spec_builder=manager_core.build_packaged_launch_spec_builder(
                executable,
                version_dir,
            )
        )
    return manager_core.BotProcessManager(
        launch_spec_builder=manager_core.build_source_launch_spec,
    )


def dispatch(
    args: argparse.Namespace,
    *,
    manager_main: Callable[[manager_core.BotProcessManager], int] | None = None,
    admin_main: Callable[[list[str]], int] | None = None,
    game_presence_main: Callable[[list[str]], int] | None = None,
) -> int:
    if args.manager:
        if args.instance:
            raise AppEntryError("--instance is only valid with --bot-runner.")
        if manager_main is None:
            import manager_gui

            manager_main = manager_gui.main
        return int(manager_main(build_manager_for_runtime()))

    if args.bot_runner == "admin":
        if not args.instance:
            raise AppEntryError("--bot-runner admin requires --instance.")
        if admin_main is None:
            import Admin

            admin_main = Admin.main
        return int(admin_main(["--instance", args.instance]))

    if args.bot_runner == "game_presence":
        if not args.instance:
            raise AppEntryError("--bot-runner game_presence requires --instance.")
        if game_presence_main is None:
            import GamePresence

            game_presence_main = GamePresence.main
        return int(game_presence_main(["--instance", args.instance]))

    raise AppEntryError(f"Unknown bot type for --bot-runner: {args.bot_runner!r}")


def main(argv: list[str] | None = None) -> int:
    runtime_layout.line_buffered_output()
    try:
        return dispatch(parse_args(argv))
    except AppEntryError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
