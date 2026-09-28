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

## Future Phases

Future phases may add:

- GUI
- updater
- launcher redesign
- GitHub integration
- packaging
- Windows service/systemd/Docker
