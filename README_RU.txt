Discord Admin Bot, Discord-only edition

Что это:
- Чистый админ-бот Discord без Gemini, OpenCode, shell, subprocess и чтения чужих файлов.
- Команда /execute сохранена, но выполняет только белый список действий через Discord API.
- Бот не может запускать команды Windows, читать диск, трогать браузер, ключи, проекты или локальные данные.

Быстрый запуск:
1. Установить Python 3.11+.
2. Запустить setup.bat.
3. setup.bat установит зависимости и создаст пользовательские каталоги/файлы данных.
4. Вставить токен бота в созданный файл %LOCALAPPDATA%\DarkAbyssBotManager\secrets\admin_bot_token.txt.
   Если задан DARKABYSS_DATA_DIR, файл токена находится в <DARKABYSS_DATA_DIR>\secrets\admin_bot_token.txt.
5. В Discord Developer Portal включить Server Members Intent.
6. Пригласить бота на сервер с granular permissions:
   - View Channels
   - Send Messages
   - Read Message History
   - Manage Messages
   - Manage Channels
   - Manage Roles
   - Moderate Members
   - Kick Members
   - Ban Members
7. Запустить Admin.bat.

Важно про права:
- Administrator не обязателен для bot account, если выданы перечисленные выше права.
- Фактическое выполнение действий всё равно зависит от Discord role hierarchy.
- Для ролей и участников роль бота должна быть выше целевой роли/участника.
- Для каналов также учитываются permissions конкретного канала.
- purge_messages требует Manage Messages и Read Message History.

Доступ:
- По умолчанию /execute доступен администраторам Discord-сервера.
- Дополнительных людей можно добавить в %LOCALAPPDATA%\DarkAbyssBotManager\config\admin.json:
  Если задан DARKABYSS_DATA_DIR, пользовательский конфиг находится в <DARKABYSS_DATA_DIR>\config\admin.json.
  allowed_user_ids: Discord user IDs
  allowed_role_ids: Discord role IDs
- Если нужно запретить всем администраторам и оставить только allowlist, поставь:
  "allow_server_administrators": false
- audit_channel_id можно поставить в ID текстового канала для логов действий.
- config\admin.json валидируется строго при старте. Строки "true" и "false" не принимаются вместо boolean true/false.

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

Git и пользовательские данные:
- Файлы программы можно заменять при обновлениях: пользовательские настройки, токен и runtime-файлы живут отдельно от кода.
- По умолчанию данные пользователя хранятся в %LOCALAPPDATA%\DarkAbyssBotManager\.
- Текущий layout данных:
  - config\admin.json — пользовательский конфиг админ-бота.
  - secrets\admin_bot_token.txt — настоящий токен Discord-бота.
  - runtime\admin_bot.lock — lock-файл одного запущенного экземпляра.
  - logs\ — каталог для будущих логов.
- При первом запуске config\admin.json создаётся из программного шаблона DarkAbyss_Core\defaults\admin_config.json.
- Для токена можно ориентироваться на шаблон DarkAbyss_Core\admin_bot_token.example.txt, но настоящий токен вставляй только в secrets\admin_bot_token.txt внутри пользовательских данных.
- DarkAbyss_Core\admin_bot_token.txt и DarkAbyss_Core\admin_config.json считаются legacy runtime-файлами и не отслеживаются Git.
- Если legacy config/token рядом с Admin.py уже существуют, они импортируются в новый каталог только если новых файлов ещё нет. Существующие файлы в %LOCALAPPDATA%\DarkAbyssBotManager\ никогда не перезаписываются legacy-файлами.
- Legacy token со значением PUT_DISCORD_BOT_TOKEN_HERE считается placeholder и не импортируется как настоящий секрет.
- Для разработки и тестов можно задать DARKABYSS_DATA_DIR, чтобы полностью переопределить корень пользовательских данных.
