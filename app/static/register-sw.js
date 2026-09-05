if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Quietly do nothing if this fails — installability is a nice-to-have,
      // never something that should block the actual page from working.
    });
  });
}
