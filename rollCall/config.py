import os
from dotenv import load_dotenv

load_dotenv()


def _parse_admins():
    admins = []
    for key in ("ADMIN1", "ADMIN2"):
        value = os.environ.get(key)
        if value is None or str(value).strip() == "":
            continue
        try:
            admins.append(int(value))
        except ValueError:
            raise ValueError(f"{key} must be a valid integer Telegram user id")
    return admins


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_KEY")
ADMINS = _parse_admins()

# Default to SQLite if DATABASE_URL is not set
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///rollcall.db")

# Webhook mode: set WEBHOOK_URL to your public HTTPS URL to enable webhooks.
# Leave unset (or empty) to use long-polling (default).
# Example: https://mybot.example.com/webhook
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip() or None

# Ghost tracking defaults
DEFAULT_ABSENT_LIMIT = 1