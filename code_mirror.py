"""Зеркало кодовой базы проекта для агента.

Агент не должен видеть рабочую копию программистов: там незакоммиченные правки
и, возможно, другая ветка. Поэтому держим отдельный клон (data/code-mirror),
где выкачаны только каталоги из GD_CODE_PATHS (+ файлы корня — их cone-режим
sparse-checkout включает всегда), и раз в GD_MIRROR_SYNC_MIN минут подтягиваем
ветку GD_CODE_BRANCH.

История зеркалу не нужна (агент читает только текущий срез, откаты — забота
git-репозитория волта), поэтому клон мелкий с двух сторон: --depth 1 (один
коммит) и --filter=blob:none (блобы качаются только под sparse-пути при
checkout). Источник указан как file:// — с голым локальным путём git включает
хардлинк-режим и молча игнорирует --depth. fetch читает у рабочей копии только
refs и объекты — человеку, работающему в ней, это не мешает.

Партиальный fetch требует в ИСХОДНОМ репозитории двух локальных опций:
    git config uploadpack.allowFilter true
    git config uploadpack.allowAnySHA1InWant true
Без них .git зеркала распухнет до полного снапшота.

GD_CODE_REPO не задан — зеркала нет вовсе: сервер не запускает синк, а агент
работает только с волтом.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from vaultlib import CODE_MIRROR, CODE_REPO

BRANCH = os.environ.get("GD_CODE_BRANCH", "main")
SYNC_MINUTES = int(os.environ.get("GD_MIRROR_SYNC_MIN", "10"))
# Пусто — выкачиваем репозиторий целиком, без sparse-checkout.
SPARSE_DIRS = [p.strip() for p in
               os.environ.get("GD_CODE_PATHS", "").split(";") if p.strip()]

_lock = asyncio.Lock()

_GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C.UTF-8"}


class MirrorError(Exception):
    pass


async def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=_GIT_ENV)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise MirrorError(f"git {' '.join(args)} -> {proc.returncode}: {text[:600]}")
    return text


def checked_out(mirror: Path) -> bool:
    """Клонируем с --no-checkout, поэтому сразу после clone HEAD уже есть, а
    файлов ещё нет — по одному HEAD «свежесть» не определить."""
    try:
        return any(p.name != ".git" for p in mirror.iterdir())
    except OSError:
        return False


async def sync_once() -> str:
    """Создать зеркало при первом вызове, затем подтянуть ветку. Возвращает
    короткий хеш HEAD зеркала."""
    if CODE_REPO is None:
        raise MirrorError("GD_CODE_REPO не задан — зеркало кода выключено")
    async with _lock:
        if not (CODE_REPO / ".git").is_dir():
            raise MirrorError(f"нет git-репозитория: {CODE_REPO}")
        if not (CODE_MIRROR / ".git").is_dir():
            CODE_MIRROR.parent.mkdir(parents=True, exist_ok=True)
            await _git("clone", "--depth", "1", "--filter=blob:none",
                       "--no-checkout", "--branch", BRANCH,
                       CODE_REPO.as_uri(), str(CODE_MIRROR))
            if SPARSE_DIRS:
                await _git("sparse-checkout", "set", "--cone", *SPARSE_DIRS,
                           cwd=CODE_MIRROR)
        await _git("fetch", "--depth", "1", "origin", BRANCH, cwd=CODE_MIRROR)
        head = (await _git("rev-parse", "HEAD", cwd=CODE_MIRROR)).strip() \
            if checked_out(CODE_MIRROR) else ""
        target = (await _git("rev-parse", "FETCH_HEAD", cwd=CODE_MIRROR)).strip()
        if head != target:
            await _git("checkout", "-f", "--detach", "FETCH_HEAD", cwd=CODE_MIRROR)
            # Старые шаллоу-паки копятся — прибираем по мере надобности.
            await _git("gc", "--auto", "--quiet", cwd=CODE_MIRROR)
        return target[:10]


async def loop() -> None:
    while True:
        try:
            sha = await sync_once()
            print(f"[mirror] код {BRANCH} @ {sha}")
        except MirrorError as err:
            print(f"[mirror] синк не удался: {err}")
        except Exception as err:  # noqa: BLE001 — цикл не должен умирать
            print(f"[mirror] {type(err).__name__}: {err}")
        await asyncio.sleep(SYNC_MINUTES * 60)
