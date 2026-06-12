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

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` / `API_KEY` | required | bot token |
| `DATABASE_URL` | `sqlite:///rollcall.db` | sqlite or postgres dsn |
| `WEBHOOK_URL` | unset (long-poll) | enable webhook mode |
| `HEALTH_CHECK_PORT` | `8080` | health server port |
| `DB_POOL_MINCONN` | `1` | PG pool min |
| `DB_POOL_MAXCONN` | `5` | PG pool max — raise if `/health` reports `db_pool_saturated` |
| `STRUCTURED_LOGS` | unset | `true`/`1`/`yes` → JSON logs |
| `SENTRY_DSN` | unset | optional, requires `sentry-sdk` |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | sentry tracing % |
| `RELEASE_VERSION`, `ENVIRONMENT` | unset / `production` | sentry tags |
