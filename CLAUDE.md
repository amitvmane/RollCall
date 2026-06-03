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
