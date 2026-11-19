# DarkAbyss Bot Manager

A Windows app for running and configuring several Discord bots from one place. Each bot has its own Discord token, process, settings, data and language (English or Russian). AI connections (Groq, Gemini) are shared: one default set for all bots, with an optional override per bot.

I run it on my own Discord community server.

## Bots

- **Admin Bot** (on the server it's called Kairo): moderation and server setup through slash commands and AI. The AI can only use a fixed list of actions, every plan needs approval, and risky steps need a second confirmation. There is an optional chat mode where it reads the conversation, remembers the server's inside jokes and chooses whether to answer, react or stay quiet. It goes quiet when asked.
- **Game Presence Bot** (Group Up): when several members start the same game, it suggests playing together and calls them to the voice channel where others are already playing.
- **Stream Director Bot**: for each Twitch stream it posts a card and a thread on Discord and runs highlights, challenges, polls and a recap.

## Install

1. Download `DarkAbyssBotManager-<version>-windows.zip` from [Releases](https://github.com/Grollex/DarkAbyss-Bot-Manager/releases/latest).
2. Unzip it to a permanent folder you can write to, for example `%LOCALAPPDATA%\Programs\DarkAbyssBotManager` (not `Program Files`).
3. Run `Launcher.exe`.

## Updates

When a new release is out, an **Update to v…** button appears in the sidebar. The update is checked against SHA-256 and installed next to the current version, and the bots that were running start again; if something fails, the old version stays. Tokens, AI keys, settings and logs are kept in `%LOCALAPPDATA%\DarkAbyssBotManager` and survive updates.

## Development

```bat
pip install -r requirements-build.txt
python -m unittest discover -s tests
build_windows.bat X.Y.Z
```

To publish a release: `git tag vX.Y.Z && git push origin main vX.Y.Z`. The workflow in `.github/workflows/release.yml` runs the tests, builds the package and publishes it.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In Russian: [README.ru.md](README.ru.md) and the full guide [README_RU.txt](README_RU.txt).
