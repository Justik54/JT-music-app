/* JT Music — офлайн-кэш
   Стратегии:
   - index.html и tracks.json — сеть в приоритете, кэш как запасной вариант
   - обложки — кэш в приоритете
   - аудио — кэшируется по запросу плеера (последние несколько треков)
*/
const VERSION = 'jt-v1.7.1';
const SHELL = './index.html';
const AUDIO_CACHE = 'jt-audio-v1';
const AUDIO_MAX = 6;   // сколько треков держим офлайн

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll([SHELL])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== VERSION && k !== AUDIO_CACHE)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

/* ── аудио ─────────────────────────────────────────────── */
async function prefetchAudio(urls) {
  const cache = await caches.open(AUDIO_CACHE);

  for (const url of urls) {
    if (!url) continue;
    try {
      const hit = await cache.match(url);
      if (hit) continue;
      // no-cors даёт «opaque»-ответ: он занимает место и не поддерживает перемотку,
      // поэтому кэшируем только нормальный CORS-ответ
      const res = await fetch(url, { mode: 'cors', credentials: 'omit', cache: 'force-cache' });
      if (res && res.ok && res.type !== 'opaque') await cache.put(url, res);
    } catch (e) {
      // нет сети, нет CORS или файл не отдался — не страшно
    }
  }

  // держим не больше AUDIO_MAX файлов
  const keys = await cache.keys();
  if (keys.length > AUDIO_MAX) {
    for (const req of keys.slice(0, keys.length - AUDIO_MAX)) {
      await cache.delete(req);
    }
  }
}

self.addEventListener('message', (event) => {
  const data = event.data || {};

  if (data.type === 'prefetch-audio' && Array.isArray(data.urls)) {
    event.waitUntil(prefetchAudio(data.urls));
  }

  if (data.type === 'clear-audio') {
    event.waitUntil(caches.delete(AUDIO_CACHE));
  }
});

/* ── сеть ──────────────────────────────────────────────── */
function isImage(url) {
  return /\.(png|jpe?g|webp|gif|svg|avif)(\?|$)/i.test(url);
}

function isAudio(url) {
  return /\.(mp3|flac|m4a|ogg|wav|aac|opus)(\?|$)/i.test(url);
}


/* офлайн: отдаём трек из кэша (перемотка в этом режиме ограничена) */
async function serveAudio(req) {
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const hit = await cache.match(req.url);
    if (hit) return hit;
  } catch (e) {}

  try {
    return await fetch(req);
  } catch (e) {
    return new Response('', { status: 504, statusText: 'Offline' });
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // аудио: пока есть сеть — вообще не вмешиваемся,
  // иначе браузер не может перематывать (перемотка через SW ломается)
  if (isAudio(url.pathname) || req.destination === 'audio') {
    if (navigator.onLine) return;
    event.respondWith(serveAudio(req));
    return;
  }

  if (url.origin !== location.origin) return; // остальные чужие CDN не трогаем

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

  // json — сеть в приоритете, при обрыве отдаём прошлую копию,
  // НО НИКОГДА не подменяем на index.html (иначе JSON.parse падает и список пустой)
  if (url.pathname.endsWith('.json')) {
    event.respondWith(
      (async () => {
        try {
          const res = await fetch(req);
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(url.pathname, copy));
          }
          return res;
        } catch (e) {
          const hit = await caches.match(url.pathname);
          if (hit) return hit;
          return new Response('[]', {
            status: 200,
            headers: { 'Content-Type': 'application/json' }
          });
        }
      })()
    );
    return;
  }

  // html — сначала сеть
  if (url.pathname.endsWith('/') || url.pathname.endsWith('.html')) {
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
