"""
RollCall Bot Runner
Main entry point with health monitoring, validation, and graceful shutdown
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from db import init_db, db_ping
from aiohttp import web


class JsonFormatter(logging.Formatter):
    """One-line JSON log records — machine-readable for log aggregators."""

    def format(self, record):
        payload = {
            # Build a naive UTC datetime via fromtimestamp(timezone.utc) +
            # tz-strip — gives the exact same "YYYY-MM-DDTHH:MM:SS.fff" output
            # the old utcfromtimestamp(record.created) produced, but without
            # the Python 3.12 DeprecationWarning. The trailing "Z" is appended
            # explicitly so the JSON shape is byte-identical to the previous
            # log-aggregator-facing format.
            "ts": datetime.fromtimestamp(record.created, timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Surface any structured extras attached via logger.info(..., extra={...}).
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_") or k in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
            ):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging():
    """Configure root logging. JSON when STRUCTURED_LOGS=true (or 1/yes),
    plain text otherwise. Writes to both stdout and /app/logs/bot.log."""
    log_dir = "/app/logs"
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        # Local dev / tests run outside the container.
        log_dir = None

    structured = os.getenv("STRUCTURED_LOGS", "").strip().lower() in ("1", "true", "yes", "on")
    text_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    handlers = []
    stream = logging.StreamHandler()
    stream.setFormatter(JsonFormatter() if structured else logging.Formatter(text_fmt))
    handlers.append(stream)
    if log_dir:
        fileh = logging.FileHandler(f"{log_dir}/bot.log")
        fileh.setFormatter(JsonFormatter() if structured else logging.Formatter(text_fmt))
        handlers.append(fileh)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("TeleBot").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _setup_error_sink():
    """Initialize Sentry if SENTRY_DSN is set and sentry-sdk is installed.
    Both are optional — the bot runs fine without either."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return None
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            release=os.getenv("RELEASE_VERSION") or None,
            environment=os.getenv("ENVIRONMENT", "production"),
            integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        )
        return "ok"
    except ImportError:
        return "sentry-sdk not installed"
    except Exception as e:
        return f"sentry init failed: {e}"


_setup_logging()
_sentry_status = _setup_error_sink()

logger = logging.getLogger(__name__)
if _sentry_status == "ok":
    logger.info("✅ Sentry error reporting enabled")
elif _sentry_status:
    logger.warning(f"⚠️  Sentry not enabled: {_sentry_status}")

# Import bot components
try:
    from config import TELEGRAM_TOKEN, DATABASE_URL, ADMINS, WEBHOOK_URL
    from telegram_helper import bot
    from rollcall_manager import manager
    from check_reminders import check_template_schedules, resume_reminder_loops
    from bot_state import _log_task_exc
except ImportError as e:
    logger.error(f"Failed to import required modules: {e}")
    sys.exit(1)

# Recreate the aiohttp session every 5 minutes so silently-dead TCP connections
# (dropped by NAT/firewall without RST) can't keep the bot frozen indefinitely.
try:
    import telebot.asyncio_helper as _asyncio_helper
    _asyncio_helper.SESSION_TIME_TO_LIVE = 300
    _asyncio_helper.RETRY_ON_ERROR = True   # auto-retry on 429 / transient errors
    logger.info("aiohttp session TTL set to 300s, retry-on-error enabled")
except Exception:
    pass


def validate_environment():
    """Validate required environment variables"""
    logger.info("Validating environment configuration...")
    
    # Check Telegram token
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TOKEN_HERE":
        logger.error("❌ TELEGRAM_TOKEN not set in environment!")
        logger.error("Please set TELEGRAM_TOKEN (or API_KEY) in your .env file")
        sys.exit(1)
    
    logger.info("✅ TELEGRAM_TOKEN configured")
    
    # Check database URL
    if not DATABASE_URL:
        logger.warning("⚠️  DATABASE_URL not set, using default SQLite")
        logger.warning("Database will be created at: /app/data/rollcall.db")
    else:
        logger.info(f"✅ DATABASE_URL configured: {DATABASE_URL.split('?')[0]}")
    
    # Check admin list
    if not ADMINS or len(ADMINS) == 0:
        logger.warning("⚠️  No ADMINS configured - broadcast feature will be disabled")
    else:
        logger.info(f"✅ {len(ADMINS)} admin(s) configured")
    
    # Verify required directories exist
    for directory in ['/app/data', '/app/logs']:
        if os.path.exists(directory):
            logger.info(f"✅ Directory exists: {directory}/")
        else:
            logger.warning(f"⚠️  Directory missing: {directory}/ (will be created)")
            os.makedirs(directory, exist_ok=True)

# Background-task tracking so /health can report liveness signals.
_health_state = {
    "scheduler_task": None,        # asyncio.Task for check_template_schedules
    "prune_task": None,            # asyncio.Task for memory_prune_loop
    "last_error_at": None,         # ISO timestamp of last unhandled exception
    "last_error_msg": None,        # one-line summary
}


def _task_alive(task) -> bool:
    return task is not None and not task.done()


async def health_check(request):
    """Deeper /health: checks DB, Telegram API, background-task liveness,
    and recent-error window. Used by container orchestration."""
    problems = []
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=10)
        bot_status = f"@{me.username}"
    except asyncio.TimeoutError:
        problems.append("telegram_api_timeout")
        bot_status = "TIMEOUT"
    except Exception as e:
        problems.append(f"telegram_api_error: {type(e).__name__}")
        bot_status = "ERROR"

    db_ok = False
    try:
        db_ok = db_ping()
        if not db_ok:
            problems.append("db_ping_failed")
    except Exception as e:
        problems.append(f"db_error: {type(e).__name__}")

    from db import get_pool_stats
    pool_stats = get_pool_stats()
    if pool_stats and pool_stats.get("saturated"):
        problems.append(f"db_pool_saturated_{pool_stats['in_use']}/{pool_stats['max']}")

    from check_reminders import _active_loops
    scheduler_ok = _task_alive(_health_state["scheduler_task"])
    prune_ok = _task_alive(_health_state["prune_task"])
    if not scheduler_ok:
        problems.append("template_scheduler_dead")
    if not prune_ok:
        problems.append("memory_prune_dead")

    cache_size = len(manager._cache)
    from bot_state import _last_error_state
    last_err = _last_error_state.get('at')
    last_err_msg = _last_error_state.get('msg')

    pool_part = (
        f" pool={pool_stats['in_use']}/{pool_stats['max']}(peak={pool_stats['high_water']})"
        if pool_stats else ""
    )
    status_text = (
        f"bot={bot_status} db={'ok' if db_ok else 'FAIL'}{pool_part} "
        f"scheduler={'ok' if scheduler_ok else 'DEAD'} "
        f"prune={'ok' if prune_ok else 'DEAD'} "
        f"chats={cache_size} reminder_loops={len(_active_loops)}"
    )
    if last_err:
        status_text += f" last_error={last_err}({last_err_msg or '?'})"
    if problems:
        status_text = "DEGRADED " + ",".join(problems) + " | " + status_text

    # 503 only when DB or Telegram are unreachable — degraded background
    # tasks should be visible but not flap container restarts.
    status_code = 503 if ("db_ping_failed" in problems or "telegram_api_error" in [p.split(":")[0] for p in problems]) else 200
    return web.Response(text=status_text, status=status_code)
    
async def ping(request):
    """Simple ping endpoint"""
    return web.Response(text="pong", status=200)


async def webhook_handler(request):
    """Receive Telegram updates via webhook POST."""
    import telebot
    if request.content_type != 'application/json':
        return web.Response(status=403)
    try:
        json_body = await request.json()
        update = telebot.types.Update.de_json(json_body)
        await bot.process_new_updates([update])
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook handler error: {e}")
        return web.Response(status=500)


# Tracked for clean shutdown so we don't leak the TCP listener on container restart.
_app_runner_ref = {"runner": None}


async def start_health_server():
    """Start HTTP health check server (and webhook endpoint if WEBHOOK_URL is set)."""
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/ping', ping)

    if WEBHOOK_URL:
        app.router.add_post('/webhook', webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    _app_runner_ref["runner"] = runner

    # Use port from environment or default to 8080
    port = int(os.getenv('HEALTH_CHECK_PORT', '8080'))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logger.info(f"✅ Health check server running on http://0.0.0.0:{port}")
    logger.info(f"   - Health: http://localhost:{port}/health")
    logger.info(f"   - Ping:   http://localhost:{port}/ping")
    if WEBHOOK_URL:
        logger.info(f"   - Webhook: POST http://localhost:{port}/webhook  →  {WEBHOOK_URL}")


async def register_commands():
    """Register bot commands at two scopes:
    - default: only commands everyone can run (voting, lists, stats, help)
    - chat administrators: full set including admin-only commands

    Pulls from the COMMANDS registry in commands_registry.py — the single
    source of truth. Adding/renaming a command means editing only that file
    and the matching handler; this function picks it up automatically.

    super_admin-scoped commands (e.g. /broadcast) are intentionally not in
    either menu — they're documented in /help admin but hidden from the
    Telegram menu so non-owners don't see permission errors."""
    import telebot.types as ttypes
    from commands_registry import COMMANDS

    user_commands  = [ttypes.BotCommand(c["name"], c["summary"]) for c in COMMANDS if c["scope"] == "user"]
    admin_commands = user_commands + [
        ttypes.BotCommand(c["name"], c["summary"]) for c in COMMANDS if c["scope"] == "admin"
    ]

    # Default scope (regular members in groups + private chats): user commands only
    await bot.set_my_commands(user_commands, scope=ttypes.BotCommandScopeDefault())
    # Chat-admin scope: full admin command set. Telegram shows this list only
    # to users with admin rights in the group.
    try:
        await bot.set_my_commands(
            admin_commands,
            scope=ttypes.BotCommandScopeAllChatAdministrators(),
        )
    except Exception as e:
        # Older telebot or transient API failures shouldn't block startup.
        logger.warning(f"Failed to register admin-scoped commands (continuing): {e}")

    logger.info(
        f"✅ Bot commands registered — {len(user_commands)} for users, "
        f"{len(admin_commands)} for chat admins"
    )


async def memory_prune_loop(interval_seconds: int = 600):
    """Drop stale entries from in-memory state on a fixed interval.

    Long-lived bots accumulate entries in `_rate_limits`, `_buzz_cooldowns`,
    pending-action dicts, panel msg ids, and per-chat erc locks. None of these
    grow huge per chat, but they never shrink either — over weeks of uptime
    that's a slow leak. This loop bounds them."""
    from bot_state import (
        _rate_limits, _buzz_cooldowns, _pending_deletes, _pending_overrides,
        _pending_proxy_add, _pending_reconf, _prune_pending, _panel_msg_ids,
    )

    RATE_LIMIT_AGE = 300   # individual vote rate-limit window is 2s; 5 min is well past stale
    BUZZ_COOLDOWN_AGE = 300  # /buzz cooldown is 30s; 5 min flushes any straggler

    while True:
        try:
            now = datetime.now().timestamp()

            # Timestamp-keyed dicts
            for k in [k for k, ts in _rate_limits.items() if now - ts > RATE_LIMIT_AGE]:
                _rate_limits.pop(k, None)
            for k in [k for k, ts in _buzz_cooldowns.items() if now - ts > BUZZ_COOLDOWN_AGE]:
                _buzz_cooldowns.pop(k, None)

            # Pending-action dicts (already have a 1h TTL)
            _prune_pending(_pending_deletes)
            _prune_pending(_pending_overrides)
            _prune_pending(_pending_proxy_add)
            _prune_pending(_pending_reconf)

            # Per-chat state — clean entries for chats whose rollcalls are
            # all gone, or panel ids past the current rollcall count.
            for cid, chat in list(manager._cache.items()):
                rc_count = len(chat.get('rollCalls', []))
                for key in list(_panel_msg_ids):
                    c, rc_num = key
                    if c == cid and rc_num > rc_count:
                        _panel_msg_ids.pop(key, None)

            # Drop erc locks for chats with no active rollcalls and no waiter
            for cid in list(manager._erc_locks):
                chat = manager._cache.get(cid)
                if (not chat or not chat.get('rollCalls')) and not manager._erc_locks[cid].locked():
                    manager._erc_locks.pop(cid, None)

            logger.debug(
                f"prune: rl={len(_rate_limits)} buzz={len(_buzz_cooldowns)} "
                f"pd={len(_pending_deletes)} po={len(_pending_overrides)} "
                f"ppa={len(_pending_proxy_add)} pr={len(_pending_reconf)} "
                f"panel={len(_panel_msg_ids)} erc_locks={len(manager._erc_locks)}"
            )
        except Exception:
            logger.exception("Error in memory_prune_loop")

        await asyncio.sleep(interval_seconds)


async def main():
    """Main bot execution with graceful shutdown"""
    logger.info("=" * 60)
    logger.info("🤖 Starting RollCall Telegram Bot")
    logger.info("=" * 60)

    # Validate environment
    validate_environment()
    
    # Initialize database
    # Verify database connectivity
    try:
        logger.info("Verifying database connectivity...")
        if not db_ping():
            raise Exception("Database ping failed")
        logger.info("✅ Database connectivity verified")
    except Exception as e:
        logger.error(f"❌ Database verification failed: {e}")
        sys.exit(1)

    # Get bot information
    try:
        me = await bot.get_me()
        logger.info("=" * 60)
        logger.info(f"✅ Bot authenticated successfully!")
        logger.info(f"   Bot Name: {me.first_name}")
        logger.info(f"   Username: @{me.username}")
        logger.info(f"   Bot ID:   {me.id}")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"❌ Failed to authenticate with Telegram: {e}")
        logger.error("Please check your TELEGRAM_TOKEN")
        sys.exit(1)

    # Register bot command menu
    try:
        await register_commands()
    except Exception as e:
        logger.warning(f"⚠️  Failed to register bot commands: {e}")

    # Start health check server
    try:
        await start_health_server()
    except Exception as e:
        logger.warning(f"⚠️  Health check server failed to start: {e}")
        logger.warning("Continuing without health check endpoint...")
    
    # Start template auto-scheduler (persistent background task)
    _sched_task = asyncio.create_task(check_template_schedules())
    _sched_task.add_done_callback(_log_task_exc)
    _health_state["scheduler_task"] = _sched_task
    logger.info("✅ Template scheduler started")

    # Start periodic memory pruning (keeps long-lived in-memory state bounded)
    _prune_task = asyncio.create_task(memory_prune_loop())
    _prune_task.add_done_callback(_log_task_exc)
    _health_state["prune_task"] = _prune_task
    logger.info("✅ Memory prune loop started")

    # Resume reminder/auto-close loops for rollcalls already in DB with a finalizeDate.
    # Without this, any rollcall created before a bot restart would never auto-close.
    await resume_reminder_loops()
    logger.info("✅ Reminder loop resumption complete")

    # Start bot — webhook or polling
    try:
        if WEBHOOK_URL:
            logger.info(f"🔗 Webhook mode enabled → {WEBHOOK_URL}")
            logger.info("🚀 Bot is now running via webhook...")
            logger.info("=" * 60)
            await bot.remove_webhook()
            await bot.set_webhook(url=WEBHOOK_URL)
            logger.info("✅ Webhook registered with Telegram")
            # Keep alive — aiohttp serves the webhook endpoint
            await asyncio.Event().wait()
        else:
            logger.info("🚀 Bot is now running via long-polling...")
            logger.info("Press Ctrl+C to stop")
            logger.info("=" * 60)
            await bot.infinity_polling(
                timeout=10,        # long-poll: Telegram responds in ≤10s
                request_timeout=35, # aiohttp HTTP timeout: 25s headroom over long-poll
                skip_pending=True,
                interval=1,        # 1s backoff between retries to avoid hammering
            )
    except KeyboardInterrupt:
        logger.info("\n" + "=" * 60)
        logger.info("⏹️  Received shutdown signal (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Bot error: {e}", exc_info=True)
    finally:
        logger.info("=" * 60)
        logger.info("🛑 Shutting down gracefully...")

        if WEBHOOK_URL:
            try:
                await bot.remove_webhook()
                logger.info("✅ Webhook removed")
            except Exception as e:
                logger.error(f"⚠️  Error removing webhook: {e}")

        try:
            await bot.close_session()
            logger.info("✅ Bot session closed")
        except Exception as e:
            logger.error(f"⚠️  Error closing bot session: {e}")

        try:
            manager.clear_cache()
            logger.info("✅ Manager cache cleared")
        except Exception as e:
            logger.error(f"⚠️  Error clearing cache: {e}")

        # Release the health-check port so the next container start can bind it.
        try:
            if _app_runner_ref.get("runner") is not None:
                await _app_runner_ref["runner"].cleanup()
                logger.info("✅ Health check server stopped")
        except Exception as e:
            logger.error(f"⚠️  Error stopping health server: {e}")

        logger.info("=" * 60)
        logger.info("👋 Goodbye!")
        logger.info("=" * 60)


def setup_global_exception_handler():
    """Install global exception handler to catch unhandled exceptions"""
    def exception_handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "💥 UNHANDLED EXCEPTION",
            exc_info=(exc_type, exc_value, exc_tb)
        )
    
    sys.excepthook = exception_handler


if __name__ == "__main__":
    setup_global_exception_handler()
    
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}", exc_info=True)
        sys.exit(1)
