/* RollCall Service Worker — offline cache + web push */
"use strict";

// Bump on any change to the caching strategy below. Note this is NOT the only
// thing that ships new code any more — see the fetch handler.
const CACHE = "rc-v4";
const PRECACHE = ["/web/", "/web/app.js", "/web/style.css", "/web/logo.svg", "/web/icon-192.png", "/web/manifest.json"];

// ── Install: precache shell ───────────────────────────────────────────────────
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

// ── Activate: clean old caches ────────────────────────────────────────────────
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: network-first for API, cache-first for static ─────────────────────
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // Always hit network for API, heartbeat, push routes
  if (url.pathname.startsWith("/api/")) return;

  // CODE (js/css): network-first, cache only as an offline fallback.
  //
  // This was cache-first, which meant `cached || fetch(...)` — once app.js was
  // in the cache the network was never consulted again, and the entry was only
  // ever evicted by bumping CACHE above. Every client-side fix shipped between
  // two CACHE bumps was therefore invisible to anyone with the PWA installed,
  // indefinitely, while server-side changes landed normally. That produced
  // months-old JS talking to a current API: identity tokens still going out as
  // `?id_token=` query params long after the server had moved to the
  // X-Identity-Token header, so the server saw no token and denied admin
  // access to a real group owner with no way to see why.
  //
  // Correctness of the deployed code matters more here than shaving a request,
  // and the app is online-centric anyway — an offline user can't vote.
  if (url.pathname.startsWith("/web/") &&
      (url.pathname.endsWith(".js") || url.pathname.endsWith(".css"))) {
    e.respondWith(
      fetch(e.request).then(r => {
        if (r.ok) {
          const clone = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return r;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // IMAGES: cache-first is fine — they're content-addressed by filename and a
  // stale icon can't desync the client from the API.
  if (url.pathname.startsWith("/web/") &&
      (url.pathname.endsWith(".svg") || url.pathname.endsWith(".png"))) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(r => {
        if (r.ok) {
          const clone = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return r;
      }))
    );
    return;
  }

  // Network-first for HTML pages (always fresh vote state)
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/web/") || caches.match(e.request))
    );
  }
});

// ── Push: show notification ───────────────────────────────────────────────────
self.addEventListener("push", e => {
  let data = { title: "RollCall", body: "A rollcall just opened — tap to vote", url: "/web/" };
  try { if (e.data) data = { ...data, ...e.data.json() }; } catch (_) {}

  e.waitUntil(
    self.registration.showNotification(data.title, {
      body:    data.body,
      icon:    "/web/icon-192.png",
      badge:   "/web/icon-192.png",
      vibrate: [200, 100, 200],
      data:    { url: data.url },
      actions: [
        { action: "vote", title: "Vote now" },
        { action: "dismiss", title: "Dismiss" },
      ],
      requireInteraction: false,
      tag: "rollcall-open",   // replaces previous unread notification
    })
  );
});

// ── Notification click: open/focus the voting page ───────────────────────────
self.addEventListener("notificationclick", e => {
  e.notification.close();
  if (e.action === "dismiss") return;

  const target = e.notification.data?.url || "/web/";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(clients => {
      // Focus an already-open tab pointing at the same group
      for (const c of clients) {
        if (c.url.includes(target.split("?")[0]) && "focus" in c) {
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
