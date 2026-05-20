# RollCall — Claude Code Instructions

## Command registry rule

Whenever a bot command is **added, renamed, or removed**, you must update **both** of the following in the same change:

1. **`register_commands()` in `rollCall/runner.py`** — the `user_commands` or `admin_commands` list (and the correct scope).
2. **`help_commands()` in `rollCall/telegram_helper.py`** — the `/help` message text sent to users.

These two must always stay in sync with each other and with the actual command handlers. Never add a handler without updating both.
