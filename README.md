# DarkAbyss Bot Manager

Windows-программа для запуска и настройки Discord-ботов:

- **Admin Bot** — администрирование сервера через slash-команды и ИИ (Groq / Gemini) с подтверждением действий;
- **Game Presence Bot** — предлагает поиграть вместе, когда несколько участников запускают одну игру.

У каждого бота свой Discord-токен, процесс, настройки и данные. ИИ-подключения общие: базовый набор для всех ботов и, по желанию, свой выбор у отдельного бота.

## Установка

1. Скачай `DarkAbyssBotManager-<версия>-windows.zip` из [Releases](https://github.com/Grollex/DarkAbyss-Bot-Manager/releases/latest).
2. Распакуй в постоянную папку с правом записи (например `%LOCALAPPDATA%\Programs\DarkAbyssBotManager`, не `Program Files`).
3. Запускай `Launcher.exe`.

## Обновления

Программа сама проверяет новые релизы (при запуске и каждые 6 часов). Когда выходит новая версия, в боковом меню появляется кнопка **⬆ Update to v…**: новая версия скачивается, проверяется (SHA-256), ставится рядом с текущей, Manager перезапускается и снова запускает работавших ботов. Если что-то пошло не так, остаётся текущая версия.

Боты, токены, ИИ-ключи, настройки и логи хранятся отдельно — в `%LOCALAPPDATA%\DarkAbyssBotManager` — и обновлением не затрагиваются. В релизах только файлы программы: их собирает GitHub Actions из этого репозитория.

## Для разработчика

```bat
pip install -r requirements-build.txt
python -m unittest discover -s tests
build_windows.bat 1.0.1
```

Новый релиз: `git tag v1.0.1 && git push origin main v1.0.1` — workflow `.github/workflows/release.yml` прогоняет тесты, собирает и публикует релиз.

Подробности: [README_RU.txt](README_RU.txt), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
