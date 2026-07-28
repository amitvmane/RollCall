"""
Identity-merge services — platform-agnostic core for combining a real
Telegram user with one or more free-text proxy aliases (or several proxy
aliases with each other) so stats/dues/ghost-tracking treat them as one
person.

Problem this solves: an admin repeatedly uses /sif to add the same
physically-present regulars by free-text name instead of having them vote
themselves via /in — fragmenting one real person's attendance/streaks/
ghost-count/dues history across a proxy-name identity, sometimes several
differently-spelled ones (nicknames, surnames, typos).

Design: an alias/link table (`identity_links`) resolved at READ time.
Historical rows in dues_entries, ghost_records, user_stats, proxy_stats,
proxy_users etc. are NEVER mutated — dues_entries is explicitly append-only
per CLAUDE.md, and this also makes unmerge trivial (delete the link row,
everything reverts since nothing was ever rewritten). The one deliberate
exception: services/dues.py's _resolve_member canonicalizes a resolved
name before returning, so future dues commands typed against an old alias
spelling attribute to the merged identity instead of forking the ledger
again — see that module for the reasoning.

Only the alias side of a link is ever a proxy name — real user_ids are
always canonical/self (never merged "into" something else). Chains are
flattened at write time (merging into an already-aliased name targets ITS
final canonical) and cascaded (any alias that was targeting the thing just
merged gets re-pointed too), so every read site needs exactly one lookup,
never a recursive walk.

Writes elsewhere (voting, ghost-increment) are NOT merge-aware — a vote or
a ghost-mark always lands on whichever identity actually did it. Only
reads/aggregation (this module, plus the db.py functions it wraps) combine
across an alias group.
"""
from __future__ import annotations

from typing import Optional

import db
from exceptions import incorrectParameter, parameterMissing
from rollcall_manager import manager

_MAX_ALIAS_LEN = 40  # matches services/proxy.py's _MAX_PROXY_NAME_LEN
_SUGGEST_THRESHOLD = 2  # tighter than services/ghost.py's find_ghost_record's 3 —
                         # suggestions run unattended over many pairs, so a lower
                         # bar avoids noisy false positives; ghost.py's 3 is a
                         # single deliberate user-typed lookup, different
                         # precision/recall tradeoff


def _norm(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise parameterMissing("A name is required.")
    if len(name) > _MAX_ALIAS_LEN:
        raise parameterMissing(f"Name is too long (max {_MAX_ALIAS_LEN} characters).")
    return name


def _pct(num: int, denom: int) -> Optional[float]:
    return round(num / denom * 100, 1) if denom > 0 else None


def _display_name_for_user(chat_id: int, user_id: int) -> str:
    info = db.get_member_display_info(chat_id, user_id)
    if info:
        return info.get("first_name") or info.get("username") or str(user_id)
    return str(user_id)


def resolve_canonical(chat_id: int, *, user_id: Optional[int] = None,
                       proxy_name: Optional[str] = None) -> dict:
    """Single-hop resolution of one identity to its canonical form.

    Pass exactly one of user_id/proxy_name. Real users are always already
    canonical (the alias side of a link is always a proxy name). A proxy
    name with no active link resolves to itself.

    Returns: {"kind": "user"|"proxy", "user_id": int|None, "proxy_name": str|None}
    """
    if user_id is not None:
        return {"kind": "user", "user_id": user_id, "proxy_name": None}
    link = db.get_identity_link(chat_id, proxy_name)
    if link is None:
        return {"kind": "proxy", "user_id": None, "proxy_name": proxy_name}
    if link["canonical_user_id"] is not None:
        return {"kind": "user", "user_id": link["canonical_user_id"], "proxy_name": None}
    return {"kind": "proxy", "user_id": None, "proxy_name": link["canonical_proxy_name"]}


def get_alias_group(chat_id: int, *, user_id: Optional[int] = None,
                     proxy_name: Optional[str] = None) -> dict:
    """Resolve to canonical, then collect every alias pointing at it.

    Returns:
      {"kind": "user"|"proxy", "user_id": int|None, "proxy_name": str|None,
       "aliases": list[str], "display_name": str}
    """
    canonical = resolve_canonical(chat_id, user_id=user_id, proxy_name=proxy_name)
    links = db.get_links_by_canonical(
        chat_id,
        canonical_user_id=canonical["user_id"],
        canonical_proxy_name=canonical["proxy_name"],
    )
    display_name = (_display_name_for_user(chat_id, canonical["user_id"])
                     if canonical["kind"] == "user" else canonical["proxy_name"])
    return {**canonical, "aliases": sorted(l["alias_proxy_name"] for l in links),
            "display_name": display_name}


def list_identity_groups(chat_id: int) -> list[dict]:
    """Every merge group currently active in a chat (canonical identities
    that have >=1 alias), with a resolved display_name for the canonical."""
    links = db.list_identity_links(chat_id, status="linked")
    groups: dict[tuple, list[str]] = {}
    proxy_casing: dict[str, str] = {}
    for link in links:
        if link["canonical_user_id"] is not None:
            key = ("user", link["canonical_user_id"])
        else:
            canon = link["canonical_proxy_name"] or ""
            key = ("proxy", canon.lower())
            proxy_casing.setdefault(canon.lower(), canon)
        groups.setdefault(key, []).append(link["alias_proxy_name"])

    result = []
    for (kind, ident), aliases in groups.items():
        if kind == "user":
            result.append({
                "kind": "user", "user_id": ident, "proxy_name": None,
                "aliases": sorted(aliases),
                "display_name": _display_name_for_user(chat_id, ident),
            })
        else:
            proxy_name = proxy_casing.get(ident, ident)
            result.append({
                "kind": "proxy", "user_id": None, "proxy_name": proxy_name,
                "aliases": sorted(aliases), "display_name": proxy_name,
            })
    return result


def link_identities(chat_id: int, alias_proxy_name: str, *,
                     canonical_user_id: Optional[int] = None,
                     canonical_proxy_name: Optional[str] = None,
                     admin_user_id: int, admin_name: str) -> dict:
    """Merge alias_proxy_name into the given canonical identity (pass
    exactly one of canonical_user_id/canonical_proxy_name).

    Flattens: if canonical_proxy_name is itself currently an alias of
    something else, the new link is written against THAT final target.
    Cascades: any OTHER existing aliases that were pointing at
    alias_proxy_name get re-pointed to the same final target in the same
    call, so no alias is ever more than one hop from canonical.

    Returns the resulting get_alias_group() for the canonical identity.
    Raises: parameterMissing (bad args), incorrectParameter (self-merge, cycle).
    """
    alias_proxy_name = _norm(alias_proxy_name)
    if (canonical_user_id is None) == (canonical_proxy_name is None):
        raise parameterMissing("Specify exactly one merge target: a real user or another proxy name.")

    if canonical_proxy_name is not None:
        canonical_proxy_name = _norm(canonical_proxy_name)
        if canonical_proxy_name.lower() == alias_proxy_name.lower():
            raise incorrectParameter("Can't merge a name into itself.")
        target = resolve_canonical(chat_id, proxy_name=canonical_proxy_name)
        final_user_id = target["user_id"] if target["kind"] == "user" else None
        final_proxy_name = target["proxy_name"] if target["kind"] == "proxy" else None
    else:
        final_user_id = canonical_user_id
        final_proxy_name = None

    if final_proxy_name is not None and final_proxy_name.lower() == alias_proxy_name.lower():
        raise incorrectParameter("This merge would create a cycle.")

    db.upsert_identity_link(
        chat_id, alias_proxy_name,
        canonical_user_id=final_user_id, canonical_proxy_name=final_proxy_name,
        created_by=admin_user_id, created_by_name=admin_name,
    )
    # Cascade: alias_proxy_name may itself have had aliases pointing at it
    # (it was a merge target before) — repoint them to the new final target
    # so nothing is ever more than one hop from canonical.
    db.repoint_links(chat_id, alias_proxy_name, to_user_id=final_user_id, to_proxy_name=final_proxy_name)

    db.log_admin_action(
        chat_id, admin_user_id, admin_name, "identity_merge",
        target_name=alias_proxy_name,
        details=(f"user:{final_user_id}" if final_user_id is not None else f"proxy:{final_proxy_name}"),
    )
    return get_alias_group(chat_id, user_id=final_user_id, proxy_name=final_proxy_name)


def unmerge_identity(chat_id: int, alias_proxy_name: str, *,
                      admin_user_id: int, admin_name: str) -> dict:
    """Delete this alias's own link row. Sibling aliases in the same group
    are untouched (chain-flattening means no alias points at another
    alias, only ever at the shared final canonical). Idempotent — a no-op
    if the alias wasn't linked."""
    alias_proxy_name = _norm(alias_proxy_name)
    deleted = db.delete_identity_link(chat_id, alias_proxy_name)
    if deleted:
        db.log_admin_action(chat_id, admin_user_id, admin_name, "identity_unmerge",
                             target_name=alias_proxy_name)
    return {"unmerged": deleted}


def list_all_identities(chat_id: int) -> list[dict]:
    """Every mergeable identity in the chat for the picker UI: active real
    members + every distinct proxy name ever used, each tagged with its
    current resolution (so the UI can show "Ajya -> merged into Ajay"
    inline instead of listing an already-merged alias as free-standing)."""
    result = []
    for m in db.get_active_members(chat_id):
        result.append({
            "kind": "user", "user_id": m["user_id"], "proxy_name": None,
            "display_name": m.get("first_name") or m.get("username") or str(m["user_id"]),
            "merged_into": None,  # real users are always canonical, never an alias
        })
    for name in db.get_all_proxy_names(chat_id):
        canonical = resolve_canonical(chat_id, proxy_name=name)
        is_self = canonical["kind"] == "proxy" and (canonical["proxy_name"] or "").lower() == name.lower()
        result.append({
            "kind": "proxy", "user_id": None, "proxy_name": name,
            "display_name": name,
            "merged_into": None if is_self else canonical,
        })
    return result


def list_suggestions(chat_id: int, limit: int = 20) -> list[dict]:
    """Fuzzy "possible duplicates" — proxy names vs each other AND vs
    active real members' first_name/username. Reuses services/ghost.py's
    lazy-import Levenshtein pattern. Never real<->real. Excludes aliases
    already linked and (alias, candidate) pairs already dismissed.

    Returns [] (no Levenshtein) if the optional dependency isn't installed
    — same graceful degrade as find_ghost_record."""
    try:
        from Levenshtein import distance as lev_distance
    except ImportError:
        return []

    proxy_names = db.get_all_proxy_names(chat_id)
    real_members = db.get_active_members(chat_id)
    linked_lower = {l["alias_proxy_name"].lower() for l in db.list_identity_links(chat_id, status="linked")}
    dismissed_pairs = set()
    for d in db.list_identity_links(chat_id, status="dismissed"):
        cand_key = (str(d["canonical_user_id"]) if d["canonical_user_id"] is not None
                    else (d["canonical_proxy_name"] or "").lower())
        dismissed_pairs.add((d["alias_proxy_name"].lower(), cand_key))

    unlinked = [p for p in proxy_names if p.lower() not in linked_lower]
    candidates = []

    for alias in unlinked:
        # proxy <-> proxy (each unordered pair emitted once)
        for other in unlinked:
            if alias.lower() >= other.lower():
                continue
            score = lev_distance(alias.lower(), other.lower())
            if score > _SUGGEST_THRESHOLD:
                continue
            # A proxy<->proxy dismissal may have been recorded in either
            # direction (whichever one was "alias" at suggestion time) —
            # this loop always emits the alphabetically-first name as
            # alias, which might not match, so check both orderings.
            if (alias.lower(), other.lower()) in dismissed_pairs or \
               (other.lower(), alias.lower()) in dismissed_pairs:
                continue
            candidates.append({
                "alias_proxy_name": alias, "candidate_kind": "proxy",
                "candidate_user_id": None, "candidate_proxy_name": other,
                "candidate_display_name": other, "score": score,
            })
        # proxy <-> real member
        for m in real_members:
            names = [n for n in (m.get("first_name"), m.get("username")) if n]
            if not names:
                continue
            best_score = min(lev_distance(alias.lower(), n.lower()) for n in names)
            if best_score > _SUGGEST_THRESHOLD:
                continue
            if (alias.lower(), str(m["user_id"])) in dismissed_pairs:
                continue
            candidates.append({
                "alias_proxy_name": alias, "candidate_kind": "user",
                "candidate_user_id": m["user_id"], "candidate_proxy_name": None,
                "candidate_display_name": m.get("first_name") or m.get("username") or str(m["user_id"]),
                "score": best_score,
            })

    candidates.sort(key=lambda c: c["score"])
    return candidates[:limit]


def dismiss_suggestion(chat_id: int, alias_proxy_name: str, *,
                        candidate_user_id: Optional[int] = None,
                        candidate_proxy_name: Optional[str] = None,
                        admin_user_id: int, admin_name: str) -> dict:
    """Record that this specific (alias, candidate) pairing was reviewed
    and rejected. Does NOT affect the alias's ability to be suggested
    against a DIFFERENT candidate, or to be manually merged into this same
    candidate later via link_identities (only list_suggestions filters on
    dismissals)."""
    alias_proxy_name = _norm(alias_proxy_name)
    if (candidate_user_id is None) == (candidate_proxy_name is None):
        raise parameterMissing("Specify exactly one candidate to dismiss.")
    db.insert_dismissed_suggestion(
        chat_id, alias_proxy_name,
        candidate_user_id=candidate_user_id, candidate_proxy_name=candidate_proxy_name,
        created_by=admin_user_id, created_by_name=admin_name,
    )
    return {"dismissed": True}


def combined_ghost_count(chat_id: int, *, user_id: Optional[int] = None,
                          proxy_name: Optional[str] = None) -> int:
    """Sum of ghost_count across every member of the alias group (canonical
    + all aliases). Used by both check_ghost_reconfirmation_needed and
    check_proxy_ghost_reconfirmation_needed."""
    group = get_alias_group(chat_id, user_id=user_id, proxy_name=proxy_name)
    if group["kind"] == "user":
        total = db.get_ghost_count(chat_id, group["user_id"])
    else:
        total = db.get_ghost_count_by_proxy_name(chat_id, group["proxy_name"])
    for alias in group["aliases"]:
        total += db.get_ghost_count_by_proxy_name(chat_id, alias)
    return total


def identity_stats(chat_id: int, *, user_id: Optional[int] = None,
                    proxy_name: Optional[str] = None) -> dict:
    """Combined attendance/vote/ghost stats for an identity's full alias
    group — the one entry point that can represent a group spanning BOTH a
    real user and proxy aliases, which neither services.stats.personal_stats
    nor .proxy_stats alone can do (those two are left untouched for
    single-identity callers).

    Streak semantics: best_streak = max() across the group (a high-water
    mark; summing would double-count non-overlapping runs). current_streak
    = the value from whichever member was most recently active (an ongoing
    streak can only belong to whoever is currently accruing it) — ties
    keep the canonical member's value (it's iterated first).
    """
    group = get_alias_group(chat_id, user_id=user_id, proxy_name=proxy_name)
    members: list[tuple[str, object]] = []
    if group["kind"] == "user":
        members.append(("user", group["user_id"]))
    else:
        members.append(("proxy", group["proxy_name"]))
    members.extend(("proxy", alias) for alias in group["aliases"])  # already sorted

    total_rollcalls = db.get_chat_ended_rollcall_count(chat_id)
    sessions_attended = 0
    total_in = total_out = total_maybe = total_waiting = 0
    total_sessions_voted = 0
    ghost_count = 0
    best_streak = 0
    current_streak = 0
    most_recent_ts = None

    for kind, ident in members:
        if kind == "user":
            attended = db.get_user_attendance_count(chat_id, ident)
            row = db.get_user_stats_row(chat_id, ident) or {}
            best = int(row.get("best_streak") or 0)
            current = int(row.get("current_streak") or 0)
            ghost = db.get_ghost_count(chat_id, ident)
            last_activity = db.get_identity_last_activity(chat_id, user_id=ident)
        else:
            attended = db.get_proxy_attendance_count(chat_id, ident)
            row = db.get_proxy_stats(chat_id, ident) or {}
            streaks = db.get_proxy_streaks(chat_id, ident) or {}
            best = int(streaks.get("best_streak") or 0)
            current = int(streaks.get("current_streak") or 0)
            ghost = db.get_ghost_count_by_proxy_name(chat_id, ident)
            last_activity = db.get_identity_last_activity(chat_id, proxy_name=ident)

        sessions_attended += attended
        total_in += int(row.get("total_in") or 0)
        total_out += int(row.get("total_out") or 0)
        total_maybe += int(row.get("total_maybe") or 0)
        total_waiting += int(row.get("total_waiting_to_in") or 0)
        total_sessions_voted += int(row.get("total_rollcalls") or 0)
        ghost_count += ghost
        best_streak = max(best_streak, best)
        if last_activity and (most_recent_ts is None or last_activity > most_recent_ts):
            most_recent_ts = last_activity
            current_streak = current

    return {
        "kind": group["kind"],
        "user_id": group["user_id"],
        "proxy_name": group["proxy_name"],
        "aliases": group["aliases"],
        "total_rollcalls_in_chat": total_rollcalls,
        "sessions_attended": sessions_attended,
        "attendance_rate": _pct(sessions_attended, total_rollcalls),
        "total_in_votes": total_in,
        "total_out_votes": total_out,
        "total_maybe_votes": total_maybe,
        "total_waiting_to_in": total_waiting,
        "total_sessions_voted": total_sessions_voted,
        "voting_rate": _pct(total_sessions_voted, total_rollcalls),
        "best_streak": best_streak,
        "current_streak": current_streak,
        "ghost_count": ghost_count,
        "absent_limit": manager.get_absent_limit(chat_id),
    }
