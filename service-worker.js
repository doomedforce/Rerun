const CACHE_NAME = 'rerun-shell-v27';
const SHELL = ['./', './index.html', './calendar.html', './styles.css', './app.js', './manifest.json', './assets/rerun-mark.png?v=20260909-mobile-logo', './assets/rerun-logo.png?v=20260909-mobile-logo', './data/screenings.json', './data/venues.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then(async (cache) => {
    const results = await Promise.allSettled(SHELL.map((asset) => cache.add(asset)));
    const failed = results.filter((result) => result.status === 'rejected');
    if (failed.length) console.warn(`Rerun shell skipped ${failed.length} unavailable asset(s)`);
  }));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const requestUrl = new URL(event.request.url);
  if (event.request.method !== 'GET' || requestUrl.origin !== self.location.origin || requestUrl.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then((response) => {
    const copy = response.clone();
    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then((cached) => cached || caches.match('./index.html'))));
});
