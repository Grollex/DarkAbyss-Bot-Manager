# DarkAbyss Bot Manager Architecture

## Phase 2A status

Phase 2A introduces metadata and storage foundations only. The live Admin Bot still uses the Phase 1 runtime paths under `DATA_ROOT/config`, `DATA_ROOT/secrets`, and `DATA_ROOT/runtime`.

The Admin Bot runtime is not migrated to an `admin-main` instance yet. That belongs to Phase 2B.

## Bot Types

A Bot Type is a program-owned definition of an available bot implementation. Bot type definitions live in the application/release tree and may be replaced by future program updates.

Current program-owned type manifest:

```text
bots/admin/manifest.json
```

The manifest describes metadata only:

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

## Bot Instances

A Bot Instance is a user-owned independent configured copy of a bot type. One bot type may have multiple instances.

Planned instance layout:

```text
<DATA_ROOT>/instances/<instance_id>/
    instance.json
    config.json
    secrets/
        token.txt
    runtime/
    logs/
    data/
```

These files are user-owned. Program updates must never overwrite them.

- `instance.json` stores minimal metadata for the instance.
- `config.json` is mutable user configuration for that instance.
- `secrets/token.txt` stores that instance's Discord token.
- `runtime/` is reserved for process/lock/runtime state later.
- `logs/` is reserved for instance logs later.
- `data/` is reserved for persistent bot-specific data later.

Minimal instance metadata:

```json
{
  "schema_version": 1,
  "id": "admin-main",
  "bot_type": "admin",
  "display_name": "Admin Bot"
}
```

## Phase 2B boundary

Phase 2A does not add process supervision, GUI, updater, launcher, GitHub integration, packaging, or Admin Bot instance runtime selection.

Phase 2B will review how to migrate the existing Phase 1 Admin Bot runtime data into an `admin-main` instance without overwriting user-owned files.
