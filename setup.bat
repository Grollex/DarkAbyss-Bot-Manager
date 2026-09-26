@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo Setup finished. Put the token into DarkAbyss_Core\admin_bot_token.txt, then run Admin.bat
pause
