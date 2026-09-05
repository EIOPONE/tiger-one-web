// Deliberately minimal — driver pages (jobs, POD status, clock-in state)
// must always be fresh, so this does NOT cache or serve them offline.
// Its only job is to satisfy the browser's installability requirement
// (a registered service worker with a fetch handler) so "Add to Home
// Screen" produces a real standalone app icon on Android, not a plain
// bookmark shortcut.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Pass every request straight through to the network, unmodified.
  event.respondWith(fetch(event.request));
});
