"""Зеркало ветки main Unity-проекта для агента.

Агент не должен видеть рабочую копию программистов: там незакоммиченные правки
и, возможно, другая ветка. Поэтому держим отдельный клон (data/unity-mirror),
где выкачан только Assets/_SlopeBuilder_ (+ файлы корня — их cone-режим
sparse-checkout включает всегда), и раз в GD_MIRROR_SYNC_MIN минут подтягиваем
local main.

История зеркалу не нужна (агент читает только текущий срез, откаты — забота
git-репозитория волта), поэтому клон мелкий с двух сторон: --depth 1 (один
коммит) и --filter=blob:none (блобы качаются только под sparse-пути при
checkout). Источник указан как file:// — с голым локальным путём git включает
хардлинк-режим и молча игнорирует --depth. fetch читает у рабочей копии только
refs и объекты — человеку, работающему в ней, это не мешает.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from vaultlib import UNITY_MIRROR, UNITY_PROJECT

BRANCH = os.environ.get("GD_UNITY_BRANCH", "main")
SYNC_MINUTES = int(os.environ.get("GD_MIRROR_SYNC_MIN", "10"))
SPARSE_DIRS = ["Assets/_SlopeBuilder_"]

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


async def sync_once() -> str:
    """Создать зеркало при первом вызове, затем подтянуть main. Возвращает
    короткий хеш HEAD зеркала."""
    async with _lock:
        if not (UNITY_PROJECT / ".git").is_dir():
            raise MirrorError(f"нет git-репозитория: {UNITY_PROJECT}")
        if not (UNITY_MIRROR / ".git").is_dir():
            UNITY_MIRROR.parent.mkdir(parents=True, exist_ok=True)
            src_url = "file:///" + str(UNITY_PROJECT).replace("\\", "/")
            await _git("clone", "--depth", "1", "--filter=blob:none",
                       "--no-checkout", "--branch", BRANCH,
                       src_url, str(UNITY_MIRROR))
            await _git("sparse-checkout", "set", "--cone", *SPARSE_DIRS,
                       cwd=UNITY_MIRROR)
        await _git("fetch", "--depth", "1", "origin", BRANCH, cwd=UNITY_MIRROR)
        head = (await _git("rev-parse", "HEAD", cwd=UNITY_MIRROR)).strip() \
            if (UNITY_MIRROR / "Assets").is_dir() else ""
        target = (await _git("rev-parse", "FETCH_HEAD", cwd=UNITY_MIRROR)).strip()
        if head != target:
            await _git("checkout", "-f", "--detach", "FETCH_HEAD", cwd=UNITY_MIRROR)
            # Старые шаллоу-паки копятся — прибираем по мере надобности.
            await _git("gc", "--auto", "--quiet", cwd=UNITY_MIRROR)
        return target[:10]


async def loop() -> None:
    while True:
        try:
            sha = await sync_once()
            print(f"[mirror] unity {BRANCH} @ {sha}")
        except MirrorError as err:
            print(f"[mirror] синк не удался: {err}")
        except Exception as err:  # noqa: BLE001 — цикл не должен умирать
            print(f"[mirror] {type(err).__name__}: {err}")
        await asyncio.sleep(SYNC_MINUTES * 60)
