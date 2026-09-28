@echo off
title Discord Admin Bot - Discord Only
cd /d "%~dp0"
set "INSTANCE=%~1"
if "%INSTANCE%"=="" set "INSTANCE=admin-main"

:loop
echo [%date% %time%] Starting Discord-only Admin Bot instance "%INSTANCE%"...
python DarkAbyss_Core\Admin.py --instance "%INSTANCE%"
echo [%date% %time%] Bot stopped or crashed. Restarting in 10 seconds...
timeout /t 10
goto loop
