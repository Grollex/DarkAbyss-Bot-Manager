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

## Future Manager Core

Future Manager Core will supervise these independent instance processes.

Not implemented yet:

- subprocess supervisor
- GUI
- updater
- launcher redesign
- GitHub integration
- packaging
- Windows service/systemd/Docker
