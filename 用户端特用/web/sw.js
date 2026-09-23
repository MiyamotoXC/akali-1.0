/* 阿卡莉 PWA Service Worker
 * 作用：让「网页端打开 + 手机添加到主屏幕」都能离线启动外壳（/index），
 *      同时保证接口数据永远走网络（不缓存 /api/，避免配额/面板读到旧值）。
 */
const CACHE = 'akali-shell-v1';
const SHELL = ['/', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;               // 只接管 GET
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return; // 跨域（CDN / 图片）不接管
  if (url.pathname.startsWith('/api/')) return;    // 接口永远直连，不缓存

  event.respondWith(
    fetch(req)
      .then((resp) => {
        const cp = resp.clone();
        caches.open(CACHE).then((c) => c.put(req, cp)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(req))              // 离线回落外壳
  );
});
