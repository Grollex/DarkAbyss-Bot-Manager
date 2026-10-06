# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).resolve()
if not (project_root / "DarkAbyss_Core").is_dir():
    project_root = project_root.parent


a = Analysis(
    [str(project_root / "DarkAbyss_Core" / "app_entry.py")],
    pathex=[str(project_root / "DarkAbyss_Core"), str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "bots"), "bots"),
        (str(project_root / "DarkAbyss_Core" / "defaults"), "DarkAbyss_Core/defaults"),
        (str(project_root / "DarkAbyss_Core" / "Admin.py"), "DarkAbyss_Core"),
        (str(project_root / "DarkAbyss_Core" / "GamePresence.py"), "DarkAbyss_Core"),
        (str(project_root / "DarkAbyss_Core" / "StreamDirector.py"), "DarkAbyss_Core"),
    ],
    hiddenimports=[
        "discord",
        "discord.ext.commands",
        "discord.ext.tasks",
        # AI-6 Admin Tool extensions are loaded by name (importlib) from admin_tools.
        "admin_tools_server",
        "admin_tools_content",
        "admin_blueprint",
        "admin_features",
        # Bot entrypoints run in-process by app_entry --bot-runner <type>.
        "Admin",
        "GamePresence",
        "game_presence",
        "game_presence_discord",
        "StreamDirector",
        "stream_director",
        "stream_director_config",
        "stream_director_store",
        "stream_director_twitch",
        "stream_director_discord",
        # Per-bot language of Discord output (catalogs are loaded on first use).
        "bot_i18n",
        "bot_i18n_ru_admin",
        "bot_i18n_ru_game_presence",
        "bot_i18n_ru_stream_director",
        # Bot event bus (Game Presence / Stream Director -> Kairo) and Kairo's Social Awareness.
        "bot_events",
        "social_awareness",
        "social_memory",
        "social_signals",
        "manager_kairo",
        # Kairo's content filter; admin_tools loads its AI tools by name.
        "content_filter",
        "admin_tools_filter",
        "manager_content_filter",
        "locked_json",
        "ai_storage",
        "ai_usage",
        "ai_connections",
        # Provider adapters are loaded by name from the ai_providers catalog.
        "ai_providers",
        "ai_groq",
        "ai_gemini",
        # Self-update from GitHub Releases (Manager).
        "app_updates",
        "github_updates",
        "update_engine",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tests",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DarkAbyssApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DarkAbyssApp",
)
