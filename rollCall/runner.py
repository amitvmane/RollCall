"""
RollCall Bot Runner
Main entry point with health monitoring, validation, and graceful shutdown
"""

import asyncio
import logging
import os
import sys
from db import init_db, db_ping
from aiohttp import web

# Configure logging before imports
log_dir = '/app/logs'
os.makedirs(log_dir, exist_ok=True)  # Ensure directory exists

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{log_dir}/bot.log'),
        logging.StreamHandler()
    ],
    force=True  # ← overrides any early init by imported modules
)
logging.getLogger("TeleBot").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

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

async def health_check(request):
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=10)
        cache_size = len(manager._cache)
        if not db_ping():
            raise Exception("Database ping failed")
        return web.Response(
            text=f"OK - Bot: @{me.username}, Cache: {cache_size} chats",
            status=200
        )
    except asyncio.TimeoutError:
        logger.warning("Health check: Telegram API timeout (bot may be slow, not dead)")
        return web.Response(text="WARN: Telegram API timeout", status=200)
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return web.Response(
            text=f"ERROR: {str(e)}",
            status=503
        )
    
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


async def start_health_server():
    """Start HTTP health check server (and webhook endpoint if WEBHOOK_URL is set)."""
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/ping', ping)

    if WEBHOOK_URL:
        app.router.add_post('/webhook', webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()

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
    """Register bot commands so the Telegram / menu is always up-to-date."""
    import telebot.types as ttypes

    user_commands = [
        ttypes.BotCommand("in",            "Mark yourself as attending"),
        ttypes.BotCommand("out",           "Mark yourself as not attending"),
        ttypes.BotCommand("maybe",         "Mark yourself as undecided"),
        ttypes.BotCommand("rollcalls",     "List all active rollcalls"),
        ttypes.BotCommand("whos_in",       "Show who's attending"),
        ttypes.BotCommand("whos_out",      "Show who's not attending"),
        ttypes.BotCommand("whos_maybe",    "Show who's undecided"),
        ttypes.BotCommand("whos_waiting",  "Show waitlist"),
        ttypes.BotCommand("stats",         "Attendance stats and leaderboard"),
        ttypes.BotCommand("history",       "Past rollcall history"),
        ttypes.BotCommand("timezone",      "Set your timezone (e.g. Asia/Kolkata)"),
        ttypes.BotCommand("help",          "User commands  |  /help admin for admin commands"),
        ttypes.BotCommand("version",       "Show bot version"),
    ]

    admin_commands = user_commands + [
        ttypes.BotCommand("start_roll_call",        "Start a new rollcall"),
        ttypes.BotCommand("end_roll_call",           "End the active rollcall"),
        ttypes.BotCommand("panel",                   "Show inline control panel"),
        ttypes.BotCommand("set_title",               "Set rollcall title"),
        ttypes.BotCommand("set_limit",               "Set max attendance limit"),
        ttypes.BotCommand("set_rollcall_time",       "Set rollcall end date/time"),
        ttypes.BotCommand("set_rollcall_reminder",   "Set reminder hours before close"),
        ttypes.BotCommand("event_fee",               "Set total event fee"),
        ttypes.BotCommand("individual_fee",          "Per-person fee split"),
        ttypes.BotCommand("location",                "Set event location"),
        ttypes.BotCommand("when",                    "Show rollcall scheduled time"),
        ttypes.BotCommand("buzz",                    "Notify members who haven't voted"),
        ttypes.BotCommand("set_in_for",              "Mark another user as IN"),
        ttypes.BotCommand("set_out_for",             "Mark another user as OUT"),
        ttypes.BotCommand("set_maybe_for",           "Mark another user as MAYBE"),
        ttypes.BotCommand("delete_user",             "Remove a user from rollcall"),
        ttypes.BotCommand("set_status",              "Move user between IN/OUT/MAYBE"),
        ttypes.BotCommand("set_admins",              "Enable admin-only mode"),
        ttypes.BotCommand("unset_admins",            "Disable admin-only mode"),
        ttypes.BotCommand("templates",               "List saved templates"),
        ttypes.BotCommand("set_template",            "Create or update a template"),
        ttypes.BotCommand("start_template",          "Start rollcall from a template"),
        ttypes.BotCommand("delete_template",         "Delete a template"),
        ttypes.BotCommand("schedule_template",       "Schedule auto-start (weekly etc.)"),
        ttypes.BotCommand("schedules",               "View and manage schedules"),
        ttypes.BotCommand("toggle_ghost_tracking",   "Enable/disable ghost tracking"),
        ttypes.BotCommand("set_absent_limit",        "Set reconfirmation threshold"),
        ttypes.BotCommand("clear_absent",            "Clear ghost count for a user"),
        ttypes.BotCommand("mark_absent",             "Mark users as absent"),
        ttypes.BotCommand("audit_log",               "View admin audit log"),
        ttypes.BotCommand("shh",                     "Enable silent mode"),
        ttypes.BotCommand("louder",                  "Disable silent mode"),
    ]

    await bot.set_my_commands(admin_commands, scope=ttypes.BotCommandScopeDefault())
    logger.info(f"✅ Bot commands registered ({len(admin_commands)} commands visible to all users)")


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
    logger.info("✅ Template scheduler started")

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
