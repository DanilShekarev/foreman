"""Создать пользователя из консоли (админ-UI появится в фазе 2).

    .venv\Scripts\python.exe add_user.py <login> <пароль> [Имя Фамилия] [--admin]

Работает и при запущенном сервере (SQLite в WAL). Личный чат с агентом и
personal-папка волта создаются сразу.
"""
from __future__ import annotations

import sys

import common  # noqa: F401  (utf-8 в консоли Windows)
import store
import vaultlib


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--admin"]
    is_admin = "--admin" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return 1
    login, password = args[0], args[1]
    display_name = " ".join(args[2:])
    conn = store.connect()
    try:
        store.init_db(conn)
        user = store.create_user(conn, login, password, display_name,
                                 is_admin=is_admin)
        vaultlib.ensure_layout([user["login"]])
        print(f"создан {user['login']} ({user['display_name']})"
              + (" [админ]" if is_admin else ""))
        return 0
    except store.StoreError as err:
        print(f"ошибка: {err}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
