#!/usr/bin/env bash
# Foreman: первый запуск сам создаёт venv и ставит зависимости.
# Настройки — в файле .env (образец: .env.example).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "[setup] создаю venv..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python server.py
