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

import re
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


def _normalize_for_match(name: str) -> str:
    """Strip whitespace/punctuation noise and lowercase before fuzzy-
    comparing two names, so "Amit K" vs "AmitK" (or "Amit-K") counts as
    identical rather than the raw Levenshtein distance penalizing the
    formatting difference as if it were a real spelling difference."""
    return re.sub(r"[\s.\-_]+", "", (name or "").lower())


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


def get_canonical_map(chat_id: int) -> dict[str, dict]:
    """Batch version of resolve_canonical for proxy names — one query
    (list_identity_links) instead of one get_identity_link call per row.
    Built for hot aggregators (get_ghost_leaderboard,
    get_leaderboard_by_attendance, get_all_dues_balances) that used to call
    resolve_canonical once per proxy row — each such call hit the DB, so a
    chat with a long ghost/proxy history turned one leaderboard render into
    1 query + N. Real users never need this (resolve_canonical is already
    O(1) in-memory for them via its early return), so this only replaces
    the proxy-row DB hit.

    Returns {lower(alias_proxy_name): canonical_dict} for every currently-
    linked alias in the chat. A name absent from this map isn't linked —
    callers should fall back to treating it as its own canonical, matching
    resolve_canonical's own behavior when get_identity_link finds nothing:
    canonical_map.get(name.lower(), {"kind": "proxy", "user_id": None, "proxy_name": name})
    """
    links = db.list_identity_links(chat_id, status="linked")
    result: dict[str, dict] = {}
    for link in links:
        canonical = (
            {"kind": "user", "user_id": link["canonical_user_id"], "proxy_name": None}
            if link["canonical_user_id"] is not None
            else {"kind": "proxy", "user_id": None, "proxy_name": link["canonical_proxy_name"]}
        )
        result[link["alias_proxy_name"].lower()] = canonical
    return result


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
    that have >=1 alias), with a resolved display_name for the canonical.

    Derived by resolving every known proxy name individually rather than
    reading identity_links rows directly: the alias-side uniqueness is
    case-insensitive (matches how every lookup in this module resolves
    names), so two case/whitespace variants of the same raw string (e.g.
    "amit" and "Amit") collapse onto a single link row — the OTHER variant
    still resolves correctly via case-insensitive lookup but has no row of
    its own, so reading rows directly would silently omit it from its
    group's alias list. Re-deriving per name keeps this complete."""
    discarded_lower = {n.lower() for n in list_discarded(chat_id)}
    groups: dict[tuple, dict] = {}
    for name in db.get_all_proxy_names(chat_id):
        if name.lower() in discarded_lower:
            continue
        canonical = resolve_canonical(chat_id, proxy_name=name)
        # Deliberately an EXACT (case-sensitive) comparison, not .lower():
        # the alias lookup itself is case-insensitive, so once "amit" is
        # linked to canonical "Amit", resolving EITHER "Amit" or "amit"
        # finds the very same row. Comparing lowercased strings can't tell
        # "the canonical querying its own name" apart from "the alias
        # querying via that same case-insensitive lookup" — they'd both
        # say "amit"=="amit". Only an exact-string match confirms this
        # query's raw spelling IS the literal stored canonical.
        is_self = canonical["kind"] == "proxy" and canonical["proxy_name"] == name
        if is_self:
            continue  # canonical identities aren't their own alias
        if canonical["kind"] == "user":
            key = ("user", canonical["user_id"])
        else:
            key = ("proxy", (canonical["proxy_name"] or "").lower())
        entry = groups.setdefault(key, {"canonical": canonical, "aliases": []})
        entry["aliases"].append(name)

    result = []
    for (kind, ident), data in groups.items():
        aliases = data["aliases"]
        if kind == "user":
            result.append({
                "kind": "user", "user_id": ident, "proxy_name": None,
                "aliases": sorted(aliases),
                "display_name": _display_name_for_user(chat_id, ident),
            })
        else:
            proxy_name = data["canonical"]["proxy_name"] or ident
            result.append({
                "kind": "proxy", "user_id": None, "proxy_name": proxy_name,
                "aliases": sorted(aliases), "display_name": proxy_name,
            })
    result.sort(key=lambda g: (g["display_name"] or "").lower())
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

    if canonical_user_id is not None and db.get_member_display_info(chat_id, canonical_user_id) is None:
        # canonical_user_id is caller-supplied and otherwise unchecked — a
        # typo or a bad-faith merge could permanently combine a proxy's
        # dues/attendance history onto an arbitrary Telegram user id who
        # has never even been in this chat. Require the target to be a
        # known member (chat_members) before the merge can proceed.
        raise incorrectParameter("That user isn't a known member of this group.")

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


def _auto_merge_exact_duplicates(chat_id: int) -> None:
    """Silently auto-merge proxy names that are identical except for case/
    whitespace (e.g. "Amit" / "amit" / " Amit "  / "Amit  K" vs "Amit K")
    — these are certainly the same physical person typed slightly
    differently, unlike the fuzzy Levenshtein suggestions which genuinely
    need human review. Deliberately proxy<->proxy ONLY: a proxy name that
    exactly matches a REAL member's name is NOT auto-merged (common first
    names could coincidentally collide — that case still needs a human to
    confirm via the suggestion list).

    Runs as a cheap idempotent pre-pass every time identities/suggestions
    are listed (once merged, the alias is filtered out of future passes).
    Uses a system actor (admin_user_id=0) in the audit log, matching the
    scheduler's "ended_by_user_id=0" sentinel convention for non-human-
    initiated actions.

    Deliberately calls db.upsert_identity_link directly rather than going
    through link_identities: that function's self-merge guard is (by
    design, and tested) case-insensitive — appropriate for a human-typed
    manual merge, where "amit" vs "Amit" being treated as identical is
    exactly what should block a confusing manual pick. Here it's the
    opposite: we've already independently verified the two raw strings are
    genuinely different (case/whitespace) with no existing link, so a
    direct upsert is correct and avoids that guard rejecting the exact
    case this function exists to handle. No chain-flattening/cascade is
    needed either — both variants are confirmed unlinked, so there's
    nothing to flatten."""
    proxy_names = db.get_all_proxy_names(chat_id)
    linked_lower = {l["alias_proxy_name"].lower() for l in db.list_identity_links(chat_id, status="linked")}
    discarded_lower = {n.lower() for n in list_discarded(chat_id)}
    groups: dict[str, list[str]] = {}
    for name in proxy_names:
        if name.lower() in linked_lower or name.lower() in discarded_lower:
            continue
        key = " ".join(name.split()).lower()
        groups.setdefault(key, []).append(name)
    for variants in groups.values():
        if len(variants) < 2:
            continue
        variants = sorted(variants)  # deterministic canonical pick
        canonical, aliases = variants[0], variants[1:]
        for alias in aliases:
            db.upsert_identity_link(chat_id, alias, canonical_proxy_name=canonical,
                                     created_by=0, created_by_name="System (auto)")
            db.log_admin_action(chat_id, 0, "System (auto)", "identity_merge",
                                 target_name=alias,
                                 details=f"proxy:{canonical} (auto, exact duplicate)")


def list_all_identities(chat_id: int) -> list[dict]:
    """Every mergeable identity in the chat for the picker UI: active real
    members + every distinct proxy name ever used (excluding discarded
    ones), each tagged with its current resolution (so the UI can show
    "Ajya -> merged into Ajay" inline instead of listing an already-merged
    alias as free-standing). Sorted alphabetically by display_name."""
    _auto_merge_exact_duplicates(chat_id)
    discarded_lower = {n.lower() for n in list_discarded(chat_id)}
    activity = db.get_proxy_name_activity(chat_id)  # {name: {"count", "last_seen"}}, one grouped query
    result = []
    for m in db.get_active_members(chat_id):
        result.append({
            "kind": "user", "user_id": m["user_id"], "proxy_name": None,
            "display_name": m.get("first_name") or m.get("username") or str(m["user_id"]),
            "merged_into": None,  # real users are always canonical, never an alias
            "proxy_count": None, "proxy_last_seen": None,  # only meaningful for proxy names
        })
    for name in db.get_all_proxy_names(chat_id):
        if name.lower() in discarded_lower:
            continue
        canonical = resolve_canonical(chat_id, proxy_name=name)
        # Deliberately an EXACT (case-sensitive) comparison, not .lower():
        # the alias lookup itself is case-insensitive, so once "amit" is
        # linked to canonical "Amit", resolving EITHER "Amit" or "amit"
        # finds the very same row. Comparing lowercased strings can't tell
        # "the canonical querying its own name" apart from "the alias
        # querying via that same case-insensitive lookup" — they'd both
        # say "amit"=="amit". Only an exact-string match confirms this
        # query's raw spelling IS the literal stored canonical.
        is_self = canonical["kind"] == "proxy" and canonical["proxy_name"] == name
        act = activity.get(name, {})
        result.append({
            "kind": "proxy", "user_id": None, "proxy_name": name,
            "display_name": name,
            "merged_into": None if is_self else canonical,
            "proxy_count": act.get("count"), "proxy_last_seen": act.get("last_seen"),
        })
    result.sort(key=lambda i: (i["display_name"] or "").lower())
    return result


def list_discarded(chat_id: int) -> list[str]:
    """Every proxy name marked invalid/garbage in this chat (hidden from
    suggestions/picker/identities list, but reversible — see
    discard_identity)."""
    return sorted(l["alias_proxy_name"] for l in db.list_identity_links(chat_id, status="discarded"))


def discard_identity(chat_id: int, alias_proxy_name: str, *,
                      admin_user_id: int, admin_name: str) -> dict:
    """Mark a garbage/invalid proxy name (a stray "2", "]", or other typo
    from /sif) so it stops appearing in suggestions, the merge picker, and
    the identities list. Nothing is deleted — its historical proxy_users
    rows are untouched — so this is always reversible via undiscard_identity."""
    alias_proxy_name = _norm(alias_proxy_name)
    db.discard_identity_name(chat_id, alias_proxy_name,
                              created_by=admin_user_id, created_by_name=admin_name)
    db.log_admin_action(chat_id, admin_user_id, admin_name, "identity_discard",
                         target_name=alias_proxy_name)
    return {"discarded": True}


def undiscard_identity(chat_id: int, alias_proxy_name: str, *,
                        admin_user_id: int, admin_name: str) -> dict:
    """Reverse discard_identity. Idempotent — a no-op if it wasn't discarded."""
    alias_proxy_name = _norm(alias_proxy_name)
    restored = db.undiscard_identity_name(chat_id, alias_proxy_name)
    if restored:
        db.log_admin_action(chat_id, admin_user_id, admin_name, "identity_undiscard",
                             target_name=alias_proxy_name)
    return {"restored": restored}


def list_suggestions(chat_id: int, limit: int = 200) -> list[dict]:
    """Fuzzy "possible duplicates" — proxy names vs each other AND vs
    active real members' first_name/username. Reuses services/ghost.py's
    lazy-import Levenshtein pattern. Never real<->real. Excludes aliases
    already linked, discarded, or (alias, candidate) pairs already
    dismissed.

    Names are normalized (whitespace/punctuation stripped) before
    comparing, and each proxy name contributes at most ONE suggestion
    (its single best-scoring match) — every candidate within threshold
    for every name produced an unbounded, noisy list; capping per-name
    keeps this to roughly one row per unmatched name, best-first.

    Each result also carries a `confidence` label so callers (the merge
    panel) can visually distinguish a near-certain match from a genuine
    guess: "exact_username"/"exact_first_name" (normalized score 0
    against a real member's username/first_name — username wins ties
    since it's the stronger identity signal, first names commonly
    collide across different people), "exact_proxy" (normalized score 0
    against another proxy name — note _auto_merge_exact_duplicates
    already silently merges away same-chat exact proxy duplicates before
    this runs, so this label is reachable but rare here), or "close"
    (a real but non-zero edit distance, i.e. a genuine guess).

    limit defaults high (200, not the old 20) — the O(n^2) comparison
    below already scores every pair before this function slices to
    limit, so raising it costs nothing extra and avoids a visible row in
    a large chat silently missing its suggestion badge just because 20
    other names elsewhere happened to score better.

    Returns [] (no Levenshtein) if the optional dependency isn't installed
    — same graceful degrade as find_ghost_record."""
    try:
        from Levenshtein import distance as lev_distance
    except ImportError:
        return []

    _auto_merge_exact_duplicates(chat_id)
    proxy_names = db.get_all_proxy_names(chat_id)
    real_members = db.get_active_members(chat_id)
    linked_lower = {l["alias_proxy_name"].lower() for l in db.list_identity_links(chat_id, status="linked")}
    discarded_lower = {n.lower() for n in list_discarded(chat_id)}
    dismissed_pairs = set()
    for d in db.list_identity_links(chat_id, status="dismissed"):
        cand_key = (str(d["canonical_user_id"]) if d["canonical_user_id"] is not None
                    else (d["canonical_proxy_name"] or "").lower())
        dismissed_pairs.add((d["alias_proxy_name"].lower(), cand_key))

    unlinked = [p for p in proxy_names if p.lower() not in linked_lower and p.lower() not in discarded_lower]
    all_candidates = []

    for alias in unlinked:
        # proxy <-> proxy (each unordered pair emitted once)
        for other in unlinked:
            if alias.lower() >= other.lower():
                continue
            score = lev_distance(_normalize_for_match(alias), _normalize_for_match(other))
            if score > _SUGGEST_THRESHOLD:
                continue
            # A proxy<->proxy dismissal may have been recorded in either
            # direction (whichever one was "alias" at suggestion time) —
            # this loop always emits the alphabetically-first name as
            # alias, which might not match, so check both orderings.
            if (alias.lower(), other.lower()) in dismissed_pairs or \
               (other.lower(), alias.lower()) in dismissed_pairs:
                continue
            all_candidates.append({
                "alias_proxy_name": alias, "candidate_kind": "proxy",
                "candidate_user_id": None, "candidate_proxy_name": other,
                "candidate_display_name": other, "score": score,
                "confidence": "exact_proxy" if score == 0 else "close",
            })
        # proxy <-> real member. Checked in (username, first_name) order so
        # that on a tie (e.g. both exactly 0, or both equally fuzzy) the
        # username field wins the argmin below — a username match is a
        # stronger identity signal than a first-name match, which commonly
        # collides across different people.
        for m in real_members:
            field_values = [(f, v) for f, v in (("username", m.get("username")),
                                                  ("first_name", m.get("first_name"))) if v]
            if not field_values:
                continue
            scored = [(lev_distance(_normalize_for_match(alias), _normalize_for_match(v)), field)
                      for field, v in field_values]
            best_score, best_field = min(scored, key=lambda t: t[0])
            if best_score > _SUGGEST_THRESHOLD:
                continue
            if (alias.lower(), str(m["user_id"])) in dismissed_pairs:
                continue
            if best_score == 0:
                confidence = "exact_username" if best_field == "username" else "exact_first_name"
            else:
                confidence = "close"
            all_candidates.append({
                "alias_proxy_name": alias, "candidate_kind": "user",
                "candidate_user_id": m["user_id"], "candidate_proxy_name": None,
                "candidate_display_name": m.get("first_name") or m.get("username") or str(m["user_id"]),
                "score": best_score, "confidence": confidence,
            })

    # Greedy cap: process best-scoring matches first, and once a name
    # (whether as alias or as a proxy candidate) is "claimed" by a
    # suggestion, skip any further weaker suggestion touching that same
    # name — bounds the list to roughly one row per unmatched name instead
    # of every pairwise match within threshold.
    all_candidates.sort(key=lambda c: c["score"])
    claimed: set[str] = set()
    result = []
    for c in all_candidates:
        involved = {c["alias_proxy_name"].lower()}
        if c["candidate_kind"] == "proxy":
            involved.add(c["candidate_proxy_name"].lower())
        if involved & claimed:
            continue
        claimed.update(involved)
        result.append(c)
    return result[:limit]


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
