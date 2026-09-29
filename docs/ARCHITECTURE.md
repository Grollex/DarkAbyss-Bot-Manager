# DarkAbyss Bot Manager Architecture

## Phase 2A Status

Phase 2A is complete. It introduced program-owned Bot Type manifests and user-owned Bot Instance storage under `DATA_ROOT/instances`.

## Phase 2B Status

Phase 2B migrates the live Admin Bot runtime to the instance architecture.

`Admin.py` now runs one selected Admin Bot instance per process:

```bat
python DarkAbyss_Core\Admin.py --instance admin-main
python DarkAbyss_Core\Admin.py --instance admin-second
```

If `--instance` is omitted, `admin-main` is selected.

Only `admin-main` receives special first-run bootstrap and Phase 1 migration behavior. Other instance IDs must already exist.

## Bot Type

A Bot Type is program-owned. It describes an available bot implementation and lives in the application/release tree.

Current manifest:

```text
bots/admin/manifest.json
```

Current manifest shape:

```json
{
  "schema_version": 1,
  "id": "admin",
  "display_name": "Admin Bot",
  "version": "1.0.0",
  "entrypoint": "DarkAbyss_Core/Admin.py",
  "default_config": "DarkAbyss_Core/defaults/admin_config.json"
}
```

Manifests must not contain user IDs, guild IDs, tokens, absolute local paths, credentials, logs, or mutable user data.

## Bot Instance

A Bot Instance is user-owned. It stores mutable configuration and secrets for one configured copy of a bot type.

Instance layout:

```text
<DATA_ROOT>/instances/<instance_id>/
    instance.json
    config.json
    secrets/
        token.txt
    runtime/
        admin_bot.lock
    logs/
    data/
```

These files are user-owned. Program updates must never overwrite them.

- `instance.json` stores minimal metadata for the instance.
- `config.json` is mutable user configuration for that instance.
- `secrets/token.txt` stores that instance's Discord token.
- `runtime/` stores runtime state such as the instance-specific lock file.
- `logs/` is reserved for instance logs.
- `data/` is reserved for persistent bot-specific data.

Minimal instance metadata:

```json
{
  "schema_version": 1,
  "id": "admin-main",
  "bot_type": "admin",
  "display_name": "Admin Bot"
}
```

## Admin Runtime Selection

Startup resolves an immutable Admin runtime context:

- selected `instance_id`
- selected `config.json`
- selected `secrets/token.txt`
- selected `runtime/admin_bot.lock`

`load_config()` reloads config from the selected instance. This preserves the existing `/execute` config reload behavior without falling back to Phase 1 paths.

`load_token()` reads only the selected instance token and rejects missing, empty, or placeholder tokens.

The lock file is instance-specific, so `admin-main` and `admin-second` do not block each other. Two processes using the same instance are still prevented from running simultaneously.

## Phase 1 Migration

Legacy Phase 1 sources:

```text
<DATA_ROOT>/config/admin.json
<DATA_ROOT>/secrets/admin_bot_token.txt
```

Migration rules:

- If `admin-main` exists, it is authoritative and is not overwritten.
- If `admin-main` does not exist, it is atomically created from Phase 1 config/token when useful Phase 1 data exists.
- If no useful Phase 1 data exists, `admin-main` is created from program defaults and a placeholder token.
- Phase 1 source files are never deleted or modified automatically.
- A Phase 1 token equal to `PUT_DISCORD_BOT_TOKEN_HERE` is treated as a placeholder.

## Multiple Admin Instances

Multiple Admin Bot instances are independent:

```bat
python DarkAbyss_Core\instance_store.py create admin admin-second
Admin.bat admin-second
```

Each instance has separate:

- config
- token
- runtime directory
- lock file
- logs directory
- data directory

Running multiple instances means running multiple independent OS processes, one selected instance per process.

## Phase 3A Manager Core

Phase 3A adds a GUI-independent process supervision layer in:

```text
DarkAbyss_Core/manager_core.py
```

Manager Core owns process lifecycle only. Bot processes own bot functionality.

Current lifecycle API:

- `start(instance_id)`
- `stop(instance_id, timeout=...)`
- `restart(instance_id, timeout=...)`
- `status(instance_id)`
- `list_status()`
- `shutdown_all(timeout=...)`

One Bot Instance maps to one OS process. Multiple instances are independent processes:

```text
Manager Core
    ├── admin-main process
    └── admin-second process
```

Manager Core resolves launch targets through:

```text
Bot Instance -> bot_type -> Bot Type manifest -> entrypoint
```

It does not hardcode `Admin.py`.

## Entrypoint Contract

Managed bot entrypoints must accept:

```text
--instance <instance_id>
```

The current source-runtime launch strategy is:

```text
sys.executable <bot_type.entrypoint> --instance <instance_id>
```

Launch construction is isolated behind `LaunchSpec` so later packaged/runtime launch strategies can replace it without rewriting process supervision.

Manager Core launches children with argument lists only. It does not use `shell=True`, `cmd /c`, PowerShell, `os.system`, `eval`, or `exec`.

## Process Status Model

Manager Core exposes immutable process status snapshots:

- `instance_id`
- `bot_type`
- `state`
- `pid`
- `started_at`
- `uptime_seconds`
- `exit_code`

States:

- `STOPPED`: no running child is owned by this Manager Core session.
- `RUNNING`: the owned child is still running according to `process.poll()`.
- `EXITED`: the owned child exited naturally and its exit code was captured.

Before first start, valid instances report `STOPPED` with no PID and no exit code.

After explicit `stop()`, status is `STOPPED` and retains the final exit code. Natural child exit is detected by polling `status()` and reported as `EXITED`.

## Process Ownership Boundary

Phase 3A process ownership is in-memory only.

The Manager Core object owns only child processes that it started during the current Python session. It does not adopt arbitrary PIDs after restart, does not persist process handles, and is not a daemon or service.

Future persistent process adoption/service mode must be designed later.

## Manager Logs

Child stdout/stderr are redirected to instance-owned logs:

```text
<DATA_ROOT>/instances/<instance_id>/logs/process.stdout.log
<DATA_ROOT>/instances/<instance_id>/logs/process.stderr.log
```

Logs are opened in append mode. They are user-owned runtime data and must not be tracked by Git.

Manager Core does not put bot tokens in process command lines and does not log token contents.

## Phase 3A Non-Goals

Not implemented yet:

- persistent auto-start or auto-restart policy
- restart delay/limit settings
- GUI or tray icon
- daemon/service mode
- HTTP/WebSocket/remote control
- updater
- GitHub integration
- packaging
- Alehandro migration

## Phase 3B Manager Core Hardening

Phase 3B keeps Manager Core GUI-independent while adding safer read models and lifecycle hardening for future GUI/headless callers.

Public read APIs now include:

- `get_instance_info(instance_id)`
- `list_instance_info()`

`InstanceInfo` is an immutable GUI-facing snapshot. It includes identity, bot type metadata, current process status, config path, logs directory, and Manager-owned stdout/stderr log paths. It does not expose token contents, config contents, secret contents, or mutable user data.

`list_status()` and `list_instance_info()` return instances sorted by `instance_id`.

All public read APIs refresh owned process records before reporting state. If a child exited naturally, `status()`, `get_instance_info()`, `list_status()`, and `list_instance_info()` report `EXITED`, capture the exit code, and finalize Manager-owned log handles.

Public Manager APIs normalize storage and registry failures as `ManagerCoreError` subclasses or `ManagerCoreError` itself. Malformed instances are reported clearly and are not silently skipped during listing.

## Manager Core Concurrency

Manager Core uses:

- a small global lock for shared dictionaries
- per-instance lifecycle locks for `start()`, `stop()`, and `restart()`

Same-instance lifecycle operations are serialized, so two concurrent `start("admin-main")` calls cannot create duplicate managed children.

Different instances remain independent. A blocking stop for `admin-main` does not hold the global manager lock while waiting on the process, so status/read operations for `admin-second` can still complete.

## LaunchSpec Validation

Before spawning a child process, Manager Core validates:

- executable is a non-empty string
- args is a tuple of strings
- cwd exists and is a directory
- env maps strings to strings
- stdout/stderr log paths stay directly under the selected instance's `logs/` directory

Child processes are still launched with argument lists only. Manager Core does not use shell execution.

Manager Core copies the `LaunchSpec` environment before spawn and enforces `DARKABYSS_DATA_DIR` itself for the child process. Custom launch-spec builders cannot omit or override the selected Manager data root, and their original environment mapping is not mutated.

Manager-created process logs are limited to:

```text
<DATA_ROOT>/instances/<instance_id>/logs/process.stdout.log
<DATA_ROOT>/instances/<instance_id>/logs/process.stderr.log
```

Manager Core does not read token contents, does not edit config/token files, and does not place token values in command arguments.

## Shutdown and Failure Behavior

Invalid timeout values are rejected. A timeout of `0` is allowed and means terminate, immediately escalate to kill if the child is still running, then reap.

If stopping one instance fails, `shutdown_all()` still attempts all other managed records and returns a result or error per considered instance.

Process ownership remains in-memory only. Phase 3B still does not add PID files, persisted process state, auto-start, auto-restart, GUI, daemon/service mode, HTTP, WebSocket, updater, or packaging.

## Phase 4A Versioned Configuration

Phase 4A separates program-owned configuration defaults from user-owned overrides.

Program-owned files:

- bot type manifests
- default config files such as `DarkAbyss_Core/defaults/admin_config.json`
- declarative config schemas such as `bots/admin/config.schema.json`
- migration code

User-owned files:

- instance `config.json` override files
- instance `config.meta.json` version metadata
- config migration backups under `<DATA_ROOT>/backups`
- tokens, runtime files, logs, databases, and bot data

Runtime effective config is:

```text
bot type default config + instance config.json overrides
```

Dictionaries merge recursively. Scalar values replace defaults. Lists replace defaults. Missing override fields inherit program defaults. The merge returns a fresh dictionary for runtime consumers and never mutates program defaults or user override files.

New instances use an empty override file:

```json
{}
```

They also receive `config.meta.json` at the bot type's current `config_version`. This lets future program defaults flow into fields the user never overrode.

Existing Phase 2/3 full `config.json` files remain compatible. They are treated as explicit user overrides, so values already present in the old file keep their behavior when merged with newer defaults.

## Config Versions and Migrations

Bot type manifests declare:

- `config_schema`
- `config_version`

`config_schema` is validated as a safe program-owned file path. It must be relative, stay inside the program tree, stay outside `DATA_ROOT`, exist, and be a regular file.

Instance config metadata lives beside the user override file:

```text
<DATA_ROOT>/instances/<instance_id>/config.meta.json
```

Current metadata shape:

```json
{
  "schema_version": 1,
  "config_version": 1
}
```

An existing instance without `config.meta.json` is legacy config version `0`. The conservative `0 -> 1` migration validates that `config.json` is a usable JSON object, creates a backup, and atomically writes metadata. It does not rewrite config bytes when no transformation is needed.

Public effective-config loading always ensures the instance config is current before returning runtime values. Legacy `0 -> 1` migration therefore happens automatically on the first effective-config load, including Admin runtime config loads.

Stale configs are never interpreted with newer defaults/schema unless a supported migration path completes first. If no migration path exists, effective config is not returned.

The `0 -> 1` migration is idempotent. After metadata is current, repeated effective-config loads validate the current config but do not create additional migration backups.

If metadata reports a config version newer than the program supports, Manager/runtime code fails clearly and never downgrades automatically.

## Config Backups

Before a config migration marks an existing user config as current, it creates a backup under:

```text
<DATA_ROOT>/backups/instances/<instance_id>/config/<unique-backup-id>/
    config.json
    backup.json
```

The backed-up `config.json` is byte-for-byte identical to the original user config. `backup.json` contains safe metadata only: backup schema version, instance id, source/target config versions, UTC creation time, and the SHA-256 of the backed-up config bytes.

Backups never include token contents, secret files, environment variables, logs, runtime files, databases, or unrelated user data. Backup paths are generated internally and must remain under `app_paths.BACKUPS_DIR`.

Migration writes to config metadata are atomic. A failed migration must leave the original config, token, and user data untouched and must not leave a successful current-version marker behind.

If metadata writing fails after a backup was completed, the valid backup may remain. This is safe user data and is not treated as a successful migration marker.

## Phase 4B Config Editing API

Future frontends must use ConfigStore APIs instead of writing instance `config.json` directly.

The GUI-facing configuration APIs are:

- `get_config_snapshot(instance_id)`
- `load_config_overrides(instance_id)`
- `save_config_overrides(instance_id, overrides)`

`config.json` remains an overrides-only user file. `get_config_snapshot()` returns separate fresh dictionaries for defaults, user overrides, and effective runtime config. Runtime normalization never rewrites user files.

`save_config_overrides()` first ensures the instance config version is current, validates proposed overrides by merging them with bot type defaults, and writes the override file atomically only after validation succeeds. Invalid overrides, stale unsupported config versions, unsafe paths, malformed current config, and write failures leave the previous override bytes intact.

Ordinary config edits do not create migration backups. Backups are created only by supported config migrations before metadata is marked current.

## Future Phases

Future phases may add:

- GUI
- updater
- launcher redesign
- GitHub integration
- packaging
- Windows service/systemd/Docker
