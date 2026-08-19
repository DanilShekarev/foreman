"""Перегенерация vault/UNITY_PROJECT_MAP.md по текущему состоянию Unity-проекта.

Структурная часть (области кода, конфиги, данные) собирается обходом проекта,
конвенции и таблица ключевых систем — курируются руками прямо здесь. Запуск:

    .venv\Scripts\python.exe tools\gen_unity_map.py

Пишет в волт (и коммитит), а также обновляет vault_seed/ для свежих установок.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: F401,E402  (utf-8 в консоли)
from vaultlib import UNITY_PROJECT, VAULT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GAME = UNITY_PROJECT / "Assets" / "_SlopeBuilder_"


def area_counts() -> list[tuple[str, int]]:
    runtime = GAME / "Scripts" / "Runtime"
    out = []
    for d in sorted(runtime.iterdir()):
        if d.is_dir():
            out.append((d.name, sum(1 for _ in d.rglob("*.cs"))))
    return out


def config_classes() -> list[str]:
    cfg = GAME / "Scripts" / "Runtime" / "Configs"
    return sorted(p.stem for p in cfg.rglob("*.cs")) if cfg.is_dir() else []


def data_folders() -> list[tuple[str, int]]:
    data = GAME / "Data"
    out = []
    if data.is_dir():
        for d in sorted(data.iterdir()):
            if d.is_dir():
                out.append((d.name, sum(1 for _ in d.rglob("*.asset"))))
    return out


def build() -> str:
    areas = area_counts()
    total = sum(n for _, n in areas)
    areas_md = ", ".join(f"`{name}` ({n})" for name, n in areas if n)
    cfgs = config_classes()
    data_md = ", ".join(f"`{name}/` ({n})" for name, n in data_folders())
    return f"""# Карта Unity-проекта slopebuildergame

Корень кода игры — путь из системного промпта (**только чтение**; это зеркало
ветки main — видны только закоммиченные изменения). Пути ниже — относительно корня.
Unity 6, URP. Жанр: кооперативный сэндбокс от первого лица про подряды на
стройку/ремонт: инструменты, разрушение, постройка, контракты, общий банк,
магазин, хаб, квесты, рыбалка. Мультиплеер (Steam).

## Где что лежит

Весь свой код и контент — **только** в `Assets/_SlopeBuilder_/`:

- `Scripts/Runtime/<Область>` — геймплейный код, namespaces `Game.Runtime.*`
  ({total} файлов). Области: {areas_md}.
- `Scripts/Editor` — редакторский код (`Game.Editor.*`).
- `Data/` — **баланс и все цифры**: ScriptableObject-инстансы в YAML `.asset`.
  Папки: {data_md}.
  Классы конфигов — `Scripts/Runtime/Configs/` ({len(cfgs)} шт.):
  {", ".join(cfgs)}.
  **Цифры бери из .asset, а не из дефолтов полей C#.**
- `Prefabs/` (по типам: Player, Tools, Shop, Destructables, Gameplay, Levels,
  Modules, UI) и `Scenes/` — сериализованные настройки менеджеров тоже там.

## Архитектура

- **DI — VContainer.** `Scripts/Runtime/Scopes/GameplayScope.cs` — карта всех
  геймплейных менеджеров (сериализует ссылки почти на всё); `BootstrapScope.cs`,
  `MainMenuScope.cs`, `GameplayFlow.cs`. Начинай разбор любой системы с
  GameplayScope.
- **Мультиплеер — PurrNet.** Логика server-authoritative: смотри, на какой
  стороне выполняется метод ([ServerRpc]/[ObserversRpc], SyncVar).
  `NetworkAssets.asset` — реестр сетевых префабов.
- **Partial-классы у больших систем** (PlayerRagdoll.*.cs, PlayerPidSpring.*.cs,
  FoamNetwork…) — ища поведение, проверяй соседние файлы с тем же префиксом.
- События — MessagePipe, async — UniTask, ввод — Input System, камера —
  Cinemachine 3.

## Ключевые системы (примеры файлов)

| Система | Где |
|---|---|
| Движение игрока | `Scripts/Runtime/Player/PlayerMovement.cs`, `PlayerRoot.cs`, `PlayerNet.cs` |
| Здоровье/стамина | `Player/Health.cs`, `Player/Stamina.cs`, `Player/Revive.cs` |
| Рэгдолл | `Player/Ragdoll/` (partial-файлы) |
| Стройка | `Mechanics/Building/` — `BuildingBodyGraph.cs`, `Construction/ConstructionArea.cs`, `Sandbox/GhostPlacer.cs` |
| Разрушение | `Mechanics/Destruction/ProceduralFractureDestructable.cs`, `Destructable.cs`, `IHittable.cs` |
| Экономика | `Mechanics/Wallet/Bank.cs` (сетевой банк с леджером), `RewardResolver.cs`, `WorkResultsController.cs` |
| Магазин | `Mechanics/Shop/ShopStockController.cs`, `ShopCheckoutBasket.cs`, `ShopSecurity.cs` |
| Покупки | `Mechanics/Purchasing/` |
| Контракты/работа | `ContractSystem/ContractsManager.cs`, `Mechanics/Work/WorkTimer.cs` |
| Квесты/планшет | `QuestSystem/QuestManager.cs`, `QuestSystem/Pad/Pad.cs` (namespace там `Game.Runtime.TaskSystem.Pad` — не совпадает с папкой) |
| Инструменты | `Tools/AbstractTool.cs`, `ToolsFactory.cs`, `SledgeHammer.cs`, `Saw.cs`, `NailHammer.cs`, `PaintBrush.cs`, `FoamGun.cs` + `Foam/FoamNetwork.cs`, `Dynamite.cs`, `VacuumCleaner/`, `Fishing/FishingRod.cs` |
| Гвозди/физика соединений | `Tools/NailPhysic/BuildingJointsController.cs` |
| Сохранения | `SaveSystem/SaveCoordinator.cs`, `SaveService.cs` (JSON, не конфиги) |
| Сеть/Steam | `Network/SteamManager.cs`, `Network/SandboxMode.cs`, `PurrNetModules/` |

## Куда НЕ ходить

Вендорные папки: `Assets/ThirdParty`, `Assets/Plugins`, `Bakery`, `ECM2`,
`HTraceSSGI`, `MetaVoiceChat`, `PurrLobby`, `ShadowShard`, `SplineMesh`,
`Tiny Giant Studio`, `AI Toolkit`, `TextMesh Pro`, `Assets/Valdic` (только
шейдеры/свет). `Library/`, `Temp/`, `Logs/` — сгенерированное.

Документации в проекте почти нет: `Docs/architecture.txt` — устаревший набросок
(описывает Root/Boot-архитектуру, которой в коде нет — реально VContainer),
опираться на него нельзя.
"""


def main() -> int:
    if not GAME.is_dir():
        print(f"нет проекта: {GAME}")
        return 1
    text = build()
    (ROOT / "vault_seed" / "UNITY_PROJECT_MAP.md").write_text(text, encoding="utf-8")
    target = VAULT / "UNITY_PROJECT_MAP.md"
    if VAULT.is_dir():
        target.write_text(text, encoding="utf-8")
        if (VAULT / ".git").is_dir():
            subprocess.run(["git", "-C", str(VAULT), "add", "UNITY_PROJECT_MAP.md"],
                           check=True, capture_output=True)
            r = subprocess.run(["git", "-C", str(VAULT), "commit", "-m",
                                "перегенерирована карта Unity-проекта"],
                               capture_output=True)
            print("коммит в волт" if r.returncode == 0 else "в волте без изменений")
    print(f"готово: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
