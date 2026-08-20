@echo off
rem GD-ассистент: первый запуск сам создаёт venv и ставит зависимости.
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo [setup] создаю venv...
    py -3.10 -m venv .venv || python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

rem 0.0.0.0 — доступ по офисной сети. Порт можно поменять через GD_PORT.
set GD_HOST=0.0.0.0
if "%GD_PORT%"=="" set GD_PORT=8010
rem Публикация за Caddy под danishe.sbs/foreman (локальный доступ без префикса
rem переменная не ломает — префикс срезается, только если реально пришёл).
if "%GD_WEB_BASE%"=="" set GD_WEB_BASE=/foreman

.venv\Scripts\python.exe server.py --host %GD_HOST% --port %GD_PORT%
pause
