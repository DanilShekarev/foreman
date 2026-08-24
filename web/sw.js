// Минимальный service worker ради установки PWA. Ничего не кэширует:
// данные живые, статику и так версионирует ?v=<mtime> с immutable-кэшем.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
