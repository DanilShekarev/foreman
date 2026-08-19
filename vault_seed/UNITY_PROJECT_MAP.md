# Карта Unity-проекта slopebuildergame

Корень кода игры — путь из системного промпта (**только чтение**; это зеркало
ветки main — видны только закоммиченные изменения). Пути ниже — относительно корня.
Unity 6, URP. Жанр: кооперативный сэндбокс от первого лица про подряды на
стройку/ремонт: инструменты, разрушение, постройка, контракты, общий банк,
магазин, хаб, квесты, рыбалка. Мультиплеер (Steam).

## Где что лежит

Весь свой код и контент — **только** в `Assets/_SlopeBuilder_/`:

- `Scripts/Runtime/<Область>` — геймплейный код, namespaces `Game.Runtime.*`
  (522 файлов). Области: `Animation` (4), `Audio` (14), `Boombox` (8), `BugReport` (5), `Camera` (7), `Cheats` (2), `CodeGen` (3), `Configs` (33), `ContractSystem` (6), `Core` (44), `Crane` (9), `Events` (3), `HubPC` (10), `Input` (2), `Mechanics` (113), `Network` (6), `Player` (40), `PurrNetModules` (1), `QuestSystem` (33), `Rendering` (5), `SaveSystem` (8), `Scopes` (7), `Services` (2), `Settings` (9), `Shredder` (7), `Tools` (57), `UI` (50), `Utils` (34).
- `Scripts/Editor` — редакторский код (`Game.Editor.*`).
- `Data/` — **баланс и все цифры**: ScriptableObject-инстансы в YAML `.asset`.
  Папки: `Audio/` (6), `Combat/` (3), `Purchasing/` (13), `Stamina/` (1), `Wallet/` (6).
  Классы конфигов — `Scripts/Runtime/Configs/` (33 шт.):
  BoosterTierConfig, BuildingMaterial, CatchTable, ContractConfig, DamageBalanceConfig, DamageType, DonationBasketConfig, FishingLootConfig, FishingRodConfig, FoodTierConfig, ItemDefinition, MaterialSoundMap, MultiPurchaseDefinition, MusicConfiguration, PurchaseActionType, PurchaseDefinition, PurchaseEntry, PurchaseLimitType, RewardDefinition, RewardFallback, RewardTable, SandboxCatalog, SandboxCategory, ShopConfig, ShopItemPool, ShopPoolEntry, ShopUnlockRequirement, SoundEffectLibrary, SoundEvent, StaminaConfig, ToolType, WeaponConfig, WeaponKind.
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
