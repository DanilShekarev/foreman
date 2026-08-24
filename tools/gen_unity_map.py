"""Заготовка CODE_MAP.md для Unity-проекта: структура собирается обходом,
смысловые разделы остаются на человека.

Опциональная утилита — нужна, только если кодовая база это Unity-проект.
Корень берётся из настроек зеркала (GD_CODE_REPO / GD_CODE_PATHS), запуск:

    .venv/bin/python tools/gen_unity_map.py          # Linux/macOS
    .venv\Scripts\python.exe tools\gen_unity_map.py  # Windows

Пишет vault/CODE_MAP.md и коммитит в волт. Готовый файл дописывается руками:
скрипт знает, где что лежит, но не знает, что из этого важно.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: F401,E402  (utf-8 в консоли, .env)
from vaultlib import VAULT, code_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def game_root() -> Path | None:
    """Каталог со своим кодом: первый путь из GD_CODE_PATHS, иначе Assets/."""
    code = code_root()
    if code is None:
        return None
    first = next((p.strip() for p in os.environ.get("GD_CODE_PATHS", "").split(";")
                  if p.strip()), "")
    root = code / first if first else code / "Assets"
    return root if root.is_dir() else None


def counted_dirs(parent: Path, pattern: str) -> list[tuple[str, int]]:
    if not parent.is_dir():
        return []
    return [(d.name, sum(1 for _ in d.rglob(pattern)))
            for d in sorted(parent.iterdir()) if d.is_dir()]


def build(game: Path) -> str:
    areas = counted_dirs(game / "Scripts" / "Runtime", "*.cs")
    total = sum(n for _, n in areas)
    areas_md = ", ".join(f"`{n}` ({c})" for n, c in areas if c) or "—"
    cfg_dir = game / "Scripts" / "Runtime" / "Configs"
    cfgs = sorted(p.stem for p in cfg_dir.rglob("*.cs")) if cfg_dir.is_dir() else []
    data_md = ", ".join(f"`{n}/` ({c})" for n, c in counted_dirs(game / "Data", "*.asset"))
    rel = game.name

    cfg_md = (f"\n  Классы конфигов — `Scripts/Runtime/Configs/` ({len(cfgs)} шт.):"
              f"\n  {', '.join(cfgs)}." if cfgs else "")
    data_line = (f"\n- `Data/` — ScriptableObject-инстансы в YAML `.asset`."
                 f" Папки: {data_md}.{cfg_md}"
                 f"\n  **Цифры бери из .asset, а не из дефолтов полей C#.**"
                 if data_md else "")

    return f"""# Карта репозитория

Unity-проект. Корень кода — путь из системного промпта (**только чтение**;
это зеркало рабочей ветки, видны лишь закоммиченные изменения). Пути ниже —
относительно корня.

Структурная часть сгенерирована `tools/gen_unity_map.py`; разделы
«Архитектура», «Ключевые системы» и «Договорённости» заполните руками —
именно они экономят агенту больше всего времени.

## Где что лежит

Свой код и контент — в `{rel}/`:

- `Scripts/Runtime/<Область>` — прикладной код ({total} файлов).
  Области: {areas_md}.
- `Scripts/Editor` — редакторский код.{data_line}
- `Prefabs/`, `Scenes/` — сериализованные настройки тоже там.

## Архитектура

_Заполните: как связываются системы (DI-контейнер и его точка сборки),
как устроен мультиплеер, где искать «вход» в незнакомую подсистему,
есть ли partial-классы и другие неочевидные соглашения._

## Ключевые системы

_Заполните таблицей «система → файлы». Самый полезный раздел карты._

| Система | Где |
|---|---|
|  |  |

## Куда НЕ ходить

Вендорные и сгенерированные каталоги: `Assets/ThirdParty`, `Assets/Plugins`,
`Library/`, `Temp/`, `Logs/`, а также папки купленных ассетов — перечислите свои.

## Документация в репозитории

_Есть ли она, актуальна ли. Если старая документация врёт — напишите об этом
прямо: агент иначе будет на неё опираться._
"""


def main() -> int:
    game = game_root()
    if game is None:
        print("не найден корень кода: задайте GD_CODE_REPO (и GD_CODE_PATHS)")
        return 1
    text = build(game)
    target = VAULT / "CODE_MAP.md"
    if not VAULT.is_dir():
        print(f"нет волта: {VAULT}")
        return 1
    target.write_text(text, encoding="utf-8", newline="\n")
    if (VAULT / ".git").is_dir():
        subprocess.run(["git", "-C", str(VAULT), "add", "CODE_MAP.md"],
                       check=True, capture_output=True)
        r = subprocess.run(["git", "-C", str(VAULT), "commit", "-m",
                            "перегенерирована карта репозитория"],
                           capture_output=True)
        print("коммит в волт" if r.returncode == 0 else "в волте без изменений")
    print(f"готово: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
