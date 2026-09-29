from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping

import app_paths
import bot_registry
import instance_store
import runtime_layout

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


@dataclass(frozen=True)
class InstanceInfo:
    instance_id: str
    display_name: str
    bot_type: str
    bot_type_display_name: str
    bot_version: str
    state: str
    pid: int | None
    started_at: datetime | None
    uptime_seconds: float | None
    exit_code: int | None
    config_path: Path
    logs_dir: Path
    stdout_log_path: Path
    stderr_log_path: Path


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


def build_packaged_launch_spec(
    instance: instance_store.BotInstance,
    bot_type: bot_registry.BotType,
    app_executable: Path | str,
    version_dir: Path | str,
) -> LaunchSpec:
    version_dir_path = Path(version_dir)
    executable_path = Path(app_executable)
    if version_dir_path.is_symlink():
        raise ProcessStartError(f"Packaged version directory must not be a symlink: {version_dir_path}")
    resolved_version_dir = version_dir_path.resolve()
    if not resolved_version_dir.is_dir():
        raise ProcessStartError(f"Packaged version directory does not exist: {resolved_version_dir}")
    if executable_path.is_symlink():
        raise ProcessStartError(f"Packaged app executable must not be a symlink: {executable_path}")
    resolved_executable = executable_path.resolve()
    if not runtime_layout.path_is_inside(resolved_executable, resolved_version_dir):
        raise ProcessStartError(
            f"Packaged app executable must stay inside version directory: {resolved_executable}"
        )
    if resolved_executable.is_symlink() or not resolved_executable.is_file():
        raise ProcessStartError(f"Packaged app executable is missing or not a regular file: {resolved_executable}")
    if resolved_executable.name != runtime_layout.app_executable_name():
        raise ProcessStartError(
            f"Packaged app executable must be named {runtime_layout.app_executable_name()}: {resolved_executable}"
        )
    if resolved_executable.parent != resolved_version_dir:
        raise ProcessStartError(f"Packaged app executable must be directly inside version directory: {resolved_executable}")

    env = os.environ.copy()
    env["DARKABYSS_DATA_DIR"] = str(app_paths.DATA_ROOT.resolve())
    return LaunchSpec(
        executable=str(resolved_executable),
        args=("--bot-runner", bot_type.id, "--instance", instance.id),
        cwd=resolved_version_dir,
        env=env,
        stdout_log_path=instance.paths.logs_dir / STDOUT_LOG_NAME,
        stderr_log_path=instance.paths.logs_dir / STDERR_LOG_NAME,
    )


def build_packaged_launch_spec_builder(
    app_executable: Path | str,
    version_dir: Path | str,
) -> Callable[[instance_store.BotInstance, bot_registry.BotType], LaunchSpec]:
    def build(
        instance: instance_store.BotInstance,
        bot_type: bot_registry.BotType,
    ) -> LaunchSpec:
        return build_packaged_launch_spec(instance, bot_type, app_executable, version_dir)

    return build


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
        self._operation_locks: dict[str, threading.RLock] = {}
        self._lock = threading.RLock()

    def start(self, instance_id: str) -> ProcessStatus:
        operation_lock = self._get_operation_lock(instance_id)
        with operation_lock:
            with self._lock:
                record = self._records.get(instance_id)
                if record is not None:
                    status = self._refresh_record(record)
                    if status.state == STATE_RUNNING:
                        raise ProcessAlreadyRunningError(f"Instance already running: {instance_id}")

            instance, bot_type = self._load_instance_and_type(instance_id)
            try:
                launch_spec = self._launch_spec_builder(instance, bot_type)
                self._validate_launch_spec(instance, launch_spec)
            except ManagerCoreError:
                raise
            except Exception as exc:
                raise ProcessStartError(f"Failed to build launch spec for instance {instance_id!r}: {exc}") from exc

            stdout_handle = None
            stderr_handle = None
            try:
                child_env = dict(launch_spec.env)
                child_env["DARKABYSS_DATA_DIR"] = str(app_paths.DATA_ROOT.resolve())
                stdout_handle = launch_spec.stdout_log_path.open("ab")
                stderr_handle = launch_spec.stderr_log_path.open("ab")
                process = self._popen_factory(
                    [launch_spec.executable, *launch_spec.args],
                    cwd=launch_spec.cwd,
                    env=child_env,
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
            with self._lock:
                self._records[instance.id] = record
                return self._status_from_record(record)

    def stop(self, instance_id: str, timeout: float = 10.0) -> ProcessStatus:
        timeout_value = self._validate_timeout(timeout)
        operation_lock = self._get_operation_lock(instance_id)
        with operation_lock:
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
                    exit_code = record.process.wait(timeout=timeout_value)
                except subprocess.TimeoutExpired:
                    record.process.kill()
                    exit_code = record.process.wait()
            except Exception as exc:
                raise ProcessStopError(f"Failed to stop instance {instance_id!r}: {exc}") from exc

            with self._lock:
                record.exit_code = exit_code
                record.stopped = True
                return self._status_from_record(record)

    def restart(self, instance_id: str, timeout: float = 10.0) -> ProcessStatus:
        self._validate_timeout(timeout)
        operation_lock = self._get_operation_lock(instance_id)
        with operation_lock:
            with self._lock:
                record = self._records.get(instance_id)
                if record is not None:
                    status = self._refresh_record(record)
                    running = status.state == STATE_RUNNING
                else:
                    running = False
            if running:
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
            return status

    def list_status(self) -> list[ProcessStatus]:
        try:
            instances = instance_store.list_instances()
        except (instance_store.InstanceStoreError, OSError) as exc:
            raise ManagerCoreError(f"Failed to list bot instances: {exc}") from exc
        return [self.status(instance.id) for instance in sorted(instances, key=lambda item: item.id)]

    def get_instance_info(self, instance_id: str) -> InstanceInfo:
        instance, bot_type = self._load_instance_and_type(instance_id)
        status = self.status(instance.id)
        return self._instance_info(instance, bot_type, status)

    def list_instance_info(self) -> list[InstanceInfo]:
        infos: list[InstanceInfo] = []
        try:
            instances = instance_store.list_instances()
        except (instance_store.InstanceStoreError, OSError) as exc:
            raise ManagerCoreError(f"Failed to list bot instances: {exc}") from exc
        for instance in sorted(instances, key=lambda item: item.id):
            try:
                bot_type = bot_registry.get_bot_type(instance.bot_type)
            except (bot_registry.BotRegistryError, OSError) as exc:
                raise ManagerCoreError(
                    f"Invalid bot type {instance.bot_type!r} for instance {instance.id!r}: {exc}"
                ) from exc
            infos.append(self._instance_info(instance, bot_type, self.status(instance.id)))
        return infos

    def shutdown_all(self, timeout: float = 10.0) -> dict[str, ProcessStatus | ManagerCoreError]:
        self._validate_timeout(timeout)
        with self._lock:
            instance_ids = sorted(self._records)
        results: dict[str, ProcessStatus | ManagerCoreError] = {}
        for instance_id in instance_ids:
            try:
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

    def _get_operation_lock(self, instance_id: str) -> threading.RLock:
        try:
            valid_id = instance_store.validate_instance_id(instance_id)
        except instance_store.InstanceStoreError as exc:
            raise ManagerCoreError(f"Invalid bot instance {instance_id!r}: {exc}") from exc
        with self._lock:
            lock = self._operation_locks.get(valid_id)
            if lock is None:
                lock = threading.RLock()
                self._operation_locks[valid_id] = lock
            return lock

    def _instance_info(
        self,
        instance: instance_store.BotInstance,
        bot_type: bot_registry.BotType,
        status: ProcessStatus,
    ) -> InstanceInfo:
        return InstanceInfo(
            instance_id=instance.id,
            display_name=instance.display_name,
            bot_type=instance.bot_type,
            bot_type_display_name=bot_type.display_name,
            bot_version=bot_type.version,
            state=status.state,
            pid=status.pid,
            started_at=status.started_at,
            uptime_seconds=status.uptime_seconds,
            exit_code=status.exit_code,
            config_path=instance.paths.config,
            logs_dir=instance.paths.logs_dir,
            stdout_log_path=instance.paths.logs_dir / STDOUT_LOG_NAME,
            stderr_log_path=instance.paths.logs_dir / STDERR_LOG_NAME,
        )

    def _validate_timeout(self, timeout: float) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ManagerCoreError(f"timeout must be a non-negative number, got {timeout!r}")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value < 0:
            raise ManagerCoreError(f"timeout must be a finite non-negative number, got {timeout!r}")
        return timeout_value

    def _validate_launch_spec(self, instance: instance_store.BotInstance, launch_spec: LaunchSpec) -> None:
        if not isinstance(launch_spec, LaunchSpec):
            raise ProcessStartError(f"Invalid launch spec for instance {instance.id!r}: expected LaunchSpec.")
        if not isinstance(launch_spec.executable, str) or not launch_spec.executable.strip():
            raise ProcessStartError(f"Invalid launch spec for instance {instance.id!r}: executable must be non-empty.")
        if not isinstance(launch_spec.args, tuple) or not all(isinstance(arg, str) for arg in launch_spec.args):
            raise ProcessStartError(f"Invalid launch spec for instance {instance.id!r}: args must be a tuple of strings.")
        if not isinstance(launch_spec.cwd, Path) or not launch_spec.cwd.is_dir():
            raise ProcessStartError(f"Invalid launch spec for instance {instance.id!r}: cwd must be an existing directory.")
        if not self._env_is_valid(launch_spec.env):
            raise ProcessStartError(
                f"Invalid launch spec for instance {instance.id!r}: env must map strings to strings."
            )
        self._validate_log_path(instance, launch_spec.stdout_log_path, "stdout_log_path")
        self._validate_log_path(instance, launch_spec.stderr_log_path, "stderr_log_path")

    def _env_is_valid(self, env: object) -> bool:
        if not isinstance(env, Mapping):
            return False
        return all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())

    def _validate_log_path(self, instance: instance_store.BotInstance, path: Path, field_name: str) -> None:
        if not isinstance(path, Path):
            raise ProcessStartError(f"Invalid launch spec for instance {instance.id!r}: {field_name} must be a Path.")
        logs_dir = instance.paths.logs_dir.resolve()
        resolved_path = path.resolve(strict=False)
        try:
            resolved_path.relative_to(logs_dir)
        except ValueError as exc:
            raise ProcessStartError(
                f"Invalid launch spec for instance {instance.id!r}: {field_name} must stay inside {logs_dir}."
            ) from exc
        if resolved_path.parent != logs_dir:
            raise ProcessStartError(
                f"Invalid launch spec for instance {instance.id!r}: {field_name} must be directly under {logs_dir}."
            )

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
        return status

    def _close_record_handles(self, record: _ProcessRecord) -> None:
        if not record.stdout_handle.closed:
            record.stdout_handle.close()
        if not record.stderr_handle.closed:
            record.stderr_handle.close()


ProcessManager = BotProcessManager
