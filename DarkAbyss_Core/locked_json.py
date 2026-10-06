"""Small JSON files shared by a bot process and the Manager (one machine).

``locked(path)`` is a short cross-process lock (a ``.<name>.lock`` file next to
the data file) around one read-modify-write; ``atomic_write_json`` replaces the
file in one step, so a reader never sees half a file. Used by Kairo's social
memory and content filter.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class FileBusyError(RuntimeError):
    """The lock could not be taken in time (another process holds it)."""


@contextmanager
def locked(path: Path, attempts: int = 100, delay: float = 0.02, error: type[Exception] = FileBusyError) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_name(f".{path.name}.lock"), "a+b")
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - the app runs on Windows
        msvcrt = None
    taken = False
    try:
        if msvcrt is not None:
            for _ in range(attempts):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    taken = True
                    break
                except OSError:
                    time.sleep(delay)
            if not taken:
                raise error(f"{path.name} is busy; try again.")
        yield
    finally:
        if taken:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def signature(path: Path) -> tuple[int, int] | None:
    """Changes whenever the file is replaced or rewritten (cheap cache key)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size
