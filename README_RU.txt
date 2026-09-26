Discord Admin Bot, Discord-only edition

Что это:
- Чистый админ-бот Discord без Gemini, OpenCode, shell, subprocess и чтения чужих файлов.
- Команда /execute сохранена, но выполняет только белый список действий через Discord API.
- Бот не может запускать команды Windows, читать диск, трогать браузер, ключи, проекты или локальные данные.

Быстрый запуск:
1. Установить Python 3.11+.
2. Запустить setup.bat.
3. Вставить токен бота в DarkAbyss_Core\admin_bot_token.txt.
4. В Discord Developer Portal включить Server Members Intent.
5. Пригласить бота на сервер с granular permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Manage Messages
   - Manage Channels
   - Manage Roles
   - Moderate Members
   - Kick Members
   - Ban Members
6. Запустить Admin.bat.

Важно про права:
- Administrator не обязателен для bot account, если выданы перечисленные выше права.
- Фактическое выполнение действий всё равно зависит от Discord role hierarchy.
- Для ролей и участников роль бота должна быть выше целевой роли/участника.
- Для каналов также учитываются permissions конкретного канала.
- purge_messages требует Manage Messages и Read Message History.

Доступ:
- По умолчанию /execute доступен администраторам Discord-сервера.
- Дополнительных людей можно добавить в DarkAbyss_Core\admin_config.json:
  allowed_user_ids: Discord user IDs
  allowed_role_ids: Discord role IDs
- Если нужно запретить всем администраторам и оставить только allowlist, поставь:
  "allow_server_administrators": false
- audit_channel_id можно поставить в ID текстового канала для логов действий.
- admin_config.json валидируется строго при старте. Строки "true" и "false" не принимаются вместо boolean true/false.

Поддерживаемые действия /execute:
- send_message: channel + content
- purge_messages: channel + count
- timeout_member: member + duration_minutes
- clear_timeout: member
- kick_member: member
- ban_member: member
- unban_user: user_id
- add_role: member + role
- remove_role: member + role
- create_text_channel: name
- create_voice_channel: name
- rename_channel: channel или voice_channel + name
- delete_channel: channel или voice_channel
- create_role: name
- delete_role: role
- lock_channel: channel
- unlock_channel: channel

Параметры каналов:
- channel выбирает текстовый канал.
- voice_channel выбирает голосовой канал.
- rename_channel и delete_channel принимают text или voice channel.
- lock_channel и unlock_channel работают только с text channel, потому что меняют send_messages для @everyone.

Audit logging:
- Если audit_channel_id указан, бот отправляет лог действия в этот текстовый канал.
- Если основное действие выполнено, но audit log не отправился, действие не откатывается.
- В таком случае пользователь увидит успешный результат с предупреждением: Action completed, but audit logging failed.
- Причина ошибки audit logging выводится в консоль.

Важно:
- В боте специально нет eval, exec, subprocess, os.system, PowerShell, Gemini/OpenCode bridge и HTTP API.
- Все действия ограничены текущим Discord-сервером, где вызвана команда.
- Если Discord отказывает действию, проверь role hierarchy, права бота и permissions конкретного канала.

Git и токен:
- Скопируй DarkAbyss_Core\admin_bot_token.example.txt в DarkAbyss_Core\admin_bot_token.txt.
- Вставляй настоящий токен только в DarkAbyss_Core\admin_bot_token.txt.
- DarkAbyss_Core\admin_bot_token.txt специально не отслеживается Git и не должен попадать в коммиты.
