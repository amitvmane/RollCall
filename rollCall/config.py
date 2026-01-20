import os
import logging
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("API_KEY")

ADMINS = [int(os.environ.get("ADMIN1")), int(os.environ.get("ADMIN2"))]

# Database configuration
# Default to SQLite if DATABASE_URL is not set
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///rollcall.db")