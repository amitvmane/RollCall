"""
Achievement badges — milestone detection at rollcall close.

Computed entirely from existing stats tables (user_stats, proxy_stats,
ended-rollcall attendance counts) — no new data collection. A badge fires
only on the session where the milestone is exactly hit, so each announcement
happens once.

Framework-agnostic: returns announcement strings; adapters decide how to post.
"""
import logging

from db import (
    get_proxy_attendance_count,
    get_proxy_streaks,
    get_user_attendance_count,
    get_user_streaks,
)

STREAK_MILESTONES = (5, 10, 25, 50)
GAMES_MILESTONES = (10, 25, 50, 100, 200)


def collect_badges(chat_id: int, in_members: list) -> list:
    """Return badge announcement lines for members who just hit a milestone.

    in_members — [(user_id_or_None, display_name)] for the final IN list of a
    just-ended rollcall. Must be called AFTER streak updates and the DB
    end-rollcall commit so both counters include the current game.
    """
    lines = []
    for uid, name in in_members:
        try:
            if isinstance(uid, int) and uid > 0:
                streak = get_user_streaks(chat_id, uid).get("current_streak", 0)
                games = get_user_attendance_count(chat_id, uid)
            else:
                streak = get_proxy_streaks(chat_id, name).get("current_streak", 0)
                games = get_proxy_attendance_count(chat_id, name)

            if streak in STREAK_MILESTONES:
                lines.append(f"🔥 {name} is on a {streak}-game streak!")
            if games in GAMES_MILESTONES:
                lines.append(f"🏅 {name} just played game #{games} with this group!")
        except Exception:
            logging.exception("collect_badges failed for member %s", name)
    return lines
