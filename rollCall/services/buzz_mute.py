"""
Stop buzzing people who never answer.

Someone pinged by /buzz, auto-buzz or a vote reminder who doesn't vote, game
after game, has most likely stopped using Telegram or the app. Pinging them
again only adds noise for everyone else. This tracks it and quietly skips them.

  * Every ping that actually goes out is logged (record), one row per
    (person, rollcall) — pinged twice for the same game is one buzz.
  * Any vote the person makes in that group, through any surface, marks all
    their buzzes answered (db.add_or_update_user does it in the same write),
    so coming back unmutes them by itself.
  * Muted = at least `buzz_mute_after` unanswered buzzes on rollcalls that
    have ENDED (an open one can still be answered). Per-group setting,
    default 3; 0 = never skip.
  * Admins see who is muted (/buzz_mute, the web settings) and can unmute
    someone by hand (/buzz_unmute), which marks their buzzes answered.

One rule, applied in services.lists.get_non_responders — the single place
/buzz, auto-buzz and the DM/push reminders all get their targets from.
"""

import logging
import time
from typing import Iterable, Optional

import db
from exceptions import incorrectParameter

DEFAULT_AFTER = 3
MAX_AFTER = 20
_last_mark = 0


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def threshold(chat_id: int) -> int:
    chat = db.get_or_create_chat(chat_id) or {}
    v = chat.get("buzz_mute_after")
    try:
        return DEFAULT_AFTER if v is None else max(0, int(v))
    except (TypeError, ValueError):
        return DEFAULT_AFTER


def set_threshold(chat_id: int, after: int, actor_user_id: int, actor_name: str) -> int:
    after = int(after)
    if not 0 <= after <= MAX_AFTER:
        raise incorrectParameter(f"Use a number from 1 to {MAX_AFTER}, or off.")
    db.update_chat_settings(chat_id, buzz_mute_after=after)
    db.log_admin_action(chat_id, actor_user_id, actor_name, "set_buzz_mute",
                        target_name="off" if after == 0 else str(after))
    return after


def record(chat_id: int, rollcall_ids: Iterable[int], user_ids: Iterable) -> None:
    """Log that these members were just pinged about these rollcalls."""
    rc_ids = [int(r) for r in rollcall_ids if r]
    uids = [int(u) for u in user_ids if isinstance(u, int)]
    if not rc_ids or not uids:
        return
    # buzzed_at is in MICROseconds and strictly increasing per process: it is
    # the buzz's identity. One call (one buzz over several open rollcalls)
    # shares it; two separate buzzes never do, even within the same second.
    global _last_mark
    now = max(int(time.time() * 1_000_000), _last_mark + 1)
    _last_mark = now
    ph = _ph()
    try:
        with db._cursor(commit=True) as cursor:
            for rc in rc_ids:
                for uid in uids:
                    cursor.execute(
                        f"INSERT INTO buzz_log (chat_id, user_id, rollcall_id, buzzed_at) "
                        f"VALUES ({ph},{ph},{ph},{ph}) ON CONFLICT (chat_id, user_id, rollcall_id) DO NOTHING",
                        (chat_id, uid, rc, now))
    except Exception:
        logging.exception("buzz_mute.record failed")


def unanswered(chat_id: int) -> dict:
    """{user_id: unanswered buzzes on ended rollcalls}, for members with any."""
    ph = _ph()
    out = {}
    try:
        with db._cursor() as cursor:
            cursor.execute(
                # DISTINCT buzzed_at = buzzes, not rollcalls: one /buzz over three
                # open rollcalls logs three rows at one moment, and counted as
                # rows it muted someone after a single ping.
                f"SELECT b.user_id, COUNT(DISTINCT b.buzzed_at) FROM buzz_log b JOIN rollcalls r ON r.id = b.rollcall_id "
                f"WHERE b.chat_id = {ph} AND b.responded = 0 AND r.ended_at IS NOT NULL "
                # A game that was called off asked nothing of anyone.
                f"AND (r.is_cancelled IS NULL OR r.is_cancelled = {'FALSE' if db.db_type == 'postgresql' else '0'}) "
                f"GROUP BY b.user_id", (chat_id,))
            for r in cursor.fetchall():
                uid, n = (list(r.values()) if isinstance(r, dict) else r)
                out[int(uid)] = int(n)
    except Exception:
        logging.exception("buzz_mute.unanswered failed")
    return out


def muted_ids(chat_id: int) -> set:
    after = threshold(chat_id)
    if after <= 0:
        return set()
    return {uid for uid, n in unanswered(chat_id).items() if n >= after}


def muted_members(chat_id: int) -> list:
    """[{user_id, name, unanswered}] — for /buzz_mute and the web settings."""
    after = threshold(chat_id)
    if after <= 0:
        return []
    out = []
    for uid, n in sorted(unanswered(chat_id).items(), key=lambda x: -x[1]):
        if n >= after:
            info = db.get_member_display_info(chat_id, uid) or {}
            out.append({"user_id": uid, "name": info.get("first_name") or info.get("username") or str(uid),
                        "unanswered": n})
    return out


def unmute(chat_id: int, user_id: int, actor_user_id: int, actor_name: str) -> None:
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE buzz_log SET responded = 1 WHERE chat_id = {_ph()} AND user_id = {_ph()}",
                       (chat_id, user_id))
    db.log_admin_action(chat_id, actor_user_id, actor_name, "buzz_unmute", target_name=str(user_id))


def find_member(chat_id: int, query: str) -> Optional[int]:
    """A muted member by @username or name (case-insensitive)."""
    q = (query or "").strip().lstrip("@").lower()
    for m in muted_members(chat_id):
        info = db.get_member_display_info(chat_id, m["user_id"]) or {}
        if q in ((info.get("username") or "").lower(), (info.get("first_name") or "").lower(), str(m["user_id"])):
            return m["user_id"]
    return None


def prune(days: int = 180) -> int:
    cutoff = (int(time.time()) - days * 86400) * 1_000_000   # buzzed_at is in µs
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM buzz_log WHERE buzzed_at < {_ph()}", (cutoff,))
        return cursor.rowcount or 0
