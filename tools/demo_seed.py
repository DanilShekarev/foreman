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
    (shared / "механики").mkdir(parents=True, exist_ok=True)
    (shared / "экономика").mkdir(parents=True, exist_ok=True)
    (vaultlib.VAULT / "PROJECT.md").write_text(
        "# Профиль проекта\n\n## О чём проект\n\n"
        "Кооперативная игра про ремонтную бригаду: игроки берут подряды,\n"
        "сносят старое и строят новое. Ключевые сущности: инструмент, подряд,\n"
        "общий банк, магазин.\n", encoding="utf-8")
    (shared / "механики" / "Пила.md").write_text(
        "# Пила\n\nРучной инструмент для демонтажа деревянных конструкций.\n"
        "Работает по балкам и доскам, для бетона — [[Кувалда]].\n\n"
        "| Параметр | Значение |\n|---|---|\n"
        "| Время распила балки | 2,4 с |\n| Балок в минуту | 25 |\n"
        "| Цена в магазине | 350 |\n\n"
        "Выпилы учитываются в прогрессе [[Подряды|подряда]].\n\n"
        "Источник: Mechanics/Tools/Saw.cs\n", encoding="utf-8")
    (shared / "механики" / "Кувалда.md").write_text(
        "# Кувалда\n\nТяжёлый инструмент для бетона и кирпича. Медленнее\n"
        "[[Пила|пилы]], но бьёт по площади.\n\n"
        "| Параметр | Значение |\n|---|---|\n| Урон за удар | 40 |\n"
        "| Радиус | 1,5 м |\n", encoding="utf-8")
    (shared / "экономика" / "Подряды.md").write_text(
        "# Подряды\n\nОсновной источник дохода бригады. Подряд — это объект,\n"
        "список работ и срок. Оплата падает в [[Общий банк]].\n", encoding="utf-8")
    (shared / "экономика" / "Общий банк.md").write_text(
        "# Общий банк\n\nОбщие деньги бригады: сюда падает оплата подрядов,\n"
        "отсюда оплачиваются инструменты в магазине.\n", encoding="utf-8")


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
    msg(conn, ch["id"], mark, "Собрали новый билд, время распила балки теперь 2,4 секунды вместо 3.")
    msg(conn, ch["id"], olya, "Ок, обновлю таблицу в доке пилы. @агент проверь потом цифры по коду.")
    root = msg(conn, ch["id"], demo, "Кто смотрит баланс магазина на этой неделе?")
    msg(conn, ch["id"], mark, "Я возьму, после подрядов.", thread_root_id=root["id"])
    agent_msg(conn, ch["id"],
              "Проверил: в данных проекта время распила балки — 2,4 с, в документе "
              "[[Пила]] уже актуально. Расхождений не нашёл.")

    store.kanban_create(conn, "Ребаланс цен магазина", body="Пройтись по ценам "
                        "инструментов после изменения доходности подрядов.",
                        column="doing", actor_name="Даша", created_by=demo["id"],
                        priority="high")
    store.kanban_create(conn, "Док по новому инструменту", column="todo",
                        actor_name="Оля", created_by=olya["id"])
    store.kanban_create(conn, "Обновить таблицу пилы", column="done",
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
