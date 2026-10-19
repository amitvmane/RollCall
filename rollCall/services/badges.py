"""
Achievement badges — milestone detection at rollcall close.

Computed from the same numbers /stats and the leaderboard show
(services.identity.identity_stats): games PLAYED — completed, not cancelled,
not marked a no-show — with guest names merged into a member counted as theirs.
It used to read raw user_id rows, so a cancelled game counted, a merged
member's guest games didn't, and "game #10" could land on someone's real 9th.

A badge fires only on the session where the milestone is exactly hit.

Framework-agnostic: returns announcement strings; adapters decide how to post.
"""
import logging

import db as _db

STREAK_MILESTONES = (5, 10, 25, 50)
GAMES_MILESTONES = (10, 25, 50, 100, 200)


def _canonical_key(chat_id: int, uid, name: str) -> tuple:
    from services.identity import resolve_canonical
    c = (resolve_canonical(chat_id, user_id=uid) if uid is not None
         else resolve_canonical(chat_id, proxy_name=name))
    return ("u", c["user_id"]) if c["kind"] == "user" else ("p", (c["proxy_name"] or "").lower())


def _games_milestone_once(chat_id: int, key: tuple, games: int) -> bool:
    """True the first time this person reaches this games milestone.

    "Games played" can step back — a no-show is marked after the game it was
    announced at — and then reach the same number again a game later. A
    streak may legitimately be hit twice; a games milestone is a one-off, so
    it is remembered (system_config, no schema) and never repeated.
    """
    stamp = f"milestone_games:{chat_id}:{key[0]}:{key[1]}:{games}"
    if _db.get_system_config(stamp):
        return False
    _db.set_system_config(stamp, "1")
    return True


def collect_badges(chat_id: int, in_members: list) -> list:
    """Return badge announcement lines for members who just hit a milestone.

    in_members — [(user_id_or_None, display_name)] for the final IN list of a
    just-ended rollcall. Must be called AFTER the DB end-rollcall commit so
    the counts include the current game.
    """
    from services.identity import identity_stats
    lines = []
    seen = set()
    for uid, name in in_members:
        try:
            uid = uid if isinstance(uid, int) and uid > 0 else None
            key = _canonical_key(chat_id, uid, name)
            if key in seen:           # member + their merged guest name both IN
                continue
            seen.add(key)
            stats = (identity_stats(chat_id, user_id=uid) if uid is not None
                     else identity_stats(chat_id, proxy_name=name))
            streak = stats["current_streak"]
            games = stats["sessions_attended"]

            if streak in STREAK_MILESTONES:
                lines.append(f"🔥 {name} is on a {streak}-game streak!")
            if games in GAMES_MILESTONES and _games_milestone_once(chat_id, key, games):
                lines.append(f"🏅 {name} just played game #{games} with this group!")
        except Exception:
            logging.exception("collect_badges failed for member %s", name)
    return lines
