// Service worker for the Price Alerts phone app: shows pushed alerts and opens the app when tapped.
// Deliberately caches nothing, so the app always shows live prices.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  const title = data.title || "Price alert";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    tag: data.tag ? `${data.tag}-${Date.now()}` : undefined,  // unique, so each alert shows separately
    icon: "icon-192.png",
    badge: "icon-192.png",
    data: { url: data.url || "/alerts/" },
    requireInteraction: true,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/alerts/", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.location.origin + "/alerts/")) { await w.focus(); return; }
    }
    await self.clients.openWindow(url);
  })());
});
