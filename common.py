"""Общие утилиты (порт из agent-game/common.py): UTF-8 в консоли Windows,
отключение QuickEdit, подписочная авторизация Agent SDK."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Консоль Windows обычно в cp1251: любой типографский символ в print валит
# скрипт UnicodeEncodeError. Чиним при импорте.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _disable_quickedit() -> None:
    """QuickEdit: клик мышью в окне cmd переводит его в «Выделить», print
    блокируется и замораживает весь процесс (включая asyncio и пайпы SDK)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x40) | 0x80)
    except Exception:
        pass


_disable_quickedit()


def setup_auth() -> str:
    """Подписочная авторизация Agent SDK: долгоживущий токен `claude setup-token`.
    Живые креды залогиненного Claude Code протухают, токен — нет. Файл читается
    на каждый запуск сессии агента: перевыпустил токен — сервер перезапускать
    не надо."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    for token_file in _token_paths():
        if not token_file.is_file():
            continue
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
            return f"авторизация: токен из {token_file}"
    if env:
        return "авторизация: токен из окружения"
    return ("авторизация: auth_token.txt не найден — CLI попробует свои креды; "
            "если протухли, выполните `claude setup-token` и положите вывод в "
            f"{ROOT / 'auth_token.txt'}")


def _token_paths() -> list[Path]:
    """Где искать токен: явный путь из окружения, свой файл, затем общий файл
    agent-game — токен один на все агентные проекты, секрет не дублируем."""
    paths = []
    explicit = os.environ.get("GD_CLAUDE_TOKEN_FILE")
    if explicit:
        paths.append(Path(explicit))
    paths.append(ROOT / "auth_token.txt")
    paths.append(Path(r"D:\Agent\agent-game\auth_token.txt"))
    return paths
