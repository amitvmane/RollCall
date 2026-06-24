/* RollCall Service Worker — offline cache + web push */
"use strict";

const CACHE = "rc-v3";
const PRECACHE = ["/web/", "/web/app.js", "/web/style.css", "/web/logo.svg", "/web/icon-192.png"];

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

  // Cache-first for static assets (js/css/svg)
  if (url.pathname.startsWith("/web/") &&
      (url.pathname.endsWith(".js") || url.pathname.endsWith(".css") || url.pathname.endsWith(".svg") || url.pathname.endsWith(".png"))) {
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
