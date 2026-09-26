@echo off
title Discord Admin Bot - Discord Only
cd /d "%~dp0"

:loop
echo [%date% %time%] Starting Discord-only Admin Bot...
python DarkAbyss_Core\Admin.py
echo [%date% %time%] Bot stopped or crashed. Restarting in 10 seconds...
timeout /t 10
goto loop
