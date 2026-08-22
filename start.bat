@echo off
rem Foreman: первый запуск сам создаёт venv и ставит зависимости.
rem Настройки — в файле .env (образец: .env.example).
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo [setup] создаю venv...
    py -3.10 -m venv .venv || python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

.venv\Scripts\python.exe server.py
pause
