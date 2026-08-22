"""Бэкап данных Foreman: база, волт, вложения и записи звонков.

Что делает за один прогон:
- база       -> GD_BACKUP_DIR/db/gd-ГГГГММДД-ЧЧММСС.sqlite — онлайн-копия
  через sqlite3 backup API, безопасно при работающем сервере (WAL);
- волт       -> GD_BACKUP_DIR/vault/vault-ГГГГММДД-ЧЧММСС.bundle — git bundle
  со всей историей; восстановление: git clone файл.bundle vault;
- data/uploads и data/calls -> GD_BACKUP_DIR/files/... — докопирование новых
  файлов (имена случайные, содержимое не меняется — инкремент естественный).

Старые копии базы и волта ротируются: остаются последние GD_BACKUP_KEEP
(по умолчанию 30). files/ не ротируется — там нет дублей по построению.

Бэкап на тот же диск защищает от кривого обновления и случайного удаления,
но не от смерти диска: GD_BACKUP_DIR стоит указывать на другой физический
диск или в папку, которую синхронизирует облако.

Ручной запуск (из корня проекта):
    .venv/bin/python tools/backup.py           # Linux/macOS
    .venv\\Scripts\\python.exe tools\\backup.py   # Windows

Сервер зовёт run_backup() сам раз в GD_BACKUP_HOURS часов (0 — выключено).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import common  # noqa: F401,E402  (.env, utf-8 в консоли)
import vaultlib  # noqa: E402

DB_PATH = Path(os.environ.get("GD_DB", str(ROOT / "data" / "gd.sqlite")))
BACKUP_DIR = Path(os.environ.get("GD_BACKUP_DIR",
                                 str(ROOT / "data" / "backups"))).resolve()
KEEP = int(os.environ.get("GD_BACKUP_KEEP", "30"))
FILE_DIRS = ("uploads", "calls")

_GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C.UTF-8"}


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _prune(folder: Path, keep: int) -> int:
    """Имена начинаются с метки времени — лексикографический порядок и есть
    хронологический."""
    files = sorted(p for p in folder.iterdir() if p.is_file())
    victims = files[:-keep] if keep > 0 else []
    for p in victims:
        p.unlink()
    return len(victims)


def backup_db() -> str:
    if not DB_PATH.is_file():
        return "база: нет файла, пропуск"
    out_dir = BACKUP_DIR / "db"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"gd-{_stamp()}.sqlite"
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _prune(out_dir, KEEP)
    return f"база: {target.name} ({target.stat().st_size // 1024} КБ)"


def backup_vault() -> str:
    vault = vaultlib.VAULT
    if not (vault / ".git").is_dir():
        return "волт: не git-репозиторий, пропуск"
    out_dir = BACKUP_DIR / "vault"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"vault-{_stamp()}.bundle"
    r = subprocess.run(["git", "-C", str(vault), "bundle", "create",
                        str(target), "--all"],
                       capture_output=True, env=_GIT_ENV)
    if r.returncode != 0:
        raise RuntimeError("git bundle: " +
                           r.stderr.decode("utf-8", "replace")[:300])
    _prune(out_dir, KEEP)
    return f"волт: {target.name} ({target.stat().st_size // 1024} КБ)"


def backup_files() -> str:
    copied = 0
    for name in FILE_DIRS:
        src_root = ROOT / "data" / name
        if not src_root.is_dir():
            continue
        dst_root = BACKUP_DIR / "files" / name
        for src in src_root.rglob("*"):
            if not src.is_file():
                continue
            dst = dst_root / src.relative_to(src_root)
            # Дозапись чанков идёт только в свежие .webm активного звонка;
            # завершённые файлы неизменны, поэтому сверка по размеру решает.
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return f"файлы: докопировано {copied}"


def run_backup() -> str:
    # Бэкап внутрь копируемого каталога зациклил бы докопирование на себя.
    if BACKUP_DIR == BACKUP_DIR.parent or any(
            _inside(BACKUP_DIR, ROOT / "data" / d) for d in FILE_DIRS):
        raise RuntimeError(f"подозрительный GD_BACKUP_DIR: {BACKUP_DIR}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    parts = [backup_db(), backup_vault(), backup_files()]
    return "; ".join(parts)


def _inside(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    print(f"[backup] каталог: {BACKUP_DIR}")
    print("[backup] " + run_backup())
