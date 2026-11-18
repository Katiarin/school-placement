@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Не удалось создать окружение. Установите Python 3.12 и повторите.
        pause
        exit /b 1
    )
)

echo Устанавливаю зависимости...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости.
    pause
    exit /b 1
)

echo.
echo Подготавливаю данные из проекта практики 2...
".venv\Scripts\python.exe" scripts\prepare_data.py %*
if errorlevel 1 (
    echo.
    echo Подготовка данных не выполнена. Проверьте путь к проекту практики 2:
    echo   setup.bat --source "C:\путь\к\frunzenskiy-ai"
    pause
    exit /b 1
)

echo.
echo Готово. Запускайте run.bat
pause
