/* JT Music — офлайн-кэш
   Стратегия:
   - index.html и tracks.json — сеть в приоритете, кэш как запасной вариант
   - обложки и прочие картинки — кэш в приоритете
   - аудио не кэшируем (слишком тяжёлое)
*/
const VERSION = 'jt-v1.0.1';
const SHELL = './index.html';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll([SHELL])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isImage(url) {
  return /\.(png|jpe?g|webp|gif|svg|avif)(\?|$)/i.test(url);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // чужие CDN не трогаем

  // картинки — сначала кэш
  if (isImage(url.pathname)) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req)
            .then((res) => {
              const copy = res.clone();
              caches.open(VERSION).then((c) => c.put(req, copy));
              return res;
            })
            .catch(() => hit)
      )
    );
    return;
  }

  // html и json — сначала сеть
  if (url.pathname.endsWith('/') || url.pathname.endsWith('.html') || url.pathname.endsWith('.json')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match(SHELL)))
    );
  }
});
