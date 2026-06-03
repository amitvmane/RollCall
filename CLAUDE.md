# RollCall — Claude Code Instructions

## Command registry rule

Whenever a bot command is **added, renamed, or removed**, you must update **both** of the following in the same change:

1. **`register_commands()` in `rollCall/runner.py`** — put it in `user_commands` (visible to everyone) or `admin_commands` (visible only to chat admins via `BotCommandScopeAllChatAdministrators`).
2. **`help_commands()` in `rollCall/handlers/core.py`** — the `/help` (user view) or `/help admin` (admin view) message text sent to users.

These must stay in sync with each other and with the actual command handlers. Never add a handler without updating both.

## Error replies

Handlers should raise the custom exception classes from `exceptions.py` for curated user-facing messages (rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter, etc.) — `bot_state.reply_error()` passes those through verbatim. For anything else, let it propagate; `reply_error` logs the full traceback and sends a generic safe message so internal errors don't leak into the chat.

Do not use `await bot.send_message(cid, str(e))` for caught exceptions — use `await reply_error(message_or_cid, e)`.

## Chat mutations

Anything that mutates a chat's rollcall state (votes, proxy adds, set_limit, end_rollcall) should run inside `async with manager.get_chat_write_lock(cid):` to serialize with concurrent operations like /erc and template auto-close. Re-fetch the rollcall *inside* the lock since /erc may have removed it while you were waiting.

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
