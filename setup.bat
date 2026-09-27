@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

python DarkAbyss_Core\app_paths.py
if errorlevel 1 (
    echo User data initialization failed.
    pause
    exit /b 1
)

echo Setup finished. Edit the generated admin token file shown above, then run Admin.bat.
pause
