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

// The production Content-Security-Policy, read from the one place it is
// defined. Served on every page here so a handler the CSP refuses fails THIS
// check — in a real browser a refused handler is just a dead button and a
// console line nobody sees. Falls back to no header (and says so) only if
// python3 is missing, so the layout checks still run.
let CSP = null;
try {
  CSP = require("child_process").execFileSync("python3", ["-c",
    "import sys; sys.path.insert(0, sys.argv[1]); import security_headers as s; print(s._csp_common())",
    API], { encoding: "utf8" }).trim();
} catch (e) {
  console.log("  (CSP not applied: could not read it from security_headers.py — " + e.message.split("\n")[0] + ")");
}
const cspViolations = [];

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
    const headers = { "Content-Type": MIME[path.extname(file)] || "text/plain" };
    if (CSP && path.extname(file) === ".html") headers["Content-Security-Policy"] = CSP;
    res.writeHead(200, headers);
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
    // A max-length (100-char) comment: side by side with the name it once
    // squeezed the name to "…" on a phone. The roster check below needs it.
    out: [{ name: "Omkar Kulkarni", is_proxy: false,
            comment: "Can't make it today, stuck at the office till late, sorry all. Will be there next week for sure" }],
    maybe: [{ name: "Piyush", is_proxy: false }],
    waiting: [],
    location: "GroundZero", event_fee: 1500, in_list_limit: 16,
  }],
};

const GROUP_EMPTY = {
  group_name: "Test FC",
  bot_username: "TestBot",
  rollcalls: [],
  upcoming: [{ display_title: "Sunday Turf", scheduled_at: "2030-09-06T13:00:00Z" }],
};

const ADMIN_GROUPS = {
  groups: [
    { chat_id: -1001, group_name: "Test FC", group_web_token: "testtoken123" },
    { chat_id: -1002, group_name: "Second Club", group_web_token: "othertoken456" },
  ],
};

// A full game: one real member IN, a guest on the waitlist. Drives the
// "which list am I on" check (detectCurrentVote) below.
const GROUP_FULL = {
  group_name: "Test FC",
  bot_username: "TestBot",
  rollcalls: [{
    num: 1, id: 2, rollcall_id: 2, title: "Full Game", status: "open", limit: 1,
    in: [{ name: "Priya", is_proxy: false, user_id: 555 }],
    out: [], maybe: [],
    waiting: [{ name: "Ravi K", is_proxy: true, user_id: null }],
  }],
};

// Email sign-in: a person with no Telegram gets an id at or above 2^52.
const EMAIL_USER = { id_token: "4503599627370500.9999999999.feedface", user_id: 4503599627370500,
                     name: "Asha", email: "asha@example.com", purpose: "login", group_token: "testtoken123" };

// Account sheet (services/account.py) for that email-only person.
const ACCOUNT = { user_id: 4503599627370500, name: "Asha", email: "asha@example.com", has_telegram: false,
                  email_only: true, email_enabled: true, email_notify: "auto", can_delete: true };

const ROUTES = [
  [/\/auth\/account\/sign-out-everywhere$/, { id_token: "4503599627370500.9999999999.fresh" }],
  [/\/auth\/account(\/email-notify)?$/, ACCOUNT],
  [/\/auth\/email\/config$/, { enabled: true }],
  [/\/auth\/email\/verify$/, EMAIL_USER],
  [/\/web\/groups$/, { chat_id: -4503599627370497, group_token: "newgrouptoken", group_name: "Sunday Turf" }],
  [/\/web\/group\/emptytoken$/, GROUP_EMPTY],
  [/\/web\/group\/fulltoken$/, GROUP_FULL],
  [/\/web\/group\/[^/]+$/, GROUP],
  [/admin-status/, { is_admin: true }],
  // /help — the theme check loads it; an empty {} throws in its renderer.
  [/\/commands$/, { commands: [], user_category_order: [], admin_category_order: [], category_emoji: {} }],
  [/\/auth\/admin\/groups/, ADMIN_GROUPS],
  [/\/portal\/groups/, { groups: ADMIN_GROUPS.groups.map(g => ({ ...g, has_active_rollcall: false })) }],
  [/\/members$/, { members: [{ user_id: 1, first_name: "Amit", username: "amit" }] }],
  [/\/templates$/, []],
  [/\/scheduled-rollcalls/, { items: [] }],
  [/\/identities\/suggestions$/, { suggestions: [] }],
  [/\/identities$/, { identities: [], groups: [], discarded: [], standalone: [] }],
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
  // Every page this script opens reports CSP refusals.
  const _newPage = browser.newPage.bind(browser);
  browser.newPage = async () => {
    const p = await _newPage();
    p.on("console", m => {
      if (/Content Security Policy/i.test(m.text())) cspViolations.push(`${p.url().replace(BASE, "")}: ${m.text().slice(0, 160)}`);
    });
    return p;
  };

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
      document.getElementById("no-rollcalls").classList.remove("hidden");
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
        adminViewActive: document.getElementById("view-admin").classList.contains("active"),
        sects,
        docScrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        // Flex siblings can paint over each other without the PAGE scrolling.
        // The group switcher in the header did exactly that — the name ran
        // under the Install button while every overflow assertion passed.
        headerOverlap: (() => {
          // Compare the RENDERED edges, not the flex wrappers: .brand-inner
          // can be wider than the control drawn inside it, so wrapper maths
          // reported a 0px gap while the group switcher was genuinely 1px
          // under the notification bell at 320px.
          const vis = el => el && el.offsetParent !== null;
          const left = [...document.querySelectorAll(".brand-inner *")].filter(vis).pop();
          const right = [...document.querySelectorAll(".brand-actions > *")].filter(vis)[0];
          if (!left || !right) return 0;
          return Math.round(left.getBoundingClientRect().right
                            - right.getBoundingClientRect().left);
        })(),
      };
    });

    const tag = `${vp.name} (${vp.w}px)`;

    if (consoleErrors.length) failures.push(`${tag}: JS error — ${consoleErrors[0]}`);

    // The header's two halves must not paint over each other.
    if (m.headerOverlap > 0) {
      failures.push(`${tag}: brand name overlaps the header buttons by ${m.headerOverlap}px`);
    }

    // No horizontal overflow at any width.
    if (m.docScrollW > m.innerW + 1) {
      failures.push(`${tag}: page scrolls horizontally (${m.docScrollW} > ${m.innerW})`);
    }

    // Account control is always reachable.
    if (!m.acct || m.acct.w === 0) failures.push(`${tag}: account button not visible`);
    // Admin entry is in the header, i.e. near the top of the page.
    if (!m.adminNav || m.adminNav.y > 80) failures.push(`${tag}: admin nav button not in the header`);
    // Rollcall is the landing view; admin is somewhere you go, not something
    // you have to scroll past.
    if (m.adminViewActive) failures.push(`${tag}: admin view should not be the default`);

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

    // The menu items must be the topmost thing at their own coordinates.
    //
    // This is the assertion the rest of this file could not make: every check
    // here drives the UI with el.click(), which dispatches straight at the
    // node and never hit-tests. The tap-to-close backdrop was appended to
    // <body> at z-index 299 while the menu sits inside .brand-bar — sticky,
    // z-index:100, therefore a stacking context — so the menu's 300 only ever
    // ranked it among the bar's own children, and the backdrop covered the
    // whole bar. Every item was dead to a real mouse: the click hit the
    // backdrop, the menu closed, nothing happened. Sign out "not working" was
    // this; so were Sign in, Change name and Admin controls.
    s = await page.evaluate(() => {
      const items = [...document.querySelectorAll("#acct-menu .acct-menu-item")];
      return items.map(it => {
        const r = it.getBoundingClientRect();
        const top = document.elementFromPoint(Math.round(r.x + r.width / 2),
                                              Math.round(r.y + r.height / 2));
        return { label: it.innerText.trim().replace(/\s+/g, " "),
                 reachable: top === it || it.contains(top),
                 blockedBy: top ? (top.id || top.className) : "nothing" };
      });
    });
    s.filter(i => !i.reachable).forEach(i =>
      failures.push(`identity: menu item "${i.label}" is not clickable — covered by ${i.blockedBy}`));

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
    s = await page.evaluate(async () => {
      window.confirm = () => true;          // auto-accept the confirmation
      // signOut awaits the confirmation (Telegram's webview needs an async
      // dialog), so the assertions below have to await it too.
      await signOut();
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

    // D) Sign-in is ONE dialog reached from a small, fixed number of places.
    //    It used to be four competing affordances with three destinations:
    //    the header menu, an inline Telegram/Guest chooser next to the vote
    //    buttons, a "Verify with Telegram" link buried in the dues card, and
    //    a button in the admin note. Beyond the always-present header chip
    //    (icon-only on narrow screens) there must be exactly one CTA.
    s = await page.evaluate(() => {
      const vis = el => el && el.offsetParent !== null && el.getBoundingClientRect().width > 0;
      const hits = [];
      document.querySelectorAll("button, a").forEach(el => {
        if (!vis(el) || el.id === "acct-btn" || el.closest("#signin-modal")) return;
        if (/sign in|verify with telegram/i.test(el.innerText || "")) {
          hits.push((el.innerText || "").trim().replace(/\s+/g, " "));
        }
      });
      return { hits, chip: vis(document.getElementById("acct-btn")) };
    });
    if (!s.chip) failures.push("identity: header account chip not visible when signed out");
    if (s.hits.length !== 1) {
      failures.push(`identity: expected exactly 1 sign-in CTA besides the header chip, found ${s.hits.length}: ${JSON.stringify(s.hits)}`);
    }

    //    The dialog BORROWS the chooser rather than cloning it — a second
    //    #name-input would make every getElementById a coin toss — and every
    //    borrowed row has to go home when it closes.
    s = await page.evaluate(() => {
      openSignIn();
      const body = document.getElementById("signin-body");
      return {
        open: !document.getElementById("signin-modal").classList.contains("hidden"),
        pickerInDialog: !!body.querySelector("#identity-picker-row"),
        pickerVisible: !document.getElementById("identity-picker-row").classList.contains("hidden"),
        promptHidden: document.getElementById("signin-prompt-row").classList.contains("hidden"),
        nameInputs: document.querySelectorAll("#name-input").length,
      };
    });
    if (!s.open) failures.push("identity: openSignIn() did not open the dialog");
    if (!s.pickerInDialog || !s.pickerVisible) failures.push("identity: dialog does not show the Telegram/Guest chooser");
    if (!s.promptHidden) failures.push("identity: inline prompt still visible behind the open dialog");
    if (s.nameInputs !== 1) failures.push(`identity: #name-input duplicated (${s.nameInputs} in the document)`);

    s = await page.evaluate(() => {
      closeSignIn();
      const card = document.getElementById("identity-card");
      return {
        closed: document.getElementById("signin-modal").classList.contains("hidden"),
        restored: ["identity-picker-row", "name-input-row", "tg-deeplink-row", "tg-widget-wrap"]
          .every(id => card.contains(document.getElementById(id))),
        promptBack: !document.getElementById("signin-prompt-row").classList.contains("hidden"),
        scrollLocked: document.body.classList.contains("modal-open"),
      };
    });
    if (!s.closed) failures.push("identity: closeSignIn() left the dialog open");
    if (!s.restored) failures.push("identity: borrowed rows were not returned to the identity card");
    if (!s.promptBack) failures.push("identity: inline sign-in prompt did not come back after closing");
    if (s.scrollLocked) failures.push("identity: body left scroll-locked after closing the dialog");

    if (errs.length) failures.push(`identity: JS error — ${errs[0]}`);
    console.log(`  identity: menu, sign-out and sign-in flows exercised`);
    await page.close();
  }

  // ── Admin menu ──────────────────────────────────────────────────────────
  // The admin card is a two-level menu: a group picker + a list of entries at
  // the root, and exactly one submenu panel below that. Before it was one flat
  // scroll of every control at once, with the group switcher hidden unless you
  // happened to administer a second group — so the panel never said which
  // group it was editing. Both halves are asserted here because neither the
  // static wiring check nor the layout assertions above can see them.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(() => {
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    // admin-status is a live round-trip with retries, so poll for the answer
    // rather than sleeping a guessed interval — a fixed wait here is a flaky
    // test that fails on a slow machine and says "admin menu broken".
    // The header group switcher is filled by a second async call after the
    // admin answer, so "card visible" alone still races it.
    await page.waitForFunction(
      () => !document.getElementById("admin-card").classList.contains("hidden")
            && !!document.getElementById("group-switch").value,
      { timeout: 8000 },
    ).catch(() => failures.push("admin: admin card / group switcher never became ready")); 

    const root = await page.evaluate(() => {
      document.getElementById("admin-nav-btn").click();
      const sel = document.getElementById("group-switch");
      return {
        open: document.getElementById("view-admin").classList.contains("active"),
        menuVisible: !document.getElementById("admin-menu").classList.contains("hidden"),
        pickerVisible: !!(sel && sel.getBoundingClientRect().width > 0),
        options: sel ? [...sel.options].map(o => o.value) : [],
        selected: sel ? sel.value : null,
        // End Active must appear on a cold load, not only after the next poll.
        endVisible: document.getElementById("end-rc-row").style.display !== "none",
      };
    });
    if (!root.open) failures.push("admin: header button did not open the panel");
    if (!root.menuVisible) failures.push("admin: menu level not visible on open");
    // The switcher lives in the HEADER now, not inside the admin panel — it
    // answers "which group am I looking at", which applies to reading stats
    // and voting just as much as to administering.
    if (!root.pickerVisible) failures.push("group switcher not visible in the header");
    if (root.options.length !== 2) failures.push(`group switcher should list both groups, got ${JSON.stringify(root.options)}`);
    if (root.selected !== "testtoken123") failures.push(`current group not selected, got "${root.selected}"`);
    if (!root.endVisible) failures.push("admin: End Active Rollcall hidden for an admin with an open rollcall");

    for (const name of ["settings", "access", "templates", "scheduled", "merge"]) {
      const s = await page.evaluate(async n => {
        await window.openAdminSection(n);
        const p = document.getElementById(`adm-panel-${n}`);
        return {
          shown: !!p && !p.classList.contains("hidden"),
          menuHidden: document.getElementById("admin-menu").classList.contains("hidden"),
          others: [...document.querySelectorAll(".adm-panel")]
            .filter(el => el.id !== `adm-panel-${n}` && !el.classList.contains("hidden")).length,
        };
      }, name);
      if (!s.shown) failures.push(`admin: ${name} panel did not open`);
      if (!s.menuHidden) failures.push(`admin: menu still shown behind the ${name} panel`);
      if (s.others) failures.push(`admin: ${s.others} other panel(s) open alongside ${name}`);

      const back = await page.evaluate(() => {
        window.closeAdminSection();
        return {
          menuVisible: !document.getElementById("admin-menu").classList.contains("hidden"),
          panelsOpen: [...document.querySelectorAll(".adm-panel")].filter(el => !el.classList.contains("hidden")).length,
        };
      });
      if (!back.menuVisible) failures.push(`admin: Back from ${name} did not return to the menu`);
      if (back.panelsOpen) failures.push(`admin: ${back.panelsOpen} panel(s) still open after Back from ${name}`);
    }

    if (errs.length) failures.push(`admin: JS error — ${errs[0]}`);
    console.log("  admin: two-level menu, group picker and all submenu panels exercised");
    await page.close();
  }

  // ── Views + empty state ─────────────────────────────────────────────────
  // The page was one long scroll: game, vote, roster, stats, dues, admin. It
  // is four destinations now, and exactly one may be on screen at a time —
  // if two ever render together the whole point is lost and nothing else
  // here would notice. The empty state is checked in the same pass because
  // "no rollcall" used to leave the main column blank while the rest of the
  // page carried on below it.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(() => {
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.waitForFunction(() => !document.getElementById("vn-admin").classList.contains("hidden"),
                               { timeout: 8000 })
              .catch(() => failures.push("views: Admin tab never appeared for an admin"));

    const VIEWS = ["rollcall", "stats", "dues", "admin"];
    for (const v of VIEWS) {
      const s = await page.evaluate(name => {
        const before = (document.querySelector(".view.active") || {}).id;
        window.showView(name);
        return {
          before,
          active: [...document.querySelectorAll(".view")]
            .filter(el => el.classList.contains("active")).map(el => el.id),
          tabActive: [...document.querySelectorAll(".vn-item")]
            .filter(el => el.classList.contains("active")).map(el => el.id),
        };
      }, v);
      // Dues is off in the fixture, so its tab is hidden and showView must
      // refuse — leaving you exactly where you were, not on a blank panel.
      const expected = v === "dues" ? s.before : `view-${v}`;
      if (s.active.length !== 1) {
        failures.push(`views: ${s.active.length} views active at once after showView("${v}") — ${s.active.join(", ")}`);
      } else if (s.active[0] !== expected) {
        failures.push(`views: showView("${v}") landed on ${s.active[0]}, expected ${expected}`);
      }
      if (s.tabActive.length !== 1) {
        failures.push(`views: ${s.tabActive.length} tabs highlighted after showView("${v}")`);
      }
    }

    // Empty state: it is the column's content, not a footnote under one.
    // Loaded through the real path from a fixture group with nothing running,
    // rather than by reaching into the page's variables.
    await page.goto(`${BASE}/web/group/emptytoken`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 900));
    const es = await page.evaluate(() => {
      const box = document.getElementById("no-rollcalls");
      return {
        shown: !box.classList.contains("hidden"),
        text: box.innerText.replace(/\n/g, " | "),
        actions: [...box.querySelectorAll("button")].map(b => b.innerText.trim()),
        // Things that only make sense while a rollcall exists.
        voteVisible: !document.getElementById("vote-card").classList.contains("hidden"),
        rcCardVisible: !document.getElementById("rc-card").classList.contains("hidden"),
        identityVisible: !document.getElementById("identity-card").classList.contains("hidden"),
        refreshVisible: !document.getElementById("refresh-bar-wrap").classList.contains("hidden"),
      };
    });
    if (!es.shown) failures.push("views: empty state not shown when there are no rollcalls");
    if (!/next/i.test(es.text)) failures.push(`views: empty state doesn't name the next scheduled rollcall — "${es.text}"`);
    if (!es.actions.length) failures.push("views: empty state offers no action at all");
    if (es.voteVisible) failures.push("views: vote buttons still shown with no rollcall");
    if (es.rcCardVisible) failures.push("views: rollcall header card still shown with no rollcall");
    if (es.identityVisible) failures.push("views: identity strip still shown with no rollcall");
    if (es.refreshVisible) failures.push("views: refresh countdown still shown with no rollcall");

    if (errs.length) failures.push(`views: JS error — ${errs[0]}`);
    console.log("  views: four destinations, one at a time, plus the empty state");
    await page.close();
  }

  // ── Telegram Mini App mode ──────────────────────────────────────────────
  // body.tg-mode used to hide the whole brand bar, on the reasoning that
  // Telegram already draws a title bar. But the bar is also where the account
  // chip and the Admin button live, so an admin opening the group from the
  // Telegram menu button had no top-level menu at all and no route to admin
  // controls. Only a browser can see this — the CSS rule reads as harmless.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.evaluateOnNewDocument(() => {
      window.Telegram = { WebApp: {
        initData: "", initDataUnsafe: { user: { id: 168415137, first_name: "Amit" } },
        ready() {}, expand() {},
      } };
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 700));

    const s = await page.evaluate(() => {
      const admin = document.getElementById("admin-nav-btn");
      return {
        tgMode: document.body.classList.contains("tg-mode"),
        barHeight: document.getElementById("brand-bar").getBoundingClientRect().height,
        acctWidth: document.getElementById("acct-btn").getBoundingClientRect().width,
        adminWidth: admin.classList.contains("hidden") ? 0 : admin.getBoundingClientRect().width,
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        installOffered: (() => {
          document.getElementById("acct-btn").click();
          const t = document.getElementById("acct-menu").innerText;
          document.getElementById("acct-btn").click();
          return /install app/i.test(t) ||
            !document.getElementById("install-btn").classList.contains("hidden");
        })(),
        bellShown: !document.getElementById("notify-btn").classList.contains("hidden"),
      };
    });
    if (s.bellShown) failures.push("tg-mode: 🔔 web-push bell shown inside the Telegram Mini App");
    if (!s.tgMode) failures.push("tg-mode: Telegram Mini App not detected from initDataUnsafe");
    if (s.installOffered) failures.push("tg-mode: Install app offered inside the Telegram Mini App");
    if (!(s.barHeight > 0)) failures.push("tg-mode: header not rendered inside the Mini App");
    if (!(s.acctWidth > 0)) failures.push("tg-mode: account chip not reachable inside the Mini App");
    if (!(s.adminWidth > 0)) failures.push("tg-mode: Admin button not reachable inside the Mini App");
    if (s.overflow > 1) failures.push(`tg-mode: page overflows by ${s.overflow}px`);
    if (errs.length) failures.push(`tg-mode: JS error — ${errs[0]}`);

    console.log("  tg-mode: header controls survive inside the Telegram Mini App");
    await page.close();
  }

  // ── Roster: a long comment never hides the name ─────────────────────────
  // The real renderLists output, at the narrowest phone, for a member and for
  // an admin (whose row also carries Move→ and a 44px ✕). A comment must not
  // take any width from the name — it goes under it, clamped to two lines.
  for (const admin of [false, true]) {
    const who = admin ? "admin" : "member";
    const page = await browser.newPage();
    await page.setViewport({ width: 320, height: 700, isMobile: true, hasTouch: true });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    // localStorage outlives the page: earlier checks sign in, so the member
    // run must clear it or it silently becomes a second admin run.
    await page.evaluateOnNewDocument(adm => {
      localStorage.clear();
      if (!adm) return;
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    }, admin);
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    const ready = await page.waitForFunction(adm => {
      const c = document.querySelector("#lists-container .li-comment");
      return c && !!c.closest("li").querySelector(".li-admin-acts") === adm;
    }, { timeout: 8000 }, admin).then(() => true, () => false);
    if (!ready) {
      failures.push(`roster(${who}): commented row never rendered`);
    } else {
      const s = await page.evaluate(() => {
        const c = document.querySelector("#lists-container .li-comment");
        const name = c.closest("li").querySelector(".li-name");
        // A row with no comment, same controls: the width a name is entitled to.
        const plain = [...document.querySelectorAll("#lists-container li")]
          .find(li => !li.querySelector(".li-comment")).querySelector(".li-name");
        const lh = parseFloat(getComputedStyle(c).lineHeight);
        return { nameW: name.clientWidth, plainW: plain.clientWidth,
                 lines: Math.round(c.getBoundingClientRect().height / lh),
                 overflow: document.documentElement.scrollWidth - window.innerWidth };
      });
      if (s.nameW < s.plainW - 1)
        failures.push(`roster(${who}): long comment squeezes the name to ${s.nameW}px (${s.plainW}px without one)`);
      if (s.lines > 2) failures.push(`roster(${who}): comment not clamped (${s.lines} lines)`);
      if (s.overflow > 1) failures.push(`roster(${who}): page overflows by ${s.overflow}px`);
    }
    if (errs.length) failures.push(`roster(${who}): JS error — ${errs[0]}`);
    await page.close();
  }
  console.log("  roster: a long comment takes no width from the name at 320px, member and admin");

  // ── Install option (outside Telegram) ───────────────────────────────────
  // telegram-web-app.js is loaded on every page and defines Telegram.WebApp in
  // ANY browser, with empty initData. The other checks block telegram.org, so
  // window.Telegram never existed here — which is how `if(tg)return;` hid the
  // ⬇ Install button from every real visitor while this file stayed green.
  // The stub below is what the real script leaves behind outside Telegram.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.evaluateOnNewDocument(() => {
      window.Telegram = { WebApp: { initData: "", initDataUnsafe: {}, ready() {}, expand() {} } };
      // Headless Chrome may or may not fire a real beforeinstallprompt for
      // this page, and when. Swallow the real one (registered before app.js,
      // so it runs first) and drive the app's listener with synthetic ones.
      window.addEventListener("beforeinstallprompt", e => { if (e.isTrusted) e.stopImmediatePropagation(); });
    });

    // A /app QR scan: the sheet opens on arrival and the flag is dropped.
    await page.goto(`${BASE}/web/group/testtoken123?install=1`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 900));
    // The bell only appears once the service worker has registered.
    await page.waitForFunction(() => !document.getElementById("notify-btn").classList.contains("hidden"),
                               { timeout: 4000 }).catch(() => {});
    let s = await page.evaluate(() => {
      const m = document.getElementById("install-modal");
      const bell = document.getElementById("notify-btn");
      const close = m.querySelector(".signin-close");
      const r = close.getBoundingClientRect();
      const top = document.elementFromPoint(Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2));
      return { open: !m.classList.contains("hidden"),
               body: document.getElementById("install-body").innerText,
               search: location.search,
               pushApi: "PushManager" in window,
               bellShown: !bell.classList.contains("hidden"),
               closeReachable: top === close || close.contains(top),
               overflow: document.documentElement.scrollWidth - window.innerWidth };
    });
    // Same `if(tg)` bug, same stub: the bell was hidden from every browser.
    if (s.pushApi && !s.bellShown) failures.push("install: 🔔 web-push bell hidden outside Telegram");
    if (!s.open) failures.push("install: ?install=1 did not open the install sheet");
    if (!s.body.trim()) failures.push("install: install sheet opened empty");
    if (/install=1/.test(s.search)) failures.push("install: ?install=1 left in the URL — a reload reopens the sheet");
    if (!s.closeReachable) failures.push("install: sheet close button is covered by something");
    if (s.overflow > 1) failures.push(`install: page overflows by ${s.overflow}px with the sheet open`);

    await page.keyboard.press("Escape");
    s = await page.evaluate(async () => {
      const modal = document.getElementById("install-modal");
      const isOpen = () => !modal.classList.contains("hidden");
      const tick = () => new Promise(r => setTimeout(r, 0));
      const out = { closed: !isOpen() };
      const clickInstallItem = () => {
        document.getElementById("acct-btn").click();
        const item = [...document.querySelectorAll("#acct-menu .acct-menu-item")]
          .find(i => /install app/i.test(i.innerText));
        if (item) item.click();
        return !!item;
      };
      let prompts = 0;
      const offerPrompt = () => {
        const ev = new Event("beforeinstallprompt", { cancelable: true });
        ev.prompt = () => { prompts++; };
        ev.userChoice = Promise.resolve({ outcome: "dismissed" });
        window.dispatchEvent(ev);
      };

      // Browser prompt available: the tap goes straight to it, no sheet.
      offerPrompt();
      out.item = clickInstallItem();
      await tick();
      out.menuPrompted = prompts === 1 && !isOpen();

      // That event is now spent, so the next tap shows the manual steps
      // rather than calling prompt() on it again (which would throw).
      clickInstallItem();
      out.manualOpened = isOpen() && prompts === 1;
      out.manualSteps = !!document.querySelector("#install-body .inst-steps, #install-body .inst-qr");

      // The browser offers a prompt while the sheet is open (a ?install=1 scan
      // usually opens it first): the steps become the one-tap button.
      offerPrompt();
      const go = document.querySelector("#install-body .inst-go");
      out.swapped = !!go;
      if (go) go.click();
      await tick();
      out.sheetPrompted = prompts === 2;
      out.fellBack = isOpen() && !document.querySelector("#install-body .inst-go");
      closeInstallHelp();
      return out;
    });
    if (!s.closed) failures.push("install: Escape did not close the install sheet");
    if (!s.item) failures.push("install: account menu has no Install app item outside Telegram");
    if (!s.menuPrompted) failures.push("install: menu item did not go straight to the browser's install prompt");
    if (!s.manualOpened) failures.push("install: spent prompt reused, or the menu item did not open the sheet");
    if (!s.manualSteps) failures.push("install: sheet opened without install steps");
    if (!s.swapped) failures.push("install: a prompt arriving while the sheet is open did not show the Install button");
    if (!s.sheetPrompted) failures.push("install: sheet's Install button did not trigger the prompt");
    if (!s.fellBack) failures.push("install: after 'Not now' the sheet did not fall back to the manual steps");
    if (errs.length) failures.push(`install: JS error — ${errs[0]}`);

    console.log("  install: menu item + sheet reachable outside Telegram, ?install=1 opens it once");
    await page.close();
  }

  // ── Home screen (/web/ with no group token) ─────────────────────────────
  // Had no account control and no theme toggle at all. The chip is MOVED here
  // rather than cloned, so this also guards against a second #acct-wrap
  // appearing — which would break every getElementById that touches it.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.goto(`${BASE}/web/`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));

    const s = await page.evaluate(() => {
      const wraps = document.querySelectorAll("#acct-wrap, .acct-wrap");
      const acct = document.getElementById("acct-wrap");
      const home = document.getElementById("home-brand-actions");
      return {
        homeVisible: !document.getElementById("home-screen").classList.contains("hidden"),
        wrapCount: wraps.length,
        inHomeHeader: !!(acct && home && home.contains(acct)),
        chipVisible: acct ? acct.getBoundingClientRect().width > 0 : false,
        hasTheme: !!document.getElementById("theme-btn-home"),
        installCard: !document.getElementById("home-install").classList.contains("hidden"),
      };
    });
    if (!s.installCard) failures.push("home: no Install card on the home screen outside the app");

    if (!s.homeVisible) failures.push("home: home screen did not render at /web/");
    if (s.wrapCount !== 1) failures.push(`home: expected exactly one .acct-wrap in the document, found ${s.wrapCount}`);
    if (!s.inHomeHeader) failures.push("home: account chip was not moved into the home header");
    if (!s.chipVisible) failures.push("home: account chip is present but not visible");
    if (!s.hasTheme) failures.push("home: no theme toggle in the home header");
    if (errs.length) failures.push(`home: JS error — ${errs[0]}`);

    console.log(`  home: account chip relocated into the home header`);
    await page.close();
  }

  // ── Portal ──────────────────────────────────────────────────────────────
  // The portal is a separate app with its own IIFE-scoped JS, so none of the
  // group page's coverage says anything about it. It now carries the same
  // account control, which means the same failure modes.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.goto(`${BASE}/portal/index.html`, { waitUntil: "load" });
    await page.evaluate(() => {
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/portal/index.html`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));

    let s = await page.evaluate(() => {
      const btn = document.getElementById("acct-btn");
      if (btn) btn.click();
      const m = document.getElementById("acct-menu");
      return {
        hasChip: !!btn,
        label: (document.getElementById("acct-label") || {}).innerText,
        av: (document.getElementById("acct-av") || {}).innerText,
        open: m ? !m.classList.contains("hidden") : false,
        text: m ? m.innerText : "",
        oldLogout: !!document.getElementById("logout-btn"),
      };
    });

    if (!s.hasChip) failures.push("portal: account chip missing");
    if (!s.open) failures.push("portal: account menu did not open");
    if (!/sign out/i.test(s.text)) failures.push(`portal: menu has no Sign out — got "${s.text.replace(/\n/g, " | ")}"`);
    if (!/amit/i.test(s.text)) failures.push("portal: menu doesn't name the signed-in user");
    if (s.av !== "A") failures.push(`portal: avatar initial should be A, got "${s.av}"`);
    if (s.oldLogout) failures.push("portal: old #logout-btn still present alongside the new control");

    // Same hit-test as the group page. The portal's .topbar is sticky at
    // z-index:200, so it has the identical stacking-context trap.
    const reach = await page.evaluate(() => {
      const items = [...document.querySelectorAll("#acct-menu .acct-menu-item")];
      return items.map(it => {
        const r = it.getBoundingClientRect();
        const top = document.elementFromPoint(Math.round(r.x + r.width / 2),
                                              Math.round(r.y + r.height / 2));
        return { label: it.innerText.trim().replace(/\s+/g, " "),
                 reachable: top === it || it.contains(top),
                 blockedBy: top ? (top.id || top.className) : "nothing" };
      });
    });
    reach.filter(i => !i.reachable).forEach(i =>
      failures.push(`portal: menu item "${i.label}" is not clickable — covered by ${i.blockedBy}`));

    // Sign out must actually sign you out — and be redrawn afterwards. It
    // cleared storage but left the chip showing the name of the account you
    // had just left, which reads as a button that did nothing.
    page.on("dialog", async d => { await d.accept(); });
    await page.evaluate(() => {
      // Re-open if the previous assertions left it closed; the item only
      // exists while the menu is rendered.
      const menu = document.getElementById("acct-menu");
      if (menu.classList.contains("hidden")) document.getElementById("acct-btn").click();
      document.getElementById("acct-signout-item").click();
    });
    await new Promise(r => setTimeout(r, 400));
    s = await page.evaluate(() => ({
      token: localStorage.getItem("rc_identity_token"),
      guestName: localStorage.getItem("rollcall_name"),
      label: document.getElementById("acct-label").innerText,
      verifyScreen: document.getElementById("verify-screen").style.display !== "none",
    }));
    if (s.token) failures.push("portal: sign out left the identity token behind");
    if (s.guestName) failures.push("portal: sign out left the group page's guest name behind");
    if (!/sign in/i.test(s.label)) failures.push(`portal: chip still reads "${s.label}" after sign out`);
    if (!s.verifyScreen) failures.push("portal: sign out did not return to the verify screen");

    // ...and a signed-OUT visitor must not be offered Sign out at all: with
    // nothing stored, the button cleared nothing and nothing changed.
    await page.evaluate(() => {
      localStorage.clear();
      location.reload();
    });
    await new Promise(r => setTimeout(r, 700));
    s = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      return { text: document.getElementById("acct-menu").innerText,
               label: document.getElementById("acct-label").innerText };
    });
    if (/sign out/i.test(s.text)) failures.push("portal: signed-out menu still offers Sign out");
    if (!/sign in/i.test(s.text)) failures.push(`portal: signed-out menu has no Sign in — got "${s.text.replace(/\n/g, " | ")}"`);
    if (!/sign in/i.test(s.label)) failures.push(`portal: signed-out chip reads "${s.label}"`);

    // A pageerror here would previously have been the stale
    // $id("portal-identity") throwing on load.
    if (errs.length) failures.push(`portal: JS error — ${errs[0]}`);

    console.log(`  portal: account chip, menu and sign-out exercised`);
    await page.close();
  }

  // ── New Rollcall modal: optional template on the Once tab ────────────────
  // Static analysis (web_ui_check.py) proves every id/handler here resolves;
  // it cannot prove the runtime SHOW/HIDE toggle actually flips the right
  // blocks, which is exactly the class of bug a typo'd element id produces
  // (both blocks visible, or both hidden, or the wrong one shown) without
  // ever throwing a JS error. Driven through the real app.js in a real
  // browser, not a re-implementation of the toggle logic.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));

    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 400));
    await page.evaluate(() => {
      document.getElementById("loading").classList.add("hidden");
      document.getElementById("main").classList.remove("hidden");
    });

    let s = await page.evaluate(() => {
      openNewRollcallModal();
      _nrcSetTiming("schedule");
      // Default recurrence is "once" — the optional block should already
      // be the one showing, with no re-render needed to reach that state.
      const vis = id => {
        const e = document.getElementById(id);
        return !!e && e.offsetParent !== null;
      };
      return {
        recurrence: document.getElementById("nrc-recurrence")?.value,
        optionalVisibleByDefault: vis("nrc-tmplname-optional"),
        requiredVisibleByDefault: vis("nrc-tmplname-required"),
      };
    });
    if (s.recurrence !== "once") failures.push(`nrc: default recurrence was "${s.recurrence}", expected "once"`);
    if (!s.optionalVisibleByDefault) failures.push("nrc: optional template block is not visible on the default Once tab");
    if (s.requiredVisibleByDefault) failures.push("nrc: required template block is ALSO visible on the Once tab (should be hidden)");

    // Switch to a recurring type — the required field must take over, since
    // a repeat genuinely needs a real, named template to live in between
    // firings (unlike a one-off, which schedule_once handles on its own).
    s = await page.evaluate(() => {
      document.getElementById("nrc-recurrence").value = "weekly";
      _nrcOnRecurrenceChange("weekly");
      const vis = id => {
        const e = document.getElementById(id);
        return !!e && e.offsetParent !== null;
      };
      return { optional: vis("nrc-tmplname-optional"), required: vis("nrc-tmplname-required") };
    });
    if (s.optional) failures.push("nrc: optional template block is still visible after switching to Weekly");
    if (!s.required) failures.push("nrc: required template block did not appear after switching to Weekly");

    // Back to Once, then exercise the checkbox itself — unchecked means the
    // name row stays hidden (nothing will be kept); checking it reveals the
    // name input, matching the immediate-start tab's own established pattern.
    s = await page.evaluate(() => {
      document.getElementById("nrc-recurrence").value = "once";
      _nrcOnRecurrenceChange("once");
      const rowHiddenInitially = document.getElementById("nrc-once-template-row").classList.contains("hidden");
      const check = document.getElementById("nrc-once-save-template-check");
      check.checked = true;
      check.dispatchEvent(new Event("change"));
      const rowVisibleAfterCheck = !document.getElementById("nrc-once-template-row").classList.contains("hidden");
      check.checked = false;
      check.dispatchEvent(new Event("change"));
      const rowHiddenAfterUncheck = document.getElementById("nrc-once-template-row").classList.contains("hidden");
      return { rowHiddenInitially, rowVisibleAfterCheck, rowHiddenAfterUncheck };
    });
    if (!s.rowHiddenInitially) failures.push("nrc: once-template name row is visible before the checkbox is ever checked");
    if (!s.rowVisibleAfterCheck) failures.push("nrc: once-template name row did not appear after checking 'save as template'");
    if (!s.rowHiddenAfterUncheck) failures.push("nrc: once-template name row stayed visible after unchecking 'save as template'");

    if (errs.length) failures.push(`nrc: JS error — ${errs[0]}`);

    console.log(`  new-rollcall: Once tab's template field is optional, toggles correctly`);
    await page.close();
  }

  // ── Theme (Light / Dark / Auto) ─────────────────────────────────────────
  // One shared implementation (/shared/theme-init.js) behind every surface.
  // It replaced a two-way toggle with no way back to following the device,
  // copied three times. Assert the modes, the live device follow, the old
  // rc_dark migration, and that no page still carries its own copy.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(`${page.url().replace(BASE, "")}: ${e.message}`));
    const state = () => page.evaluate(() => {
      let stored = null, old = null;
      try { stored = localStorage.getItem("rc_theme"); old = localStorage.getItem("rc_dark"); } catch (e) {}
      return { dark: document.documentElement.classList.contains("dark"),
               mode: window.rcTheme && rcTheme.get(), stored, old,
               icons: [...document.querySelectorAll("[data-theme-toggle]")].map(b => b.textContent) };
    });
    const GROUP = `${BASE}/web/group/testtoken123`;

    // Existing users: a saved rc_dark carries over instead of resetting.
    await page.goto(GROUP, { waitUntil: "load" });
    await page.evaluate(() => { localStorage.clear(); localStorage.setItem("rc_dark", "1"); });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    await page.reload({ waitUntil: "load" });
    let s = await state();
    if (!s.dark || s.mode !== "dark") failures.push(`theme: rc_dark=1 did not carry over as Dark (mode=${s.mode}, dark=${s.dark})`);
    if (s.stored !== "dark" || s.old !== null) failures.push(`theme: rc_dark not migrated to rc_theme (rc_theme=${s.stored}, rc_dark=${s.old})`);

    // Auto, and it follows the device live — no reload.
    await page.evaluate(() => { localStorage.clear(); });
    await page.reload({ waitUntil: "load" });
    s = await state();
    if (s.mode !== "auto" || s.dark) failures.push(`theme: fresh visitor on a light device should be Auto/light (mode=${s.mode}, dark=${s.dark})`);
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
    await new Promise(r => setTimeout(r, 100));
    s = await state();
    if (!s.dark) failures.push("theme: Auto did not follow the device switching to dark");

    // Header button cycles Auto → Light → Dark → Auto, with real clicks.
    const seen = [];
    for (let i = 0; i < 3; i++) {
      await page.click("#theme-btn");
      s = await state();
      seen.push(`${s.mode}/${s.dark ? "dark" : "light"}`);
    }
    if (seen.join(",") !== "light/light,dark/dark,auto/dark")
      failures.push(`theme: header button cycle wrong — got ${seen.join(", ")}`);
    if (new Set(s.icons).size !== 1) failures.push(`theme: header toggles disagree on the mode: ${s.icons.join(" ")}`);

    // Account menu: segmented row, real click, menu stays open.
    await page.click("#acct-btn");
    await new Promise(r => setTimeout(r, 100));
    const opts = await page.$$eval("#acct-menu [data-theme-opt]", els => els.map(e => e.dataset.themeOpt));
    if (opts.join(",") !== "light,dark,auto") failures.push(`theme: account menu theme row wrong — ${JSON.stringify(opts)}`);
    else {
      await page.click('#acct-menu [data-theme-opt="light"]');
      s = await state();
      const m = await page.evaluate(() => ({
        open: !document.getElementById("acct-menu").classList.contains("hidden"),
        on: [...document.querySelectorAll("#acct-menu [data-theme-opt].on")].map(e => e.dataset.themeOpt) }));
      if (s.mode !== "light" || s.dark) failures.push(`theme: menu Light did not apply (mode=${s.mode}, dark=${s.dark})`);
      if (!m.open) failures.push("theme: picking a theme closed the account menu");
      if (m.on.join() !== "light") failures.push(`theme: menu highlights ${JSON.stringify(m.on)}, expected light`);
    }

    // Every other surface runs the same code, not a leftover copy.
    for (const p of ["/web/", "/portal/index.html", "/help/index.html", "/index/index.html"]) {
      await page.goto(`${BASE}${p}`, { waitUntil: "load" });
      const r = await page.evaluate(() => ({
        shared: !!window.rcTheme && window.toggleTheme === window.rcTheme.cycle,
        buttons: document.querySelectorAll("[data-theme-toggle]").length,
        dark: document.documentElement.classList.contains("dark") }));
      if (!r.shared) failures.push(`theme: ${p} overrides the shared toggleTheme`);
      if (!r.buttons) failures.push(`theme: ${p} has no theme button`);
      if (r.dark) failures.push(`theme: ${p} ignored the saved Light choice`);
    }
    if (errs.length) failures.push(`theme: JS error — ${errs[0]}`);

    console.log("  theme: Light/Dark/Auto cycle, live device follow, rc_dark migration, all 4 surfaces");
    await page.close();
  }

  // Vote status for a guest (no Telegram). A guest who taps IN on a full
  // game is on the waitlist and used to see no status at all; and a guest
  // who types a real member's name must not be shown that member's vote —
  // their own tap would add a separate guest entry.
  {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on("request", req =>
      req.url().startsWith(BASE) ? req.continue()
                                 : req.respond({ status: 200, contentType: "text/plain", body: "" }));
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    const statusFor = async name => {
      await page.goto(`${BASE}/web/group/fulltoken`, { waitUntil: "load" });
      await page.evaluate(n => { localStorage.clear(); localStorage.setItem("rollcall_name", n); }, name);
      await page.reload({ waitUntil: "load" });
      await new Promise(r => setTimeout(r, 500));
      return page.evaluate(() => (document.getElementById("vote-status-row") || {}).innerText || "");
    };
    let t = await statusFor("Ravi K");
    if (!/WAITLIST/.test(t) || !/#1/.test(t))
      failures.push(`vote status: a waitlisted guest should see "on the WAITLIST (#1)", got "${t.trim()}"`);
    t = await statusFor("priya");
    if (/You're/.test(t))
      failures.push(`vote status: a guest named like a real member was shown that member's vote: "${t.trim()}"`);
    if (errs.length) failures.push(`vote status: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log("  vote status: waitlisted guest, guest sharing a member's name");
    await page.close();
  }

  // Sign-in without Telegram: an emailed link (?email_code=) signs in, is
  // stripped from the URL, joins the group, and the menu says "email" —
  // plus the Email option in the sign-in dialog and Create group.
  {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    const posts = [];
    page.on("request", req => {
      if (req.method() === "POST") posts.push(req.url().replace(BASE, ""));
      return req.url().startsWith(BASE) ? req.continue()
        : req.respond({ status: 200, contentType: "text/plain", body: "" });
    });
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(() => localStorage.clear());
    await page.goto(`${BASE}/web/group/testtoken123?email_code=abc123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 700));
    let s = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      return { tok: localStorage.getItem("rc_identity_token"), search: location.search,
               menu: document.getElementById("acct-menu").innerText };
    });
    if (s.tok !== "4503599627370500.9999999999.feedface") failures.push("email sign-in: id token not stored");
    if (s.search.includes("email_code")) failures.push("email sign-in: code left in the URL");
    if (!/Signed in with email/.test(s.menu)) failures.push(`email sign-in: menu should say "Signed in with email" — got "${s.menu.replace(/\n/g, " | ")}"`);
    if (/Email sign-in/.test(s.menu)) failures.push("email sign-in: an email-only account was offered 'add email'");
    if (!/Create a group/.test(s.menu)) failures.push("email sign-in: menu has no Create a group");
    if (!posts.some(u => u === "/api/v1/auth/email/verify")) failures.push("email sign-in: code was not POSTed");
    if (!posts.some(u => u === "/api/v1/web/group/testtoken123/join")) failures.push("email sign-in: did not join the group");

    // Create group from the menu → form → POST /web/groups → navigates.
    const opened = await page.evaluate(() => {
      document.querySelector('#acct-menu [data-on-click="closeAcctMenu openCreateGroup"]').click();
      const open = !document.getElementById("form-modal").classList.contains("hidden");
      if (open) document.getElementById("fs-gname").value = "Sunday Turf";
      return open;
    });
    if (!opened) failures.push("create group: form did not open");
    else {
      await Promise.all([
        page.waitForNavigation({ timeout: 5000 }).catch(() => null),
        page.evaluate(() => document.getElementById("form-submit").click()),
      ]);
      const at = await page.evaluate(() => location.pathname);
      if (at !== "/web/group/newgrouptoken") failures.push(`create group: expected to land on the new group, at ${at}`);
    }

    // Signed out: the sign-in dialog offers email when the server does.
    await page.evaluate(() => localStorage.clear());
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 500));
    s = await page.evaluate(() => {
      window.openSignIn();
      const btn = document.getElementById("picker-email-btn");
      const shown = btn && !btn.classList.contains("hidden");
      if (btn) btn.click();
      const row = document.getElementById("email-input-row");
      return { shown, rowOpen: row && !row.classList.contains("hidden") };
    });
    if (!s.shown) failures.push("sign-in: Email option hidden though the server offers it");
    if (!s.rowOpen) failures.push("sign-in: Email option did not open the email form");
    // Linking an email that already has its own account: the page asks, and
    // on yes posts the merge WITH the Telegram sign-in (and the code verify
    // carried it too — a link only works where it was asked for).
    const saved = { ...EMAIL_USER };
    Object.assign(EMAIL_USER, { user_id: 168415137, id_token: "168415137.9999999999.deadbeef", name: "Amit",
      purpose: "merge_offer", merge_from_user_id: 4503599627370500, merge_from_name: "Amit E",
      merge_token: "mt", merge_votes: 3, merge_groups: 1 });
    const heads = {};
    page.on("request", req => { if (req.method() === "POST") heads[req.url().replace(BASE, "")] = req.headers(); });
    let asked = "";
    page.on("dialog", d => { asked = d.message(); d.accept(); });
    await page.evaluate(() => {
      localStorage.clear();
      localStorage.setItem("rc_verified_tg_user_id", "168415137");
      localStorage.setItem("rc_verified_tg_name", "Amit");
      localStorage.setItem("rc_identity_token", "168415137.9999999999.deadbeef");
    });
    await page.goto(`${BASE}/web/group/testtoken123?email_code=mergecode`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 800));
    Object.keys(EMAIL_USER).forEach(k => delete EMAIL_USER[k]);
    Object.assign(EMAIL_USER, saved);
    if (!/already has its own RollCall account/.test(asked)) failures.push(`merge: no confirmation asked — "${asked}"`);
    const vh = heads["/api/v1/auth/email/verify"] || {};
    if (vh["x-identity-token"] !== "168415137.9999999999.deadbeef") failures.push("merge: code verify didn't carry the Telegram sign-in");
    const mh = heads["/api/v1/auth/email/merge"];
    if (!mh || mh["x-identity-token"] !== "168415137.9999999999.deadbeef") failures.push("merge: confirmed merge was not posted with the Telegram sign-in");

    if (errs.length) failures.push(`email sign-in: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log("  email sign-in: link redeemed + joined, menu, create group, sign-in option, merge offer");
    await page.close();
  }

  // Account sheet: from the menu and from ?account=1 (the link in emails);
  // the email setting saves, "sign out everywhere" keeps THIS device signed
  // in with the fresh token, and it fits a 320px phone.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 320, height: 700 });
    await page.setRequestInterception(true);
    const posts = {};
    page.on("request", req => {
      if (req.method() === "POST") posts[req.url().replace(BASE, "")] = req.postData() || "";
      return req.url().startsWith(BASE) ? req.continue()
        : req.respond({ status: 200, contentType: "text/plain", body: "" });
    });
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    page.on("dialog", d => d.accept());
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(u => {
      localStorage.clear();
      localStorage.setItem("rc_verified_tg_user_id", String(u.user_id));
      localStorage.setItem("rc_verified_tg_name", u.name);
      localStorage.setItem("rc_identity_token", u.id_token);
    }, EMAIL_USER);
    await page.reload({ waitUntil: "load" });
    await new Promise(r => setTimeout(r, 500));
    const item = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      const b = document.querySelector('#acct-menu [data-on-click="closeAcctMenu openAccount"]');
      if (b) b.click();
      return !!b;
    });
    if (!item) failures.push("account: menu has no Account item");
    await new Promise(r => setTimeout(r, 400));
    let a = await page.evaluate(() => {
      const m = document.getElementById("account-modal");
      const sel = document.getElementById("acct-notify");
      if (sel) { sel.value = "off"; sel.dispatchEvent(new Event("change", { bubbles: true })); }
      return { open: m && !m.classList.contains("hidden"), text: (document.getElementById("account-body") || {}).innerText || "",
               sel: !!sel, wide: document.documentElement.scrollWidth > window.innerWidth };
    });
    if (!a.open) failures.push("account: sheet did not open");
    if (!/asha@example\.com/.test(a.text)) failures.push(`account: email not shown — "${a.text.slice(0, 80)}"`);
    for (const label of ["Change email", "Sign out on all other devices", "Delete account"])
      if (!a.text.includes(label)) failures.push(`account: no "${label}" button`);
    if (!a.sel) failures.push("account: no email-notification setting for an email-only account");
    if (a.wide) failures.push("account: sheet overflows a 320px screen");
    await new Promise(r => setTimeout(r, 300));
    if (!/"pref":"off"/.test(posts["/api/v1/auth/account/email-notify"] || "")) failures.push("account: email setting was not saved");
    await page.evaluate(() => document.querySelector('#account-body [data-on-click="acctSignOutEverywhere"]').click());
    await new Promise(r => setTimeout(r, 400));
    const tok = await page.evaluate(() => localStorage.getItem("rc_identity_token"));
    if (tok !== "4503599627370500.9999999999.fresh") failures.push(`account: sign out everywhere didn't keep this device signed in (${tok})`);
    // The "Email settings" link in a notification email.
    await page.goto(`${BASE}/web/group/testtoken123?account=1`, { waitUntil: "load" });
    await new Promise(r => setTimeout(r, 900));
    a = await page.evaluate(() => ({ open: !document.getElementById("account-modal").classList.contains("hidden"),
                                     search: location.search }));
    if (!a.open) failures.push("account: ?account=1 did not open the sheet");
    if (a.search.includes("account=")) failures.push("account: ?account=1 left in the URL");
    if (errs.length) failures.push(`account: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log("  account: menu + ?account=1, email setting saved, sign out everywhere keeps this device, 320px");
    await page.close();
  }

  // Membership card for someone without Telegram who isn't in the group yet:
  // "Ask to join" → waiting; once a member, the card goes and the menu
  // offers "Leave this group". Fits 320px.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 320, height: 700 });
    await page.setRequestInterception(true);
    let ms = { member: false, can_join: true, needs_approval: true, pending: false, denied: false };
    const posts = [];
    page.on("request", req => {
      const u = req.url();
      if (/\/admin-status/.test(u)) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ is_admin: false }) });
      if (/\/membership$/.test(u)) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify(ms) });
      if (/\/join$/.test(u) && req.method() === "POST") {
        posts.push(u); ms = { ...ms, pending: true, can_join: true };
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ joined: false, pending: true }) });
      }
      return u.startsWith(BASE) ? req.continue() : req.respond({ status: 200, contentType: "text/plain", body: "" });
    });
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(u => {
      localStorage.clear();
      localStorage.setItem("rc_verified_tg_user_id", String(u.user_id));
      localStorage.setItem("rc_verified_tg_name", u.name);
      localStorage.setItem("rc_identity_token", u.id_token);
    }, EMAIL_USER);
    await page.reload({ waitUntil: "load" });
    await new Promise(r => setTimeout(r, 700));
    let c = await page.evaluate(() => {
      const el = document.getElementById("membership-card");
      return { shown: el && !el.classList.contains("hidden"), text: el ? el.innerText : "",
               wide: document.documentElement.scrollWidth > window.innerWidth };
    });
    if (!c.shown) failures.push("membership: card not shown to a non-member without Telegram");
    if (!/Ask to join/.test(c.text)) failures.push(`membership: no "Ask to join" — "${c.text.slice(0, 80)}"`);
    if (c.wide) failures.push("membership: card overflows a 320px screen");
    await page.evaluate(() => document.querySelector('#membership-card [data-on-click="joinThisGroup"]').click());
    await new Promise(r => setTimeout(r, 500));
    c = await page.evaluate(() => document.getElementById("membership-card").innerText);
    if (!posts.length) failures.push("membership: Ask to join did not POST /join");
    if (!/Waiting for an admin/.test(c)) failures.push(`membership: not shown as waiting after asking — "${c.slice(0, 80)}"`);
    ms = { member: true, can_join: false, can_leave: true };
    await page.reload({ waitUntil: "load" });
    await new Promise(r => setTimeout(r, 700));
    c = await page.evaluate(() => {
      document.getElementById("acct-btn").click();
      return { hidden: document.getElementById("membership-card").classList.contains("hidden"),
               leave: !!document.querySelector('#acct-menu [data-on-click="closeAcctMenu leaveThisGroup"]') };
    });
    if (!c.hidden) failures.push("membership: card still shown to a member");
    if (!c.leave) failures.push("membership: member without Telegram has no 'Leave this group' in the menu");
    if (errs.length) failures.push(`membership: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log("  membership: ask to join → waiting, member gets Leave this group, 320px");
    await page.close();
  }

  // Lineup: the two teams on a pitch (services/lineup.py). At 320px the pitch
  // fits, every spot sits inside it, a drag from Subs onto an empty spot and
  // a tap-then-tap both send lineup_place, and nothing overflows.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 320, height: 1000 });
    await page.setRequestInterception(true);
    const slots = (form, team, names) => {
      const lines = [1].concat(form.split("-").map(Number));
      const roles = lines.length === 4 ? ["GK", "DEF", "MID", "FWD"] : ["GK", "DEF", "FWD"];
      const out = []; let k = 0;
      lines.forEach((n, li) => { for (let i = 0; i < n; i++) {
        const name = names[k++] || null;
        out.push({ pos: `${li}:${i}`, role: roles[li], key: name ? `u${team}${k}` : null, name });
      } });
      return out;
    };
    const lineup = {
      available: true, sport: "football", side: 7, side_auto: true, options: ["2-3-1", "3-2-1", "2-2-2"],
      locked: false, placed: 13, text: "⚽ Lineup — Lineup Game · 7-a-side",
      teams: [
        { no: 1, label: "A", emoji: "🔴", formation: "2-3-1",
          slots: slots("2-3-1", 1, ["Ravi", "Priya", "Sam", "Amit", "Kiran", "Leo"]),   // FWD empty
          subs: [{ key: "pdev", name: "Dev" }] },
        { no: 2, label: "B", emoji: "🔵", formation: "3-2-1",
          slots: slots("3-2-1", 2, ["Arjun", "Zaid", "Neha", "Omar", "Tanvi", "Vikram", "Joel Fernandes-Long"]),
          subs: [] },
      ],
    };
    const teams = { formed: true, count: 2, locked: false, locked_by: null, players_in: 14,
                    teams: [{ no: 1, label: "A", emoji: "🔴", players: [] }, { no: 2, label: "B", emoji: "🔵", players: [] }],
                    lineup };
    const group = { group_name: "Test FC", bot_username: "TestBot", teams_enabled: true, sport: "football",
                    rollcalls: [{ num: 1, id: 7, rollcall_id: 7, title: "Lineup Game", status: "open",
                                  in: [], out: [], maybe: [], waiting: [], teams }] };
    const posts = [];
    page.on("request", req => {
      const u = req.url();
      if (/\/api\/v1\/web\/group\/testtoken123$/.test(u.split("?")[0]))
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify(group) });
      if (/\/teams$/.test(u) && req.method() === "POST") {
        posts.push(JSON.parse(req.postData() || "{}"));
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ ...teams, message: "" }) });
      }
      if (/\/admin-status/.test(u)) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ is_admin: false }) });
      return u.startsWith(BASE) ? req.continue() : req.respond({ status: 200, contentType: "text/plain", body: "" });
    });
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(u => {
      localStorage.clear();
      localStorage.setItem("rc_verified_tg_user_id", String(u.user_id));
      localStorage.setItem("rc_verified_tg_name", u.name);
      localStorage.setItem("rc_identity_token", u.id_token);
    }, EMAIL_USER);
    await page.reload({ waitUntil: "load" });
    await new Promise(r => setTimeout(r, 800));
    const g = await page.evaluate(() => {
      const card = document.getElementById("teams-card");
      const pitch = card && card.querySelector(".lu-pitch");
      if (!pitch) return { pitch: false };
      const pr = pitch.getBoundingClientRect();
      const discs = [...pitch.querySelectorAll(".lu-disc")].map(d => d.getBoundingClientRect());
      const inside = discs.every(r => r.left >= pr.left - 1 && r.right <= pr.right + 1 && r.top >= pr.top - 1 && r.bottom <= pr.bottom + 1);
      let overlap = 0;
      for (let i = 0; i < discs.length; i++) for (let j = i + 1; j < discs.length; j++) {
        const a = discs[i], b = discs[j];
        if (a.left < b.right - 2 && b.left < a.right - 2 && a.top < b.bottom - 2 && b.top < a.bottom - 2) overlap++;
      }
      const labels = [...pitch.querySelectorAll(".lu-nm")].map(n => n.getBoundingClientRect());
      let covers = 0;
      for (const l of labels) for (const d of discs)
        if (l.left < d.right - 2 && d.left < l.right - 2 && l.top < d.bottom - 2 && d.top < l.bottom - 2) covers++;
      overlap += covers;
      return { pitch: true, spots: discs.length, inside, overlap, w: pr.width,
               wide: document.documentElement.scrollWidth > window.innerWidth,
               formSelects: card.querySelectorAll(".lu-form select").length,
               copy: !!card.querySelector('[data-on-click="lineupCopy"]') };
    });
    if (!g.pitch) failures.push("lineup: no pitch drawn for a group with a sport and 2 teams");
    else {
      if (g.spots !== 14) failures.push(`lineup: expected 14 spots on the pitch, got ${g.spots}`);
      if (!g.inside) failures.push("lineup: a spot is drawn outside the pitch at 320px");
      if (g.overlap) failures.push(`lineup: ${g.overlap} spot/name overlap(s) at 320px`);
      if (g.wide) failures.push("lineup: the card overflows a 320px screen");
      if (g.formSelects !== 2) failures.push("lineup: each team should have its own formation picker");
      if (!g.copy) failures.push("lineup: no Copy text button");
    }
    // Drag Dev (a sub) onto Team A's empty forward spot.
    await page.evaluate(() => document.querySelector("#teams-card .lu-pitch").scrollIntoView({ block: "start" }));
    await new Promise(r => setTimeout(r, 200));
    const box = async sel => page.evaluate(s => { const r = document.querySelector(s).getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }, sel);
    if (g.pitch) {
      const from = await box('.lu-chip[data-lu-key="pdev"]');
      const to = await box('.lu-slot[data-lu-team="1"][data-lu-pos="3:0"] .lu-disc');
      await page.mouse.move(from.x, from.y); await page.mouse.down();
      await page.mouse.move(from.x + 10, from.y - 10, { steps: 3 });
      await page.mouse.move(to.x, to.y, { steps: 8 }); await page.mouse.up();
      await new Promise(r => setTimeout(r, 400));
      const drop = posts.find(p => p.action === "lineup_place");
      if (!drop || drop.who_key !== "pdev" || drop.team_no !== 1 || drop.position !== "3:0")
        failures.push(`lineup: dragging a sub onto an empty spot sent ${JSON.stringify(drop)}`);
      // Tap a player, then a spot on the other team.
      posts.length = 0;
      await page.evaluate(() => document.querySelector('.lu-slot[data-lu-key="u11"]').click());
      await page.evaluate(() => document.querySelector('.lu-slot[data-lu-team="2"][data-lu-pos="1:0"]').click());
      await new Promise(r => setTimeout(r, 400));
      const tap = posts.find(p => p.action === "lineup_place");
      if (!tap || tap.who_key !== "u11" || tap.team_no !== 2 || tap.position !== "1:0")
        failures.push(`lineup: tap-then-tap sent ${JSON.stringify(tap)}`);
    }
    if (errs.length) failures.push(`lineup: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log(`  lineup: 14 spots inside a ${Math.round(g.w || 0)}px pitch, no overlaps, drag + tap place, 320px`);
    await page.close();
  }

  // Lineup, 3 teams (services/lineup.py layout "boards"): a half-pitch per
  // team. At a phone (320, stacked) and a laptop (1024, two per row — the
  // page column is ~550px, and a third board would be too small) every spot sits
  // inside its own board, no spot or name overlaps another, the page doesn't
  // scroll sideways, and dragging a player onto another team's board works.
  for (const width of [320, 1024]) {
    const page = await browser.newPage();
    await page.setViewport({ width, height: 1400 });
    await page.setRequestInterception(true);
    const slots = (form, team, names) => {
      const lines = [1].concat(form.split("-").map(Number));
      const roles = lines.length === 4 ? ["GK", "DEF", "MID", "FWD"] : ["GK", "DEF", "FWD"];
      const out = []; let k = 0;
      lines.forEach((n, li) => { for (let i = 0; i < n; i++) {
        const name = names[k++] || null;
        out.push({ pos: `${li}:${i}`, role: roles[li], key: name ? `u${team}${k}` : null, name });
      } });
      return out;
    };
    const lineup = {
      available: true, sport: "football", kind: "pitch", layout: "boards", side: 6, side_auto: true,
      options: ["2-2-1", "2-1-2", "3-1-1"], locked: false, placed: 17, text: "⚽ Lineup — Rotation · 6-a-side · 3 teams",
      teams: [
        { no: 1, label: "A", emoji: "🔴", formation: "2-2-1",
          slots: slots("2-2-1", 1, ["Ravi", "Priya", "Sam", "Amit", null, "Leo"]), subs: [{ key: "pdev", name: "Dev" }] },
        { no: 2, label: "B", emoji: "🔵", formation: "2-1-2",
          slots: slots("2-1-2", 2, ["Arjun", "Zaid", "Neha", "Omar", "Tanvi", "Vikram"]), subs: [] },
        { no: 3, label: "C", emoji: "🟢", formation: "3-1-1",
          slots: slots("3-1-1", 3, ["Joel Fernandes-Long", "Kiran", "Meera", "Rohan", "Sahil", "Yash"]), subs: [] },
      ],
    };
    const tl = lineup.teams.map(t => ({ no: t.no, label: t.label, emoji: t.emoji, players: [] }));
    const teams = { formed: true, count: 3, locked: false, locked_by: null, players_in: 18, teams: tl, lineup };
    const group = { group_name: "Test FC", bot_username: "TestBot", teams_enabled: true, sport: "football", activity: "football",
                    rollcalls: [{ num: 1, id: 8, rollcall_id: 8, title: "Rotation", status: "open",
                                  in: [], out: [], maybe: [], waiting: [], teams }] };
    const posts = [];
    page.on("request", req => {
      const u = req.url();
      if (/\/api\/v1\/web\/group\/testtoken123$/.test(u.split("?")[0]))
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify(group) });
      if (/\/teams$/.test(u) && req.method() === "POST") {
        posts.push(JSON.parse(req.postData() || "{}"));
        return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ ...teams, message: "" }) });
      }
      if (/\/admin-status/.test(u)) return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ is_admin: false }) });
      return u.startsWith(BASE) ? req.continue() : req.respond({ status: 200, contentType: "text/plain", body: "" });
    });
    const errs = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    await page.goto(`${BASE}/web/group/testtoken123`, { waitUntil: "load" });
    await page.evaluate(u => {
      localStorage.clear();
      localStorage.setItem("rc_verified_tg_user_id", String(u.user_id));
      localStorage.setItem("rc_verified_tg_name", u.name);
      localStorage.setItem("rc_identity_token", u.id_token);
    }, EMAIL_USER);
    await page.reload({ waitUntil: "load" });
    await new Promise(r => setTimeout(r, 800));
    const g = await page.evaluate(() => {
      const card = document.getElementById("teams-card");
      const boards = card ? [...card.querySelectorAll(".lu-board .lu-pitch")] : [];
      let outside = 0, overlap = 0, spots = 0;
      const rects = boards.map(b => b.getBoundingClientRect());
      const tops = rects.map(r => Math.round(r.top));
      let boardsOverlap = 0;
      for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i], b = rects[j];
        if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) boardsOverlap++;
      }
      for (const pitch of boards) {
        const pr = pitch.getBoundingClientRect();
        const discs = [...pitch.querySelectorAll(".lu-disc")].map(d => d.getBoundingClientRect());
        const labels = [...pitch.querySelectorAll(".lu-nm")].map(n => n.getBoundingClientRect());
        spots += discs.length;
        for (const r of discs.concat(labels))
          if (r.left < pr.left - 1 || r.right > pr.right + 1 || r.top < pr.top - 1 || r.bottom > pr.bottom + 1) outside++;
        const hit = (a, b) => a.left < b.right - 2 && b.left < a.right - 2 && a.top < b.bottom - 2 && b.top < a.bottom - 2;
        for (let i = 0; i < discs.length; i++) for (let j = i + 1; j < discs.length; j++) if (hit(discs[i], discs[j])) overlap++;
        for (const l of labels) for (const d of discs) if (hit(l, d)) overlap++;
        for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) if (hit(labels[i], labels[j])) overlap++;
      }
      return { boards: boards.length, spots, outside, overlap, boardsOverlap, rows: new Set(tops).size,
               wide: document.documentElement.scrollWidth > window.innerWidth,
               formSelects: card ? card.querySelectorAll(".lu-form select").length : 0,
               w: boards[0] ? boards[0].getBoundingClientRect().width : 0 };
    });
    const tag = `lineup 3 teams @${width}`;
    if (g.boards !== 3) failures.push(`${tag}: expected 3 half-pitch boards, got ${g.boards}`);
    else {
      if (g.spots !== 18) failures.push(`${tag}: expected 18 spots, got ${g.spots}`);
      if (g.outside) failures.push(`${tag}: ${g.outside} spot(s)/name(s) spill outside their board`);
      if (g.overlap) failures.push(`${tag}: ${g.overlap} spot/name overlap(s)`);
      if (g.wide) failures.push(`${tag}: the page scrolls sideways`);
      if (g.formSelects !== 3) failures.push(`${tag}: each team should have its own formation picker`);
      if (g.boardsOverlap) failures.push(`${tag}: boards overlap each other`);
      if (width >= 1024 && g.rows !== 2) failures.push(`${tag}: expected two boards per row on a laptop, got ${g.rows} rows`);
      if (width <= 320 && g.rows !== 3) failures.push(`${tag}: boards should stack on a phone, got ${g.rows} rows`);
      // Team C's player onto Team A's empty spot (another board): dragged on a
      // laptop, where the boards sit side by side; tap-then-tap on a phone,
      // where they're stacked and a drag would have to scroll.
      if (width >= 1024) {
        // A and C are in different rows; scroll so both are on screen.
        await page.evaluate(() => document.querySelector(".lu-boards").scrollIntoView({ block: "start" }));
        await new Promise(r => setTimeout(r, 200));
        const box = sel => page.evaluate(s => { const r = document.querySelector(s).getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }, sel);
        const from = await box('.lu-slot[data-lu-key="u35"] .lu-disc');
        const to = await box('.lu-slot[data-lu-team="1"][data-lu-pos="2:0"] .lu-disc');
        await page.mouse.move(from.x, from.y); await page.mouse.down();
        await page.mouse.move(from.x + 10, from.y - 10, { steps: 3 });
        await page.mouse.move(to.x, to.y, { steps: 10 }); await page.mouse.up();
      } else {
        await page.evaluate(() => document.querySelector('.lu-slot[data-lu-key="u35"]').click());
        await page.evaluate(() => document.querySelector('.lu-slot[data-lu-team="1"][data-lu-pos="2:0"]').click());
      }
      await new Promise(r => setTimeout(r, 400));
      const drop = posts.find(p => p.action === "lineup_place");
      if (!drop || drop.who_key !== "u35" || drop.team_no !== 1 || drop.position !== "2:0")
        failures.push(`${tag}: placing across boards sent ${JSON.stringify(drop)}`);
    }
    if (errs.length) failures.push(`${tag}: JS error — ${errs[0]}`);
    await page.evaluate(() => localStorage.clear());
    console.log(`  ${tag}: ${g.boards} boards (${Math.round(g.w)}px), ${g.spots} spots inside, no overlaps, placed across boards`);
    await page.close();
  }

  // Landing page: both ways in (Telegram, and the web without it), at 320px.
  {
    const page = await browser.newPage();
    await page.setViewport({ width: 320, height: 700 });
    await page.goto(`${BASE}/index/index.html`, { waitUntil: "load" });
    const l = await page.evaluate(() => ({
      web: !!document.querySelector('a.cta[href="/web/?create=1"]'),
      tg: !!document.querySelector("a.cta:not(.cta-alt)"),
      wide: document.documentElement.scrollWidth > window.innerWidth,
    }));
    if (!l.web) failures.push("landing: no 'Start on the web' way in for people without Telegram");
    if (!l.tg) failures.push("landing: no 'Add to a Telegram group' button");
    if (l.wide) failures.push("landing: overflows a 320px screen");
    console.log("  landing: Telegram and web-only ways in, 320px");
    await page.close();
  }

  // shared/actions.js — the one listener every button now goes through.
  {
    const page = await browser.newPage();
    const errs = [], logged = [];
    page.on("pageerror", e => errs.push(String(e.message)));
    page.on("console", m => logged.push(m.text()));
    await page.goto(`${BASE}/web/`, { waitUntil: "load" });
    const r = await page.evaluate(() => {
      const out = {};
      const nasty = `O'Brien "x" <img src=x> & \\ \u2603`;
      window.__probe = function (a, b, el) { out.args = [a, b]; out.el = el && el.id; };
      window.__outer = function () { out.outer = (out.outer || 0) + 1; };
      window.__stopper = function (ev) { ev.stopPropagation(); out.stopped = true; };
      document.body.insertAdjacentHTML("beforeend", `
        <div id="t-outer" data-on-click="__outer">
          <button id="t-btn" data-on-click="__probe" data-args="${rcArgs(nasty, 7, RC_THIS)}"><span id="t-inner">x</span></button>
          <a id="t-stop" data-on-click="$stop">link</a>
          <button id="t-sp" data-on-click="__stopper" data-args="${rcArgs(RC_EVENT)}">s</button>
          <button id="t-native" data-on-click="fetch" data-args="${rcArgs("/api/x")}">n</button>
        </div>`);
      let docSaw = 0;
      document.addEventListener("click", () => { docSaw++; });
      document.getElementById("t-inner").click();      // bubbles to button, then outer
      out.argsOk = JSON.stringify(out.args) === JSON.stringify([nasty, 7]) && out.el === "t-btn";
      out.outerAfterBtn = out.outer;
      document.getElementById("t-stop").click();        // $stop: outer must NOT run
      out.outerAfterStop = out.outer;
      const before = docSaw;
      document.getElementById("t-sp").click();          // stopPropagation keeps it from document
      out.docAfterStop = docSaw - before;
      document.getElementById("t-native").click();      // built-in: must be refused
      return out;
    });
    if (!r.argsOk) failures.push(`actions: data-args did not round-trip (${JSON.stringify(r.args)})`);
    if (r.outerAfterBtn !== 1) failures.push("actions: handler did not bubble to the ancestor once");
    if (r.outerAfterStop !== 1) failures.push("actions: $stop let the click reach the ancestor's handler");
    if (r.docAfterStop !== 0 || !r.stopped) failures.push("actions: stopPropagation() in a handler did not stop the click");
    if (!logged.some(t => t.includes("no such page function fetch")))
      failures.push("actions: a built-in (fetch) was callable from data-on-click");
    if (errs.length) failures.push(`actions: JS error — ${errs[0]}`);
    console.log("  actions: args round-trip, bubbling, $stop, stopPropagation, built-ins refused");
    await page.close();
  }

  await browser.close();
  server.close();

  if (!CSP) failures.push("CSP: could not load the production policy, so inline-script regressions went unchecked");
  for (const v of new Set(cspViolations)) failures.push(`CSP refused: ${v}`);

  if (failures.length) {
    console.log(`\nFAILED: ${failures.length} layout problem(s)\n`);
    failures.forEach(f => console.log(`  ✗ ${f}`));
    process.exit(1);
  }
  console.log(`\nPASSED: layout correct at ${WIDTHS.length} viewport widths`);
})().catch(e => { console.error("layout check crashed:", e); process.exit(1); });
