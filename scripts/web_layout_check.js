/**
 * Renders the group page in headless Chromium at several viewport widths and
 * asserts the layout actually does what the CSS intends.
 *
 * The static checker (scripts/web_ui_check.py) proves ids and handlers resolve;
 * it cannot tell you whether the desktop grid produced two columns or silently
 * collapsed. This does — it measures real boxes after real layout.
 *
 * Network is stubbed, so this needs no server and no database: the API calls
 * the page makes are answered from fixtures below.
 *
 * Run:  node scripts/web_layout_check.js
 * Needs puppeteer; skips with exit 0 (and says so) when it isn't installed, so
 * it can sit in CI before the dependency is added.
 */
"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");

const API = path.join(__dirname, "..", "rollCall", "api");
const WEB = path.join(API, "web");

// The page links /web/style.css and /shared/tokens.css as absolute paths, so a
// file:// load silently gets no CSS at all and every layout assertion below
// would be measuring an unstyled document. Serve the real directory layout
// over HTTP instead — same paths the browser sees in production.
const MIME = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
               ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json" };

function startServer() {
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url.startsWith("/api/")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(fixtureFor(url)));
    }
    // /web/group/<token> is a client-routed page served by index.html — the
    // app reads the token off the URL and only enters group mode when it's
    // there. Loading plain /web/index.html renders the home screen instead,
    // which has none of the layout under test.
    let rel = url;
    if (url === "/" || url === "/web/" || /^\/web\/group\//.test(url)) rel = "/web/index.html";
    const file = path.join(API, rel.replace(/^\//, ""));
    if (!file.startsWith(API) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end("nope");
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "text/plain" });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(r => server.listen(0, "127.0.0.1", () => r(server)));
}

// puppeteer bundles a Chromium; puppeteer-core needs one pointed at via
// PUPPETEER_EXECUTABLE_PATH. Accept either so this runs both in CI (bundled)
// and against a distro chromium locally, which matters on arm64 where the
// prebuilt x86 image only runs under emulation and times out.
let puppeteer;
try {
  puppeteer = require("puppeteer");
} catch (_) {
  try {
    puppeteer = require("puppeteer-core");
  } catch (_) {
    console.log("SKIPPED: neither puppeteer nor puppeteer-core installed");
    process.exit(0);
  }
}
const EXEC = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;

const GROUP = {
  group_name: "Test FC",
  bot_username: "TestBot",
  rollcalls: [{
    num: 1, id: 1, title: "Saturday Football", status: "open",
    in: Array.from({ length: 14 }, (_, i) => ({ name: `Player ${i + 1}`, is_proxy: false })),
    out: [{ name: "Omkar", is_proxy: false, comment: "away" }],
    maybe: [{ name: "Piyush", is_proxy: false }],
    waiting: [],
    location: "GroundZero", event_fee: 1500, in_list_limit: 16,
  }],
};

const ROUTES = [
  [/\/web\/group\/[^/]+$/, GROUP],
  [/admin-status/, { is_admin: true }],
  [/\/stats/, { sessions: 81, avg_attendance: 11.1, members: 128, leaderboard: [], recent: [] }],
  [/\/presence/, { viewers: 1 }],
  [/\/dues\//, { enabled: false }],
  [/\/upcoming/, { items: [] }],
];

function fixtureFor(url) {
  for (const [re, body] of ROUTES) if (re.test(url)) return body;
  return {};
}

const WIDTHS = [
  // 320 is the narrowest mainstream viewport (iPhone SE / small Android) and
  // the one the header runs out of room on first.
  { name: "small-phone", w: 320, h: 700, twoCol: false, roster: false },
  { name: "phone", w: 390, h: 844, twoCol: false, roster: false },
  { name: "tablet", w: 820, h: 1180, twoCol: false, roster: null },
  { name: "laptop", w: 1280, h: 900, twoCol: true, roster: true },
  { name: "wide", w: 1680, h: 1000, twoCol: true, roster: true },
];

(async () => {
  const failures = [];
  const server = await startServer();
  const BASE = `http://127.0.0.1:${server.address().port}`;
  const browser = await puppeteer.launch({
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
    ...(EXEC ? { executablePath: EXEC } : {}),
  });

  for (const vp of WIDTHS) {
    const page = await browser.newPage();
    await page.setViewport({ width: vp.w, height: vp.h });

    const consoleErrors = [];
    page.on("pageerror", e => consoleErrors.push(String(e.message)));

    // Local server answers same-origin assets and /api/; block only the
    // third-party calls (Google Fonts, telegram.org) so this runs offline.
    await page.setRequestInterception(true);
    page.on("request", req => {
      const url = req.url();
      if (url.startsWith(BASE)) return req.continue();
      return req.respond({ status: 200, contentType: "text/plain", body: "" });
    });

    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));   // let the initial fetch render

    // Force the states under test to be present regardless of what the
    // fixture fetch decided, so the assertions measure layout rather than
    // data plumbing.
    await page.evaluate(g => {
      document.getElementById("loading").classList.add("hidden");
      document.getElementById("main").classList.remove("hidden");
      if (typeof groupData !== "undefined") { try { groupData = g; } catch (_) {} }
      // Populate the roster so the container query has real content to lay out.
      const sect = (label, cls, items) =>
        `<div class="list-sect"><div class="list-lbl ${cls}">${label}<span class="list-cnt">(${items.length})</span></div>` +
        `<ul class="list-items">${items.map((u, i) =>
          `<li><span class="li-pos">${i + 1}</span><span class="av">${u.name[0]}</span>` +
          `<span class="li-name">${u.name}</span></li>`).join("")}</ul></div>`;
      document.getElementById("lists-container").innerHTML =
        sect("IN", "in", g.rollcalls[0].in) +
        sect("OUT", "out", g.rollcalls[0].out) +
        sect("MAYBE", "maybe", g.rollcalls[0].maybe);
      document.getElementById("admin-card").classList.remove("hidden");
      document.getElementById("stats-card").classList.remove("hidden");
      document.getElementById("bookmark-card").classList.remove("hidden");
      // Worst-case header: every optional control visible at once. Install is
      // shown whenever the PWA is installable and notify whenever push is
      // available, so an admin on a phone really can get all of them — that
      // combination is what overflowed the bar and made the page scroll
      // sideways, and it only shows up if the test asks for it.
      document.getElementById("admin-nav-btn").classList.remove("hidden");
      document.getElementById("install-btn").classList.remove("hidden");
      document.getElementById("notify-btn").classList.remove("hidden");
    }, GROUP);

    const m = await page.evaluate(() => {
      const box = id => {
        const el = document.getElementById(id);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), right: Math.round(r.right) };
      };
      const sects = [...document.querySelectorAll("#lists-container > .list-sect")]
        .map(e => { const r = e.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y) }; });
      return {
        app: box("app"),
        lists: box("lists-card"),
        side: box("col-side"),
        main: box("col-main"),
        acct: box("acct-btn"),
        adminNav: box("admin-nav-btn"),
        adminBodyVisible: !!document.querySelector("#admin-card:not(.adm-collapsed) #admin-card-body"),
        sects,
        docScrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
      };
    });

    const tag = `${vp.name} (${vp.w}px)`;

    if (consoleErrors.length) failures.push(`${tag}: JS error — ${consoleErrors[0]}`);

    // No horizontal overflow at any width.
    if (m.docScrollW > m.innerW + 1) {
      failures.push(`${tag}: page scrolls horizontally (${m.docScrollW} > ${m.innerW})`);
    }

    // Account control is always reachable.
    if (!m.acct || m.acct.w === 0) failures.push(`${tag}: account button not visible`);
    // Admin entry is in the header, i.e. near the top of the page.
    if (!m.adminNav || m.adminNav.y > 80) failures.push(`${tag}: admin nav button not in the header`);
    // Admin panel starts collapsed.
    if (m.adminBodyVisible) failures.push(`${tag}: admin panel should start collapsed`);

    // Two-column expectations.
    const sideBesideMain = m.side && m.main && m.side.x > m.main.x + 100;
    if (vp.twoCol && !sideBesideMain) {
      failures.push(`${tag}: expected two columns, side col at x=${m.side && m.side.x} vs main x=${m.main && m.main.x}`);
    }
    if (vp.twoCol === false && sideBesideMain) {
      failures.push(`${tag}: expected single column, but side col sits beside main`);
    }

    // Roster: IN should sit beside OUT once the card is wide enough.
    if (vp.roster === true && m.sects.length >= 2) {
      if (!(m.sects[1].x > m.sects[0].x + 50)) {
        failures.push(`${tag}: roster did not split — OUT at x=${m.sects[1].x}, IN at x=${m.sects[0].x}`);
      }
    }
    if (vp.roster === false && m.sects.length >= 2) {
      if (m.sects[1].x > m.sects[0].x + 50) {
        failures.push(`${tag}: roster split on a phone — should stay stacked`);
      }
    }

    // Content shouldn't be a thin ribbon in a sea of nothing, nor edge-to-edge.
    if (vp.w >= 1280 && m.app && m.app.w > 1400) {
      failures.push(`${tag}: content width ${m.app.w}px — no max-width applied`);
    }

    console.log(`  ${tag}: app=${m.app && m.app.w}px twoCol=${sideBesideMain} sections=${m.sects.map(s => s.x).join(",")}`);
    await page.close();
  }

  // ── Identity behaviour ──────────────────────────────────────────────────
  // Layout alone can't tell you the account menu opens, offers the right
  // items for each identity state, or that Sign out actually clears the
  // stored credentials. Those are the point of the change, so assert them.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    // A) Signed out.
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));
    let s = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      const m = document.getElementById("acct-menu");
      return { open: !m.classList.contains("hidden"), text: m.innerText,
               label: document.getElementById("acct-label").innerText };
    });
    if (!s.open) failures.push("identity: account menu did not open when signed out");
    if (!/sign in/i.test(s.text)) failures.push(`identity: signed-out menu has no Sign in — got "${s.text.replace(/\n/g, " | ")}"`);
    if (/sign out/i.test(s.text)) failures.push("identity: signed-out menu offers Sign out");
    if (!/sign in/i.test(s.label)) failures.push(`identity: chip should read "Sign in", got "${s.label}"`);

    // Backdrop click closes it.
    s = await page.evaluate(() => {
      const bd = document.getElementById("acct-backdrop");
      if (bd) bd.click();
      return { closed: document.getElementById("acct-menu").classList.contains("hidden"),
               backdropGone: !document.getElementById("acct-backdrop") };
    });
    if (!s.closed) failures.push("identity: backdrop click did not close the menu");
    if (!s.backdropGone) failures.push("identity: backdrop left behind after close");

    // B) Signed in as a verified Telegram user.
    await page.evaluate(() => {
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));
    s = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      const m = document.getElementById("acct-menu");
      return { text: m.innerText, label: document.getElementById("acct-label").innerText,
               av: document.getElementById("acct-av").innerText };
    });
    if (!/sign out/i.test(s.text)) failures.push(`identity: signed-in menu has no Sign out — got "${s.text.replace(/\n/g, " | ")}"`);
    if (!/amit/i.test(s.text)) failures.push("identity: signed-in menu doesn't name the user");
    if (!/amit/i.test(s.label)) failures.push(`identity: chip should show the name, got "${s.label}"`);
    if (s.av !== "A") failures.push(`identity: avatar initial should be A, got "${s.av}"`);

    // C) Sign out really clears everything.
    s = await page.evaluate(() => {
      window.confirm = () => true;          // auto-accept the confirmation
      signOut();
      return {
        tok: localStorage.getItem("rc_identity_token"),
        uid: localStorage.getItem("rc_verified_tg_user_id"),
        name: localStorage.getItem("rc_verified_tg_name"),
        label: document.getElementById("acct-label").innerText,
        adminHidden: document.getElementById("admin-card").classList.contains("hidden"),
        navHidden: document.getElementById("admin-nav-btn").classList.contains("hidden"),
        menuClosed: document.getElementById("acct-menu").classList.contains("hidden"),
      };
    });
    if (s.tok || s.uid || s.name) failures.push(`identity: sign out left credentials behind (token=${s.tok} uid=${s.uid} name=${s.name})`);
    if (!/sign in/i.test(s.label)) failures.push(`identity: chip after sign out should read "Sign in", got "${s.label}"`);
    if (!s.adminHidden) failures.push("identity: admin card still visible after sign out");
    if (!s.navHidden) failures.push("identity: admin nav button still visible after sign out");
    if (!s.menuClosed) failures.push("identity: menu left open after sign out");

    // D) The unified sign-in entry reveals the chooser rather than committing
    //    the user to one method.
    s = await page.evaluate(() => {
      openSignIn();
      return {
        picker: !document.getElementById("identity-picker-row").classList.contains("hidden"),
        card: !document.getElementById("identity-card").classList.contains("hidden"),
      };
    });
    if (!s.picker) failures.push("identity: openSignIn() did not reveal the Telegram/Guest chooser");
    if (!s.card) failures.push("identity: openSignIn() left the identity card hidden");

    if (errs.length) failures.push(`identity: JS error — ${errs[0]}`);
    console.log(`  identity: menu, sign-out and sign-in flows exercised`);
    await page.close();
  }

  await browser.close();
  server.close();

  if (failures.length) {
    console.log(`\nFAILED: ${failures.length} layout problem(s)\n`);
    failures.forEach(f => console.log(`  ✗ ${f}`));
    process.exit(1);
  }
  console.log(`\nPASSED: layout correct at ${WIDTHS.length} viewport widths`);
})().catch(e => { console.error("layout check crashed:", e); process.exit(1); });
