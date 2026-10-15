/* harvest SW: статика — cache-first, state.json — network-first, офлайн — offline.html.
   CACHE bump при каждом релизе: иначе клиенты сидят на старых ассетах. */
const CACHE = "harvest-v2.4.3";
const STATIC = [
  "./",
  "./index.html",
  "./field.html",
  "./demo.html",
  "./docs.html",
  "./terminal.html",
  "./404.html",
  "./offline.html",
  "./manifest.webmanifest",
  "./assets/common.js",
  "./assets/field.js",
  "./assets/demo.js",
  "./assets/site.css",
  "./assets/terminal.css",
  "./assets/tokens.css",
  "./assets/og.png",
  "./assets/icon-192.png",
  "./assets/icon-512.png",
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
// state.json поллится с ?t=<Date.now()>: ключ кэша — без query, иначе рост.
// Кладём только r.ok — ошибки сети в кэш не пишем.
function stateKey(req) {
  const u = new URL(req.url);
  u.search = "";
  return new Request(u.toString(), { headers: req.headers });
}
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // state.json — всегда свежее, при офлайне — кэш.
  if (url.pathname.endsWith("state.json")) {
    e.respondWith(
      fetch(e.request, { cache: "no-store" })
        .then((r) => {
          if (r.ok) {
            const copy = r.clone();
            caches.open(CACHE).then((c) => c.put(stateKey(e.request), copy));
          }
          return r;
        })
        .catch(() => caches.match(stateKey(e.request)))
    );
    return;
  }
  // Навигация — офлайн-страница.
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("./offline.html")));
    return;
  }
  // Ключ кэша — без query: файлы с ?v= не плодят дубликаты.
  const cacheKey = stateKey(e.request);
  e.respondWith(caches.match(cacheKey).then((hit) => hit || fetch(e.request).then((r) => {
    if (r.ok) {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(cacheKey, copy));
    }
    return r;
  }).catch(() => caches.match(cacheKey).then((hit2) => hit2 || caches.match("./offline.html")))));
});
