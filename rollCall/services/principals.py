"""
App-local principals — an identity that doesn't belong to a chat platform.

Every table in this app keys on a Telegram user id, and identity tokens carry
one as the subject. That is fine while Telegram is the only way in, and
impossible the moment it isn't: an app with no Telegram behind it has neither
the ids nor a way to say that two accounts are the same person.

A principal is the person. `principal_bindings` maps them to zero or more
platform accounts:

    principal 7  ──┬── telegram:168415137
                   └── discord:4409...          (linking two accounts)

    principal 8  ──                             (email login: no platform)

WHAT THIS IS NOT: the 11 tables keyed on `tg_user_id` are untouched. Re-keying
them is a large migration against a live database, and speculative until a
second login method exists — there is nothing to migrate *for* yet. This gives
the stable id to migrate *to*, and everything here is additive: rows appear,
nothing reads them for authorisation.

Bindings are unique per (platform, platform_user_id), so one Telegram account
can never point at two principals — the constraint is in the schema rather
than in this module, because "same person" is exactly the invariant that gets
violated by a race between two concurrent first-logins.
"""

import logging
from typing import List, Optional

import db

TELEGRAM = "telegram"


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def resolve(platform: str, platform_user_id) -> Optional[int]:
    """Principal id for a platform account, or None if it isn't bound yet."""
    try:
        with db._cursor() as cursor:
            ph = _ph()
            cursor.execute(
                f"SELECT principal_id FROM principal_bindings "
                f"WHERE platform={ph} AND platform_user_id={ph}",
                (platform, str(platform_user_id)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return int(row["principal_id"] if isinstance(row, dict) else row[0])
    except Exception:
        logging.exception("principals.resolve failed")
        return None


def get_or_create(platform: str, platform_user_id, display_name: str = None) -> Optional[int]:
    """Principal id for a platform account, creating both if needed.

    Idempotent, and safe against a concurrent first-login: the UNIQUE
    constraint on (platform, platform_user_id) means the loser of that race
    re-reads the winner's row rather than creating a second principal for the
    same person.
    """
    existing = resolve(platform, platform_user_id)
    if existing is not None:
        return existing
    try:
        with db._cursor(commit=True) as cursor:
            ph = _ph()
            if db.db_type == "postgresql":
                cursor.execute(
                    f"INSERT INTO principals (display_name) VALUES ({ph}) RETURNING id",
                    (display_name,),
                )
                row = cursor.fetchone()
                pid = int(row["id"] if isinstance(row, dict) else row[0])
            else:
                cursor.execute(f"INSERT INTO principals (display_name) VALUES ({ph})", (display_name,))
                pid = int(cursor.lastrowid)
            cursor.execute(
                f"INSERT INTO principal_bindings (principal_id, platform, platform_user_id) "
                f"VALUES ({ph},{ph},{ph})",
                (pid, platform, str(platform_user_id)),
            )
            return pid
    except Exception:
        # Almost certainly the UNIQUE constraint, i.e. someone else created it
        # between our resolve() and our INSERT. Their row is the right answer.
        logging.warning("principals.get_or_create: insert lost a race or failed; re-resolving")
        return resolve(platform, platform_user_id)


def for_telegram(tg_user_id: int, display_name: str = None) -> Optional[int]:
    """Convenience for the only platform that exists today."""
    return get_or_create(TELEGRAM, tg_user_id, display_name)


def bindings(principal_id: int) -> List[dict]:
    """Every platform account attached to a principal."""
    try:
        with db._cursor() as cursor:
            ph = _ph()
            cursor.execute(
                f"SELECT platform, platform_user_id, linked_at FROM principal_bindings "
                f"WHERE principal_id={ph} ORDER BY linked_at, id",
                (principal_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logging.exception("principals.bindings failed")
        return []


def link(principal_id: int, platform: str, platform_user_id) -> bool:
    """Attach another platform account to an existing principal.

    Returns False if that account is already bound — to this principal or any
    other. Silently re-pointing it would merge two people's history on a
    typo, so the caller has to unlink deliberately first.
    """
    if resolve(platform, platform_user_id) is not None:
        return False
    try:
        with db._cursor(commit=True) as cursor:
            ph = _ph()
            cursor.execute(
                f"INSERT INTO principal_bindings (principal_id, platform, platform_user_id) "
                f"VALUES ({ph},{ph},{ph})",
                (principal_id, platform, str(platform_user_id)),
            )
            return True
    except Exception:
        logging.exception("principals.link failed")
        return False


def unlink(platform: str, platform_user_id) -> bool:
    """Detach a platform account. The principal itself survives — it may still
    have other bindings, and deleting it would orphan whatever comes to
    reference it."""
    try:
        with db._cursor(commit=True) as cursor:
            ph = _ph()
            cursor.execute(
                f"DELETE FROM principal_bindings WHERE platform={ph} AND platform_user_id={ph}",
                (platform, str(platform_user_id)),
            )
            return cursor.rowcount > 0
    except Exception:
        logging.exception("principals.unlink failed")
        return False


def backfill_from_telegram() -> int:
    """Give every Telegram user this app already knows about a principal.

    Without this, principals would only exist for people who sign in after the
    feature shipped, and "the same person" would mean different things either
    side of the deploy. Idempotent — get_or_create skips anyone already bound
    — so it is safe to run on every startup.

    Returns how many principals were created.
    """
    created = 0
    try:
        with db._cursor() as cursor:
            # Union of everywhere a real Telegram id is recorded. Proxy rows
            # are excluded: a guest name is not an account, and -1 is the
            # sentinel those rows use.
            cursor.execute(
                """SELECT DISTINCT user_id AS uid FROM user_stats WHERE user_id > 0
                   UNION SELECT DISTINCT user_id FROM chat_members WHERE user_id > 0
                   UNION SELECT DISTINCT tg_user_id FROM web_admins WHERE tg_user_id > 0"""
            )
            ids = [int(r["uid"] if isinstance(r, dict) else r[0]) for r in cursor.fetchall()]
    except Exception:
        logging.exception("principals.backfill: could not read existing Telegram ids")
        return 0

    for uid in ids:
        if resolve(TELEGRAM, uid) is None and get_or_create(TELEGRAM, uid) is not None:
            created += 1
    if created:
        logging.warning("principals: created %d principal(s) for existing Telegram users", created)
    return created
