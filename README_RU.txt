Discord Admin Bot, Discord-only edition

Что это:
- Чистый админ-бот Discord без Gemini, OpenCode, shell, subprocess и чтения чужих файлов.
- Команда `/execute` сохранена, но выполняет только белый список действий через Discord API.
- Бот не умеет запускать команды Windows, читать диск, трогать браузер, ключи, проекты или локальные данные.

## Быстрый запуск

1. Установить Python 3.11+.
2. Запустить `setup.bat`.
3. `setup.bat` установит зависимости, создаст базовый каталог пользовательских данных и подготовит экземпляр `admin-main`.
4. Вставить токен Discord-бота в файл:
   `%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-main\secrets\token.txt`
5. Если задан `DARKABYSS_DATA_DIR`, токен находится здесь:
   `<DARKABYSS_DATA_DIR>\instances\admin-main\secrets\token.txt`
6. При необходимости отредактировать конфиг:
   `%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-main\config.json`
7. Если задан `DARKABYSS_DATA_DIR`, конфиг находится здесь:
   `<DARKABYSS_DATA_DIR>\instances\admin-main\config.json`
8. В Discord Developer Portal включить `Server Members Intent`.
9. Пригласить бота на сервер с granular permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Manage Messages
   - Manage Channels
   - Manage Roles
   - Moderate Members
   - Kick Members
   - Ban Members
10. Запустить `Admin.bat`.

Настоящий токен хранится только в `secrets\token.txt` выбранного экземпляра. Не вставляй токен в файлы программы.

## Минимальный GUI менеджер

Для разработки можно запустить GUI:

```bat
python DarkAbyss_Core\manager_gui.py
```

GUI управляет Bot Instances через Manager Core: показывает экземпляры, запускает/останавливает/перезапускает их, создаёт дополнительные Admin-экземпляры и редактирует JSON overrides через ConfigStore.

Phase 5A GUI не хранит и не показывает токены. Токены остаются в `instances\<instance_id>\secrets\token.txt`.

Если GUI владеет запущенными процессами, закрытие окна требует остановить управляемые экземпляры или отменить закрытие. В Phase 5A режим "оставить запущенными после закрытия GUI" ещё не включён.

Упаковка в `.exe`, updater и GitHub-интеграция пока не реализованы.

## Экземпляры бота

Экземпляр по умолчанию: `admin-main`.

Его пользовательские файлы:

```text
%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-main\
    instance.json
    config.json
    secrets\
        token.txt
    runtime\
        admin_bot.lock
    logs\
    data\
```

Создать дополнительный экземпляр Admin Bot:

```bat
python DarkAbyss_Core\instance_store.py create admin admin-second
```

Затем отредактировать:

```text
%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-second\config.json
%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-second\secrets\token.txt
```

Запуск дополнительного экземпляра:

```bat
Admin.bat admin-second
```

или:

```bat
python DarkAbyss_Core\Admin.py --instance admin-second
```

Разные экземпляры используют разные config, token, runtime, lock, logs и data-каталоги. Один процесс `Admin.py` обслуживает один выбранный экземпляр.

## Миграция старых данных

Если `admin-main` уже существует, он считается главным источником истины. Его `config.json`, `secrets\token.txt`, metadata и пользовательские файлы не перезаписываются legacy-данными.

Если `admin-main` ещё не существует, источники миграции проверяются в таком порядке:

1. Phase 1-файлы в пользовательском `DATA_ROOT`:

```text
%LOCALAPPDATA%\DarkAbyssBotManager\config\admin.json
%LOCALAPPDATA%\DarkAbyssBotManager\secrets\admin_bot_token.txt
```

2. Более старые source-adjacent файлы рядом с программой:

```text
DarkAbyss_Core\admin_config.json
DarkAbyss_Core\admin_bot_token.txt
```

3. Если подходящих старых данных нет, создаётся `admin-main` с программным config по умолчанию и placeholder token.

Правила миграции:
- Phase 1 `DATA_ROOT` config/token имеют приоритет над source-adjacent файлами.
- Source-adjacent файлы используются только как fallback для прямого обновления со старых версий.
- Все старые source-файлы остаются на месте, не удаляются и не изменяются автоматически.
- Выбранные config/token копируются в `admin-main` с сохранением байтов там, где это практично.
- Токен `PUT_DISCORD_BOT_TOKEN_HERE`, пустой token-файл и whitespace-only token-файл не считаются настоящими credentials.
- Новые установки больше не создают Phase 1 runtime-файлы как активные файлы бота.

`DarkAbyss_Core\defaults\admin_config.json` — программный шаблон по умолчанию. Его не нужно редактировать для обычной настройки.

## Доступ

По умолчанию `/execute` доступен администраторам Discord-сервера.

Дополнительных пользователей и роли можно добавить в config выбранного экземпляра, например:

```text
%LOCALAPPDATA%\DarkAbyssBotManager\instances\admin-main\config.json
```

Поля:
- `allowed_user_ids`: Discord user IDs.
- `allowed_role_ids`: Discord role IDs.
- `allow_server_administrators`: `true` или `false`.
- `audit_channel_id`: ID текстового канала для audit-логов или `null`.

Если нужно запретить доступ всем администраторам сервера и оставить только allowlist, поставь:

```json
"allow_server_administrators": false
```

`config.json` строго валидируется при старте и при reload для `/execute`. Строки `"true"` и `"false"` не принимаются вместо boolean `true`/`false`.

## Поддерживаемые действия `/execute`

- `send_message`: channel + content
- `purge_messages`: channel + count
- `timeout_member`: member + duration_minutes
- `clear_timeout`: member
- `kick_member`: member
- `ban_member`: member
- `unban_user`: user_id
- `add_role`: member + role
- `remove_role`: member + role
- `create_text_channel`: name
- `create_voice_channel`: name
- `rename_channel`: channel или voice_channel + name
- `delete_channel`: channel или voice_channel
- `create_role`: name
- `delete_role`: role
- `lock_channel`: channel
- `unlock_channel`: channel

## Важно про права

- Administrator не обязателен для bot account, если выданы перечисленные выше granular permissions.
- Фактическое выполнение действий всё равно зависит от Discord role hierarchy.
- Для ролей и участников роль бота должна быть выше целевой роли/участника.
- Для каналов учитываются permissions конкретного канала.
- `purge_messages` требует Manage Messages и Read Message History.

## Audit logging

- Если `audit_channel_id` указан, бот отправляет лог действия в этот текстовый канал.
- Если основное действие выполнено, но audit log не отправился, действие не откатывается.
- В таком случае пользователь увидит предупреждение: `Action completed, but audit logging failed.`
- Причина ошибки audit logging выводится в консоль.

## Безопасность и Git

- В боте специально нет `eval`, `exec`, `subprocess`, `os.system`, PowerShell, Gemini/OpenCode bridge и HTTP API.
- Все действия ограничены текущим Discord-сервером, где вызвана команда.
- Пользовательские настройки, токены и runtime-файлы живут отдельно от кода.
- Настоящие токены, logs, generated instances и user data не должны попадать в Git.
- `DarkAbyss_Core\admin_bot_token.example.txt` — только шаблон с placeholder.
- `DarkAbyss_Core\admin_bot_token.txt` — legacy/local runtime-файл старых установок и не отслеживается Git.
- Для разработки и тестов можно задать `DARKABYSS_DATA_DIR`, чтобы полностью переопределить корень пользовательских данных.
