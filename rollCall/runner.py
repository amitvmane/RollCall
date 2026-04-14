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
    ]
)
logging.getLogger("TeleBot").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Import bot components
try:
    from config import TELEGRAM_TOKEN, DATABASE_URL, ADMINS
    from telegram_helper import bot
    from rollcall_manager import manager
except ImportError as e:
    logger.error(f"Failed to import required modules: {e}")
    sys.exit(1)


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


async def start_health_server():
    """Start HTTP health check server"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/ping', ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Use port from environment or default to 8080
    port = int(os.getenv('HEALTH_CHECK_PORT', '8080'))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f"✅ Health check server running on http://0.0.0.0:{port}")
    logger.info(f"   - Health: http://localhost:{port}/health")
    logger.info(f"   - Ping:   http://localhost:{port}/ping")


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
    
    # Start health check server
    try:
        await start_health_server()
    except Exception as e:
        logger.warning(f"⚠️  Health check server failed to start: {e}")
        logger.warning("Continuing without health check endpoint...")
    
    # Start bot polling
    logger.info("🚀 Bot is now running and listening for messages...")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 60)
    
    try:
        # Run bot with automatic reconnection
        await bot.infinity_polling(
            timeout=20,
            request_timeout=30,
            #long_polling_timeout=20,
            skip_pending=True  # Skip messages sent while bot was offline
        )
    except KeyboardInterrupt:
        logger.info("\n" + "=" * 60)
        logger.info("⏹️  Received shutdown signal (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Bot polling error: {e}", exc_info=True)
    finally:
        logger.info("=" * 60)
        logger.info("🛑 Shutting down gracefully...")
        
        # Clean up resources
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}", exc_info=True)
        sys.exit(1)
