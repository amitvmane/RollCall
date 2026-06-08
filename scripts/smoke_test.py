#!/usr/bin/env python3
"""
scripts/smoke_test.py

Runs the same import chain the production bot does, against the REAL
pinned dependencies (no test mocks). Verifies:

  1. Every rollCall module compiles and imports cleanly.
  2. The bot instance is constructed via AsyncTeleBot — catches kwarg or
     signature mismatches after a pyTelegramBotAPI bump.
  3. The member-tracking middleware actually installs (the feature that
     silently broke in v7.8 and would not have been caught by unit tests
     because they mock telebot).
  4. Every handler module registers without error and that several known
     command handlers end up in bot.message_handlers.
  5. A couple of important helpers are still importable from db (defends
     against accidental rename/delete).

Usage:
    python3 scripts/smoke_test.py

Exit codes:
    0  all checks passed
    1  at least one check failed (full traceback printed)

This is intentionally NOT a pytest test: tests/conftest.py mocks telebot
to import-isolate handlers, so it can't surface signature mismatches
against the real library. The smoke test is the layer that does.
"""

import os
import sys
import tempfile
import traceback


def main() -> int:
    # Set env so config.py doesn't trip on missing token / DB.
    os.environ.setdefault("TELEGRAM_TOKEN", "123:smoke-test")
    tmp_db = tempfile.NamedTemporaryFile(prefix="rollcall-smoke-", suffix=".db", delete=False)
    tmp_db.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.name}"

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "rollCall"))

    failures = []

    def check(name, fn):
        print(f"  • {name} ... ", end="", flush=True)
        try:
            fn()
            print("ok")
        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
            failures.append((name, traceback.format_exc()))

    print("RollCall smoke test\n")

    # ── 1. Compile every .py under rollCall/ ────────────────────────────
    def compile_all():
        import compileall
        rc_dir = os.path.join(repo_root, "rollCall")
        ok = compileall.compile_dir(rc_dir, quiet=1, force=True)
        if not ok:
            raise RuntimeError("compileall reported failures (see stderr above)")
    check("compile rollCall/*.py", compile_all)

    # ── 2. Import bot_state — constructs AsyncTeleBot + installs middleware ──
    def import_bot_state():
        import bot_state
        from telebot.async_telebot import AsyncTeleBot
        if not isinstance(bot_state.bot, AsyncTeleBot):
            raise RuntimeError(f"bot is not AsyncTeleBot: {type(bot_state.bot)}")
    check("import bot_state, AsyncTeleBot constructs", import_bot_state)

    # ── 3. Member-tracking middleware actually installed ────────────────
    def middleware_installed():
        import bot_state
        mws = getattr(bot_state.bot, "middlewares", None) or []
        names = [type(m).__name__ for m in mws]
        if "_MemberTrackingMiddleware" not in names:
            raise RuntimeError(
                f"_MemberTrackingMiddleware not registered. Got: {names or 'no middlewares at all'}. "
                "Either the install path raised, or AsyncTeleBot's middleware API changed."
            )
    check("_MemberTrackingMiddleware registered", middleware_installed)

    # ── 4. Handler modules register without error ───────────────────────
    def import_handlers():
        # Importing handlers triggers @bot.message_handler / @bot.callback_query_handler
        # decorators across all submodules.
        import handlers  # noqa: F401
    check("import handlers package (registers decorators)", import_handlers)

    def some_handlers_present():
        import bot_state
        mh = getattr(bot_state.bot, "message_handlers", None) or []
        # 30+ commands exist; sanity-check we have most of them.
        if len(mh) < 25:
            raise RuntimeError(f"only {len(mh)} message handlers registered, expected ≥ 25")
    check("≥25 message handlers registered", some_handlers_present)

    # ── 5. runner.py import chain (without running main()) ──────────────
    def import_runner_chain():
        import telegram_helper  # noqa: F401
        import rollcall_manager  # noqa: F401
        import check_reminders  # noqa: F401
    check("telegram_helper / rollcall_manager / check_reminders import", import_runner_chain)

    # ── 5b. runner module imports (main() is guarded by __name__) ───────
    def import_runner():
        import runner  # noqa: F401
        # If runner.main exists as a coroutine, that's a useful smoke too.
        if not callable(getattr(runner, "main", None)):
            raise RuntimeError("runner.main not callable — entry point changed")
    check("runner module imports cleanly, runner.main callable", import_runner)

    # ── 6. Key db helpers exist (catches accidental rename/delete) ──────
    def db_helpers_present():
        from db import (
            upsert_chat_member, get_active_members, mark_member_inactive,
            increment_ghost_count, decrement_ghost_count, reset_ghost_count,
            get_ghost_count, get_rollcall_in_users, mark_rollcall_absent_done,
        )
        _ = (upsert_chat_member, get_active_members, mark_member_inactive,
             increment_ghost_count, decrement_ghost_count, reset_ghost_count,
             get_ghost_count, get_rollcall_in_users, mark_rollcall_absent_done)
    check("known db helpers importable", db_helpers_present)

    # Cleanup
    try:
        os.unlink(tmp_db.name)
    except OSError:
        pass

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) failed\n")
        for name, tb in failures:
            print(f"=== {name} ===")
            print(tb)
        return 1
    print("PASSED: all smoke checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
