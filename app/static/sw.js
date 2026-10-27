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

// --- push notifications ---------------------------------------------------
// Fires even when no Tiger One tab/app is open — this is what makes a
// "new job assigned" alert show up as a real OS notification rather than
// something only visible while the app happens to be on screen.

self.addEventListener('push', (event) => {
  let payload = { title: 'Tiger One', body: 'You have an update', url: '/driver' };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch (e) { /* fall back to the defaults above */ }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: payload.url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/driver';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(url) && 'focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
