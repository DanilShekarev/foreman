"""Git поверх волта: история правок и откат.

Волт — отдельный репозиторий (не репо приложения). Все вызовы git — под одним
asyncio.Lock: параллельные ходы агента и правки из редактора не должны
перемешивать index. Каждая правка — свой коммит с автором-актором.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from vaultlib import VAULT

_lock = asyncio.Lock()

# Версия волта: растёт на каждом коммите (ход агента, правка в редакторе,
# загрузка, откат). Фронт видит её в /api/changes и обновляет дерево без
# перезагрузки страницы. Сброс при рестарте безвреден: клиент заметит смену
# значения и просто перечитает дерево лишний раз.
VERSION = 0


def _bump() -> None:
    global VERSION
    VERSION += 1

_GIT_ENV = {
    **os.environ,
    # Не подцеплять глобальные хуки/конфиги пользователя.
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C.UTF-8",
}


class GitError(Exception):
    pass


async def _git(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(VAULT), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=_GIT_ENV)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} -> {proc.returncode}: {text[:800]}")
    return text


def ensure_repo_sync() -> None:
    """Инициализация при старте сервера (до event loop, поэтому синхронно)."""
    import subprocess
    if (VAULT / ".git").is_dir():
        return
    VAULT.mkdir(parents=True, exist_ok=True)
    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(VAULT), *args], check=True,
                       capture_output=True, env=_GIT_ENV)
    run("init")
    run("config", "user.name", "gd-assist")
    run("config", "user.email", "gd-assist@local")
    (VAULT / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init vault", "--allow-empty")


async def commit_paths(paths: list[Path], message: str, author: str) -> str | None:
    """Закоммитить только перечисленные файлы. Возвращает хеш или None,
    если менять оказалось нечего (агент объявил Edit, но текст совпал)."""
    if not paths:
        return None
    rels = []
    for p in paths:
        try:
            rels.append(p.resolve().relative_to(VAULT).as_posix())
        except ValueError:
            continue  # вне волта (Unity read-only) — коммитить нечего
    if not rels:
        return None
    async with _lock:
        await _git("add", "--", *rels)
        status = await _git("status", "--porcelain", "--", *rels)
        if not status.strip():
            return None
        await _git("commit", "-m", message[:200],
                   f"--author={author} <{author.split()[0]}@gd-assist>")
        _bump()
        return (await _git("rev-parse", "HEAD")).strip()


async def commit_all_message(message: str, author: str,
                             paths: list[Path]) -> str | None:
    return await commit_paths(paths, message, author)


async def show(commit_hash: str) -> str:
    """Unified diff коммита (для панели «что изменил агент»)."""
    async with _lock:
        return await _git("show", "--stat", "--patch", "--no-color", commit_hash)


async def revert(commit_hash: str, author: str) -> str:
    """Откат коммита новым коммитом (история не теряется)."""
    async with _lock:
        try:
            await _git("revert", "--no-edit", "--no-commit", commit_hash)
            await _git("commit", "-m", f"revert {commit_hash[:10]}",
                       f"--author={author} <{author.split()[0]}@gd-assist>")
        except GitError:
            await _git("revert", "--abort", check=False)
            raise
        _bump()
        return (await _git("rev-parse", "HEAD")).strip()


async def file_history(rel: str, limit: int = 30) -> list[dict]:
    async with _lock:
        out = await _git("log", f"-{limit}", "--follow",
                         "--format=%H%x09%an%x09%ad%x09%s", "--date=iso-strict",
                         "--", rel, check=False)
    items = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            items.append({"hash": parts[0], "author": parts[1],
                          "date": parts[2], "subject": parts[3]})
    return items
