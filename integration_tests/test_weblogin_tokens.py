"""
Real-SQLite tests for the web_direct_login_tokens read path added this
session: peek_web_direct_login_token (db.py) — a read-only lookup used by
the GET /auth/weblogin/{token} redirect handler so it can resolve which
group to send the browser to WITHOUT spending the token (only the later
POST /auth/weblogin/redeem does that, via the pre-existing
consume_web_direct_login_token). Exercises the real SQL against a real
DB, which the route-level tests (tests/test_weblogin.py) mock away.
"""

import time
from datetime import datetime, timedelta

import db


CHAT = -(int(time.time() * 1000) % 10**12) - 10**14


def _future(minutes=10):
    return datetime.utcnow() + timedelta(minutes=minutes)


def _past(minutes=10):
    return datetime.utcnow() - timedelta(minutes=minutes)


def test_peek_finds_a_valid_unused_token_without_consuming_it():
    db.get_or_create_chat(CHAT)
    token = f"code1-{CHAT}"
    db.create_web_direct_login_token(token, CHAT, 111, "Ravi", 1, "Admin", _future())

    payload = db.peek_web_direct_login_token(token)
    assert payload is not None
    assert payload["chat_id"] == CHAT
    assert payload["tg_user_id"] == 111

    # Peeking must not have consumed it — a real consume still succeeds.
    consumed = db.consume_web_direct_login_token(token)
    assert consumed is not None
    assert consumed["tg_user_id"] == 111


def test_peek_returns_none_for_already_consumed_token():
    db.get_or_create_chat(CHAT)
    token = f"code2-{CHAT}"
    db.create_web_direct_login_token(token, CHAT, 222, "Amit", 1, "Admin", _future())
    db.consume_web_direct_login_token(token)

    assert db.peek_web_direct_login_token(token) is None


def test_peek_returns_none_for_expired_token():
    db.get_or_create_chat(CHAT)
    token = f"code3-{CHAT}"
    db.create_web_direct_login_token(token, CHAT, 333, "Sam", 1, "Admin", _past())

    assert db.peek_web_direct_login_token(token) is None


def test_peek_returns_none_for_unknown_token():
    assert db.peek_web_direct_login_token(f"never-issued-{CHAT}") is None
