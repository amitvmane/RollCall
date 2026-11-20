"""
Teams for a rollcall — split the IN list into 2–4 teams.

Opt-in per group (chats.teams_enabled, off by default): an admin turns
teams on with /teams on or the web setting. While off there is no panel
button or web card, and every action here refuses.

Rules (agreed 2026-11-20):
  - A first split is random and as even as possible (7 vs 6 is fine).
    Reshuffle draws a new random split.
  - Anyone in the group may move a player between teams until the teams are
    locked. An admin picks how many teams and locks/unlocks; teams also lock
    on their own once the game time passes (an admin unlock overrides that).
  - Only IN players (members and guests) are on the sheet. Someone who drops
    is taken off with no reshuffle; whoever is newly IN — a waitlist
    promotion or a late vote — joins the smallest team, which is the gap the
    drop left.
  - Moves are silent. Publish (anyone) posts the teams to the group.

The sheet lives in rollcall_teams; teams_count / teams_locked /
teams_published_at on the rollcall row. Reading never writes: view() works
out the current sheet against the IN list in memory, and every mutation
saves that same reconciled sheet before changing it.

Mutations must run under manager.get_chat_write_lock(chat_id) — callers
re-find the rollcall inside the lock, as for votes.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import db
from exceptions import incorrectParameter, insufficientPermissions
from services.common import game_time, rc_db_id

MIN_TEAMS = 2
MAX_TEAMS = 4
DEFAULT_TEAMS = 2

LABELS = ["A", "B", "C", "D"]
EMOJI = ["🔴", "🔵", "🟢", "🟡"]

# teams_locked: 0 = automatic (locks at game time), 1 = locked by an admin,
# -1 = unlocked by an admin (stays editable after game time).
_AUTO, _LOCKED, _UNLOCKED = 0, 1, -1


OFF_MESSAGE = "Teams are off for this group — an admin can turn them on with /teams on."


def enabled(chat_id: int) -> bool:
    return bool(int(db.get_or_create_chat(chat_id).get("teams_enabled") or 0))


def set_enabled(chat_id: int, on: bool, admin_user_id: int, admin_name: str) -> dict:
    db.update_chat_settings(chat_id, teams_enabled=1 if on else 0)
    db.log_admin_action(chat_id, admin_user_id, admin_name, "teams_enabled", details="on" if on else "off")
    return {"teams_enabled": on}


def _require_enabled(rc) -> None:
    if not enabled(rc.chat_id):
        raise incorrectParameter(OFF_MESSAGE)


def who_key(user) -> str:
    """A player's identity on the sheet: 'u<id>' for a member, 'p<name>' for
    a guest (guests are keyed by name, case-insensitively, as in stats)."""
    if isinstance(user.user_id, int):
        return f"u{user.user_id}"
    return "p" + str(user.user_id).strip().lower()


def _players(rc) -> list[tuple[str, str]]:
    return [(who_key(u), u.name) for u in rc.inList]


def _meta(rc) -> dict:
    row = db.get_rollcall(rc_db_id(rc)) or {}
    return {
        "count": int(row.get("teams_count") or 0),
        "lock_flag": int(row.get("teams_locked") or 0),
        "published_at": row.get("teams_published_at"),
    }


def _locked(rc, lock_flag: int, now: datetime | None = None) -> tuple[bool, str | None]:
    if lock_flag == _LOCKED:
        return True, "admin"
    if lock_flag == _UNLOCKED:
        return False, None
    when = game_time(rc)
    if when is not None and (now or datetime.now(timezone.utc)) >= when:
        return True, "game"
    return False, None


def _reconcile(rc, count: int) -> tuple[list[dict], list[str], list[dict]]:
    """The sheet as it should be now: (rows, keys to drop, rows to add).
    Saved rows of players no longer IN are dropped; IN players with no row
    go to the smallest team (lowest number on a tie), in IN-list order."""
    saved = db.get_team_rows(rc_db_id(rc))
    players = _players(rc)
    names = dict(players)
    keep = [dict(r, name=names[r["who_key"]]) for r in saved
            if r["who_key"] in names and 1 <= int(r["team_no"]) <= count]
    placed = {r["who_key"] for r in keep}
    drop = [r["who_key"] for r in saved if r["who_key"] not in placed]
    sizes = {n: 0 for n in range(1, count + 1)}
    for r in keep:
        sizes[int(r["team_no"])] += 1
    added = []
    for key, name in players:
        if key in placed:
            continue
        team = min(sizes, key=lambda n: (sizes[n], n))
        sizes[team] += 1
        added.append({"who_key": key, "name": name, "team_no": team})
    return keep + added, drop, added


def view(rc, now: datetime | None = None) -> dict:
    """The teams for one open rollcall, read-only.

    {formed, count, locked, locked_by ('admin'|'game'|None), published_at,
     players_in, teams: [{no, label, emoji, players: [{key, name}]}]}"""
    meta = _meta(rc)
    count = meta["count"]
    locked, locked_by = _locked(rc, meta["lock_flag"], now)
    out = {
        "formed": count >= MIN_TEAMS,
        "count": count if count >= MIN_TEAMS else DEFAULT_TEAMS,
        "locked": locked,
        "locked_by": locked_by,
        "published_at": str(meta["published_at"]) if meta["published_at"] else None,
        "players_in": len(rc.inList),
        "teams": [],
    }
    if not out["formed"]:
        return out
    rows, _drop, _added = _reconcile(rc, count)
    order = {key: i for i, (key, _n) in enumerate(_players(rc))}
    for n in range(1, count + 1):
        members = sorted((r for r in rows if int(r["team_no"]) == n),
                         key=lambda r: order.get(r["who_key"], 0))
        out["teams"].append({
            "no": n, "label": LABELS[n - 1], "emoji": EMOJI[n - 1],
            "players": [{"key": r["who_key"], "name": r["name"]} for r in members],
        })
    return out


def _sync(rc, count: int) -> list[dict]:
    """Save the reconciled sheet; returns its rows."""
    rows, drop, added = _reconcile(rc, count)
    rid = rc_db_id(rc)
    db.remove_team_players(rid, drop)
    for r in added:
        db.set_team_player(rid, r["who_key"], r["name"], r["team_no"], "auto")
    return rows


def _require_open(rc, now=None) -> dict:
    _require_enabled(rc)
    meta = _meta(rc)
    locked, locked_by = _locked(rc, meta["lock_flag"], now)
    if locked:
        why = "the game has started" if locked_by == "game" else "an admin locked them"
        raise incorrectParameter(f"Teams are locked ({why}). An admin can unlock them.")
    return meta


def _validate_count(count) -> int:
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise incorrectParameter(f"Number of teams must be {MIN_TEAMS}–{MAX_TEAMS}.")
    if not MIN_TEAMS <= count <= MAX_TEAMS:
        raise incorrectParameter(f"Number of teams must be {MIN_TEAMS}–{MAX_TEAMS}.")
    return count


def _split(rc, count: int, actor: str, rng) -> None:
    players = _players(rc)
    if len(players) < 2:
        raise incorrectParameter("Need at least 2 players IN to make teams.")
    players = list(players)
    rng.shuffle(players)
    rows = [{"who_key": key, "name": name, "team_no": i % count + 1}
            for i, (key, name) in enumerate(players)]
    db.replace_team_rows(rc_db_id(rc), rows, actor)
    db.update_rollcall(rc_db_id(rc), teams_count=count)


def form(rc, count, actor: str, is_admin: bool, rng=random) -> dict:
    """Make teams (or re-make them with a different number of teams).
    Choosing anything but the default number is an admin's call."""
    count = _validate_count(count)
    meta = _require_open(rc)
    if count != (meta["count"] or DEFAULT_TEAMS) and not is_admin:
        raise insufficientPermissions("Only an admin can change the number of teams.")
    _split(rc, count, actor, rng)
    return view(rc)


def reshuffle(rc, actor: str, rng=random) -> dict:
    meta = _require_open(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    _split(rc, meta["count"], actor, rng)
    return view(rc)


def move(rc, key: str, team_no, actor: str) -> dict:
    """Move one player to another team. team_no=None moves them to the next
    team round (what a Telegram tap does)."""
    meta = _require_open(rc)
    count = meta["count"]
    if count < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    rows = _sync(rc, count)
    row = next((r for r in rows if r["who_key"] == key), None)
    if row is None:
        raise incorrectParameter("That player isn't IN any more.")
    if team_no is None:
        team_no = int(row["team_no"]) % count + 1
    try:
        team_no = int(team_no)
    except (TypeError, ValueError):
        raise incorrectParameter("Unknown team.")
    if not 1 <= team_no <= count:
        raise incorrectParameter("Unknown team.")
    db.set_team_player(rc_db_id(rc), key, row["name"], team_no, actor)
    result = view(rc)
    result["moved"] = {"name": row["name"], "team": LABELS[team_no - 1]}
    return result


def set_locked(rc, locked: bool, is_admin: bool) -> dict:
    _require_enabled(rc)
    if not is_admin:
        raise insufficientPermissions("Only an admin can lock or unlock teams.")
    meta = _meta(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    if locked:
        _sync(rc, meta["count"])
    db.update_rollcall(rc_db_id(rc), teams_locked=_LOCKED if locked else _UNLOCKED)
    return view(rc)


def publish(rc) -> tuple[dict, str]:
    """Record a publish; returns (view, text to post). Anyone may publish."""
    _require_enabled(rc)
    meta = _meta(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    _sync(rc, meta["count"])
    db.update_rollcall(rc_db_id(rc), teams_published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    v = view(rc)
    return v, render_text(rc, v)


def player_by_token(v: dict, token: str) -> str | None:
    """The sheet key a Telegram button token stands for. Members ride as
    their key; guests as 'g' + callback_key(key), re-checked against the
    current sheet so a stale or forged token can't pick anyone else."""
    from utils.text import callback_key
    for team in v["teams"]:
        for p in team["players"]:
            if token == p["key"] or token == "g" + callback_key(p["key"]):
                return p["key"]
    return None


def button_token(key: str) -> str:
    from utils.text import callback_key
    return key if key.startswith("u") else "g" + callback_key(key)


def render_text(rc, v: dict) -> str:
    title = rc.title if rc.title and rc.title != "<Empty>" else "this game"
    if not v["formed"]:
        return (f"⚽ Teams — {title}\n\n"
                f"No teams yet. {v['players_in']} player(s) IN.")
    lines = [f"⚽ Teams — {title}"]
    for team in v["teams"]:
        lines.append("")
        lines.append(f"{team['emoji']} Team {team['label']} ({len(team['players'])})")
        lines.extend(f"• {p['name']}" for p in team["players"]) if team["players"] \
            else lines.append("—")
    if v["locked"]:
        lines.append("")
        lines.append("🔒 Locked" + (" — game time reached" if v["locked_by"] == "game" else ""))
    return "\n".join(lines)
