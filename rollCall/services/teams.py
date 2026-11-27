"""
Teams for a rollcall — split the IN list into 2–4 teams.

Opt-in per group (chats.teams_enabled, off by default): an admin turns
teams on with /teams on or the web setting. While off there is no panel
button or web card, and every action here refuses.

Rules (agreed 2026-11-20):
  - A first split is random and as even as possible (7 vs 6 is fine).
    Reshuffle draws a new random split.
  - Anyone in the group may make teams (any number), reshuffle and move
    players until the teams are locked. Lock/unlock and publish are an
    admin's (user decision 2026-11-22); teams also lock on their own once
    the game time passes (an admin unlock overrides that).
  - Only IN players (members and guests) are on the sheet. Someone who drops
    is taken off with no reshuffle; whoever is newly IN — a waitlist
    promotion or a late vote — joins the smallest team, which is the gap the
    drop left.
  - Moves are silent. Publish (admin) posts the teams to the group.

The sheet lives in rollcall_teams; teams_count / teams_locked /
teams_published_at on the rollcall row. Reading never writes: view() works
out the current sheet against the IN list in memory, and every mutation
saves that same reconciled sheet before changing it.

Mutations must run under manager.get_chat_write_lock(chat_id) — callers
re-find the rollcall inside the lock, as for votes.
"""

from __future__ import annotations

import logging
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

    A player who leaves IN keeps their saved row as a VACANCY on their team
    — the sheet remembers the empty spot. Whoever is IN next and has no
    row (a /in, /sif, or waitlist promotion, however much later) fills a
    vacancy first, on the smallest team that has one, and that vacancy row
    goes. With no vacancy left, a newcomer joins the smallest team (lowest
    number on a tie). A player who comes back while their own row is still
    vacant simply returns to their team. In IN-list order throughout."""
    saved = db.get_team_rows(rc_db_id(rc))
    players = _players(rc)
    names = dict(players)
    keep = [dict(r, name=names[r["who_key"]]) for r in saved
            if r["who_key"] in names and 1 <= int(r["team_no"]) <= count]
    placed = {r["who_key"] for r in keep}
    vacant = [r for r in saved if r["who_key"] not in placed and 1 <= int(r["team_no"]) <= count]
    # Rows for a team that no longer exists can't be vacancies.
    stray = [r["who_key"] for r in saved
             if r["who_key"] not in placed and not 1 <= int(r["team_no"]) <= count]
    sizes = {n: 0 for n in range(1, count + 1)}
    for r in keep:
        sizes[int(r["team_no"])] += 1
    added, filled = [], []
    for key, name in players:
        if key in placed:
            continue
        open_teams = {int(r["team_no"]) for r in vacant}
        if open_teams:
            team = min(open_teams, key=lambda n: (sizes[n], n))
            slot = next(r for r in vacant if int(r["team_no"]) == team)
            vacant.remove(slot)
            filled.append(slot["who_key"])
        else:
            team = min(sizes, key=lambda n: (sizes[n], n))
        sizes[team] += 1
        added.append({"who_key": key, "name": name, "team_no": team})
    return keep + added, stray + filled, added


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
    """Save the reconciled sheet; returns its rows. Vacancy rows stay
    until someone fills them (see _reconcile)."""
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


def form(rc, count, actor: str, rng=random) -> dict:
    """Make teams (or re-make them with a different number of teams)."""
    count = _validate_count(count)
    _require_open(rc)
    _split(rc, count, actor, rng)
    from services import lineup
    lineup.autofill_new_teams(rc, actor)
    return view(rc)


def reshuffle(rc, actor: str, rng=random) -> dict:
    meta = _require_open(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    _split(rc, meta["count"], actor, rng)
    from services import lineup
    lineup.autofill_new_teams(rc, actor)
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
    # Changing team drops their spot on the pitch: they're a sub on the new
    # team (services/lineup). Same team = nothing changes.
    db.set_team_players(rc_db_id(rc), [dict(
        {"who_key": key, "name": row["name"], "team_no": team_no},
        **({"position": None} if team_no != int(row["team_no"]) else {}))], actor)
    result = view(rc)
    result["moved"] = {"name": row["name"], "team": LABELS[team_no - 1]}
    return result


def swap(rc, key_a: str, key_b: str, actor: str) -> dict:
    """Trade two players between their teams — the sizes stay as they are."""
    meta = _require_open(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    rows = {r["who_key"]: r for r in _sync(rc, meta["count"])}
    a, b = rows.get(key_a), rows.get(key_b)
    if a is None or b is None:
        raise incorrectParameter("That player isn't IN any more.")
    if int(a["team_no"]) == int(b["team_no"]):
        raise incorrectParameter("Those two are on the same team.")
    # A swap trades their spots on the pitch too (services/lineup).
    db.set_team_players(rc_db_id(rc), [
        {"who_key": key_a, "name": a["name"], "team_no": int(b["team_no"]), "position": b.get("position")},
        {"who_key": key_b, "name": b["name"], "team_no": int(a["team_no"]), "position": a.get("position")},
    ], actor)
    result = view(rc)
    result["swapped"] = (a["name"], b["name"])
    return result


def check_editable(rc) -> None:
    """Raise unless teams can still be changed (on, formed and not locked)."""
    if _require_open(rc)["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")


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


def publish(rc, is_admin: bool) -> tuple[dict, str]:
    """Record a publish; returns (view, text to post to the group)."""
    _require_enabled(rc)
    if not is_admin:
        raise insufficientPermissions("Only an admin can publish teams.")
    meta = _meta(rc)
    if meta["count"] < MIN_TEAMS:
        raise incorrectParameter("No teams yet — make teams first.")
    _sync(rc, meta["count"])
    db.update_rollcall(rc_db_id(rc), teams_published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    v = view(rc)
    return v, render_text(rc, v)


def summary_section(rc) -> str | None:
    """The end-of-game summary's team section, replacing its IN list — or
    None when this game had no teams (the summary then lists IN as usual).
    Read before the rollcall closes, so the sheet still matches its IN list."""
    try:
        if not enabled(rc.chat_id):
            return None
        v = view(rc)
    except Exception:
        logging.exception("teams summary failed for rollcall %s", rc_db_id(rc))
        return None
    if not v["formed"]:
        return None
    lines = ["Teams:"]
    for team in v["teams"]:
        lines.append(f"{team['emoji']} Team {team['label']} ({len(team['players'])})")
        lines.extend(f"{i}. {p['name']}" for i, p in enumerate(team["players"], 1))
        if not team["players"]:
            lines.append("Nobody")
        lines.append("")
    return "\n".join(lines) + "\n"


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
        lines.extend(f"{i}. {p['name']}" for i, p in enumerate(team["players"], 1)) if team["players"] \
            else lines.append("—")
    if v["locked"]:
        lines.append("")
        lines.append("🔒 Locked" + (" — game time reached" if v["locked_by"] == "game" else ""))
    return "\n".join(lines)
