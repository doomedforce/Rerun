const CACHE_NAME = 'rerun-shell-v34';
const SHELL = ['./', './index.html', './calendar.html', './styles.css?v=20261021-pages', './app.js?v=20261021-pages', './manifest.json?v=20261021-pages', './favicon.svg?v=20261021-pages', './app-icon.svg?v=20261021-pages', './assets/rerun-mark.png?v=20261021-pages', './assets/rerun-logo.png?v=20261021-pages'];

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
  if (event.request.method !== 'GET' || requestUrl.origin !== self.location.origin || requestUrl.pathname.startsWith('/api/') || requestUrl.pathname.startsWith('/img/')) return;
  event.respondWith(fetch(event.request).then((response) => {
    const copy = response.clone();
    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then((cached) => cached || caches.match('./index.html'))));
});
