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

const ROUTES = [
  [/\/web\/group\/emptytoken$/, GROUP_EMPTY],
  [/\/web\/group\/[^/]+$/, GROUP],
  [/admin-status/, { is_admin: true }],
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
      };
    });
    if (!s.tgMode) failures.push("tg-mode: Telegram Mini App not detected from initDataUnsafe");
    if (!(s.barHeight > 0)) failures.push("tg-mode: header not rendered inside the Mini App");
    if (!(s.acctWidth > 0)) failures.push("tg-mode: account chip not reachable inside the Mini App");
    if (!(s.adminWidth > 0)) failures.push("tg-mode: Admin button not reachable inside the Mini App");
    if (s.overflow > 1) failures.push(`tg-mode: page overflows by ${s.overflow}px`);
    if (errs.length) failures.push(`tg-mode: JS error — ${errs[0]}`);

    console.log("  tg-mode: header controls survive inside the Telegram Mini App");
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
      };
    });

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

  await browser.close();
  server.close();

  if (failures.length) {
    console.log(`\nFAILED: ${failures.length} layout problem(s)\n`);
    failures.forEach(f => console.log(`  ✗ ${f}`));
    process.exit(1);
  }
  console.log(`\nPASSED: layout correct at ${WIDTHS.length} viewport widths`);
})().catch(e => { console.error("layout check crashed:", e); process.exit(1); });
