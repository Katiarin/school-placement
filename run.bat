@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Сначала выполните setup.bat — он создаст окружение и подготовит данные.
    pause
    exit /b 1
)

if not exist "data\meta.json" (
    echo Подготовленные данные не найдены, запускаю подготовку...
    ".venv\Scripts\python.exe" scripts\prepare_data.py
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" app.py %*
pause
