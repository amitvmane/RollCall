# RollCall — Claude Code Instructions

## Command registry rule

`rollCall/commands_registry.py` is the **single source of truth** for every bot command. It feeds:
- `register_commands()` in `rollCall/runner.py` (drives the Telegram BotCommand menu, user-scope vs admin-scope)
- `help_commands()` in `rollCall/handlers/core.py` (drives `/help`, `/help admin`, and `/help <name>` detail view)

Whenever a bot command is **added, renamed, or removed**:
1. Edit only `commands_registry.py` — add/modify/remove the entry in the `COMMANDS` list with all eight fields (`name`, `aliases`, `scope`, `category`, `args`, `sample`, `summary`, `details`).
2. Make sure the actual handler function exists and uses the same command name (and aliases) in its `@bot.message_handler(...)` decorator.

The menu and `/help` re-render automatically — no other files to keep in sync.

## Error replies

Handlers should raise the custom exception classes from `exceptions.py` for curated user-facing messages (rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter, etc.) — `bot_state.reply_error()` passes those through verbatim. For anything else, let it propagate; `reply_error` logs the full traceback and sends a generic safe message so internal errors don't leak into the chat.

Do not use `await bot.send_message(cid, str(e))` for caught exceptions — use `await reply_error(message_or_cid, e)`.

## Chat mutations

Anything that mutates a chat's rollcall state (votes, proxy adds, set_limit, end_rollcall) should run inside `async with manager.get_chat_write_lock(cid):` to serialize with concurrent operations like /erc and template auto-close. Re-fetch the rollcall *inside* the lock since /erc may have removed it while you were waiting.

## Smoke test (real-import boot check)

`scripts/smoke_test.py` runs the production import chain against the **real** pinned dependencies — no test mocks. It compiles every module under `rollCall/`, constructs `AsyncTeleBot`, verifies the member-tracking middleware installs, imports the full handlers package, and confirms `runner.py` loads. Run it locally before pushing any dep bump or any change to `bot_state.py` / `runner.py`:

```bash
python scripts/smoke_test.py
```

CI runs the same script as the `Smoke (real-import boot check)` job. Unit tests can't catch signature mismatches (telebot is mocked in `conftest.py`), so this layer is the one that surfaces issues like the v7.8 `use_class_middlewares=True` crash.

## Logging

Prefer `logging.exception("context")` inside `except` blocks — it captures the traceback automatically. Do not use `traceback.format_exc()` interpolation. The bot supports `STRUCTURED_LOGS=true` to emit one-line JSON to stdout for log aggregators, and `SENTRY_DSN=...` (with `sentry-sdk` installed as an optional dep) for error reporting.

## Dues & Treasury ledger rule

`dues_entries` and `fund_transactions` are **append-only**. Never `UPDATE` or `DELETE` rows in these tables. Corrections are compensating entries (a new row with the opposite-signed amount). This invariant is the durability backbone of the financial system — if data is lost from the DB, the group-chat announcement history (which always posts, even in shh mode) is the reconstruction source.

Consequences:
- New dues/fund services must use `db.add_dues_entry` / `db.add_fund_transaction` only — never raw UPDATE.
- Reversals write `cancel_credit` / `adjustment` entry types, not deletes.
- `game_closures` is NOT append-only (it stores metadata, not money rows) — its `collector_uid` fields may be updated via `update_game_closure_collector`.

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
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | unset | Web-Push keypair. Unset → push notifications silently disabled; rotating them invalidates existing subscriptions (members must re-subscribe) |
| `TRUSTED_PROXY_IPS` | `*` | direct peers trusted to set `X-Forwarded-For` (so `request.client`/rate-limit buckets see the real visitor IP, not the proxy's). `*` is safe only because `REST_API_PORT` is never published to the host in `docker-compose.yml` — tighten to the proxy's actual IP/CIDR if that ever changes |
| `MINIAPP_URL` | unset | public URL of `/miniapp/` — sets Telegram menu button on startup |
| `WEB_BASE_URL` | unset | public base URL (e.g. `https://yourdomain.com`) — enables magic-link web voting; if unset `/weblink` shows a config warning and no link is appended to panels |
| `TG_LOGIN_WIDGET` | unset | `true`/`1` → offer Telegram's Login Widget as a sign-in option on the group page. Set this **only after** running `/setdomain` in BotFather for this deployment's domain: without that registration Telegram renders its own error ("Username invalid" / "Bot domain invalid") inside a cross-origin iframe the page cannot read, so the visitor sees a broken third sign-in option. Off by default; the deep link and guest voting are unaffected |
| `MEMORY_MODE` | unset | `true`/`1` → in-memory SQLite, all data lost on restart (original v1 behaviour); overrides `DATABASE_URL` |
| `BACKUP_DIR` | `/app/data/backups` | where the db-backup sidecar writes snapshots, and where `/health` + `/health` (bot command) look to report backup freshness |
| `BACKUP_MAX_AGE_HOURS` | `48` | newest snapshot older than this is reported as `backup=STALE` by `/health` and `make backup-check`. Deliberately never causes a 503 — a stale backup doesn't mean the bot is unhealthy, and returning 503 would make Docker restart a working bot |
| `GHOST_AUTOFORGIVE_DAYS` | `7` | days an ended rollcall may sit unreviewed before `periodic_jobs._ghost_auto_forgive` treats it as "everyone who was IN attended" and forgives one absence each; `0` disables. Only ever decrements — an unreviewed session never recorded a ghost against anyone |
