@echo off
setlocal

cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: build_windows.bat ^<version^>
    exit /b 2
)

set "DARKABYSS_VERSION=%~1"

python -m PyInstaller --clean --noconfirm packaging\DarkAbyssApp.spec
if errorlevel 1 (
    echo DarkAbyssApp build failed.
    exit /b 1
)

python -m PyInstaller --clean --noconfirm packaging\Launcher.spec
if errorlevel 1 (
    echo Launcher build failed.
    exit /b 1
)

python packaging\assemble_distribution.py --version "%DARKABYSS_VERSION%" --app-bundle dist\DarkAbyssApp --launcher dist\Launcher.exe --output dist\DarkAbyssBotManager
if errorlevel 1 (
    echo Distribution assembly failed.
    exit /b 1
)

echo Build completed.
echo Versioned app: dist\DarkAbyssApp\DarkAbyssApp.exe
echo Launcher: dist\Launcher.exe
echo Distribution: dist\DarkAbyssBotManager
