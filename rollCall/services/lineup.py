"""
Lineup — the teams drawn on a pitch, each player in a position.

Built on services/teams.py (the sheet, its lock and its publish rules are
unchanged); this only adds WHERE each player stands. Available when the
group has picked a sport (chats.sport) and the game has 2–4 teams —
otherwise teams stay the numbered list. This shows each team's formation
only — how the teams play (winner stays on, the rest as subs, anything else)
is the admin's call. Two teams share one pitch ("faceoff"); three or four
get a half-pitch each ("boards").

What a group does is its "activity" (activity()/set_activity()): an event
or gathering has no teams, a game has team lists, football and cricket add
the pitch / roles. It is stored as the two settings it always was —
chats.teams_enabled and chats.sport — so nothing else changes.

Rules (agreed 2026-11-27):
  - Players per side defaults to the IN list split by the number of teams
    (16 or 17 IN in 2 teams → 8-a-side, 20 IN in 3 → 6-a-side),
    4–11; an admin or member picks fewer for more subs. Stored once set, so
    later votes don't keep resizing a lineup someone arranged.
  - Each team picks its own formation from the presets for that size.
  - A player's spot is rollcall_teams.position = "<line>:<index>" (line 0 is
    the keeper); NULL = a sub. A team with nobody placed is filled in order
    on the first look after teams are made (fill()).
  - Someone who drops leaves an empty spot; whoever comes in late is a sub.
    Moving a player to the other team makes them a sub there; a swap trades
    positions too.
  - Empty spots are allowed when publishing ("—"). Publish posts a picture
    or the text, the publisher's choice.

Cricket (agreed 2026-11-27) has no pitch or formation: each player gets a
role — WK (wicket-keeper), BAT, AR (all-rounder), BOWL — stored in the same
position column as the role code (no colon, so it never reads as a pitch
spot). Players without a role yet are listed as such; nothing is "empty".

Mutations follow teams.py: under manager.get_chat_write_lock, editable only
while the teams are (check_editable).
"""

from __future__ import annotations

import json
import logging
import os
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

import db
from exceptions import incorrectParameter
from services.common import rc_db_id

SPORTS = {
    "football": {"label": "Football", "emoji": "⚽", "kind": "pitch"},
    "cricket": {"label": "Cricket", "emoji": "🏏", "kind": "roles"},
}

# Cricket roles, in the order a team sheet lists them.
ROLES = [("WK", "Wicket-keeper", "🧤"), ("BAT", "Batter", "🏏"),
         ("AR", "All-rounder", "⭐"), ("BOWL", "Bowler", "🎯")]
_ROLE_CODES = [r[0] for r in ROLES]


def kind(sport_key: Optional[str]) -> Optional[str]:
    return SPORTS.get(sport_key or "", {}).get("kind")


# What the group's rollcalls are: no teams for an event, team lists for any
# game, and the pitch / roles for the sports above.
ACTIVITIES = {
    "event": {"label": "Event / gathering", "emoji": "🎉", "teams": False, "sport": None},
    "game": {"label": "Game", "emoji": "🔢", "teams": True, "sport": None},
    "football": {"label": "Football", "emoji": "⚽", "teams": True, "sport": "football"},
    "cricket": {"label": "Cricket", "emoji": "🏏", "teams": True, "sport": "cricket"},
}
_ACTIVITY_ALIASES = {"gathering": "event", "meetup": "event", "social": "event", "teams": "game",
                     "other": "game", "soccer": "football", "futsal": "football"}


MIN_SIDE, MAX_SIDE = 4, 11

# Presets by players per side: outfield lines, back to front (keeper implied).
FORMATIONS = {
    4: ["2-1", "1-2", "1-1-1"],
    5: ["2-2", "1-2-1", "2-1-1"],
    6: ["2-2-1", "2-1-2", "3-1-1"],
    7: ["2-3-1", "3-2-1", "2-2-2"],
    8: ["3-3-1", "3-2-2", "2-3-2"],
    9: ["3-3-2", "3-4-1", "4-3-1"],
    10: ["4-3-2", "3-4-2", "4-4-1"],
    11: ["4-3-3", "4-4-2", "3-5-2"],
}
_LINE_ROLES = {2: ["DEF", "FWD"], 3: ["DEF", "MID", "FWD"]}


# ── sport (per group) ───────────────────────────────────────────────────────

def sport(chat_id: int) -> Optional[str]:
    v = (db.get_or_create_chat(chat_id) or {}).get("sport")
    return v if v in SPORTS else None


def set_sport(chat_id: int, value: Optional[str], admin_user_id: int, admin_name: str) -> Optional[str]:
    """value: a key of SPORTS, or None / 'none' / 'off' for no sport."""
    v = (value or "").strip().lower() or None
    if v in ("none", "off"):
        v = None
    if v is not None and v not in SPORTS:
        raise incorrectParameter(f"Sport must be one of: {', '.join(SPORTS)}, or off.")
    db.update_chat_settings(chat_id, sport=v)
    db.log_admin_action(chat_id, admin_user_id, admin_name, "set_sport", target_name=v or "off")
    return v


def activity(chat_id: int) -> str:
    from services import teams as teams_svc
    if not teams_svc.enabled(chat_id):
        return "event"
    return sport(chat_id) or "game"


def parse_activity(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    v = _ACTIVITY_ALIASES.get(v, v)
    if v not in ACTIVITIES:
        raise incorrectParameter("Pick one: event (no teams), game (team lists), football or cricket.")
    return v


def set_activity(chat_id: int, value: Optional[str], admin_user_id: int, admin_name: str) -> dict:
    """Set what the group does — one choice that sets both teams on/off and
    the sport. Returns {"activity", "teams_changed"}. Caller holds the chat
    write lock and, for a sport, calls autofill_open() under it."""
    from services import teams as teams_svc
    was_on = teams_svc.enabled(chat_id)
    if (value or "").strip().lower() in ("off", "none"):     # "/sport off": no sport, teams as they are
        value = "game" if was_on else "event"
    v = parse_activity(value)
    a = ACTIVITIES[v]
    if a["teams"] != was_on:
        teams_svc.set_enabled(chat_id, a["teams"], admin_user_id, admin_name)
    if a["teams"] and sport(chat_id) != a["sport"]:
        set_sport(chat_id, a["sport"], admin_user_id, admin_name)
    return {"activity": v, "teams_changed": a["teams"] != was_on}


def activity_note(v: str) -> str:
    """One line telling the group what the choice means."""
    return {
        "event": "🎉 Event / gathering — no teams, just who's coming.",
        "game": "🔢 Game — split the IN list into 2, 3 or 4 teams (/teams); teams are numbered lists.",
        "football": "⚽ Football — make 2, 3 or 4 teams and each one's formation is drawn on a pitch "
                    "(/lineup). How the teams play is up to the admin.",
        "cricket": "🏏 Cricket — cricket doesn't need a pitch or formation, so instead each player gets a role: "
                   "🧤 wicket-keeper, 🏏 batter, ⭐ all-rounder or 🎯 bowler "
                   "(set them on the group's web page; /lineup shows them).",
    }[v]


# ── formations ──────────────────────────────────────────────────────────────

def default_side(players_in: int, teams: int = 2) -> int:
    return max(MIN_SIDE, min(MAX_SIDE, players_in // max(2, teams)))


def layout_for(count: int) -> str:
    """'faceoff': 2 teams on one pitch. 'boards': a half-pitch per team."""
    return "faceoff" if count == 2 else "boards"


def slots(formation: str) -> list[tuple[str, str]]:
    """[(position, role)] for one team: keeper first, then back to front."""
    lines = [int(n) for n in formation.split("-")]
    roles = _LINE_ROLES[len(lines)]
    out = [("0:0", "GK")]
    for li, n in enumerate(lines, 1):
        out.extend((f"{li}:{i}", roles[li - 1]) for i in range(n))
    return out


def _stored(rc) -> dict:
    raw = (db.get_rollcall(rc_db_id(rc)) or {}).get("lineup")
    try:
        v = json.loads(raw) if raw else {}
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _settings(rc, players_in: int, count: int = 2) -> tuple[int, bool, dict]:
    """(side, side_is_auto, {team_no: formation})."""
    st = _stored(rc)
    side = st.get("side")
    auto = not (isinstance(side, int) and MIN_SIDE <= side <= MAX_SIDE)
    if auto:
        side = default_side(players_in, count)
    forms = {}
    saved = st.get("formations") or {}
    presets = FORMATIONS[side]
    for no in range(1, count + 1):
        f = saved.get(str(no))
        forms[no] = f if f in presets else presets[(no - 1) % len(presets)]
    return side, auto, forms


def _save(rc, side: int, forms: dict) -> None:
    db.update_rollcall(rc_db_id(rc), lineup=json.dumps(
        {"side": side, "formations": {str(k): v for k, v in forms.items()}}))


# ── reading ────────────────────────────────────────────────────────────────

def available(rc, tv: Optional[dict] = None) -> bool:
    from services import teams as teams_svc
    if sport(rc.chat_id) is None or not teams_svc.enabled(rc.chat_id):
        return False
    tv = tv if tv is not None else teams_svc.view(rc)
    return bool(tv["formed"]) and 2 <= tv["count"] <= teams_svc.MAX_TEAMS


def view(rc, tv: Optional[dict] = None) -> dict:
    """The lineup for one open rollcall, read-only.

    {available, sport, side, side_auto, options, locked, placed,
     teams: [{no, label, emoji, formation,
              slots: [{pos, role, key|None, name|None}], subs: [{key, name}]}]}"""
    from services import teams as teams_svc
    tv = tv if tv is not None else teams_svc.view(rc)
    sp = sport(rc.chat_id)
    out = {"available": False, "sport": sp, "kind": kind(sp), "layout": layout_for(tv.get("count") or 2),
           "side": None, "side_auto": True,
           "options": [], "roles": [], "locked": bool(tv.get("locked")), "placed": 0, "teams": []}
    if not available(rc, tv):
        return out
    saved = {r["who_key"]: r for r in db.get_team_rows(rc_db_id(rc))}
    if kind(sp) == "roles":
        return _roles_view(out, tv, saved)
    side, auto, forms = _settings(rc, tv["players_in"], tv["count"])
    out.update(available=True, kind="pitch", side=side, side_auto=auto, options=FORMATIONS[side])
    for team in tv["teams"]:
        layout = slots(forms[team["no"]])
        valid = {p for p, _r in layout}
        taken: dict = {}
        subs = []
        for p in team["players"]:          # IN order: the first claim on a spot wins
            row = saved.get(p["key"]) or {}
            pos = row.get("position")
            if (pos in valid and pos not in taken
                    and int(row.get("team_no") or 0) == team["no"]):
                taken[pos] = p
            else:
                subs.append({"key": p["key"], "name": p["name"]})
        out["placed"] += len(taken)
        out["teams"].append({
            "no": team["no"], "label": team["label"], "emoji": team["emoji"],
            "formation": forms[team["no"]],
            "slots": [{"pos": pos, "role": role,
                       "key": taken[pos]["key"] if pos in taken else None,
                       "name": taken[pos]["name"] if pos in taken else None}
                      for pos, role in layout],
            "subs": subs,
        })
    return out


def _roles_view(out: dict, tv: dict, saved: dict) -> dict:
    """Cricket: each team's players grouped by role, in ROLES order; the
    ones without a role yet go in "subs" (shown as "No role yet")."""
    out.update(available=True, kind="roles",
               roles=[{"code": c, "label": lbl, "emoji": e} for c, lbl, e in ROLES])
    for team in tv["teams"]:
        by_role: dict = {c: [] for c in _ROLE_CODES}
        unassigned = []
        for p in team["players"]:
            row = saved.get(p["key"]) or {}
            role = row.get("position")
            if role in by_role and int(row.get("team_no") or 0) == team["no"]:
                by_role[role].append(p)
            else:
                unassigned.append({"key": p["key"], "name": p["name"]})
        slots = [{"pos": f"{c}:{i}", "role": c, "key": p["key"], "name": p["name"]}
                 for c in _ROLE_CODES for i, p in enumerate(by_role[c])]
        out["placed"] += len(slots)
        out["teams"].append({"no": team["no"], "label": team["label"], "emoji": team["emoji"],
                             "formation": "", "slots": slots, "subs": unassigned})
    return out


# ── changing ───────────────────────────────────────────────────────────────

def _editable(rc) -> tuple[dict, list[dict]]:
    """(teams view, saved+reconciled rows) — raises unless the lineup can change."""
    from services import teams as teams_svc
    teams_svc.check_editable(rc)
    tv = teams_svc.view(rc)
    if not available(rc, tv):
        raise incorrectParameter("The lineup needs a sport set for the group and 2–4 teams.")
    rows = teams_svc._sync(rc, tv["count"])
    return tv, rows


def _write(rc, rows: list[dict], actor: str) -> None:
    db.set_team_players(rc_db_id(rc), rows, actor)


def _reflow(lv_team: dict, new_formation: str) -> list[dict]:
    """Re-seat a team's placed players, in spot order, into new_formation."""
    placed = [s for s in lv_team["slots"] if s["key"]]
    new = slots(new_formation)
    out = []
    for i, s in enumerate(placed):
        out.append({"who_key": s["key"], "name": s["name"], "team_no": lv_team["no"],
                    "position": new[i][0] if i < len(new) else None})
    return out


def _pitch_only(rc) -> None:
    if kind(sport(rc.chat_id)) != "pitch":
        raise incorrectParameter("Cricket has no formation — give each player a role instead.")


def set_side(rc, side, actor: str) -> dict:
    _pitch_only(rc)
    try:
        side = int(side)
    except (TypeError, ValueError):
        side = 0
    if not MIN_SIDE <= side <= MAX_SIDE:
        raise incorrectParameter(f"Players per side must be {MIN_SIDE}–{MAX_SIDE}.")
    _editable(rc)
    lv = view(rc)
    forms = {}
    for t in lv["teams"]:
        old = FORMATIONS[lv["side"]].index(t["formation"]) if t["formation"] in FORMATIONS[lv["side"]] else 0
        forms[t["no"]] = FORMATIONS[side][min(old, len(FORMATIONS[side]) - 1)]
    rows = [r for t in lv["teams"] for r in _reflow(t, forms[t["no"]])]
    _save(rc, side, forms)
    _write(rc, rows, actor)
    return view(rc)


def set_formation(rc, team_no, formation: str, actor: str) -> dict:
    _pitch_only(rc)
    _editable(rc)
    lv = view(rc)
    team = next((t for t in lv["teams"] if str(t["no"]) == str(team_no)), None)
    if team is None:
        raise incorrectParameter("Unknown team.")
    if formation not in FORMATIONS[lv["side"]]:
        raise incorrectParameter(f"For {lv['side']}-a-side pick one of: {', '.join(FORMATIONS[lv['side']])}.")
    forms = {t["no"]: t["formation"] for t in lv["teams"]}
    forms[team["no"]] = formation
    _save(rc, lv["side"], forms)
    _write(rc, _reflow(team, formation), actor)
    return view(rc)


def place(rc, key: str, team_no, pos: Optional[str], actor: str) -> dict:
    """Put a player in a spot (pos=None: make them a sub of team_no).
    Whoever held the spot takes the mover's old place — a swap."""
    _tv, _rows = _editable(rc)
    lv = view(rc)
    team = next((t for t in lv["teams"] if str(t["no"]) == str(team_no)), None)
    if team is None:
        raise incorrectParameter("Unknown team.")
    where = {}                                   # key -> (team_no, pos|None, name)
    for t in lv["teams"]:
        for s in t["slots"]:
            if s["key"]:
                where[s["key"]] = (t["no"], s["pos"], s["name"])
        for s in t["subs"]:
            where[s["key"]] = (t["no"], None, s["name"])
    if key not in where:
        raise incorrectParameter("That player isn't IN any more.")
    if lv.get("kind") == "roles":
        # Cricket: "pos" is the role (WK/BAT/AR/BOWL) or None for no role.
        if pos is not None and pos not in _ROLE_CODES:
            raise incorrectParameter("Role must be WK, BAT, AR or BOWL.")
        _old_team, _old_pos, name = where[key]
        _write(rc, [{"who_key": key, "name": name, "team_no": team["no"], "position": pos}], actor)
        return view(rc)
    if pos is not None and pos not in {s["pos"] for s in team["slots"]}:
        raise incorrectParameter("That spot isn't in this formation.")
    old_team, old_pos, name = where[key]
    rows = [{"who_key": key, "name": name, "team_no": team["no"], "position": pos}]
    if pos is not None:
        occupant = next((s for s in team["slots"] if s["pos"] == pos and s["key"] and s["key"] != key), None)
        if occupant:
            rows.append({"who_key": occupant["key"], "name": occupant["name"],
                         "team_no": old_team, "position": old_pos})
    _write(rc, rows, actor)
    return view(rc)


def fill(rc, actor: str, only_empty_teams: bool = False) -> dict:
    """Seat each team's subs in its empty spots, keeper first."""
    _editable(rc)
    lv = view(rc)
    if lv.get("kind") != "pitch":
        return lv            # cricket roles are the group's call, never guessed
    rows = []
    for t in lv["teams"]:
        if only_empty_teams and any(s["key"] for s in t["slots"]):
            continue
        subs = list(t["subs"])
        for s in t["slots"]:
            if not s["key"] and subs:
                p = subs.pop(0)
                rows.append({"who_key": p["key"], "name": p["name"], "team_no": t["no"], "position": s["pos"]})
    if rows:
        _write(rc, rows, actor)
    return view(rc)


def clear(rc, actor: str) -> dict:
    _editable(rc)
    lv = view(rc)
    rows = [{"who_key": s["key"], "name": s["name"], "team_no": t["no"], "position": None}
            for t in lv["teams"] for s in t["slots"] if s["key"]]
    if rows:
        _write(rc, rows, actor)
    return view(rc)


def autofill_new_teams(rc, actor: str) -> None:
    """After teams are made or reshuffled: put everyone in a spot, so the
    pitch opens arranged. Best-effort — never costs the split."""
    try:
        if available(rc):
            fill(rc, actor, only_empty_teams=True)
    except incorrectParameter:
        pass
    except Exception:
        logging.exception("lineup autofill failed for rollcall %s", rc_db_id(rc))


def autofill_open(chat_id: int, actor: str) -> None:
    """After the sport is set: place the players of every open game whose
    teams are already made (unlocked ones — a locked sheet isn't touched).
    Caller holds manager.get_chat_write_lock(chat_id)."""
    from rollcall_manager import manager
    for rc in manager.get_rollcalls(chat_id):
        if getattr(rc, "id", None) is not None:
            autofill_new_teams(rc, actor)


# ── output ─────────────────────────────────────────────────────────────────

def _title(rc) -> str:
    return rc.title if rc.title and rc.title != "<Empty>" else "this game"


def render_text(rc, lv: dict) -> str:
    """Plain text that pastes cleanly into Telegram, WhatsApp or a forum."""
    emoji = SPORTS.get(lv.get("sport") or "", {}).get("emoji", "⚽")
    if lv.get("kind") == "roles":
        return _roles_text(rc, lv, emoji)
    lines = [f"{emoji} Lineup — {_title(rc)} · {lv['side']}-a-side"
             + (f" · {len(lv['teams'])} teams" if lv.get("layout") == "boards" else "")]
    for t in lv["teams"]:
        lines.append("")
        lines.append(f"{t['emoji']} Team {t['label']} ({t['formation']})")
        by_line: dict = {}                       # one row per line, keeper first
        for s in t["slots"]:
            by_line.setdefault(s["pos"].split(":")[0], (s["role"], []))[1].append(s["name"] or "—")
        for role, names in by_line.values():
            lines.append(f"{role:<4}{', '.join(names)}")
        if t["subs"]:
            lines.append(f"SUB {', '.join(s['name'] for s in t['subs'])}")
    if lv.get("locked"):
        lines += ["", "🔒 Locked"]
    return "\n".join(lines)


def _roles_text(rc, lv: dict, emoji: str) -> str:
    labels = {c: (e, c) for c, _l, e in ROLES}
    lines = [f"{emoji} Teams — {_title(rc)}"]
    for t in lv["teams"]:
        lines.append("")
        lines.append(f"{t['emoji']} Team {t['label']} ({len(t['slots']) + len(t['subs'])})")
        for c in _ROLE_CODES:
            names = [s["name"] for s in t["slots"] if s["role"] == c]
            if names:
                lines.append(f"{labels[c][0]} {c:<4} {', '.join(names)}")
        if t["subs"]:
            lines.append(f"·  No role yet: {', '.join(s['name'] for s in t['subs'])}")
    lines += ["", "🧤 WK wicket-keeper · 🏏 BAT batter · ⭐ AR all-rounder · 🎯 BOWL bowler"]
    if lv.get("locked"):
        lines += ["", "🔒 Locked"]
    return "\n".join(lines)


def api_payload(rc, lv: dict) -> dict:
    """The lineup as the partner API and the lineup.published webhook show it."""
    def who(key, name):
        if key and key.startswith("u"):
            try:
                return {"user_id": int(key[1:]), "name": name, "guest": False}
            except ValueError:
                pass
        return {"user_id": None, "name": name, "guest": True}
    return {
        "rollcall_id": rc_db_id(rc),
        "available": lv["available"],
        "sport": lv["sport"],
        "players_per_side": lv["side"],
        "locked": lv["locked"],
        "teams": [{
            "label": t["label"],
            "formation": t["formation"],
            "positions": [{"position": s["pos"], "role": s["role"],
                           "player": who(s["key"], s["name"]) if s["key"] else None}
                          for s in t["slots"]],
            "subs": [who(s["key"], s["name"]) for s in t["subs"]],
        } for t in lv["teams"]],
    }


def emit_published(rc, lv: dict) -> None:
    try:
        from services.webhooks import emit
        payload = api_payload(rc, lv)
        payload.pop("rollcall_id", None)
        emit("lineup.published", chat_id=rc.chat_id, rollcall_id=rc_db_id(rc), data=payload)
    except Exception:
        logging.exception("lineup.published webhook failed")


# Half-pitch board (3–4 teams): every team attacks up, keeper at the bottom.
BOARD_RATIO = (68, 62)


def pitch_xy(team_index: int, pos: str, formation: str, board: bool = False) -> tuple[float, float]:
    """Where a spot sits, in % (x across, y down). On the full pitch team
    index 0 plays up from the bottom, 1 down from the top; on a half-pitch
    board (board=True) every team plays up. The web uses the same numbers
    (app.js _lineupXY) so the picture matches the page."""
    lines = [1] + [int(n) for n in formation.split("-")]
    li, i = (int(p) for p in pos.split(":"))
    if board:
        ys = [84, 61, 38, 15] if len(lines) == 4 else [84, 52, 20]
        return (i + 1) / (lines[li] + 1) * 100, ys[li]
    ys = [91, 79.5, 68, 56.5] if len(lines) == 4 else [91, 75, 59]
    y = ys[li] if team_index == 0 else 100 - ys[li]
    return (i + 1) / (lines[li] + 1) * 100, y


# Pitch picture (Pillow — already a dependency via utils/card_gen).
_TEAM_RGB = {1: (229, 72, 77), 2: (62, 99, 221), 3: (18, 165, 148), 4: (245, 165, 36)}
_FOOT_H = 44


def made_with() -> tuple[str, str | None]:
    """The credit under a shared picture: ("Made with RollCall · host", url).
    Text inside a picture can't be tapped, so the caption carries the url;
    without WEB_BASE_URL there is no page to send anyone to, just the name."""
    base = os.environ.get("WEB_BASE_URL", "").strip().rstrip("/")
    host = urlparse(base).netloc if base else ""
    return (f"Made with RollCall · {host}" if host else "Made with RollCall"), (base + "/" if host else None)


def credit_caption(caption: str) -> str:
    url = made_with()[1]
    return f"{caption}\nMade with RollCall — {url}" if url else caption


def _draw_footer(d, W: int, H: int) -> None:
    from utils.card_gen import _font, _text_w
    f = _font(20)
    label = made_with()[0]
    d.line([40, H - _FOOT_H, W - 40, H - _FOOT_H], fill=(51, 65, 85), width=1)
    d.text(((W - _text_w(d, label, f)) / 2, H - _FOOT_H + 11), label, font=f, fill=(148, 163, 184))


def _render_roles_png(rc, lv: dict) -> BytesIO:
    """Cricket: the two teams side by side, grouped by role."""
    from PIL import Image, ImageDraw
    from utils.card_gen import _font, _sanitize, _ellipsize, _text_w, _to_bytes
    pad, col_w = 40, 400
    W = pad * 2 + len(lv["teams"]) * col_w + (len(lv["teams"]) - 1) * 20
    rows_per_team = [sum(1 for c in _ROLE_CODES if any(s["role"] == c for s in t["slots"])) + (1 if t["subs"] else 0)
                     for t in lv["teams"]]
    names_h = max([sum(len(s["name"]) // 22 + 1 for s in t["slots"]) + len(t["subs"]) // 3 + 1
                   for t in lv["teams"]] + [1])
    H = 170 + 64 * max(rows_per_team + [1]) + 30 * names_h + 60 + _FOOT_H
    img = Image.new("RGB", (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_role, f_name = _font(38, True), _font(24), _font(22, True), _font(22)
    d.text((pad, 30), _sanitize(_ellipsize(f"Teams — {_title(rc)}", 40), bold=True), font=f_title, fill=(255, 255, 255))
    d.text((pad, 78), "WK wicket-keeper · BAT batter · AR all-rounder · BOWL bowler", font=f_sub, fill=(148, 163, 184))
    for ti, t in enumerate(lv["teams"]):
        x = pad + ti * (col_w + 20)
        color = _TEAM_RGB.get(t["no"], (100, 116, 139))
        d.rounded_rectangle([x, 130, x + col_w, 170], radius=12, fill=color)
        d.text((x + 16, 136), _sanitize(f"Team {t['label']} · {len(t['slots']) + len(t['subs'])}", bold=True),
               font=f_role, fill=(255, 255, 255))
        y = 190
        groups = [(c, [s["name"] for s in t["slots"] if s["role"] == c]) for c in _ROLE_CODES]
        if t["subs"]:
            groups.append(("—", [s["name"] for s in t["subs"]]))
        for code, names in groups:
            if not names:
                continue
            d.text((x + 4, y), code, font=f_role, fill=color)
            line, ly = "", y
            for n in names:
                piece = (", " if line else "") + _sanitize(_ellipsize(n, 18))
                if _text_w(d, line + piece, f_name) > col_w - 90:
                    d.text((x + 86, ly), line, font=f_name, fill=(226, 232, 240))
                    line, ly = _sanitize(_ellipsize(n, 18)), ly + 30
                else:
                    line += piece
            d.text((x + 86, ly), line, font=f_name, fill=(226, 232, 240))
            y = ly + 44
    _draw_footer(d, W, H)
    return _to_bytes(img)


def _render_boards_png(rc, lv: dict) -> BytesIO:
    """3–4 teams: a half-pitch per team, every team attacking up."""
    from PIL import Image, ImageDraw
    from utils.card_gen import _font, _sanitize, _ellipsize, _text_w, _to_bytes
    n = len(lv["teams"])
    cols = 2 if n == 4 else n
    rows = -(-n // cols)
    BW = 420
    BH = BW * BOARD_RATIO[1] // BOARD_RATIO[0]
    TAG, SUBS, GAP, HEAD, pad = 46, 64, 30, 120, 40
    W = pad * 2 + cols * BW + (cols - 1) * GAP
    H = HEAD + rows * (TAG + BH + SUBS) + (rows - 1) * GAP + 20 + _FOOT_H
    img = Image.new("RGB", (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(38, True), _font(24)
    f_name, f_disc, f_role, f_tag, f_subs = _font(19, True), _font(21, True), _font(15), _font(20, True), _font(19)
    d.text((pad, 30), _sanitize(_ellipsize(f"Lineup — {_title(rc)}", 48), bold=True), font=f_title, fill=(255, 255, 255))
    d.text((pad, 78), _sanitize(f"{lv['side']}-a-side · {n} teams · "
                                + " · ".join(f"{t['label']} {t['formation']}" for t in lv["teams"])),
           font=f_sub, fill=(148, 163, 184))
    chalk, lw = (235, 245, 238), 2
    for ti, t in enumerate(lv["teams"]):
        color = _TEAM_RGB.get(t["no"], (100, 116, 139))
        bx = pad + (ti % cols) * (BW + GAP)
        by = HEAD + (ti // cols) * (TAG + BH + SUBS + GAP)
        tag = _sanitize(f"Team {t['label']} · {len([s for s in t['slots'] if s['key']]) + len(t['subs'])}", bold=True)
        d.rounded_rectangle([bx, by, bx + _text_w(d, tag, f_tag) + 28, by + 34], radius=17, fill=color)
        d.text((bx + 14, by + 5), tag, font=f_tag, fill=(255, 255, 255))
        y0, x0, x1, y1 = by + TAG, bx, bx + BW, by + TAG + BH
        for i in range(8):
            sy, ey = y0 + BH * i // 8, y0 + BH * (i + 1) // 8
            d.rectangle([x0, sy, x1, ey], fill=(47, 138, 74) if i % 2 == 0 else (42, 125, 67))
        m = 10
        d.rectangle([x0 + m, y0 + m, x1 - m, y1 - m], outline=chalk, width=lw)
        cx = (x0 + x1) / 2
        r = BW * 0.15
        d.arc([cx - r, y0 + m - r, cx + r, y0 + m + r], 0, 180, fill=chalk, width=lw)   # centre circle, half
        bw_, bh_ = BW * 0.53, BH * 0.2
        d.rectangle([cx - bw_ / 2, y1 - m - bh_, cx + bw_ / 2, y1 - m], outline=chalk, width=lw)
        sw_, sh_ = BW * 0.24, BH * 0.07
        d.rectangle([cx - sw_ / 2, y1 - m - sh_, cx + sw_ / 2, y1 - m], outline=chalk, width=lw)
        for sl in t["slots"]:
            px, py = pitch_xy(ti, sl["pos"], t["formation"], board=True)
            px, py = x0 + BW * px / 100, y0 + BH * py / 100
            rr = 22
            if sl["key"]:
                d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=color, outline=(255, 255, 255), width=3)
                ini, fnt, dy = _sanitize((sl["name"] or "?")[:2].upper(), bold=True), f_disc, 13
            else:
                d.ellipse([px - rr, py - rr, px + rr, py + rr], outline=(255, 255, 255), width=2)
                ini, fnt, dy = sl["role"], f_role, 9
            d.text((px - _text_w(d, ini, fnt) / 2, py - dy), ini, font=fnt, fill=(255, 255, 255))
            if sl["name"]:
                label = _sanitize(_ellipsize(sl["name"], 12), bold=True)
                lx = px - _text_w(d, label, f_name) / 2
                d.text((lx + 1, py + rr + 3), label, font=f_name, fill=(0, 0, 0))
                d.text((lx, py + rr + 2), label, font=f_name, fill=(255, 255, 255))
        if t["subs"]:
            d.text((bx, y1 + 14), _sanitize(_ellipsize("Subs: " + ", ".join(s["name"] for s in t["subs"]), 40)),
                   font=f_subs, fill=(226, 232, 240))
    _draw_footer(d, W, H)
    return _to_bytes(img)


def render_png(rc, lv: dict) -> BytesIO:
    if lv.get("kind") == "roles":
        return _render_roles_png(rc, lv)
    if lv.get("layout") == "boards":
        return _render_boards_png(rc, lv)
    from PIL import Image, ImageDraw
    from utils.card_gen import _font, _sanitize, _ellipsize, _text_w, _to_bytes

    W, PH, HEAD = 900, 1240, 120
    subs_lines = [t for t in lv["teams"] if t["subs"]]
    H = HEAD + PH + 40 + 46 * len(subs_lines) + 30 + _FOOT_H
    img = Image.new("RGB", (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(38, True), _font(24)
    f_name, f_disc, f_role, f_tag = _font(22, True), _font(24, True), _font(17), _font(20, True)

    d.text((40, 30), _sanitize(_ellipsize(f"Lineup — {_title(rc)}", 40), bold=True), font=f_title, fill=(255, 255, 255))
    d.text((40, 78), _sanitize(f"{lv['side']}-a-side · "
                               + " vs ".join(f"Team {t['label']} {t['formation']}" for t in lv["teams"])),
           font=f_sub, fill=(148, 163, 184))

    x0, y0, x1, y1 = 40, HEAD, W - 40, HEAD + PH
    stripes = 10
    for i in range(stripes):
        sy = y0 + (y1 - y0) * i // stripes
        ey = y0 + (y1 - y0) * (i + 1) // stripes
        d.rectangle([x0, sy, x1, ey], fill=(47, 138, 74) if i % 2 == 0 else (42, 125, 67))
    chalk, lw = (235, 245, 238), 3
    pw, ph = x1 - x0, y1 - y0
    d.rectangle([x0 + 12, y0 + 12, x1 - 12, y1 - 12], outline=chalk, width=lw)
    d.line([x0 + 12, y0 + ph // 2, x1 - 12, y0 + ph // 2], fill=chalk, width=lw)
    r = pw * 0.12
    cx, cy = x0 + pw / 2, y0 + ph / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=chalk, width=lw)
    for top in (True, False):
        bw, bh = pw * 0.53, ph * 0.14
        gy0 = y0 + 12 if top else y1 - 12 - bh
        d.rectangle([cx - bw / 2, gy0, cx + bw / 2, gy0 + bh], outline=chalk, width=lw)
        sw, sh = pw * 0.26, ph * 0.05
        sy0 = y0 + 12 if top else y1 - 12 - sh
        d.rectangle([cx - sw / 2, sy0, cx + sw / 2, sy0 + sh], outline=chalk, width=lw)

    def spot(team_index: int, pos: str, formation: str) -> tuple[float, float]:
        x, y = pitch_xy(team_index, pos, formation)
        return x0 + pw * x / 100, y0 + ph * y / 100

    for ti, t in enumerate(lv["teams"]):
        color = _TEAM_RGB.get(t["no"], (100, 116, 139))
        tag = _sanitize(f"Team {t['label']}", bold=True)
        tw = _text_w(d, tag, f_tag) + 24
        ty = y1 - 48 if ti == 0 else y0 + 20
        d.rounded_rectangle([x0 + 24, ty, x0 + 24 + tw, ty + 30], radius=15, fill=color)
        d.text((x0 + 36, ty + 3), tag, font=f_tag, fill=(255, 255, 255))
        for s in t["slots"]:
            px, py = spot(ti, s["pos"], t["formation"])
            rr = 28
            if s["key"]:
                d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=color, outline=(255, 255, 255), width=3)
                ini = _sanitize((s["name"] or "?")[:2].upper(), bold=True)
            else:
                d.ellipse([px - rr, py - rr, px + rr, py + rr], outline=(255, 255, 255), width=2)
                ini = s["role"]
            fnt = f_disc if s["key"] else f_role
            iw = _text_w(d, ini, fnt)
            d.text((px - iw / 2, py - (15 if s["key"] else 10)), ini, font=fnt, fill=(255, 255, 255))
            if not s["name"]:
                continue
            label = _sanitize(_ellipsize(s["name"], 14), bold=True)
            lw_ = _text_w(d, label, f_name)
            d.text((px - lw_ / 2 + 1, py + rr + 5), label, font=f_name, fill=(0, 0, 0))
            d.text((px - lw_ / 2, py + rr + 4), label, font=f_name, fill=(255, 255, 255))

    y = y1 + 30
    for t in subs_lines:
        color = _TEAM_RGB.get(t["no"], (148, 163, 184))
        head = _sanitize(f"Subs · Team {t['label']}:", bold=True)
        d.text((40, y), head, font=f_name, fill=color)
        d.text((40 + _text_w(d, head, f_name) + 12, y),
               _sanitize(_ellipsize(", ".join(s["name"] for s in t["subs"]), 50)), font=f_name,
               fill=(226, 232, 240))
        y += 46
    _draw_footer(d, W, H)
    return _to_bytes(img)


def publish_content(rc, tv: dict, teams_text: str, as_picture: bool = False) -> dict:
    """What a publish posts, after teams.publish() recorded it.

    {"text": str, "photo": BytesIO|None, "caption": str|None}. With no pitch
    (no sport, or not 2 teams) it is the team sheet's text, as before. Call
    it AFTER leaving the chat lock — drawing the picture takes a moment."""
    lv = view(rc, tv)
    if not lv["available"]:
        return {"text": teams_text, "photo": None, "caption": None}
    emit_published(rc, lv)
    text = render_text(rc, lv)
    if not as_picture:
        return {"text": text, "photo": None, "caption": None}
    try:
        caption = (f"{SPORTS[lv['sport']]['emoji']} Lineup — {_title(rc)} · {lv['side']}-a-side"
                   if lv.get("kind") == "pitch" else f"{SPORTS[lv['sport']]['emoji']} Teams — {_title(rc)}")
        return {"text": text, "photo": render_png(rc, lv), "caption": credit_caption(caption)}
    except Exception:
        logging.exception("lineup picture failed for rollcall %s — posting text", rc_db_id(rc))
        return {"text": text, "photo": None, "caption": None}
