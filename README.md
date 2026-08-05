# RollCall Bot

A feature-rich Telegram bot for tracking event attendance in group chats. Members can mark themselves as **in**, **out**, or **maybe** for any event — with support for multiple simultaneous roll calls, waitlists, fee splitting, reminders, templates, ghost tracking, and more.

[![CI](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml/badge.svg)](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-9.9-green)](rollCall/version.json)

---

## Features

- **Attendance tracking** — in / out / maybe with optional comments
- **Multiple roll calls** — run up to 3 events simultaneously in one group
- **Attendance limits & waitlists** — cap attendees; overflow goes on a waitlist; promoted users get a private DM
- **Proxy responses** — mark attendance on behalf of non-Telegram members (`/sif`, `/sof`, `/smf`) — also available from the group web page for web admins
- **Event details** — title, date/time, location, and fee with automatic per-person cost splitting
- **Templates & scheduling** — save reusable configs; weekly, biweekly, or monthly auto-start; `/repeat` clones last week's game in one command
- **Reminders** — scheduled notifications before events; auto-closes at event time; `/auto_buzz` pings only non-voters before close
- **💰 Dues & Treasury** — `/settle_dues` splits the ground fee per head with a UPI QR to pay; configurable penalty tiers (late/no-show), waivers, collector flow with UPI memory, group fund, weekly dues reports & reminders — all on an append-only, tamper-evident ledger where every transaction is announced in the chat. `/new_season` resets balances for a fresh season without deleting history
- **Ghost tracking** — record no-shows per user, show leaderboard, prompt for reconfirmation on repeat offenders
- **Achievement badges** — streak and games-played milestones announced at close and shown on the web leaderboard
- **Statistics & history** — per-user attendance rate, streaks, personal bests, paginated session history, monthly wrap-up card & treasury statement
- **In-place panel editing** — votes update the panel message instead of flooding the chat
- **Admin audit log** — every admin action recorded; viewable with `/audit_log`
- **Admin controls** — restrict commands to designated group admins; manual status override with `/set_status`
- **Web voting** — shareable browser links for non-Telegram users; permanent per-group bookmarkable URL that works even when Telegram is down; installable PWA with push notifications
- **Manage your group from the web** — the group web page covers everything day-to-day: move/remove voters, create and edit templates, deep stats (session history, ghost leaderboard, response-time leaderboard), settings — sign in with Telegram, no token needed
- **Merge identities** — fold a repeated proxy name into the real member (or another proxy) it belongs to; stats, dues, streaks and ghost tracking combine retroactively, and unmerging is always safe (nothing is deleted, only un-linked)
- **User portal** — cross-group personal dashboard (`/portal/`): attendance, streaks, upcoming games, dues balance
- **Flexible web login** — Telegram deep-link verify, Telegram Login Widget (no app needed), admin-issued single-use links (`/weblogin`), and personal login codes (`/mytoken`) that work even when Telegram itself is unreachable
- **Telegram Mini App** — in-app voting interface via the Telegram menu button (no browser switch)
- **REST API** — FastAPI layer with bearer-token auth; powers the web + Mini App frontends
- **Webhook mode** — opt-in webhook support via `WEBHOOK_URL` env var (falls back to long-polling)
- **Dual database support** — SQLite (default) or PostgreSQL
- **Docker-ready** — Dockerfile + Docker Compose with Cloudflare Tunnel and daily-DB-backup sidecars
- **Health checks** — HTTP `/health` and `/ping` endpoints on port 8080

---

## Quick Start

### Prerequisites

- Python 3.10 or higher
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- The bot must be added as an **admin** in your Telegram group

### Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/amitvmane/RollCall.git
cd RollCall

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
# Edit .env and set your TELEGRAM_TOKEN and other options

# 4. Run the bot
cd rollCall
python runner.py
```

### Docker (Recommended)

```bash
# Build and start with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Using the Makefile (recommended)

The bundled `Makefile` wraps Docker Compose (with the `web` profile) into a
one-command workflow — it also **auto-detects the Cloudflare tunnel URL and
writes it into `.env`** for you, so you don't have to copy it by hand.

**Prerequisites:** Docker with the Compose plugin (`docker compose`), a
configured `.env` (`cp .env.example .env` and fill in `API_KEY`/`ADMIN1`), and
`sqlite3` on the host for the token/link helper targets.

```bash
make            # or `make help` — list every target
make up         # start tunnel + bot, detect the URL, update .env, print links
make status     # container status + Telegram/Cloudflare/health reachability
make logs       # tail bot logs (Ctrl+C to stop)
make restart    # restart the bot to pick up .env changes
make down       # stop all containers
```

| Group | Target | What it does |
|---|---|---|
| **Lifecycle** | `make up` | Start tunnel + bot, auto-detect tunnel URL into `.env`, show voting links |
| | `make down` | Stop all containers |
| | `make restart` | Restart the bot (picks up `.env` changes) |
| | `make build` | Rebuild the bot image and restart |
| **Observability** | `make logs` / `make logs-cf` | Tail bot / Cloudflare tunnel logs |
| | `make status` | Container status + external-service reachability + `/health` |
| | `make url` | Current tunnel URL, API docs link, and per-group voting links |
| | `make chats` | List known groups with their chat IDs |
| | `make notify` | DM all voting links to `ADMIN1` (prints them if Telegram is unreachable) |
| **Tokens** | `make token [LABEL="..."] [DAYS=N]` | Issue a **global** admin API token (all groups) |
| | `make group-token CHAT=<id> [SCOPES=read,vote] [LABEL="..."] [DAYS=N]` | Issue a token scoped to one group (`make chats` for the ID) |

> `make up` starts the Cloudflare tunnel (the `web` profile), so it's the path
> for web/Mini-App deployments. For a Telegram-only bot, `docker compose up -d`
> is enough. The daily DB-backup sidecar starts automatically with either.

---

## Configuration

Copy `.env.example` to `.env` — it documents **every** supported variable,
grouped and commented with its default. Uncomment a line to override it; leave
it commented to keep the default. Only `API_KEY` is required. The most common
variables:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` (or `API_KEY`) | Yes | Telegram Bot Token from @BotFather |
| `ADMIN1` | No | Telegram user ID of first super-admin |
| `ADMIN2` | No | Telegram user ID of second super-admin |
| `DATABASE_URL` | No | PostgreSQL URL — omit to use SQLite |
| `WEBHOOK_URL` | No | Public HTTPS URL to enable webhook mode |
| `HEALTH_CHECK_PORT` | No | HTTP port for health endpoints (default: `8080`) |
| `REST_API_ENABLED` | No | `true` to start FastAPI on port 8081 (required for web voting + Mini App) |
| `REST_API_PORT` | No | FastAPI port (default: `8081`) |
| `WEB_BASE_URL` | No | Your public HTTPS base URL — enables web voting links in panels and `/weblink` |
| `MINIAPP_URL` | No | Public URL of `/miniapp/` — wires the Telegram menu button on startup |

**SQLite** (default) stores the database at `/app/data/rollcall.db`.  
**PostgreSQL** example: `postgresql://user:password@host:5432/dbname`

**Webhook mode:** set `WEBHOOK_URL` to switch from long-polling to webhook delivery. Leave unset (default) to use long-polling.

---

## Commands

The tables below cover the most-used commands. The **authoritative, always-current
list** lives in the bot itself — `/help` (user commands), `/help admin`, and
`/help <command>` for a detail card — all rendered from
[`commands_registry.py`](rollCall/commands_registry.py), the single source of truth.

Append `::N` to most commands to target a specific rollcall when multiple are active (e.g. `/in ::2`).

### Core

| Command | Alias | Description |
|---|---|---|
| `/start_roll_call [title]` | `/src` | Start a new roll call |
| `/end_roll_call [::N]` | `/erc` | End rollcall #N |
| `/cancel_roll_call [::N]` | `/xrc` | Cancel a rollcall without recording stats |
| `/repeat` | `/rpt` | Clone the last ended rollcall (title, limit, location, fee) |
| `/rollcalls` | `/r` | List all active rollcalls |
| `/panel [::N]` | | Show inline control panel for rollcall #N |
| `/in [comment] [::N]` | | Mark yourself IN |
| `/out [comment] [::N]` | | Mark yourself OUT |
| `/maybe [comment] [::N]` | | Mark yourself MAYBE |
| `/summary [days]` | | Recap of recent sessions (count, avg attendance, top 3) |

### Lists

| Command | Alias | Description |
|---|---|---|
| `/whos_in [::N]` | `/wi` | Show IN list |
| `/whos_out [::N]` | `/wo` | Show OUT list |
| `/whos_maybe [::N]` | `/wm` | Show MAYBE list |
| `/whos_waiting [::N]` | `/ww` | Show waitlist |

### Event Settings (admin only)

| Command | Alias | Description |
|---|---|---|
| `/set_title title [::N]` | `/st` | Set rollcall title |
| `/set_limit N [::N]` | `/sl` | Set max attendees (enables waitlist) |
| `/set_rollcall_time DD-MM-YYYY H:M [::N]` | `/srt` | Set event date/time (`cancel` to clear) |
| `/set_rollcall_reminder hours [::N]` | `/srr` | Reminder hours before event (`cancel` to clear) |
| `/event_fee amount [::N]` | `/ef` | Set total event fee |
| `/individual_fee [::N]` | `/if` | Show per-person fee split |
| `/location place [::N]` | `/loc` | Set event location |
| `/when [::N]` | `/w` | Show event time |

### Proxy Voting (admin only)

For adding non-Telegram members to a rollcall. Proxy names are limited to **40 characters**.

| Command | Alias | Description |
|---|---|---|
| `/set_in_for name [::N]` | `/sif` | Add proxy member as IN |
| `/set_out_for name [::N]` | `/sof` | Add proxy member as OUT |
| `/set_maybe_for name [::N]` | `/smf` | Add proxy member as MAYBE |

### Templates (admin only)

| Command | Description |
|---|---|
| `/set_template name "Title" [limit=N] [location=X] [fee=X] [offset_days=D] [event_day=weekday] [event_time=HH:MM]` | Save a template (`event_day`/`event_time` set when the rollcall auto-closes). Name max 50 chars. |
| `/templates` | List saved templates (shows schedule status) |
| `/start_template name [extra title]` | Start a rollcall from a template |
| `/delete_template name` | Delete a template |
| `/schedule_template name <weekday> <HH:MM>` | Weekly auto-start (must be before `event_time`) |
| `/schedule_template name <weekday> <HH:MM> biweekly` | Every-2-weeks auto-start |
| `/schedule_template name monthly <day> <HH:MM>` | Monthly auto-start on day N of the month |
| `/schedule_template name off` | Disable auto-start for a template |
| `/schedule_template name` | Show current schedule for a template |
| `/schedules` | View all scheduled templates with inline ☑️ multi-select; tap to check/uncheck, then bulk ⏸ Pause or ▶️ Resume selected |

### Ghost Tracking (admin only)

| Command | Description |
|---|---|
| `/toggle_ghost_tracking` | Enable or disable no-show tracking |
| `/set_absent_limit N` | Set no-show threshold for reconfirmation prompts |
| `/absent_stats` | Show ghost leaderboard |
| `/mark_absent` | Manually mark no-shows from a past session |
| `/clear_absent name` | Clear ghost count for a user |

### Admin Tools

| Command | Description |
|---|---|
| `/delete_user name [::N]` | Remove a user (shows confirmation prompt) |
| `/set_status name <in\|out\|maybe> [::N]` | ⭐ Move a user to a different status (shows confirmation prompt) |
| `/buzz [message] [::N]` | Ping members who haven't voted; 30s rate-limited; pings all known members if no rollcall is active |
| `/audit_log [N]` | ⭐ Show last N admin actions for this chat (default 20) |
| `/set_admins` | Enable admin-only mode (group admins only) |
| `/unset_admins` | Disable admin-only mode |

### Dues & Treasury (admin only unless noted)

Enable once with `/enable_dues` — a guided setup card walks through UPI and
penalty tiers. The ledger is **append-only**: corrections are compensating
entries, never deletes, and every money mutation is announced in the chat.

| Command | Alias | Description |
|---|---|---|
| `/settle_dues [subsidy] [::N]` | | Close a game's books — guided flow: penalty panel → confirm card → per-head split with UPI QR |
| `/pick_collector [::N]` | | Pick the collector from a button panel (IN members or any known member); remembers a returning collector's UPI |
| `/set_collector name [paid] [upi] [::N]` | | Typed collector assignment |
| `/mark_late name minutes` / `/mark_ditch name` | | Assess late/no-show penalty by tier |
| `/mark_paid name [amount]` | `/paid` | Record a payment (admin or designated collector; user-scoped panel with no args) |
| `/waive` `/reimburse` `/add_adhoc` `/cancel_game_dues` | | Corrections — all as compensating entries |
| `/my_dues` | `/md` | Your own balance (everyone) |
| `/dues` / `/dues_snapshot` / `/dues_export` | `/ds` `/de` | Full ledger view / group snapshot / CSV export |
| `/fund` `/fund_history` `/fund_topup` `/log_expense` | | Group fund (penalties + rounding accumulate here) |
| `/dues_report weekly\|off` | `/dr` | Auto-post a snapshot every Sunday evening |
| `/new_season` | `/dues_reset` | Season reset — zero all balances via compensating entries, fund carry/zero choice, history preserved |
| `/dues_setup` | | Re-open the guided setup status card |

### Web Access

| Command | Alias | Description |
|---|---|---|
| `/weblink` | | Permanent group web page + per-rollcall links (everyone) |
| `/mytoken [off]` | | DM yourself a personal web login code — works even when Telegram is down (everyone) |
| `/weblogin name` | | Admin issues a 7-day single-use login link for a member |

### Info & Stats

| Command | Alias | Description |
|---|---|---|
| `/stats [name\|@user\|group\|top\|bot]` | `/s` | Attendance rate, streaks, IN/OUT/MAYBE counts |
| `/history [N] [page]` | | Paginated ended rollcalls with counts (default 10 per page) |
| `/card` | `/mc` | Shareable match-day card image of the IN list |
| `/version` | `/v` | Show bot version |

### Chat Settings

| Command | Alias | Description |
|---|---|---|
| `/shh` | | Silent mode — suppresses confirmations and list output after each vote |
| `/louder` | | Resume full panel output after votes |
| `/timezone Region/City` | `/tz` | Set timezone (e.g. `Asia/Kolkata`) |

### Super Admin

| Command | Description |
|---|---|
| `/broadcast "message"` | Send a message to all chats the bot is in |

---

## Architecture

```
RollCall/
├── rollCall/
│   ├── runner.py              # Entry point, health server, webhook/polling setup
│   ├── commands_registry.py   # Single source of truth for every command (menu + /help)
│   ├── handlers/              # Thin Telegram adapters (one file per feature area)
│   ├── services/              # Platform-agnostic business logic (bot + web + REST share this)
│   ├── api/                   # FastAPI REST layer (gated by REST_API_ENABLED)
│   │   ├── main.py            # App factory, mounts routes + static files
│   │   ├── identity.py        # Signed id_token issue/verify for web identity
│   │   ├── routes/            # REST endpoints (rollcalls, votes, web, portal, auth, dues…)
│   │   ├── schemas/           # Pydantic request/response models
│   │   ├── web/               # Group voting SPA (PWA: push, manifest, service worker)
│   │   ├── portal/            # Cross-group user portal SPA
│   │   ├── admin/             # Admin console SPA (legacy — group page covers it now)
│   │   ├── index/             # Public landing page
│   │   └── miniapp/           # Telegram Mini App SPA
│   ├── models.py              # RollCall and User data models
│   ├── rollcall_manager.py    # In-memory cache + DB sync layer
│   ├── db.py                  # Database abstraction (SQLite / PostgreSQL)
│   ├── config.py              # Environment variable parsing
│   ├── functions.py           # Shared helpers (timezone, admin checks)
│   ├── check_reminders.py     # Timed reminder, auto-close, and template auto-start scheduler
│   ├── periodic_jobs.py       # Weekly dues nudges/reports, monthly wrap-up & treasury digest
│   ├── exceptions.py          # Custom exception types
│   └── version.json           # Version history
├── tests/                     # Unit tests, fully mocked (1,190+)
├── integration_tests/         # Real-DB + real-handler scenario tests (830+)
├── scripts/smoke_test.py      # Real-import boot check (run before dep bumps / handler refactors)
├── .github/workflows/         # GitHub Actions CI/CD
├── dockerfile
├── docker-compose.yml
└── requirements.txt
```

**Key design decisions:**

- **Service layer** — `services/` is platform-agnostic; the Telegram bot, REST API, and web voting front-end all call the same service functions.
- **Manager pattern** — `RollCallManager` provides an in-memory cache per chat with lazy loading from the database, minimising repeated DB queries.
- **Async throughout** — uses `AsyncTeleBot` (pyTelegramBotAPI) with `asyncio` for non-blocking Telegram API calls and the health check server.
- **Dual DB backend** — the same `db.py` layer supports both SQLite (zero-config) and PostgreSQL (production-scale) via a `DATABASE_URL` environment variable.
- **In-place panel editing** — votes update the panel message rather than posting a new one, keeping the chat clean.
- **Cloudflare Tunnel** — the recommended public-access pattern: `cloudflared` sidecar in Docker Compose makes an outbound connection to Cloudflare; port 8081 is never opened on the host firewall and the server IP is never exposed.

---

## Development

### Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v               # unit suite — all external deps mocked, fully offline
pytest integration_tests/ -v   # scenario suite — real SQLite + real handlers, Telegram mocked
python scripts/smoke_test.py   # real-import boot check against pinned deps
```

### Linting

```bash
pip install flake8
flake8 rollCall/ --max-line-length=120
```

### CI/CD

GitHub Actions runs automatically on every push and pull request:

- **Tests** — Python 3.10, 3.11, 3.12
- **Lint** — flake8
- **Docker build** — validates the image builds successfully
- **PR checks** — conventional commit format enforcement and auto-labelling

---

## Deployment

### 1. Traditional Bot (Telegram only)

No extra steps beyond the Quick Start. Long-polling works out of the box with just `TELEGRAM_TOKEN`.

```bash
# .env — minimum required
API_KEY=your-telegram-bot-token
ADMIN1=123456789   # your Telegram user ID (optional)
```

```bash
docker-compose up -d
```

Health check endpoints on port 8080:

| Endpoint | Response |
|---|---|
| `GET /health` | Bot status, username, cache size |
| `GET /ping` | `pong` |

**PostgreSQL** (optional):

```bash
DATABASE_URL=postgresql://user:password@db-host:5432/rollcall docker-compose up -d
```

**Webhook mode** (optional): set `WEBHOOK_URL=https://yourdomain.com/webhook` in `.env`. The bot registers and deregisters the webhook automatically.

---

### 2. Web App (browser voting for non-Telegram users)

Lets anyone vote via a link — no Telegram account required. Works even when Telegram is banned/blocked.

**How it works:**
- Each rollcall panel gets a `🔗 Web:` link appended automatically.
- `/weblink` in any group shows a permanent bookmarkable group URL that never expires.
- Opening the link shows an IN/OUT/MAYBE voting page that auto-refreshes every 30s.

**Setup — using Cloudflare Tunnel (recommended, free, hides your server IP):**

> **Shortcut:** `make up` performs all three steps below automatically — it starts
> the tunnel, detects the URL, writes `WEB_BASE_URL` into `.env`, and restarts the
> bot. The manual steps are shown here for reference.

```bash
# Step 1 — add to .env
REST_API_ENABLED=true
WEB_BASE_URL=https://<your-tunnel-url>   # fill in after step 3

# Step 2 — start bot + tunnel together
docker-compose --profile web up -d

# Step 3 — get your tunnel URL from the cloudflared logs
docker-compose logs cloudflared | grep "https://"
# Copy the URL (e.g. https://abc123.trycloudflare.com)
# Paste it as WEB_BASE_URL in .env, then restart:
docker-compose --profile web up -d
```

> The tunnel URL changes on every restart with the quick-tunnel method. For a **stable URL**, set up a named Cloudflare Tunnel — see comments in `docker-compose.yml`.

**Verify:** open `https://<your-tunnel-url>/web/group/<token>` in a browser. Start a rollcall in your Telegram group — the panel will show a `🔗 Web:` link.

---

### 3. Telegram Mini App (in-app voting via menu button)

Adds a menu button inside Telegram that opens the voting interface directly in-app — no browser switch needed.

**Prerequisites:** same Cloudflare Tunnel URL as the web app (both run on port 8081).

```bash
# Step 1 — add to .env (alongside REST_API_ENABLED and WEB_BASE_URL from above)
MINIAPP_URL=https://<your-tunnel-url>/miniapp/

# Step 2 — restart
docker-compose --profile web up -d
```

On startup the bot automatically sets the Telegram menu button to open `MINIAPP_URL`. Members tap the button icon in any group chat to vote.

**Verify:** open your bot in Telegram — a menu button (⊞) should appear in the message bar. Tapping it opens the Mini App.

> If you only want the Mini App but not web voting links in panels, set `MINIAPP_URL` without `WEB_BASE_URL`.

---

### All three active — full `.env` example

```env
# Bot
API_KEY=your-telegram-bot-token
ADMIN1=123456789

# Web App + Mini App
REST_API_ENABLED=true
WEB_BASE_URL=https://abc123.trycloudflare.com
MINIAPP_URL=https://abc123.trycloudflare.com/miniapp/

# Optional
DATABASE_URL=sqlite:////app/data/rollcall.db
STRUCTURED_LOGS=true
```

```bash
docker-compose --profile web up -d
```

---

## Changelog

See [version.json](rollCall/version.json) for the full version history.

| Version | Highlights |
|---|---|
| **9.9** | Group web page now does everything the admin console used to — voter management, template creation, deep stats (session history, ghost/response-time leaderboards); admin sign-in matches the portal (Telegram verify, widget, or code); multi-group switcher |
| **9.8** | Security fix — Mini App proxy-vote endpoint enforced admin rights like `/sif`; landing page gained Sign in / Admin links; admin console sign-in via Telegram (no more `/gentoken` token paste); portal Dues tab promoted to its own tab |
| **9.7** | Merge identities — fold a repeated proxy name into a real member/proxy, one tap on the group web page, with retroactive stats/dues/streak/ghost merge and safe unmerge; template delete from web/admin/Telegram; one-time "Schedule → Once" auto-close fix |
| **9.6** | Security & reliability audit — REST API vote/proxy/start-rollcall permission gaps closed; concurrent-mutation locking on 2 more admin actions; silent-failure fixes on 5 commands; DST and monthly-schedule-clamping scheduler bugs fixed |
| **9.5** | Dues season reset (`/new_season` — compensating entries, fund carry/zero choice); dues epoch (games played while dues was off never resurface as unsettled); collector UPI memory with one-tap reuse |
| **9.4** | Panel button fix (critical); pinned settle nudge with Settle-now button; collector picker reaches non-playing members; guided `/enable_dues` setup card + `/dues_setup`; web proxy voting; emoji-grouped command menu; `/mytoken` personal web login codes |
| **9.1–9.3** | 💰 Dues & Treasury — per-head fee split with UPI QR, penalty tiers, waivers, collector flow, group fund, append-only ledger; dues web UI (member card + admin console); weekly dues reports & reminders; Telegram Login Widget; monthly wrap-up card & treasury statement |
| **9.0** | Web platform — permanent group voting page (works when Telegram is down), installable PWA with push notifications, live stats/leaderboard, user portal, Mini App |
| **8.x** | Achievement badges, `/repeat`, `/summary`, `/auto_buzz`, `/xrc` cancel-without-stats, collector rotation, idle re-engagement, REST API with bearer-token auth |
| **7.4** | Auto panel on `/src` and template auto-start; "Ended by" attribution on rollcall end; panel_msg_id persistence fix (was silently broken); `_dm_promoted_real_user` proxy guard at all 5 call sites; debounce cancel on rollcall end; rate limit on panel vote buttons |
| **7.3** | Louder mode fixes — `/sif`/`/sof`/`/smf` acks restored, `/in`/`/maybe` acks added, shh gating for all proxy commands; panel debounce (5 min, non-blocking) in louder mode; panel message ID persisted across restarts; SQLite cursor leak fix |
| **7.2** | Bot freeze fix (TCP session TTL), audit log 2.0 (pagination, 6 new tracked actions, RC name instead of ID), auto-close fix after restart, 12-bug final audit (naive datetime, release_connection on SQLite, schedule columns preserved on template update, panel ID shift after end, and more), `/schedules` multi-select panel |
| **7.1** | Silent Mode hardening — `/shh` suppresses all confirmations and in-place panel edits; reminder loop and auto-close restored after bot restart; ID snapshot fix in auto-close messages |
| **7.0** | Waitlist DMs, admin audit log (`/audit_log`), `/buzz` rate limiting, `/history` pagination, biweekly/monthly template schedules, `/set_status` manual override |
| **6.2** | Bug fixes — bare except cleanup, IN-position reset on re-vote, status validation, per-template auto-start error handling |
| **6.1** | Bug fixes — concurrent `/erc` lock, proxy delete cleans ghost record, proxy ghost events audit trail |
| **6.0** | Code review hardening — background task exceptions surfaced, buzz timeout, duplicate proxy guard, partial template update, improved renumber message |
| **5.9** | Scheduled templates — weekly/biweekly/monthly auto-start per template via `/schedule_template` |
| **5.8** | `/buzz` rework — DB-persisted member list, concurrent membership check, auto-remove leavers |
| **5.7** | In-place panel editing — votes update the panel instead of flooding the chat |
| **5.6** | `/buzz` command — ping unvoted members or all known members |
| **5.5** | Webhook mode, attendance rate + streaks in `/stats`, `/history`, `/delete_user` confirmation, rate limiting |
| **5.4** | Ghost feature fixes — cache key mismatch, panel-end ghost prompt, ghost selections restored on restart |
| **5.0** | Ghost tracking — no-show recording, reconfirmation prompts, leaderboard |
| **4.6** | Bug fixes and security hardening |
| **4.3** | Attendance ordering revamp, stats collection |
| **4.2** | Templates, end confirmation |
| **4.0** | SQLite / PostgreSQL support, major UI/UX overhaul |
| **3.0** | Docker support |
| **2.2** | Multiple Roll Call (MRC) support |

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository and create a feature branch.
2. Follow [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (enforced by CI).
3. Ensure all tests pass (`pytest tests/ -v`).
4. Open a pull request with a clear description of your changes.

Bug reports and feature requests can be filed via [GitHub Issues](../../issues).

---

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE) for details.
