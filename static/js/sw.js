/* Service worker: lets the room install as an app and keeps its own files available offline.
   It NEVER caches /api/ or the pages themselves (they hold your messages and login) -- only static files. */
const CACHE = "room-static-v2-irc";
const SHELL = ["/offline"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {                       // stale-while-revalidate for CSS, JS, icons
    e.respondWith(caches.open(CACHE).then(async (c) => {
      const hit = await c.match(req);
      const fresh = fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; }).catch(() => hit);
      return hit || fresh;
    }));
    return;
  }
  if (req.mode === "navigate") {                                   // pages: always the network; a friendly page if it is down
    e.respondWith(fetch(req).catch(() => caches.match("/offline")));
  }
});
