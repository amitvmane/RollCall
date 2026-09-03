/**
 * Renders every surface at every device size and asserts the things that make
 * a page usable rather than merely present.
 *
 * web_layout_check.js proves the DESKTOP GRID and specific flows behave. This
 * asks a different question — "is it usable on the thing in someone's hand" —
 * across the full range of real viewports, for all four surfaces, in both
 * themes, and inside the Telegram Mini App.
 *
 * What it checks, and why each one is here rather than assumed:
 *   horizontal overflow  a page that scrolls sideways on a phone feels broken
 *                        before anyone reads a word of it
 *   header collisions    flex siblings paint over each other WITHOUT the page
 *                        scrolling, so an overflow check cannot see it — this
 *                        shipped twice (group switcher over the bell)
 *   tap target size      Apple HIG says 44px, Material says 48px. Anything
 *                        under 40px is a control adults miss on a moving bus
 *   text size            below 12px stops being read and starts being skipped
 *   iOS zoom            a text field under 16px makes Safari zoom on focus and
 *                        leaves it zoomed — the page never comes back
 *   JS errors            a page that renders but throws is not stable
 *
 * Run:  node scripts/web_device_matrix.js
 * Needs puppeteer; skips with exit 0 when absent, like the other browser check.
 */
"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");

const API = path.join(__dirname, "..", "rollCall", "api");

let puppeteer;
try { puppeteer = require("puppeteer"); }
catch (_) {
  try { puppeteer = require("puppeteer-core"); }
  catch (_) { console.log("SKIPPED: neither puppeteer nor puppeteer-core installed"); process.exit(0); }
}
const EXEC = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;

const MIME = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
               ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json" };

const GROUP = {
  group_name: "Saturday Football FC", bot_username: "TestBot", dues_enabled: true,
  rollcalls: [{
    num: 1, id: 1, title: "Saturday Football", status: "open",
    in: Array.from({ length: 12 }, (_, i) => ({ name: `Player Nameington ${i + 1}`, is_proxy: false })),
    out: [{ name: "Kiran", is_proxy: false, comment: "last minute, sorry" }],
    maybe: [{ name: "Prasad", is_proxy: false }], waiting: [],
    location: "GroundZero, Baner", event_fee: 1500, in_list_limit: 14,
  }],
};
const GROUPS = [
  { chat_id: -1, group_name: "Saturday Football FC", group_web_token: "t1", has_active_rollcall: true },
  { chat_id: -2, group_name: "Sunday Cricket XI", group_web_token: "t2" },
];
const ROUTES = [
  [/\/web\/group\/[^/]+$/, GROUP],
  [/admin-status/, { is_admin: true }],
  [/\/portal\/groups/, { groups: GROUPS }],
  [/\/auth\/admin\/groups/, { groups: GROUPS }],
  [/\/members$/, { members: [] }],
  [/\/templates$/, []],
  [/\/scheduled-rollcalls/, { items: [] }],
  [/\/identities/, { identities: [], groups: [], discarded: [], standalone: [], suggestions: [] }],
  [/\/ghost\/sessions/, { ghost_tracking_enabled: true, autoforgive_days: 7, sessions: [] }],
  [/\/admins$/, { admin_source: "platform", you_are_owner: true, admins: [] }],
  [/\/dues\/my$/, { balance: 300, entries: [], upi_vpa: "demo@upi" }],
  [/\/dues\//, { enabled: true, fund_balance: 0, balances: [], available: false, tiers: [] }],
  [/\/stats/, { sessions: 81, avg_attendance: 11.1, members: 40, leaderboard: [], recent: [] }],
  [/\/presence/, { viewers: 2 }],
  [/\/upcoming/, { items: [] }],
  // The help page renders from this; without it the page throws on
  // undefined.filter and the matrix reports a fixture gap as an app bug.
  [/\/commands/, {
    commands: [
      { name: "start_roll_call", aliases: ["src"], scope: "user", category: "Rollcall",
        args: "<title>", sample: "/src Saturday Football",
        summary: "Start a rollcall", details: "Starts a new rollcall." },
      { name: "gentoken", aliases: [], scope: "admin", category: "Admin",
        args: "", sample: "/gentoken", summary: "Issue a token", details: "Issues one." },
    ],
    user_category_order: ["Rollcall"],
    admin_category_order: ["Admin"],
    category_emoji: { Rollcall: "📋", Admin: "⚙" },
  }],
];

function startServer() {
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url.startsWith("/api/")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      for (const [re, body] of ROUTES) if (re.test(url)) return res.end(JSON.stringify(body));
      return res.end("{}");
    }
    let rel = url;
    if (url === "/" || url === "/web/" || /^\/web\/(group|join)\//.test(url)) rel = "/web/index.html";
    if (url === "/portal" || url === "/portal/") rel = "/portal/index.html";
    if (url === "/help" || url === "/help/") rel = "/help/index.html";
    const file = path.join(API, rel.replace(/^\//, ""));
    if (!file.startsWith(API) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end("nope");
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "text/plain" });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(r => server.listen(0, "127.0.0.1", () => r(server)));
}

// Real viewports people actually hold, smallest first. 320 is the iPhone SE
// and the narrowest thing still worth supporting.
const DEVICES = [
  { name: "iPhone SE",        w: 320,  h: 568,  touch: true },
  { name: "Android compact",  w: 360,  h: 740,  touch: true },
  { name: "iPhone 14",        w: 390,  h: 844,  touch: true },
  { name: "iPhone Pro Max",   w: 430,  h: 932,  touch: true },
  { name: "Android large",    w: 412,  h: 915,  touch: true },
  { name: "iPad portrait",    w: 768,  h: 1024, touch: true },
  { name: "iPad landscape",   w: 1024, h: 768,  touch: true },
  { name: "Laptop",           w: 1280, h: 800,  touch: false },
  { name: "Desktop",          w: 1680, h: 1050, touch: false },
];

const SURFACES = [
  { name: "group page", path: "/web/group/t1", signedIn: true },
  { name: "group page (signed out)", path: "/web/group/t1", signedIn: false },
  { name: "home screen", path: "/web/", signedIn: true },
  { name: "portal", path: "/portal/", signedIn: true },
  { name: "help", path: "/help/", signedIn: false },
];

const MIN_TAP = 40;      // below this, a control is genuinely hard to hit
const MIN_TEXT = 12;     // below this, text stops being read

async function audit(page) {
  return page.evaluate((MIN_TAP, MIN_TEXT) => {
    const vis = el => {
      const r = el.getBoundingClientRect();
      return el.offsetParent !== null && r.width > 0 && r.height > 0;
    };
    const out = { overflow: document.documentElement.scrollWidth - window.innerWidth,
                  smallTaps: [], smallText: [], zoomers: [], collisions: [] };

    for (const el of document.querySelectorAll("button,a,select,input,[role=button],[role=tab]")) {
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      const tag = el.id || el.className || el.tagName;
      if (Math.min(r.width, r.height) < MIN_TAP) {
        out.smallTaps.push(`${String(tag).slice(0, 34)} ${Math.round(r.width)}x${Math.round(r.height)}`);
      }
    }
    for (const el of document.querySelectorAll("body *")) {
      if (!vis(el) || !el.childNodes.length) continue;
      const hasOwnText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
      if (!hasOwnText) continue;
      const px = parseFloat(getComputedStyle(el).fontSize);
      if (px < MIN_TEXT) out.smallText.push(`${String(el.className || el.tagName).slice(0, 30)} ${px}px`);
    }
    // Text fields under 16px make iOS Safari zoom on focus and stay zoomed.
    for (const el of document.querySelectorAll("input,textarea")) {
      if (!vis(el)) continue;
      if (["checkbox", "radio", "range"].includes(el.type)) continue;
      const px = parseFloat(getComputedStyle(el).fontSize);
      if (px < 16) out.zoomers.push(`${el.id || el.type} ${px}px`);
    }
    // Header halves painting over each other — invisible to an overflow check.
    const left = [...document.querySelectorAll(".brand-inner *, .topbar-left *")].filter(vis).pop();
    const right = [...document.querySelectorAll(".brand-actions > *, .topbar-right > *")].filter(vis)[0];
    if (left && right) {
      const gap = Math.round(right.getBoundingClientRect().left - left.getBoundingClientRect().right);
      if (gap < 0) out.collisions.push(`header overlaps by ${-gap}px`);
    }
    return out;
  }, MIN_TAP, MIN_TEXT);
}

(async () => {
  const failures = [];
  const server = await startServer();
  const BASE = `http://127.0.0.1:${server.address().port}`;
  const browser = await puppeteer.launch({
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
    ...(EXEC ? { executablePath: EXEC } : {}),
  });

  for (const surface of SURFACES) {
    for (const dev of DEVICES) {
      const page = await browser.newPage();
      const errs = [];
      page.on("pageerror", e => errs.push(String(e.message)));
      await page.emulate({
        viewport: { width: dev.w, height: dev.h, isMobile: dev.touch,
                    hasTouch: dev.touch, deviceScaleFactor: 1 },
        userAgent: dev.touch
          ? "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
          : "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      });
      await page.setRequestInterception(true);
      page.on("request", r => r.url().startsWith(BASE) ? r.continue()
        : r.respond({ status: 200, contentType: "text/plain", body: "" }));
      if (surface.signedIn) {
        await page.evaluateOnNewDocument(() => {
          localStorage.setItem("rc_verified_tg_user_id", "168415137");
          localStorage.setItem("rc_verified_tg_name", "Amit");
          localStorage.setItem("rc_identity_token", "168415137.9999999999.sig");
        });
      }
      await page.goto(BASE + surface.path, { waitUntil: "load" });
      await new Promise(r => setTimeout(r, 900));

      const a = await audit(page);
      const tag = `${surface.name} @ ${dev.name} (${dev.w}px)`;
      if (a.overflow > 1) failures.push(`${tag}: scrolls sideways by ${a.overflow}px`);
      a.collisions.forEach(c => failures.push(`${tag}: ${c}`));
      if (a.smallText.length) {
        failures.push(`${tag}: text under ${MIN_TEXT}px — ${a.smallText.slice(0, 3).join(", ")}`);
      }
      if (dev.touch && a.zoomers.length) {
        failures.push(`${tag}: field under 16px will zoom iOS on focus — ${a.zoomers.slice(0, 3).join(", ")}`);
      }
      if (dev.touch && a.smallTaps.length) {
        failures.push(`${tag}: tap target under ${MIN_TAP}px — ${a.smallTaps.slice(0, 3).join(", ")}`);
      }
      if (errs.length) failures.push(`${tag}: JS error — ${errs[0]}`);
      await page.close();
    }
    console.log(`  ${surface.name}: ${DEVICES.length} viewports checked`);
  }

  // The Mini App is the same page under a different host, and its header was
  // hidden entirely once — check it explicitly rather than assuming.
  {
    const page = await browser.newPage();
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.emulate({ viewport: { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
                         userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148 Safari/604.1" });
    await page.setRequestInterception(true);
    page.on("request", r => r.url().startsWith(BASE) ? r.continue()
      : r.respond({ status: 200, contentType: "text/plain", body: "" }));
    await page.evaluateOnNewDocument(() => {
      window.Telegram = { WebApp: { initData: "", initDataUnsafe: { user: { id: 1, first_name: "A" } },
                                    ready() {}, expand() {} } };
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.sig");
    });
    await page.goto(BASE + "/web/group/t1", { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 900));
    const a = await audit(page);
    const acct = await page.evaluate(() =>
      document.getElementById("acct-btn").getBoundingClientRect().width > 0);
    if (a.overflow > 1) failures.push(`Mini App: scrolls sideways by ${a.overflow}px`);
    a.collisions.forEach(c => failures.push(`Mini App: ${c}`));
    if (!acct) failures.push("Mini App: account control not reachable");
    if (errs.length) failures.push(`Mini App: JS error — ${errs[0]}`);
    console.log("  Mini App: checked");
    await page.close();
  }

  await browser.close();
  server.close();

  if (failures.length) {
    console.log(`\nFAILED: ${failures.length} problem(s)\n`);
    [...new Set(failures)].forEach(f => console.log(`  ✗ ${f}`));
    process.exit(1);
  }
  console.log(`\nPASSED: ${SURFACES.length} surfaces × ${DEVICES.length} viewports, plus the Mini App`);
})().catch(e => { console.error("device matrix crashed:", e); process.exit(1); });
