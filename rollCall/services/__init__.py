"""
Service layer — framework-agnostic business logic.

Services take primitive Python args (chat_id, user_id, etc.), perform the
business operation against the manager / models / db layer, and return
serializable dicts. They never import telebot, never call bot.send_message,
and never format Markdown.

Adapters (Telegram bot handlers, REST API, future Discord adapter) parse
their platform-specific input into primitives, call the service, and format
the returned dict into their platform's output.

Curated user-facing exceptions from `exceptions.py` propagate from services
up to the adapter, which decides how to surface them (chat reply, HTTP 4xx,
etc.).
"""

from . import ghost, lists, proxy, push, rollcalls, settings, stats, templates, voting, web

__all__ = ["ghost", "lists", "proxy", "push", "rollcalls", "settings", "stats", "templates", "voting", "web"]
