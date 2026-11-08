/* 师傅来了 PWA Service Worker —— 应用壳缓存，保证「添加到主屏幕」后秒开、可离线。
 * 策略：
 *  - 应用壳（首页 / 图标 / manifest）：network-first（在线即用最新，保证灰度即时生效；离线回退缓存）
 *  - API 请求：永不缓存，始终走网络（任务/消息/状态必须实时）
 * 灰度说明：服务端下发的 index.html 即最新功能；network-first 确保新版本打开即见，无需审核。
 */
// 缓存名随版本自增：每次发布若需强制失效旧壳，只需改此版本号（如 v1→v2）。
// activate 阶段会删除所有非当前名的缓存，从而保证旧版 index.html 不会被继续服务。
const CACHE = 'yantan-shell-v2';
const SHELL = [
  // 注意：'/' 与 '/index.html' 故意不纳入预缓存壳，
  // 首页始终由 fetch 处理器的 network-first(+no-cache) 实时拉取，避免壳缓存留下旧 HTML。
  '/manifest.webmanifest', '/sw.js',
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
    // 新 SW 接管后通知所有页面：版本已更新，请自动刷新以加载最新 UI
    .then(() => self.clients.matchAll({ includeUncontrolled: true }).then((cls) =>
      cls.forEach((c) => c.postMessage({ type: 'SW_UPDATED', cache: CACHE }))))
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
    // 首页：network-first，离线回退缓存。
    // 关键：fetch 强制 cache:'no-cache'，绕过浏览器 HTTP 启发式缓存，
    // 确保每次都向服务端校验最新 index.html（避免「服务端已更新、浏览器仍显示旧版」）。
    e.respondWith(
      fetch(req, { cache: 'no-cache' }).then((res) => {
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
