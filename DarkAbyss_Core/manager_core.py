from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

import app_paths
import bot_registry
import instance_store

STATE_STOPPED = "STOPPED"
STATE_RUNNING = "RUNNING"
STATE_EXITED = "EXITED"

STDOUT_LOG_NAME = "process.stdout.log"
STDERR_LOG_NAME = "process.stderr.log"


class ManagerCoreError(RuntimeError):
    pass


class ProcessAlreadyRunningError(ManagerCoreError):
    pass


class ProcessNotManagedError(ManagerCoreError):
    pass


class ProcessStartError(ManagerCoreError):
    pass


class ProcessStopError(ManagerCoreError):
    pass


@dataclass(frozen=True)
class LaunchSpec:
    executable: str
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdout_log_path: Path
    stderr_log_path: Path

    @property
    def command(self) -> tuple[str, ...]:
        return (self.executable, *self.args)


@dataclass(frozen=True)
class ProcessStatus:
    instance_id: str
    bot_type: str
    state: str
    pid: int | None
    started_at: datetime | None
    uptime_seconds: float | None
    exit_code: int | None


@dataclass
class _ProcessRecord:
    instance_id: str
    bot_type: str
    process: subprocess.Popen
    started_at: datetime
    stdout_handle: BinaryIO
    stderr_handle: BinaryIO
    exit_code: int | None = None
    stopped: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project_root() -> Path:
    return bot_registry.PROJECT_ROOT


def build_source_launch_spec(
    instance: instance_store.BotInstance,
    bot_type: bot_registry.BotType,
) -> LaunchSpec:
    env = os.environ.copy()
    env["DARKABYSS_DATA_DIR"] = str(app_paths.DATA_ROOT.resolve())
    return LaunchSpec(
        executable=sys.executable,
        args=(str(bot_type.entrypoint), "--instance", instance.id),
        cwd=_project_root(),
        env=env,
        stdout_log_path=instance.paths.logs_dir / STDOUT_LOG_NAME,
        stderr_log_path=instance.paths.logs_dir / STDERR_LOG_NAME,
    )


class BotProcessManager:
    def __init__(
        self,
        launch_spec_builder: Callable[
            [instance_store.BotInstance, bot_registry.BotType],
            LaunchSpec,
        ] = build_source_launch_spec,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._launch_spec_builder = launch_spec_builder
        self._popen_factory = popen_factory
        self._records: dict[str, _ProcessRecord] = {}
        self._last_status: dict[str, ProcessStatus] = {}
        self._lock = threading.RLock()

    def start(self, instance_id: str) -> ProcessStatus:
        with self._lock:
            record = self._records.get(instance_id)
            if record is not None:
                status = self._refresh_record(record)
                if status.state == STATE_RUNNING:
                    raise ProcessAlreadyRunningError(f"Instance already running: {instance_id}")

            instance, bot_type = self._load_instance_and_type(instance_id)
            launch_spec = self._launch_spec_builder(instance, bot_type)
            stdout_handle = None
            stderr_handle = None
            try:
                launch_spec.stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
                launch_spec.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_handle = launch_spec.stdout_log_path.open("ab")
                stderr_handle = launch_spec.stderr_log_path.open("ab")
                process = self._popen_factory(
                    [launch_spec.executable, *launch_spec.args],
                    cwd=launch_spec.cwd,
                    env=launch_spec.env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                )
            except Exception as exc:
                if stdout_handle is not None:
                    stdout_handle.close()
                if stderr_handle is not None:
                    stderr_handle.close()
                raise ProcessStartError(f"Failed to start instance {instance_id!r}: {exc}") from exc

            record = _ProcessRecord(
                instance_id=instance.id,
                bot_type=instance.bot_type,
                process=process,
                started_at=_now(),
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
            )
            self._records[instance.id] = record
            return self._status_from_record(record)

    def stop(self, instance_id: str, timeout: float = 10.0) -> ProcessStatus:
        with self._lock:
            record = self._records.get(instance_id)
            if record is None:
                raise ProcessNotManagedError(f"No process managed for instance: {instance_id}")

            status = self._refresh_record(record)
            if status.state != STATE_RUNNING:
                return status

            try:
                record.process.terminate()
                try:
                    exit_code = record.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    record.process.kill()
                    exit_code = record.process.wait()
            except Exception as exc:
                raise ProcessStopError(f"Failed to stop instance {instance_id!r}: {exc}") from exc

            record.exit_code = exit_code
            record.stopped = True
            return self._status_from_record(record)

    def restart(self, instance_id: str, timeout: float = 10.0) -> ProcessStatus:
        with self._lock:
            record = self._records.get(instance_id)
            if record is not None:
                status = self._refresh_record(record)
                if status.state == STATE_RUNNING:
                    self.stop(instance_id, timeout=timeout)
            return self.start(instance_id)

    def status(self, instance_id: str) -> ProcessStatus:
        with self._lock:
            record = self._records.get(instance_id)
            if record is not None:
                return self._refresh_record(record)

            instance = self._load_instance(instance_id)
            status = ProcessStatus(
                instance_id=instance.id,
                bot_type=instance.bot_type,
                state=STATE_STOPPED,
                pid=None,
                started_at=None,
                uptime_seconds=None,
                exit_code=None,
            )
            self._last_status[instance.id] = status
            return status

    def list_status(self) -> list[ProcessStatus]:
        with self._lock:
            statuses: list[ProcessStatus] = []
            for instance in instance_store.list_instances():
                statuses.append(self.status(instance.id))
            return statuses

    def shutdown_all(self, timeout: float = 10.0) -> dict[str, ProcessStatus | ManagerCoreError]:
        with self._lock:
            instance_ids = list(self._records)
        results: dict[str, ProcessStatus | ManagerCoreError] = {}
        for instance_id in instance_ids:
            try:
                with self._lock:
                    record = self._records.get(instance_id)
                    if record is None:
                        continue
                    status = self._refresh_record(record)
                    if status.state != STATE_RUNNING:
                        results[instance_id] = status
                        continue
                results[instance_id] = self.stop(instance_id, timeout=timeout)
            except ManagerCoreError as exc:
                results[instance_id] = exc
        return results

    def _load_instance(self, instance_id: str) -> instance_store.BotInstance:
        try:
            return instance_store.load_instance(instance_id)
        except (instance_store.InstanceStoreError, OSError) as exc:
            raise ManagerCoreError(f"Invalid bot instance {instance_id!r}: {exc}") from exc

    def _load_instance_and_type(
        self,
        instance_id: str,
    ) -> tuple[instance_store.BotInstance, bot_registry.BotType]:
        instance = self._load_instance(instance_id)
        try:
            bot_type = bot_registry.get_bot_type(instance.bot_type)
        except (bot_registry.BotRegistryError, OSError) as exc:
            raise ManagerCoreError(
                f"Invalid bot type {instance.bot_type!r} for instance {instance_id!r}: {exc}"
            ) from exc
        return instance, bot_type

    def _record_is_running(self, record: _ProcessRecord) -> bool:
        return record.process.poll() is None

    def _refresh_record(self, record: _ProcessRecord) -> ProcessStatus:
        return self._status_from_record(record)

    def _status_from_record(self, record: _ProcessRecord) -> ProcessStatus:
        exit_code = record.process.poll()
        if exit_code is None:
            status = ProcessStatus(
                instance_id=record.instance_id,
                bot_type=record.bot_type,
                state=STATE_RUNNING,
                pid=record.process.pid,
                started_at=record.started_at,
                uptime_seconds=max(0.0, (_now() - record.started_at).total_seconds()),
                exit_code=None,
            )
        else:
            record.exit_code = exit_code
            self._close_record_handles(record)
            status = ProcessStatus(
                instance_id=record.instance_id,
                bot_type=record.bot_type,
                state=STATE_STOPPED if record.stopped else STATE_EXITED,
                pid=None,
                started_at=record.started_at,
                uptime_seconds=None,
                exit_code=record.exit_code,
            )
        self._last_status[record.instance_id] = status
        return status

    def _close_record_handles(self, record: _ProcessRecord) -> None:
        if not record.stdout_handle.closed:
            record.stdout_handle.close()
        if not record.stderr_handle.closed:
            record.stderr_handle.close()


ProcessManager = BotProcessManager
