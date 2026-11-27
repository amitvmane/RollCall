# RollCall — Claude Code Instructions

## Open items

`docs/OPEN_ITEMS.md` is the one maintained list of open work. When a change
opens, closes or re-scopes an item, update that file in the same commit —
move finished items to "Recently done" with the date and commit.

## Command registry rule

`rollCall/commands_registry.py` is the **single source of truth** for every bot command. It feeds:
- `register_commands()` in `rollCall/runner.py` (drives the Telegram BotCommand menu, user-scope vs admin-scope)
- `help_commands()` in `rollCall/handlers/core.py` (drives `/help`, `/help admin`, and `/help <name>` detail view)

Whenever a bot command is **added, renamed, or removed**:
1. Edit only `commands_registry.py` — add/modify/remove the entry in the `COMMANDS` list with all eight fields (`name`, `aliases`, `scope`, `category`, `args`, `sample`, `summary`, `details`). Optional ninth field `menu: False` makes a command typed-only: out of the Telegram menu (capped at 100 — the admin menu is full), still in `/help` and still working when typed.
2. Make sure the actual handler function exists and uses the same command name (and aliases) in its `@bot.message_handler(...)` decorator.

The menu and `/help` re-render automatically — no other files to keep in sync.

## Error replies

Handlers should raise the custom exception classes from `exceptions.py` for curated user-facing messages (rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter, etc.) — `bot_state.reply_error()` passes those through verbatim. For anything else, let it propagate; `reply_error` logs the full traceback and sends a generic safe message so internal errors don't leak into the chat.

Do not use `await bot.send_message(cid, str(e))` for caught exceptions — use `await reply_error(message_or_cid, e)`.

Every handler must answer even when it fails: wrap its body in try/except → `reply_error`, or decorate it with `@guarded` (bot_state) under the `@bot.message_handler` lines. `tests/test_handlers_never_silent.py` fails on a handler that does neither.

## REST: name rollcalls by id

A rollcall's display number (#1, #2…) shifts when an earlier one ends; its
`id` never does. Every REST route about one rollcall is registered with
`api/rollcall_ref.dual(...)` and takes `ref: RcRef = Depends(rollcall_ref)`
(after the token dependency, so an unauthenticated caller can't probe which
ids exist): that gives the stable `/rollcalls/by-id/{rollcall_id}/…` path plus
the deprecated `/rollcalls/{rc_number}/…` one, on ONE handler. Number paths
are retiring: app keys get 410 on them, everyone else a `Sunset` header
(`rollcall_ref.SUNSET`); they are deleted on that date (docs/OPEN_ITEMS.md #8). Inside the chat
lock, use `ref.index_now()` (re-finds by id) — never the number from the URL.
`tests/test_rest_rollcall_ids.py` fails on a number-only route.

## Partner API keys and gateway

App keys (`services/partner_api.py`, Admin → Integrations) are issued like a
bank's partner credentials: partner, contact, agreement reference, optional IP
allowlist (enforced in `api/auth`), 1–730 day expiry with reminders at 14 and
3 days (`notify_expiring`, daily), rotation with a 7-day overlap. **Every app
key — any scope — is re-checked against its issuer's admin standing** (a vote
key votes for anyone, an admin's power). App-key votes are for *current*
members only, under the group's name for them. `/gentoken` tokens are listed
and revocable there too.

`api/partner_gateway.py` gives every `/api/` call `X-Request-Id` and every
error `{error, detail, request_id}` (the HTTPException/validation handlers in
`api/main.py` — keep `detail` as it was, the web page reads it), and replays
`Idempotency-Key` writes under `/api/v1/chats/` for 24h (`api_idempotency`).
`api/rate_limit.py` adds `X-RateLimit-*` and counts per-key usage
(`services/api_usage.py`: memory → `api_key_usage` each minute, app keys and
/gentoken tokens only, 90 days) for the Integrations → Usage dashboard,
`/api_usage` and a key's own `GET /chats/{chat_id}/usage`. Readers are always
scoped to one chat; a partner sees only its own key. Public contract: `docs/API.md`.

## Lineup (teams on a pitch / by role)

`services/lineup.py` sits on top of `services/teams.py` (same sheet, lock and
publish rules) when `chats.sport` is set and a game has 2–4 teams (2: one
pitch, `layout: faceoff`; 3–4: a half-pitch each, `layout: boards`). What a
group does — event (teams off) / game / football / cricket — is
`lineup.activity()`, stored as the existing `teams_enabled` + `sport`.
Football (`kind: pitch`): `rollcall_teams.position` = `"<line>:<index>"`,
NULL = sub; `rollcalls.lineup` JSON holds players per side (default the IN
list split by the team count, 4–11) and each team's formation (presets in `FORMATIONS`). Cricket
(`kind: roles`): the same column holds WK/BAT/AR/BOWL, never guessed. A team
move makes a player a sub, a swap trades spots; teams are auto-placed when
made (`autofill_new_teams`) or when the sport is set. `pitch_xy` and app.js
`_lineupXY` must stay the same numbers (picture = page). Publish posts text or
a Pillow picture (`publish_content`, outside the lock) and emits the
`lineup.published` webhook; partners read `GET …/by-id/{id}/lineup` (by id
only — new routes never get a number path).

## Partner webhooks

`services/webhooks.py` is fed by hooks in the db.py writers every surface uses
(`create_rollcall`, `end_rollcall`, `add_or_update_user`, `add_or_update_proxy_user`,
`delete_user_by_*`, `add_dues_entry`). `db._webhook` QUEUES the event and
`_cursor` flushes it only after the outermost block commits (dropped on
rollback) — never emit from inside an open write. Delivery is async with
retries from the minute tick; URLs are https and the resolver refuses private
addresses at connect time. Public contract: `docs/API.md` (v1 — add fields,
never remove or rename).

## Chat mutations

Anything that mutates a chat's rollcall state (votes, proxy adds, set_limit, end_rollcall) should run inside `async with manager.get_chat_write_lock(cid):` to serialize with concurrent operations like /erc and template auto-close. Re-fetch the rollcall *inside* the lock since /erc may have removed it while you were waiting.

## Smoke test (real-import boot check)

`scripts/smoke_test.py` runs the production import chain against the **real** pinned dependencies — no test mocks. It compiles every module under `rollCall/`, constructs `AsyncTeleBot`, verifies the member-tracking middleware installs, imports the full handlers package, and confirms `runner.py` loads. Run it locally before pushing any dep bump or any change to `bot_state.py` / `runner.py`:

```bash
python scripts/smoke_test.py
```

CI runs the same script as the `Smoke (real-import boot check)` job. Unit tests can't catch signature mismatches (telebot is mocked in `conftest.py`), so this layer is the one that surfaces issues like the v7.8 `use_class_middlewares=True` crash.

## Telegram rate limits (burst safety)

Telegram throttles a bot to ~1 message/sec per chat (all messages, whatever
command sent them) and answers 429 beyond that — e.g. ten scheduled commands
firing at 9:01. `chat_outbox.install_request_pacing` (installed in
`bot_state`) wraps telebot's single `_process_request`, so EVERY chat-posting
call — any command, background job, web action — waits out a 429 in order
instead of being lost. Nothing to do per call site; new code is covered.

Two rules that follow:
- **Never send to Telegram inside `async with manager.get_chat_write_lock(...)`.**
  A paced send can wait up to a minute, and every vote in that chat queues on
  the same lock. Change state under the lock, announce after it — see
  `_close_panel_bookkeeping`. Enforced by `tests/test_no_send_under_chat_lock.py`.
- Panel refreshes go through `_update_panel` (coalesces overlapping redraws).

`reply_error` never tells a user "something went wrong" for a 429 — the
command already did its work. Covered by `phase_vote_burst` and
`phase_mixed_command_burst` in `scripts/functional_test.py` (fake clock +
fake Telegram at the HTTP layer) and the smoke test (pacing installed on the
real telebot).

## Web front-end wiring (CSP)

The CSP refuses inline script (`api/security_headers.py`). Never write an
inline `onclick=`/`onchange=` attribute or an inline `<script>` block — it
renders and then silently does nothing. Declare handlers instead:
`data-on-click="fnName" data-args="${rcArgs(a, b)}"` (static HTML: a literal
JSON array). `/shared/actions.js` dispatches them; the function must be a
top-level `function` or `window.x =` (a `const` is not on `window`), and
built-ins are refused. `scripts/web_ui_check.py` and `web_layout_check.js`
(which serves the real CSP) fail on violations.

## Logging

Prefer `logging.exception("context")` inside `except` blocks — it captures the traceback automatically. Do not use `traceback.format_exc()` interpolation. The bot supports `STRUCTURED_LOGS=true` to emit one-line JSON to stdout for log aggregators, and `SENTRY_DSN=...` (with `sentry-sdk` installed as an optional dep) for error reporting.

## Dues & Treasury ledger rule

`dues_entries` and `fund_transactions` are **append-only**. Never `UPDATE` or `DELETE` rows in these tables. Corrections are compensating entries (a new row with the opposite-signed amount). This invariant is the durability backbone of the financial system — if data is lost from the DB, the group-chat announcement history (which always posts, even in shh mode) is the reconstruction source.

Consequences:
- New dues/fund services must use `db.add_dues_entry` / `db.add_fund_transaction` only — never raw UPDATE.
- Reversals write `cancel_credit` / `adjustment` entry types, not deletes.
- `game_closures` is NOT append-only (it stores metadata, not money rows) — its `collector_uid` fields may be updated via `update_game_closure_collector`.

## Principals (app-local identity)

`principals` + `principal_bindings` are an identity that doesn't belong to a
chat platform. A principal is a person; bindings map them to zero or more
platform accounts (`telegram:168415137`, `discord:…`, or none at all for an
email login). Linking two accounts is adding a second binding.

The 11 tables keyed on `tg_user_id` are **not** re-keyed. A person without
Telegram gets an id from `identity_space.py` instead (see below), so those
tables work unchanged; the principal is what an email login resolves to.

- `services/principals.py` is the only entry point; `for_telegram()` is called
  wherever identity is established, so principals stay current.
- `(platform, platform_user_id)` is UNIQUE **in the schema**, not checked in
  Python: "one account, one person" is exactly the invariant a race between
  two concurrent first-logins would break.
- `link()` refuses an account already bound to anyone — silently re-pointing
  it would merge two people's history on a typo.
- `backfill_from_telegram()` runs idempotently; without it "the same person"
  would mean different things either side of the deploy.
- `acting_user_id(principal)` is the ONE rule for which user id a person acts
  under: their Telegram id if bound (so an existing member who adds an email
  signs in as exactly that member), else `identity_space.local_user_id()`.
  Derived, never stored.

## Web-only people and groups (identity_space)

Telegram ids have at most 52 significant bits, so `identity_space.py` hands
out this app's own ids where Telegram never will — people at `2**52 + principal_id`,
web-only groups at `-(2**52) - n` — both under `2**53` so they survive JSON
numbers in the browser. Consequences:

- A local person is a real user everywhere (`isinstance(uid, int) and uid > 0`):
  votes, stats, ghosts and dues key on them like any member, not as a guest.
- **Nothing naming a local id may reach Telegram.** `chat_outbox.refuse_local_ids`
  (inside the same wrapper as pacing) raises Telegram's own 400 "chat not
  found" for any request whose `chat_id`/`user_id` is local, so every
  best-effort send already copes. Mirror/announce paths also skip web-only
  chats up front to keep logs quiet — the guard is the safety net, not the
  mechanism. Never build a `tg://user?id=` link for a local id (bot_state's
  `format_mention*` already don't).
- Web-only groups are always `admin_source='local'`: `api/web_admin.check_web_admin_live`
  reads only `web_admins` for them (and for any chat that opted into local),
  and `services/admin.set_admin_source` refuses `platform` for them. Owners
  grant/remove admins via `services/local_groups`.
- Email sign-in (`services/email_auth.py`): single-use 15-minute codes, stored
  hashed in `email_login_codes` (purged by `memory_prune_loop`), redeemed by
  POST from the page so a mail scanner can't spend them. Linking an email to a
  Telegram member needs a **global** identity token — a chat-bound /weblogin
  token must never mint a login that works everywhere. An address bound to one
  person is never re-pointed to another. A link code is only redeemable by the
  Telegram sign-in that asked for it (checked before the code is spent) —
  otherwise a stranger could bind your address to their account.
- Duplicate accounts (`services/account_merge.py`): an email-only account found
  while linking is offered for merge; the merge token is scoped to BOTH ids.
  Votes/stats/teams/admin grants are re-keyed (Telegram row wins on conflict,
  higher role wins), dues move by a pair of compensating `adjustment` entries
  (the ledger stays append-only), and `principals.merged_into_user_id` makes the
  old local tokens verify as the Telegram id (`api/identity._follow_merge`, the
  one DB lookup on that path, local ids only).
- `chats.join_approval` (on for new web-only groups, off for everyone else):
  joining from the link files a `join_requests` row an admin approves, and web
  votes come from members only — guests too. Telegram members are never gated
  in Telegram groups. One rule: `services/local_groups.gate_web_vote`.
- Quiet logs: `chat_outbox.install_log_filter()` (runner + bot_state) drops
  log records caused by `refuse_local_ids` — handler-level, since records from
  child loggers bypass root-logger filters. Real Telegram errors log unchanged.
  The panel path (`_update_panel`) returns early for web-only chats.

## Email-only people: notices and their account

- **Notification emails** (`services/email_notify.py`): an email-only member
  (local id, no Telegram binding) gets by email what Telegram members get in
  Telegram — the per-person notices that go through `push.notify_users`
  (waitlist promotion, not-voted reminders, dues) plus "a rollcall opened".
  `principals.email_notify`: `auto` (default — only when they have no web push
  on for that group, so nobody hears twice), `always`, `off`. Never emailed:
  Telegram members, even with a linked email. Queued in `email_outbox` (no
  network at the call site), sent from the minute tick with its own time budget
  (never `wait_for` — a cancel mid-send would send twice), 4 attempts with
  backoff. Caps: 10/person/day, 300/hour overall, same message not repeated
  within 6h; `security` notices (email changed) skip prefs and caps. Every mail
  carries RFC 8058 one-click unsubscribe; the token never expires, and GET only
  shows a button (mail scanners open links) — POST unsubscribes.
- **Account** (`services/account.py`, `/auth/account*`, global tokens only):
  change email (link to the NEW address, usable only by this account on its
  device; old address told), sign out everywhere, delete (email-only accounts).
- **Sign out everywhere** = `principals.tokens_valid_after`, checked in
  `api.identity.verify_identity_token` (`_revoked`). A token's issue time is
  `exp - IDENTITY_TOKEN_TTL_SECONDS`, so **every identity token must be minted
  with the default TTL** — a shorter one would look issued earlier and be
  refused after a sign-out. Cached per user id for 30s; fails open on a DB
  error (signature + expiry still apply).
- **Delete** keeps the groups' records (votes, stats, the append-only ledger)
  under the id; removes email, name, memberships, admin grants (through
  `services.admin.revoke_admin`), API keys they issued, push, queued mail,
  codes, sessions.
- **Membership** for people the bot can't see (local ids anywhere, everyone in
  a web-only group): `services/local_groups` — `membership`, `join`, `leave`,
  `remove_member`. A member is an **active** `chat_members` row
  (`db.is_active_chat_member`): leaving/removal keeps the row for history, so
  never treat "has a row" as "is a member" in a gate. Leave/remove/delete are
  refused while the person is on an open rollcall (`on_open_rollcall`).
- Merge routes take the email token **unfollowed** but still revocable:
  `api.identity.verify_identity_token_unfollowed`, never a raw `_verify`. Refused
  while they're IN/MAYBE/waiting on an open rollcall, or the only owner of a
  group that has other members.

## Web-admin roles

`web_admins.role` is `owner` or `admin`, and `chats.admin_source` is `platform`
(ask Telegram who the admins are — today's only behaviour) or `local` (this
app's own `web_admins` list is the source of truth). Both are additive: every
existing chat reads `platform`, so nothing changes until a chat opts in.

The rules live in `services/admin.py`, never at the call site, because they
guard one unrecoverable state — **a chat with no owner that has also stopped
asking Telegram cannot be administered by anyone**, and there is no recovery
short of editing the database:
- only an owner may promote, demote, or change `admin_source`
- the last owner cannot be demoted
- a chat cannot switch to `local` without an owner
- the schema reconciler promotes each chat's earliest grant to `owner`, so no
  existing group is left ownerless

Note `set_web_admin` must stay an upsert. It was `INSERT OR REPLACE` on SQLite,
which deletes and re-inserts the row — that silently reset `role` on every
`/weblink`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` / `API_KEY` | required | bot token |
| `DATABASE_URL` | `sqlite:///rollcall.db` | sqlite or postgres dsn |
| `WEBHOOK_URL` | unset (long-poll) | enable webhook mode |
| `WEBHOOK_SECRET_TOKEN` | auto-generated when `WEBHOOK_URL` is set | verifies `/webhook` POSTs actually came from Telegram (checked against the `X-Telegram-Bot-Api-Secret-Token` header) — no persistence needed, re-registered with Telegram on every restart |
| `HEALTH_CHECK_PORT` | `8080` | health server port |
| `DB_POOL_MINCONN` | `1` | PG pool min |
| `DB_POOL_MAXCONN` | `5` | PG pool max — raise if `/health` reports `db_pool_saturated` |
| `STRUCTURED_LOGS` | unset | `true`/`1`/`yes` → JSON logs |
| `SENTRY_DSN` | unset | optional, requires `sentry-sdk` |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | sentry tracing % |
| `RELEASE_VERSION`, `ENVIRONMENT` | unset / `production` | sentry tags |
| `REST_API_ENABLED` | unset | `true`/`1` → start FastAPI on `REST_API_PORT` |
| `API_DOCS_ENABLED` | unset | `true`/`1` → serve `/api/docs`, `/api/redoc`, OpenAPI schema. Off by default: unauthenticated **and** on the rate-limiter bypass list, so a tunnel-exposed deployment would otherwise publish a free, unthrottled map of every endpoint |
| `REST_API_PORT` | `8081` | port for REST API + Mini App static files |
| `REST_API_HOST` | `127.0.0.1` | bind address for REST API |
| `CORS_ALLOWED_ORIGINS` | derived from `WEB_BASE_URL` | comma-separated allowed origins. Defaults to `WEB_BASE_URL` (not `*`) because identity tokens are long-lived — a wildcard origin lets a leaked token read authenticated responses cross-origin. Falls back to `*` only when `WEB_BASE_URL` is also unset |
| `REST_API_RATE_LIMIT_WINDOW_SECONDS` | `60` | sliding-window length for the REST rate limiter |
| `REST_API_RATE_LIMIT_MAX_REQUESTS` | `60` | max requests per window, per bearer token (hashed) or client IP |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | unset (auto-generated) | Web-Push keypair. Unset is a **working** setup, not a disabled one: `services/push.py` generates a pair on first use and stores it in `system_config`. Set them only to pin the keys across a lost or moved database — new keys invalidate every existing subscription (members must re-subscribe) |
| `TRUSTED_PROXY_IPS` | `*` | direct peers trusted to set `X-Forwarded-For` (so `request.client`/rate-limit buckets see the real visitor IP, not the proxy's). With `*` uvicorn reads the **left-most** entry, which the visitor controls whenever a proxy appends (`$proxy_add_x_forwarded_for`) — so `*` is safe only because blobsystems' nginx **replaces** the header with `$http_cf_connecting_ip`. Keeping `REST_API_PORT` off the host is necessary but not sufficient; tighten to the proxy's IP/CIDR too if that ever changes |
| `MINIAPP_URL` | unset | public URL of `/miniapp/` — sets Telegram menu button on startup |
| `WEB_BASE_URL` | unset | public base URL (e.g. `https://yourdomain.com`) — enables magic-link web voting; if unset `/weblink` shows a config warning and no link is appended to panels |
| `TG_LOGIN_WIDGET` | unset | `true`/`1` → offer Telegram's Login Widget as a sign-in option on the group page. Set this **only after** running `/setdomain` in BotFather for this deployment's domain: without that registration Telegram renders its own error ("Username invalid" / "Bot domain invalid") inside a cross-origin iframe the page cannot read, so the visitor sees a broken third sign-in option. Off by default; the deep link and guest voting are unaffected. Turning it on also adds `'unsafe-eval'` to the CSP's script-src — the widget `eval()`s its `data-onauth` string; nothing of ours evals |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_STARTTLS` | unset (email sign-in off) | outgoing mail for email sign-in. Email sign-in is offered only when `SMTP_HOST`, `SMTP_FROM` **and** `WEB_BASE_URL` are set. Port 465 uses implicit TLS; any other port uses STARTTLS unless `SMTP_STARTTLS=false` |
| `HUMAN_CHECK` | `pow` | CAPTCHA for actions that need no sign-in — guest votes and sign-in emails (`services/human_check.py`). `pow`: self-hosted proof of work (~1s in the browser, nothing external, single-use challenges); `turnstile`: Cloudflare Turnstile (needs both keys below; falls back to `pow` without them, never to off); `off`. Passing gives a 12-hour pass; signed-in people skip it. Missing pass → HTTP 428 |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | unset | Cloudflare Turnstile keys for `HUMAN_CHECK=turnstile`; the CSP then allows `challenges.cloudflare.com` (script + frame) |
| `IDENTITY_SECRET` | unset (falls back to the bot token) | signing key for web/portal identity tokens. Unset keeps today's behaviour exactly — the key is derived from `TELEGRAM_TOKEN`, which means identity can't work without Telegram. Setting it decouples the two. **Verification accepts every configured key**, so setting this on a running deployment does NOT sign anyone out: existing tokens stay valid for the rest of their 30-day life while new ones are minted under the new key. Once that window passes the bot token can be rotated or removed with no user impact |
| `APP_UID` / `APP_GID` | unset (`0` = root) | user the bot, db-backup and watchdog containers run as (compose `user:`). Switch order matters: set both, `make fix-perms` (stops containers, `chown`s `DATA_DIR` + `./logs`), then `make build`. `check-data-dir` refuses to start while anything in those dirs isn't owned by `APP_UID` — a bot that can't write its DB looks like a healthy container |
| `MEMORY_MODE` | unset | `true`/`1` → in-memory SQLite, all data lost on restart (original v1 behaviour); overrides `DATABASE_URL` |
| `BACKUP_DIR` | `/app/data/backups` | where the db-backup sidecar writes snapshots, and where `/health` + `/health` (bot command) look to report backup freshness |
| `BACKUP_MAX_AGE_HOURS` | `48` | newest snapshot older than this is reported as `backup=STALE` by `/health` and `make backup-check`. Deliberately never causes a 503 — a stale backup doesn't mean the bot is unhealthy, and returning 503 would make Docker restart a working bot |
| `BACKUP_SYNC_MAX_AGE_HOURS` | `6` | age of the newest **successful** off-site copy before `/health` reports `offsite=STALE`. The sidecar used to run `rclone copy … \|\| true`, so a remote rejecting writes was indistinguishable from one that worked; it now stamps `BACKUP_SYNC_STATE_DIR/last-success` only on success and `backup_status.remote_sync_freshness()` reads that file's age. Like `backup=`, never a 503 — the bot is serving fine and restarting it fixes nothing about a broken remote. `NA` when `RCLONE_REMOTE` is unset, because off-site backup is a choice and not a fault |
| `BACKUP_SYNC_STATE_DIR` | `/app/data/sync-state` | where that stamp lives. Its own writable mount because `/backups` stays read-only in the sync sidecar — it must never be able to alter the snapshots it ships |
| `GHOST_AUTOFORGIVE_DAYS` | `7` | days an ended rollcall may sit unreviewed before `periodic_jobs._ghost_auto_forgive` treats it as "everyone who was IN attended" and forgives one absence each; `0` disables. Only ever decrements — an unreviewed session never recorded a ghost against anyone |
| `UPDATE_STALE_HOURS` | `24` | total inbound silence before `/health` reports `no_updates`. Two signals here, doing different jobs: `bot_state.update_ages()` tracks the last update **per type** and is shown on `/health` to *diagnose* ("commands 2m ago · button taps 3d ago" names the fault), while only **total** silence is allowed to alarm — a per-type alarm cannot tell a broken group from one that never taps buttons. The unambiguous alarm is `bot_state.check_update_delivery()`, which asks Telegram hourly what it will actually deliver and flags `undelivered_<type>` when our requested list isn't stored. Never a 503: a deaf bot still serves web/REST, and restarting doesn't fix Telegram's delivery config |
| `DOWNTIME_MIN_MINUTES` | `5` | a boot gap shorter than this is recorded but not announced, so a routine `make build` restart doesn't page anyone. The bot stamps a heartbeat into `system_config` every minute (`uptime.py`, called from the `check_reminders` minute tick) and compares it to the clock on the next boot; the gap is DMed to `WATCHDOG_CHAT_ID`/`ADMIN1` from `_post_connect_setup`, i.e. **after** Telegram is reachable — a long outage is usually a network outage, so announcing at boot would send it into the void. This is the only downtime record that survives an outage which took the watchdog container down too |
| `WATCHDOG_CHAT_ID` | falls back to `ADMIN1` | where the watchdog sidecar sends alerts. The watchdog polls `/health` and DMs a human when the bot is unreachable or reports DEGRADED — the only component that *notifies* rather than just recovers. Needs the target to have DMed the bot once (Telegram forbids bots opening a conversation); with neither this nor `ADMIN1` set it logs a reminder and alerts nobody |
| `WATCHDOG_INTERVAL_SECONDS` | `300` | how often the watchdog polls `/health` |
| `WATCHDOG_FAILURES_BEFORE_ALERT` | `3` | consecutive bad polls before alerting — rides out a routine restart without paging anyone |
| `WATCHDOG_REPEAT_HOURS` | `12` | while still broken, how often to re-send the alert, so a real outage isn't announced once at 3am and then never mentioned again |
