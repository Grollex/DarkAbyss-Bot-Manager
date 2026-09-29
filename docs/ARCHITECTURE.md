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

## Phase 5A Minimal GUI

The Windows GUI is a frontend only. It must not host Discord bot execution and must not duplicate process lifecycle logic.

Phase 5A dependencies flow one way:

```text
GUI / Qt frontend
  -> Manager Core
  -> ConfigStore
  -> InstanceStore / BotRegistry
```

Bot children remain separate OS processes owned by `BotProcessManager`. The GUI owns one manager instance for its session and calls `list_instance_info()`, `get_instance_info()`, `start()`, `stop()`, `restart()`, and `shutdown_all()` instead of launching shells or running bot code in the GUI process.

Qt is isolated to the frontend layer. `manager_core.py`, `config_store.py`, `instance_store.py`, `bot_registry.py`, and `Admin.py` remain headless and must not import PySide6.

The GUI uses a lightweight `QTimer` for read-only status refresh. It does not poll Discord and does not implement auto-restart.

Potentially blocking lifecycle operations run through a small Qt worker/thread path so the UI thread is not blocked by `stop()`, `restart()`, or `shutdown_all()`. `start()` uses the same path for consistency. While an action is active for an instance, duplicate actions for that instance are disabled.

The config editor is JSON-based in Phase 5A. It displays user overrides as editable JSON and effective config as read-only JSON. Saves go through `ConfigStore.save_config_overrides()` only; the GUI does not write `config.json` directly and does not read or display token files.

Closing the GUI must not silently orphan managed running children. If managed instances are running, Phase 5A offers an explicit stop-all-and-exit path or cancellation. It does not offer "leave running" until process adoption/persistence exists.

## Phase 6A Local Update Engine

The local update engine owns program code only. It must never install release payloads into `DATA_ROOT` or modify user-owned tokens, config overrides, config metadata, databases, logs, runtime files, instance data, or backups.

Phase 6A uses a prepared local release directory, not network downloads and not archive extraction. A release directory contains:

```text
release.json
<program payload files>
```

The release manifest schema is:

```json
{
  "schema_version": 1,
  "version": "1.0.0",
  "files": [
    {
      "path": "DarkAbyss_Core/example.py",
      "sha256": "<64 hex characters>",
      "size": 123
    }
  ]
}
```

The engine validates manifests strictly: schema version, version string, unique normalized relative paths, exact file sizes, and SHA-256 hashes. Release paths must stay inside the release payload root and the resulting installed version directory. Absolute paths, Windows drive paths, `..` traversal, symlinks that could redirect outside containment, and reserved user-data roots such as `instances`, `secrets`, `runtime`, `logs`, `backups`, `downloads`, `user_data`, root `config`, and database roots are rejected.

The application-owned install layout is:

```text
<PROGRAM_INSTALL_ROOT>/
    versions/
        <version>/
    updates/
        staging/
    current.json
```

`PROGRAM_INSTALL_ROOT` is injectable for tests and future packaging. It is separate from `DATA_ROOT`. The source checkout is not moved or rewritten by Phase 6A.

`PROGRAM_INSTALL_ROOT` and `DATA_ROOT` must be disjoint trees. The updater rejects an install root equal to `DATA_ROOT`, inside `DATA_ROOT`, or containing `DATA_ROOT`. Program update state and user-owned runtime state must never overlap.

Staging copies a fully verified local release into a unique application-owned staging directory under `<PROGRAM_INSTALL_ROOT>/updates/staging/`. The staged bytes are verified before publication. Only after the full copy succeeds does the engine publish the staged directory into `<PROGRAM_INSTALL_ROOT>/versions/<version>/` using a same-filesystem rename. Existing installed versions are not overwritten silently, and incomplete staging is cleaned on handled failure.

Updater-owned structural directories are:

- `<PROGRAM_INSTALL_ROOT>/versions/`
- `<PROGRAM_INSTALL_ROOT>/updates/`
- `<PROGRAM_INSTALL_ROOT>/updates/staging/`

Before creating or writing through any of these paths, the engine validates every existing structural component. Existing structural components must be real directories, must not be symlinks, and must resolve inside the install root. This containment check happens before `mkdir`, temporary staging creation, payload copy, or version publication, so a pre-existing symlinked `updates`, `updates/staging`, or `versions` path cannot redirect writes outside the install root.

Activation changes only `<PROGRAM_INSTALL_ROOT>/current.json`:

```json
{
  "schema_version": 1,
  "version": "1.0.0"
}
```

The pointer write is atomic: temporary file in the same directory, flush/fsync, then `os.replace`. Activation verifies the installed version metadata before updating the pointer. If pointer writing fails, the previous `current.json` remains valid. No mutable `current/` program directory is populated.

Previous version directories are retained for future rollback support. Phase 6A does not implement automatic rollback, network downloading, GitHub integration, packaging, or config/database migration execution. It may install migration code as program files, but it does not run migrations or modify user data during activation.

## Phase 7A GitHub Releases Transport

Phase 7A adds a GUI-independent GitHub Releases transport layer in:

```text
DarkAbyss_Core/github_updates.py
```

GitHub integration is transport only:

```text
GitHub Releases
    -> downloaded ZIP artifact
    -> safe local extraction/preparation
    -> update_engine.inspect_release()
    -> update_engine.stage_release()
    -> explicit update_engine.activate_staged_release()
```

The GitHub layer does not duplicate Phase 6 manifest validation, per-file SHA-256 verification, version publication, or `current.json` activation. The Phase 6 `release.json` remains the authoritative payload manifest, and ZIP extraction success is never treated as equivalent to release verification.

Repository owner/name are explicit API inputs. Public GitHub Releases work without authentication. An optional GitHub token may be supplied by API parameter or `GITHUB_TOKEN` for API requests only; it is never persisted, never placed in URLs, and is not forwarded to unrelated redirected hosts.

Supported lookups:

- latest stable release from GitHub Releases, excluding drafts and prereleases by default
- prereleases only when explicitly allowed
- exact tag lookup

Version normalization is intentionally small: `v1.2.3` maps to `1.2.3`; unrelated tag formats are not silently remapped. The selected GitHub tag version, expected asset name, and extracted `release.json` version must match before staging.

The deterministic release asset name is:

```text
darkabyss-release-<version>.zip
```

The ZIP must contain the Phase 6 release layout directly at archive root:

```text
release.json
DarkAbyss_Core/...
bots/...
```

Archives with an extra parent directory are not accepted. ZIP extraction is explicit and hardened: absolute paths, `..` traversal, Windows drive paths, backslash separators, symlink entries, special files, duplicate normalized paths, and case-colliding paths are rejected. `extractall()` is not used.

Network policy:

- API requests use HTTPS GitHub API endpoints with an explicit `User-Agent` and finite timeout.
- Asset downloads require HTTPS and allow only GitHub/GitHubusercontent asset hosts.
- Redirects to HTTP or unrelated hosts are rejected.
- Authorization is removed before following a redirect to a different host.

Downloads are written to a unique temporary file first under:

```text
<PROGRAM_INSTALL_ROOT>/updates/downloads/
```

Prepared extractions are written under:

```text
<PROGRAM_INSTALL_ROOT>/updates/prepared/
```

These are program/update-owned locations, not `DATA_ROOT`. Existing updater structural directories such as `updates/downloads` and `updates/prepared` must be real directories, must not be symlinks, and must resolve inside the install root before any network artifact is written or extracted. GitHub transport never writes to instances, config, secrets, logs, backups, databases, or other user-owned `DATA_ROOT` paths.

Downloads are bounded and streamed. The configured maximum compressed artifact size is enforced from `Content-Length` when present and again while bytes are streamed. If GitHub asset metadata declares a size, the completed byte count must match it. If GitHub provides a SHA-256 digest, the transport validates it; if no digest exists, the layer relies on Phase 6 per-file SHA-256 verification after extraction.

ZIP preparation has independent resource limits for extracted payload bytes and archive entry count. Before extraction, the transport validates `ZipInfo` metadata: the entry count must be below the configured limit, each declared uncompressed file size must be valid and within the extracted-size limit, and the total declared uncompressed size must fit within the same limit. During extraction, bytes are copied in bounded chunks; the actual bytes written for each file must exactly match its declared `ZipInfo.file_size`, and the running total must not exceed the configured extracted-size limit. Rejected archives do not proceed to Phase 6 staging or activation.

Phase 7A does not add GUI automatic update installation, unattended updates, rollback/recovery, GitHub publishing, PyInstaller packaging, or Discord runtime behavior changes. Activation remains an explicit caller decision through `update_engine.activate_staged_release()`.

## Phase 8A Rollback and Recovery Backend

Phase 8A keeps rollback GUI-independent and local to the already installed program versions managed by `update_engine.py`.

Rollback is a pointer operation only:

```text
<PROGRAM_INSTALL_ROOT>/
    versions/
        1.0.0/
        1.1.0/
    current.json
```

No program files are copied, restored from backups, downloaded, or deleted during rollback. No user-owned data is rolled back or modified. Tokens, config overrides, config metadata, databases, instance data, logs, and backups remain byte-identical across activation, rollback, and recovery.

`current.json` remains schema version 1 and is extended compatibly with optional `previous_version`:

```json
{
  "schema_version": 1,
  "version": "1.1.0",
  "previous_version": "1.0.0"
}
```

Old Phase 6 pointers without `previous_version` remain valid. First activation records `previous_version` as `null`. `get_current_version()` continues to return only the active version, while `get_activation_state()` returns both current and previous pointer fields.

Activation records the former current version atomically in the same `current.json` write. There is no second state file. If pointer writing fails, the previous valid `current.json` remains byte-identical.

Explicit rollback APIs:

- `rollback_to_version(target_version, install_root)`
- `rollback_to_previous(install_root)`

`rollback_to_version()` requires the target version directory to already exist under `versions/`, pass full Phase 6 `inspect_release()` verification, and have a `release.json` version matching the requested target. A rollback from `1.1.0` to `1.0.0` writes `version = 1.0.0` and `previous_version = 1.1.0`, enabling an explicit forward switch later. Rolling back to the already-current version is a no-op and does not rewrite the pointer.

`rollback_to_previous()` never guesses. It reads `previous_version` from the activation state, requires it to be present, and delegates to `rollback_to_version()`.

Explicit recovery API:

```text
recover_current_pointer(target_version, install_root)
```

Recovery is for an unusable `current.json`: missing, malformed, pointing to a missing version, or pointing to a corrupt installed version. The caller must explicitly provide the target version. Recovery verifies the target installed version with the same Phase 6 release checks and atomically writes a fresh `current.json` with no inferred previous version. If the current pointer is already healthy, recovery is rejected as unnecessary.

No Phase 8A operation selects versions by lexical order, directory mtime, or "latest installed" heuristics. Corruption never triggers automatic repair or network download.

Health inspection is non-mutating:

```text
check_install_health(install_root)
```

Health states:

- `HEALTHY`: pointer parses, current version exists, release verification passes, and release version matches pointer.
- `NO_CURRENT_POINTER`: `current.json` is absent.
- `INVALID_CURRENT_POINTER`: `current.json` is malformed, unsupported, non-file, symlinked, or contains invalid version fields.
- `CURRENT_VERSION_MISSING`: pointer is valid but the selected version directory is absent.
- `CURRENT_VERSION_CORRUPT`: selected version exists but fails release manifest, size, hash, or version checks.

Rollback and recovery preserve Phase 6 path safety: `versions/` is a real structural directory, `current.json` symlinks are rejected, version directories remain inside the install root, and `PROGRAM_INSTALL_ROOT` stays disjoint from `DATA_ROOT`.

Phase 8A still does not delete old versions, prune versions, add automatic unattended rollback, add GUI updater controls, add PyInstaller packaging, or change Discord bot behavior.

## Future Phases

Future phases may add:

- GUI
- updater
- launcher redesign
- GitHub integration
- packaging
- Windows service/systemd/Docker
