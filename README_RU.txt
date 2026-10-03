Discord Admin Bot, Discord-only edition

Что это:
- Админ-бот Discord без OpenCode, shell, subprocess и чтения чужих файлов.
- Команда `/execute` выполняет только белый список действий через Discord API.
- Опциональный AI-помощник (Groq/Gemini через Manager -> AI Providers): команда `/ai` и, при желании, отдельный AI control channel. AI использует те же белые списки Admin Tool действий и всегда спрашивает подтверждение для изменяющих и опасных действий. Без настроенного AI бот и `/execute` работают как раньше.
- Бот не умеет запускать команды Windows, читать диск, трогать браузер, ключи, проекты или локальные данные.

## Быстрый запуск

1. Запустить Manager:
   `python DarkAbyss_Core\manager_gui.py`
   или готовую программу:
   `dist\DarkAbyssBotManager\Launcher.exe`
2. Нажать `Add Bot` или выбрать уже созданный `admin-main`.
3. Нажать `Setup Bot` и пройти пошаговый мастер внутри GUI.
4. Мастер поможет:
   - задать локальное имя бота в Manager;
   - открыть Discord Developer Portal;
   - вставить Discord bot token;
   - включить и отметить Server Members Intent;
   - настроить доступ через administrators, user IDs и role IDs;
   - отдельно настроить AI-доступ (AI allowed user IDs / AI allowed role IDs) и, при желании, AI control channel ID;
   - сгенерировать invite link с нужными granular permissions;
   - выбрать режим установки Discord application:
     - `Public Bot = OFF`: установить может только owner/developer team;
     - `Public Bot = ON`: другой владелец сервера может открыть Manager-generated invite link;
     - этот выбор не меняет DarkAbyss whitelist/access/runtime behavior;
   - запустить бота кнопкой `Save && Start Bot`.
5. Чтобы открыть сырой JSON-конфиг, используй `Advanced JSON...`; обычная настройка этого не требует.

Настоящий токен хранится только в `secrets\token.txt` выбранного экземпляра. Не вставляй токен в файлы программы.

## Минимальный GUI менеджер

Для разработки можно запустить GUI:

```bat
python DarkAbyss_Core\manager_gui.py
```

GUI управляет Bot Instances через Manager Core: показывает экземпляры, запускает/останавливает/перезапускает их, создаёт ботов любого типа (Bots -> Add Bot: Admin Bot или Game Presence Bot — у каждого свой Discord-токен, процесс, настройки, данные и AI-ключи) и редактирует JSON overrides через ConfigStore.

Интерфейс (тёмная тема, боковое меню):
- Dashboard — статус бота (Online определяется по логу подключения к Discord), карточки Discord / AI Providers / Commands / AI Routing, панель бота (Start/Stop, логи, настройки), AI-провайдеры с Test Connection, лента последних событий и быстрые действия.
- Bots — таблица экземпляров и все прежние кнопки (Start, Stop, Restart, Setup Bot, Advanced JSON, Add Bot).
- AI Providers — подключения к провайдерам, базовый набор и выбор у каждого бота (подробно ниже). У каждого подключения — расход за сегодня по данным самого API, например «GPT-OSS 120B · 18 req · 41k tok / 34k in / 6.8k out · TPM 6.1k left» или «Gemini 3.8 Flash · 11 req · 27k tok / 19k in / 5k out / 3k think». На дашборде — подключения выбранного бота и его собственный расход. Остаток лимита (TPM/RPD) показывается только если Groq прислал его в заголовках и окно ещё не сбросилось; Gemini остаток не сообщает — он не показывается. Нет данных — «No usage yet». Запросом считается каждый ответ API, реально полученный ключом: повторы (retry), ответы 429/503 и Test Connection тоже; неуспешные показываются как «(N failed)». Токены берутся только из ответа провайдера и не придумываются. Статистика бота хранится в `instances\<id>\data\ai_usage.json`, Test Connection — в `config\ai_usage_manager.json` (без текстов запросов, ответов и ключей).
- Game Presence — настройки Game Presence ботов (сервер, канал, задержки, кулдауны, пауза), их статус и обязательные intents.
- AI Terminal — пиши задачи боту прямо из программы: выбери бота, сервер и (по желанию) канал, который будет «здесь». Ты действуешь как оператор бота (владелец токена): ограничения ролей Discord не действуют, но план и опасные действия всё равно подтверждаются кнопками прямо в терминале. Бот должен быть запущен. Связь идёт через файлы в папке `runtime` экземпляра — бот не открывает сетевых портов.
- Группы ботов: на странице Bots вкладки «All bots» и группы (New Group / Move to Group / Remove Group) — например, по одному боту на человека или сервер. Группы хранятся только в Manager (`config\manager_groups.json`) и не меняют самих ботов.
- Commands & Tools — slash-команды и все инструменты ИИ по категориям с уровнем риска.
- Logs — хвост логов выбранного бота и кнопка открытия папки логов.

Кнопка `Setup Bot` открывает понятный мастер настройки выбранного экземпляра. Для Admin-бота: Discord bot token, allowed user IDs, allowed role IDs, audit channel ID и режим доступа для server administrators. Для Game Presence бота: имя, токен его собственного приложения, Presence + Server Members intents и приглашение (только View Channels и Send Messages). Рядом с каждым важным полем есть кнопка `ⓘ`: она показывает, где взять bot token, user ID, role ID или channel ID и что означает настройка.

Существующий токен не показывается обратно; если поле token оставить пустым, сохранённый токен не меняется. Токены остаются в `instances\<instance_id>\secrets\token.txt` и не записываются в файлы программы.

Если GUI владеет запущенными процессами, закрытие окна требует остановить управляемые экземпляры или отменить закрытие. В Phase 5A режим "оставить запущенными после закрытия GUI" ещё не включён.

## Установка и обновления

Установка: скачай `DarkAbyssBotManager-<версия>-windows.zip` со страницы
https://github.com/Grollex/DarkAbyss-Bot-Manager/releases, распакуй в постоянную папку (например
`%LOCALAPPDATA%\Programs\DarkAbyssBotManager` или `D:\Apps\DarkAbyssBotManager`; не в `Program Files` —
программе нужно право записи в свою папку для обновлений) и запускай `Launcher.exe`.

Обновления: установленная программа сама проверяет GitHub Releases через несколько секунд после запуска и
затем каждые 6 часов (кнопка `Check for updates` внизу бокового меню проверяет сразу). Если вышла новая
версия, появляется кнопка `⬆ Update to v…`. После подтверждения Manager:

1. скачивает `darkabyss-release-<версия>.zip`, проверяет SHA-256 и `release.json` каждого файла;
2. ставит новую версию рядом с текущей (`versions\<версия>`) и переключает `current.json`;
3. только после этого останавливает запущенных ботов, перезапускается через `Launcher.exe`, и новая
   версия сама запускает тех же ботов снова.

Если скачивание или проверка не удались, ничего не меняется и текущая версия продолжает работать.
Хранятся текущая и предыдущая версии (для отката), более старые удаляются.

Твои данные обновление не трогает: боты, токены, AI-подключения и ключи, настройки, группы, логи и
состояние лежат в `%LOCALAPPDATA%\DarkAbyssBotManager`, а обновление содержит только файлы программы.
Новые типы ботов приходят вместе с программой (`bots\<тип>\manifest.json`) и появляются в `Add Bot`;
старые экземпляры и их настройки остаются, новые поля конфигов получают значения по умолчанию.
Релизы собираются GitHub Actions на чистой машине из репозитория, поэтому личные данные в них попасть не могут.

Выпуск новой версии (для разработчика): закоммить изменения и запушь тег `v<версия>`, например

```bat
git tag v1.0.1
git push origin main v1.0.1
```

Workflow `.github\workflows\release.yml` прогонит тесты, соберёт программу и опубликует релиз; установленные
копии увидят его при следующей проверке. Версия должна быть больше текущей (1.0.1 > 1.0.0 > 1.0.0-rc1);
pre-release и draft релизы обновлением не предлагаются.

## Windows packaged runtime для разработчика

Phase 9A добавляет основу `.exe`-запуска без системного Python на машине пользователя.

Целевая структура дистрибутива:

```text
DarkAbyssBotManager\
    Launcher.exe
    current.json
    versions\<version>\
        DarkAbyssApp.exe
        release.json
        _internal\...
```

Исходный режим разработки остаётся прежним:

```bat
python DarkAbyss_Core\manager_gui.py
python DarkAbyss_Core\Admin.py --instance admin-main
```

Локальная Windows-сборка:

```bat
python -m pip install -r requirements-build.txt
build_windows.bat 0.9.0
```

Скрипт собирает PyInstaller intermediate output и затем формирует готовое дерево:

```text
dist\DarkAbyssBotManager\
    Launcher.exe
    current.json
    versions\0.9.0\
        DarkAbyssApp.exe
        release.json
        _internal\...
```

`DarkAbyssApp.exe` поддерживает `--manager` и `--bot-runner admin --instance <id>`. В packaged mode Manager Core запускает дочерние боты отдельными процессами через `DarkAbyssApp.exe --bot-runner ...`, не через `Admin.py` и не через shell.

Если нужно вручную проверить генерацию `release.json` для version directory:

```bat
python DarkAbyss_Core\release_manifest.py dist\DarkAbyssApp 0.9.0
```

Локальная сборка релизных ZIP-артефактов из уже собранного дерева:

```bat
python packaging\build_release_artifacts.py --version 0.9.0 --tag v0.9.0 --distribution dist\DarkAbyssBotManager --output dist\release-artifacts
```

Она создаёт два разных архива:

```text
dist\release-artifacts\darkabyss-release-0.9.0.zip
dist\release-artifacts\DarkAbyssBotManager-0.9.0-windows.zip
```

`darkabyss-release-<version>.zip` — update artifact для Phase 7 updater. В корне ZIP лежит payload версии напрямую: `release.json`, `DarkAbyssApp.exe`, `_internal\...`; без `Launcher.exe`, `current.json` и `versions\...` wrapper.

`DarkAbyssBotManager-<version>-windows.zip` — fresh-install artifact. Внутри него находится готовая папка `DarkAbyssBotManager\` с `Launcher.exe`, `current.json` и `versions\<version>\...`.

GitHub Actions workflow `.github\workflows\release.yml` собирает эти архивы на Windows при push тега `v*`, проверяет тесты/manifest/update-preparation/fresh-install layout и публикует только ожидаемые ZIP и `.sha256` файлы. Ручной `workflow_dispatch` только собирает и загружает workflow artifact для проверки, но не публикует GitHub Release. Discord token для сборки не нужен.

Release helper не должен писать артефакты внутрь `dist\DarkAbyssBotManager` или в его родительский каталог: output должен быть отдельным sibling-каталогом вроде `dist\release-artifacts`. Update ZIP дополнительно проверяется на совместимость с лимитом загрузки Phase 7 updater.

В пакет нельзя включать реальные токены, пользовательские config/database/logs/backups, содержимое `%LOCALAPPDATA%`, generated instances или временные update/download артефакты. Выдача токена пока остаётся ручной: токен хранится только в user-data token file, не в программе.

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

Разные экземпляры используют разные config, token, runtime, lock, logs и data-каталоги. Один процесс `Admin.py` обслуживает один выбранный экземпляр. Game Presence бот создаётся так же (`python DarkAbyss_Core\instance_store.py create game_presence gp-main` или Manager -> Add Bot) и запускается своим процессом (`python DarkAbyss_Core\GamePresence.py --instance gp-main`; в установленной программе Manager запускает `DarkAbyssApp.exe --bot-runner game_presence --instance gp-main`).

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

## AI: `/ai` и AI control channel

- AI-доступ отдельный и только явный: `ai_allowed_user_ids` / `ai_allowed_role_ids` (в Manager: Setup Bot -> Access Settings -> AI allowed user IDs / AI allowed role IDs). Discord Administrator и списки `/execute` доступ к AI НЕ дают. Пустые списки = AI не может использовать никто. Рекомендуется выдавать доступ ролью.
- `/ai prompt:<текст> mode:<routine|planner|creative>` — ответы и подтверждения видны только вызвавшему (ephemeral).
- AI control channel (необязательно): в Manager -> Setup Bot -> Access Settings -> `AI control channel ID` укажи ID одного текстового канала (`ai_control_channel_id`). В этом канале Kairo отвечает на обычные сообщения пользователей из AI-списков без `/ai`; ответы и кнопки подтверждения публичные в этом канале, но нажать Approve/Cancel может только автор запроса.
- Обращение через упоминание (Setup Bot -> «Answer when someone pings the bot or its role», поля `ai_mention_enabled` / `ai_mention_channel_ids`): напиши `@Kairo сделай ...` (пинг бота или его роли) в выбранных каналах (пусто = во всех, которые бот видит). Ответы и кнопки публичные в этом канале, нажать может только автор. Тем, у кого нет AI-доступа, бот отвечает «Сорян, но у тебя нет прав мной командовать.» (не чаще раза в минуту на человека). Нужен Message Content Intent и перезапуск бота.
- Короткая память диалога: бот помнит ~6 последних обменов одного пользователя в одном канале (до 30 минут, только в оперативной памяти; после перезапуска пусто), поэтому работают уточнения вроде «а теперь удали его». Сброс: `/ai_reset` или сообщение `сброс` / `reset` в AI control channel.
- Message Content Intent (Developer Portal -> Application -> Bot -> Privileged Gateway Intents -> Message Content Intent) запрашивается, только если заполнен `AI control channel ID` или включено «AI can read message text» в Setup Bot. Без него текст сообщений боту не виден: `purge` по тексту/ссылкам честно отказывается, а не удаляет «ничего». Для `/execute` и `/ai` intent не нужен. После изменения перезапусти бота.
- Подтверждения (Setup Bot -> AI confirmations, поле `ai_confirmation_mode`):
  - `plan` (по умолчанию): бот показывает план (шаги + какие изменения разрешены) с одной кнопкой «Approve plan»; после неё обычные действия выполняются сами. Удаления, баны, кики, purge, изменение прав ролей и прочие опасные действия всё равно спрашивают отдельно с точными данными.
  - `strict`: Approve перед каждым пакетом изменений с полным показом аргументов.
  - Чтение (READ) выполняется сразу в обоих режимах. AI-сообщения никогда не пингуют @everyone/@here/пользователей/роли.
- Если инструмент вернул ошибку (не тот ID, Discord отказал), ИИ видит причину и может исправиться (до 3 раз за запрос), а не обрывает весь запрос. Уже выполненные действия перечисляются в ответе.
- ИИ получает контекст запроса: кто спрашивает, в каком канале («сюда» = этот канал), позицию роли бота и компактный список каналов/ролей с ID. Названия каналов и ролей передаются как данные, не как инструкции.
- Сбои провайдера (лимит запросов/токенов, 5xx, сеть) повторяются автоматически с паузой, которую подсказывает сам Groq/Gemini (если она не больше 30 секунд). Таймаут запроса — 60 секунд.
- Под ответом AI мелким шрифтом показывается, какой движок ответил: провайдер · профиль · модель (для двухэтапных запросов: `plan: ... | run: ...`).

## AI-6: что умеет AI и как он работает

- Двухэтапная схема: сначала «планировщик» (Manager -> AI Providers -> вкладка Routing -> Planning) получает запрос и короткий каталог возможностей и выбирает нужные инструменты; затем «исполнитель» (Routing -> Execution) получает только эти инструменты и выполняет. Удобно: планирование на более сильной модели (например Gemini), выполнение на быстрой/дешёвой (например Groq). Можно включить запасной провайдер, если основной не ответил. Если планировщик не настроен, планирует профиль исполнения. Ключи нужны у обоих выбранных провайдеров.
- Изменения выполняются только после одобрения (см. режимы `plan`/`strict` выше). Чтение выполняется сразу.
- Возможности (только Discord-сервер, без доступа к Windows/файлам/сети хоста):
  - структура: категории, текстовые/голосовые/трибуны/форумы/анонсы, редактирование (тема, slowmode, NSFW, категория, позиция, лимит, битрейт, синхронизация прав), клонирование, удаление, приватные каналы для ролей, права каналов (allow/deny/reset для @everyone/роли/участника);
  - «чертёж сервера»: роли + категории + каналы + права одним планом (`apply_server_blueprint`), существующие объекты с тем же именем не трогаются; откат последнего чертежа (`undo_last_blueprint`);
  - роли: цвет, отображение отдельно, упоминаемость, права, позиция;
  - участники: роли, ники, массовая выдача/снятие роли (с лимитом), перемещение/отключение в голосе, server mute/deafen;
  - модерация: очистка с фильтрами, тайм-аут, кик, бан/разбан, бан-лист, журнал аудита, блокировка канала, lockdown всего сервера и снятие;
  - настройки сервера: название, описание, иконка/баннер (из вложения), уровень верификации, уведомления, фильтр контента, системный/правил/AFK каналы, welcome screen, onboarding;
  - AutoMod: правила по словам/шаблонам/пресетам, спам, массовые упоминания;
  - сообщения: embed, редактирование сообщений бота, удаление, закрепление, реакции, опросы, публикация анонсов; ветки и посты форума, теги форума;
  - вебхуки (без показа токенов), приглашения, эмодзи и стикеры (из вложений), запланированные события;
  - постоянные функции бота: меню ролей кнопками, кнопка верификации, приветствие новичков + автороли, сообщения по расписанию (не чаще раза в 10 минут, без пингов).
- Картинки для иконки/баннера/эмодзи/стикеров: прикрепи файл к `/ai` (параметры `file`, `file2`) или к сообщению в AI control channel.
- Защита от повышения прав через AI: нельзя выдать Administrator; нельзя трогать роли и участников на уровне своей высшей роли и выше; можно выдавать только те права, которые есть у тебя самого (владелец сервера — без этих ограничений). Роли из меню ролей/верификации/авторолей/onboarding не могут иметь модераторских прав.
- `/execute`: тот же фиксированный набор из 17 действий; с AI-6.2 к нему тоже применяется защита иерархии (не владелец сервера не может через бота трогать роли/участников на уровне своей высшей роли и выше).
- Настройки постоянных функций хранятся в `data/admin_features.json` экземпляра бота.
- Новым возможностям нужны дополнительные права бота. Manager генерирует invite link с ними (без Administrator). Для уже добавленного бота: заново пройди invite link или выдай роли бота недостающие права (Manage Server, Manage Webhooks, Manage Expressions, Manage Events, Manage Threads, View Audit Log, Move/Mute/Deafen Members, Manage Nicknames и т.д.) и подними роль бота выше ролей, которыми он должен управлять.

## Game Presence: «соберитесь поиграть вместе»

Отдельный бот со своим Discord-приложением, токеном, процессом, настройками, данными и (по желанию) своими AI-ключами. Создаётся как любой бот: Manager -> Bots -> Add Bot -> тип «Game Presence Bot», затем Bot Setup (имя, токен ВТОРОГО приложения из Developer Portal, intents, приглашение). Admin-бот (Kairo) об этой функции больше ничего не знает. Оба бота работают одновременно.

- Что делает: если минимум 2 участника одного сервера несколько минут (по умолчанию 3) играют в одну игру и при этом не сидят вместе в одном голосовом канале, бот публикует ОДНО публичное сообщение в выбранном текстовом канале и пингует их, например: «@Alex @Max, вы оба уже несколько минут в Overwatch 2 и не в одном войсе. Может, соберётесь?». Если игроки присоединяются, пока идёт ожидание, все попадают в одно сообщение («вас уже трое…»). Если часть группы уже сидит в войсе, бот зовёт остальных туда (voice-aware, отключается).
- Источник данных: только Discord Presence (активность «Играет в…») и голосовые каналы сервера. Никаких внешних API.
- Перед отправкой всегда повторная проверка: все ли ещё играют, та же ли игра, не собрались ли уже в войсе, не выключил ли кто-то пинги, нет ли кулдауна.
- Анти-спам (настраивается): задержка 3 мин; та же группа + игра — 3 часа (изменение состава на одного человека не даёт нового сообщения); один человек — не чаще раза в час; сервер — не чаще раза в 15 минут. История, кулдауны и отказы от пингов хранятся в `instances\<id>\data\game_presence_state.json` Game Presence бота и переживают перезапуск. Если этот файл повреждён, бот ничего не публикует, а кнопки отвечают «недоступно» (отказы не «забываются», файл не перезаписывается). Manager -> Game Presence показывает «State file problem» с объяснением даже при остановленном боте; та же причина пишется в лог бота. Исправление: восстановить файл из резервной копии или удалить его (тогда отказы и кулдауны начнутся заново) и перезапустить бота.
- Кнопки под каждым предложением: **Mute pings** / **Allow pings**. Видны всем, но меняют настройку только того, кто нажал; ответ виден только ему. По умолчанию пинги разрешены. Замьюченных бот не упоминает; если после этого остаётся меньше двух человек — сообщения не будет.
- Упоминания формируются только кодом из ID одобренных участников; @everyone, @here и роли запрещены. Необязательный режим «AI varies the wording» меняет только формулировку: кого и когда пинговать решают правила, при сбое AI используется обычный шаблон.
- Настройка: Manager -> Game Presence -> выбрать Game Presence бота, ЯВНО выбрать сервер и канал (пока ничего не сохранено, в списках стоит «— choose a server —» / «— choose a channel —») -> Save. Без сохранённых сервера и канала бот подключён (Online), но ничего не публикует — Manager пишет «Not configured — nothing is posted». Галочка «Post suggestions» — пауза.
- Почему нет предложения: на странице Game Presence строка «Now: …» (и строка «Game Presence: …» на дашборде) показывает текущую причину — нет активности «Играет в…», в игре только один человек, идёт ожидание задержки, кто-то отключил пинги или на кулдауне, все уже в одном войсе, игра в ignore/allowlist, канал недоступен, у бота нет права Send Messages / View Channel, проблема конфига или state-файла. Строка «Discord presence data: N members · K online · M playing a game · other: …» показывает, что Discord реально присылает боту. 0 members — нет данных об участниках (Server Members Intent); все офлайн — проверить Presence Intent; люди онлайн, но 0 playing — Discord не делится их игрой: участник должен видеть у себя в этом сервере «Играет в …» под своим ником (Настройки Discord → Конфиденциальность активности → делиться активностью, это можно отключить и для отдельного сервера; игра должна определяться в «Зарегистрированные игры»). Бот видит ровно то, что видно в списке участников сервера. Проблемы и отправленные предложения пишутся в лог бота (Logs) один раз при изменении. Нужны **Presence Intent** и **Server Members Intent** у приложения этого бота (Developer Portal -> Bot -> Privileged Gateway Intents). Message Content Intent не нужен. Если intent не включён, бот остановится и в логе/статусе будет написано, какой включить.
- Приглашение Game Presence бота просит только View Channels и Send Messages.
- AI-формулировки: по умолчанию бот использует базовый набор подключений; в Manager -> AI Providers -> «AI of a bot» можно дать ему своё подключение (например отдельный Groq-ключ). Используется только подключение «Execution». Без рабочего подключения бот пишет обычные шаблоны. Инструментов Admin у него нет.
- Старые установки: если раньше Game Presence был включён в Admin-боте, Admin его больше не запускает (настройки не удаляются). Страница Game Presence покажет это и по кнопке Import перенесёт настройки (а при остановленном Game Presence боте — и отказы/кулдауны) в выбранного Game Presence бота. Автоматически новый бот не создаётся — нужен твой токен.

## AI-подключения: базовый набор и выбор у бота

- **Подключение** — один аккаунт провайдера: провайдер (Groq или Gemini), имя, API-ключ, модель, уровень reasoning. Можно несколько подключений к одному провайдеру, например «Groq main» и «Groq backup» с разными ключами. Manager -> AI Providers -> «Add Connection» / «Edit». Сохранённый ключ никогда не показывается обратно; пустое поле ключа оставляет его как есть. Ключи лежат в `secrets\ai_connections\<провайдер>\<id>.secret`, остальное — в `config\ai_connections.json` (без ключей).
- **Базовый набор** — какие подключения планируют (Planning), выполняют (Execution), запасное (Extra fallback) и «если одно не отвечает — работает другое». Его используют все боты, у которых нет своего выбора. Первое сохранённое подключение автоматически становится базовым.
- **AI of a bot** — выбери бота: «Use the base set» (по умолчанию) или «Choose connections for this bot» — свои Planning/Execution/fallback. Бот использует только выбранные для него подключения и никогда не «заимствует» другие. Боты на одном подключении делят его ключ и лимиты; для отдельных лимитов дай боту своё подключение со своим ключом.
- Изменения применяются у запущенных ботов со следующего AI-запроса, перезапуск не нужен. Подключение нельзя удалить, пока его использует базовый набор или чей-то выбор.
- При первом запуске новой версии ключи, сохранённые раньше у бота (обычно у admin-main), автоматически становятся базовым набором («Groq main», «Gemini main») с тем же роутингом; старые файлы не удаляются. Если ключи были у нескольких ботов — у каждого создаются свои подключения, а базовый набор остаётся пустым.
- Новые провайдеры в будущем: адаптер + одна запись в каталоге `ai_providers.py` — Manager и боты подхватят его сами. Сейчас доступны только Groq и Gemini.

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
