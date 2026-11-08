/* 师傅来了 PWA Service Worker —— 应用壳缓存，保证「添加到主屏幕」后秒开、可离线。
 * 策略：
 *  - 应用壳（首页 / 图标 / manifest）：network-first（在线即用最新，保证灰度即时生效；离线回退缓存）
 *  - API 请求：永不缓存，始终走网络（任务/消息/状态必须实时）
 * 灰度说明：服务端下发的 index.html 即最新功能；network-first 确保新版本打开即见，无需审核。
 */
const CACHE = 'yantan-shell-v1';
const SHELL = [
  '/', '/index.html', '/manifest.webmanifest', '/sw.js',
  '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isShell(url) {
  const p = url.pathname;
  return SHELL.includes(p) || p === '/' || p.endsWith('.png');
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // 写操作直接走网络
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // 跨域不拦截
  if (!isShell(url)) return;                        // API 等动态请求：network-only

  if (url.pathname === '/' || url.pathname === '/index.html') {
    // 首页：network-first，离线回退缓存
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((r) => r || caches.match('/index.html')))
    );
  } else {
    // 其它静态资源：cache-first
    e.respondWith(
      caches.match(req).then((r) => r || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
  }
});
