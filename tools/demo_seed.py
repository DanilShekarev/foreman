"""Демо-инсталляция для скриншотов README: отдельные база и волт во временной
папке, выдуманная команда, наполненные чаты, волт и доска.

Боевые данные не трогаются: пути подменяются переменными окружения ДО импорта
модулей. Запуск (из корня проекта):

    .venv/bin/python tools/demo_seed.py           # Linux/macOS
    .venv\Scripts\python.exe tools\demo_seed.py   # Windows

Скрипт печатает готовую команду запуска сервера на порту 8011 — выполните её
и снимайте скриншоты под логином demo / demo1234 (админ).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

NL = chr(10)
SEP = chr(92) if os.name == "nt" else "/"

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
DEMO.mkdir(parents=True, exist_ok=True)
ENV_FILE = DEMO / "demo.env"
if not ENV_FILE.is_file():
    ENV_FILE.write_text(f"GD_DB={DEMO / 'gd.sqlite'}" + NL +
                        f"GD_VAULT={DEMO / 'vault'}" + NL +
                        "GD_PORT=8012" + NL, encoding="utf-8")
# Собственный env-файл вместо .env установки: демо не должно ни видеть
# боевые пути, ни зависеть от возможности выставить пустую переменную в cmd.
os.environ["GD_ENV_FILE"] = str(ENV_FILE)
os.environ["GD_DB"] = str(DEMO / "gd.sqlite")
os.environ["GD_VAULT"] = str(DEMO / "vault")
os.environ["GD_CODE_REPO"] = ""

sys.path.insert(0, str(ROOT))
import common  # noqa: F401,E402
import gitops  # noqa: E402
import store  # noqa: E402
import vaultlib  # noqa: E402


def msg(conn, chat_id, user, text, **kw):
    return store.add_message(conn, chat_id, author_kind="user",
                             author_user_id=user["id"],
                             author_name=user["display_name"], text=text, **kw)


def agent_msg(conn, chat_id, text):
    return store.add_message(conn, chat_id, author_kind="agent",
                             author_user_id=None, author_name="Агент", text=text)


def seed_vault():
    shared = vaultlib.VAULT / "shared"
    (shared / "фичи").mkdir(parents=True, exist_ok=True)
    (shared / "api").mkdir(parents=True, exist_ok=True)
    (vaultlib.VAULT / "PROJECT.md").write_text("""# Профиль проекта

## О чём проект

B2B-сервис аналитики: веб-приложение (FastAPI + React). Ключевые
сущности: воркспейс, отчёт, дашборд, тариф. Готовим публичный API.
""", encoding="utf-8", newline="\n")
    (shared / "api" / "Лимиты API.md").write_text("""# Лимиты API

Ограничения запросов к публичному API по тарифам. Превышение — 429
и заголовок Retry-After. Планы — в [[Тарифы]].

| Тариф | Запросов в минуту | Всплеск |
|---|---|---|
| Free | 20 | 40 |
| Pro | 120 | 240 |
| Team | 600 | 1200 |

Лимит считается на воркспейс, а не на пользователя.

Источник: src/api/ratelimit.py
""", encoding="utf-8", newline="\n")
    (shared / "фичи" / "Тарифы.md").write_text("""# Тарифы

Три плана; апгрейд мгновенный, даунгрейд — со следующего периода.
Ограничения API — в [[Лимиты API]].

| План | Цена | Дашбордов | Участников |
|---|---|---|---|
| Free | 0 | 3 | 2 |
| Pro | $29 | 30 | 10 |
| Team | $99 | без лимита | 50 |
""", encoding="utf-8", newline="\n")
    (shared / "фичи" / "Авторизация.md").write_text("""# Авторизация

Вход по magic-link на почту и через Google OAuth. Пароль не храним вовсе.

| Параметр | Значение |
|---|---|
| TTL сессии | 30 дней |
| TTL magic-link | 15 минут |
| Попыток ввода кода | 5 |

Лимиты на эндпоинты входа — общие, см. [[Лимиты API]].

Источник: src/api/auth.py
""", encoding="utf-8", newline="\n")


def main() -> int:
    if (DEMO / "gd.sqlite").exists():
        print(f"уже существует: {DEMO} — удалите папку, если нужен свежий сид")
        return 1
    seed_vault()
    gitops.ensure_repo_sync()

    conn = store.connect()
    store.init_db(conn)
    demo = store.create_user(conn, "demo", "demo1234", "Даша", is_admin=True)
    mark = store.create_user(conn, "mark", "demo1234", "Марк")
    olya = store.create_user(conn, "olya", "demo1234", "Оля")
    vaultlib.ensure_layout([u["login"] for u in store.list_users(conn)])

    ch = store.create_channel(conn, demo, "Общий", False,
                              [mark["id"], olya["id"]])
    msg(conn, ch["id"], mark, "Выкатил новый rate limit: на Pro теперь 120 запросов в минуту вместо 100.")
    msg(conn, ch["id"], olya, "Ок, обновлю таблицу в доке лимитов. @агент проверь потом цифры по коду.")
    root = msg(conn, ch["id"], demo, "Кто смотрит воронку онбординга на этой неделе?")
    msg(conn, ch["id"], mark, "Я возьму, после релиза API.", thread_root_id=root["id"])
    agent_msg(conn, ch["id"],
              "Проверил: в конфиге ratelimit для Pro — 120/мин, всплеск 240. "
              "Документ [[Лимиты API]] уже актуален, расхождений не нашёл.")

    store.kanban_create(conn, "Пересмотреть тарифную сетку", body="Сравнить "
                        "лимиты с конкурентами и обновить [[Тарифы]].",
                        column="doing", actor_name="Даша", created_by=demo["id"],
                        priority="high")
    store.kanban_create(conn, "Док по вебхукам", column="todo",
                        actor_name="Оля", created_by=olya["id"])
    store.kanban_create(conn, "Обновить таблицу лимитов API", column="done",
                        actor_name="Марк", created_by=mark["id"])

    conn.close()
    py = ".venv" + SEP + ("Scripts" + SEP + "python.exe" if os.name == "nt"
                          else "bin" + SEP + "python")
    print("демо готово. Запуск (из корня проекта):")
    if os.name == "nt":
        print(f"  set GD_ENV_FILE={ENV_FILE}")
        print(f"  {py} server.py")
    else:
        print(f"  GD_ENV_FILE=" + chr(39) + str(ENV_FILE) + chr(39) + f" {py} server.py")
    print("адрес: http://127.0.0.1:8012  вход: demo / demo1234")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
