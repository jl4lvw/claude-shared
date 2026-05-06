/* Minimal service worker. ネットワーク優先、フォールバックでキャッシュ. */
const CACHE = "r-pwa-v20260506-191900";
const ASSETS = [
  "./",
  "./index.html",
  "./style.css",
  "./app.js",
  "./manifest.json",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  // /tasksapi/ 系は常にネットワーク（キャッシュしない）
  if (req.url.includes("/tasksapi/")) {
    return; // default network handling
  }
  e.respondWith(
    fetch(req)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return r;
      })
      .catch(() => caches.match(req).then((r) => r || Response.error()))
  );
});
