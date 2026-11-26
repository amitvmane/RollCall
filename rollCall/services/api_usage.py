"""
API usage per key — what the Integrations dashboard, /api_usage and a
partner's own GET /chats/{chat_id}/usage show.

Counted once per REST request in api/rate_limit.py (the outermost API layer,
so 429s count too) by bearer-token hash, hour and endpoint: requests, client
errors (4xx except 429), server errors (5xx), rate-limited (429) and total
latency. Like services/traffic.py it is a dict increment on the hot path,
flushed to api_key_usage once a minute; only app keys and /gentoken tokens
are kept (a member's own token, or a bearer that isn't a key, is dropped at
flush), so the table holds partner traffic only. RETENTION_DAYS of history.

Every reader takes a chat_id and only ever sees keys issued for that chat —
a partner's own view (usage_for_key) sees only its own key.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import db

RETENTION_DAYS = 90
WINDOWS = (1, 7, 30, 90)

_pending: dict = {}      # (token_hash, hour, route) -> [requests, client_err, server_err, limited, ms]
_lock = threading.Lock()


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _hour(ts: Optional[float] = None) -> int:
    t = int(ts if ts is not None else time.time())
    return t - t % 3600


def route_label(request) -> str:
    """'POST /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/votes' — the
    route's template, never the raw path (ids would make every call unique)."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not path:
        return f"{request.method} (no such endpoint)"
    return f"{request.method} {path.replace('/api/v1', '', 1)}"


def record(token_hash: str, route: str, status: int, elapsed_ms: float) -> None:
    """Hot path. Never raises."""
    try:
        key = (token_hash, _hour(), route[:200])
        with _lock:
            row = _pending.setdefault(key, [0, 0, 0, 0, 0])
            row[0] += 1
            if status == 429:
                row[3] += 1
            elif status >= 500:
                row[2] += 1
            elif status >= 400:
                row[1] += 1
            row[4] += int(elapsed_ms)
    except Exception:
        pass


def _tracked(hashes: set) -> set:
    """Of these token hashes, the app keys and /gentoken tokens."""
    if not hashes:
        return set()
    ph = _ph()
    out = set()
    hl = list(hashes)
    with db._cursor() as cursor:
        for i in range(0, len(hl), 200):
            chunk = hl[i:i + 200]
            cursor.execute(
                f"SELECT token_hash FROM api_tokens WHERE token_hash IN ({','.join([ph] * len(chunk))}) "
                f"AND (app_key = 1 OR label LIKE {ph})", (*chunk, "%/gentoken%"))
            out |= {r["token_hash"] if isinstance(r, dict) else r[0] for r in cursor.fetchall()}
    return out


def flush() -> int:
    """Add the in-memory counts to api_key_usage. Returns rows written."""
    with _lock:
        pending = dict(_pending)
        _pending.clear()
    if not pending:
        return 0
    try:
        keep = _tracked({k[0] for k in pending})
        rows = [(k, v) for k, v in pending.items() if k[0] in keep]
        ph = _ph()
        ex = "EXCLUDED" if db.db_type == "postgresql" else "excluded"
        with db._cursor(commit=True) as cursor:
            for (th, hour, route), (n, ce, se, rl, ms) in rows:
                cursor.execute(
                    f"INSERT INTO api_key_usage (token_hash, hour, route, requests, client_errors, "
                    f"server_errors, rate_limited, total_ms) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph}) "
                    f"ON CONFLICT (token_hash, hour, route) DO UPDATE SET "
                    f"requests = api_key_usage.requests + {ex}.requests, "
                    f"client_errors = api_key_usage.client_errors + {ex}.client_errors, "
                    f"server_errors = api_key_usage.server_errors + {ex}.server_errors, "
                    f"rate_limited = api_key_usage.rate_limited + {ex}.rate_limited, "
                    f"total_ms = api_key_usage.total_ms + {ex}.total_ms",
                    (th, hour, route, n, ce, se, rl, ms))
        return len(rows)
    except Exception:
        with _lock:   # put them back; the next tick retries
            for k, v in pending.items():
                row = _pending.setdefault(k, [0, 0, 0, 0, 0])
                for i in range(5):
                    row[i] += v[i]
        logging.exception("api_usage.flush failed")
        return 0


def prune() -> int:
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM api_key_usage WHERE hour < {_ph()}",
                       (_hour() - RETENTION_DAYS * 86400,))
        return cursor.rowcount or 0


def reset_for_tests() -> None:
    with _lock:
        _pending.clear()


# ── Reading ──────────────────────────────────────────────────────────────────

def _days(days) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    return min(WINDOWS, key=lambda w: abs(w - d))


def _rows(hashes: list, since: int) -> list:
    if not hashes:
        return []
    ph = _ph()
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT token_hash, hour, route, requests, client_errors, server_errors, rate_limited, total_ms "
            f"FROM api_key_usage WHERE hour >= {ph} AND token_hash IN ({','.join([ph] * len(hashes))})",
            (since, *hashes))
        cols = ("token_hash", "hour", "route", "requests", "client_errors", "server_errors",
                "rate_limited", "total_ms")
        return [dict(r) if isinstance(r, dict) else dict(zip(cols, r)) for r in cursor.fetchall()]


def _empty() -> dict:
    return {"requests": 0, "errors": 0, "server_errors": 0, "rate_limited": 0, "total_ms": 0}


def _add(acc: dict, r: dict) -> None:
    acc["requests"] += int(r["requests"])
    acc["errors"] += int(r["client_errors"]) + int(r["server_errors"])
    acc["server_errors"] += int(r["server_errors"])
    acc["rate_limited"] += int(r["rate_limited"])
    acc["total_ms"] += int(r["total_ms"])


def _finish(acc: dict) -> dict:
    n = acc["requests"]
    return {"requests": n, "errors": acc["errors"], "server_errors": acc["server_errors"],
            "rate_limited": acc["rate_limited"],
            "error_rate": round(100.0 * acc["errors"] / n, 1) if n else 0.0,
            "avg_ms": round(acc["total_ms"] / n) if n else 0}


def _report(rows: list, days: int, key_of) -> dict:
    now_hour = _hour()
    daily = {}
    for i in range(days):
        d = datetime.fromtimestamp(now_hour - i * 86400, timezone.utc).strftime("%Y-%m-%d")
        daily[d] = _empty()
    total, per_key, per_route = _empty(), {}, {}
    for r in rows:
        d = datetime.fromtimestamp(int(r["hour"]), timezone.utc).strftime("%Y-%m-%d")
        if d in daily:
            _add(daily[d], r)
        _add(total, r)
        _add(per_key.setdefault(key_of(r["token_hash"]), _empty()), r)
        _add(per_route.setdefault(r["route"], _empty()), r)
    routes = sorted(({"route": k, **_finish(v)} for k, v in per_route.items()),
                    key=lambda x: -x["requests"])[:15]
    return {
        "days": days,
        "totals": _finish(total),
        "daily": [{"date": d, **_finish(v)} for d, v in sorted(daily.items())],
        "per_key": {k: _finish(v) for k, v in per_key.items()},
        "top_routes": routes,
    }


def _webhook_stats(chat_id: int, since: int) -> list:
    ph = _ph()
    out = {}
    with db._cursor() as cursor:
        cursor.execute(f"SELECT id, url, active FROM webhook_endpoints WHERE chat_id={ph}", (chat_id,))
        for r in cursor.fetchall():
            r = dict(r) if isinstance(r, dict) else dict(zip(("id", "url", "active"), r))
            out[int(r["id"])] = {"id": int(r["id"]), "url": r["url"], "active": bool(int(r["active"] or 0)),
                                 "delivered": 0, "failed": 0, "pending": 0}
        if out:
            ids = list(out)
            cursor.execute(
                f"SELECT endpoint_id, status, COUNT(*) AS n FROM webhook_deliveries WHERE created_at >= {ph} "
                f"AND endpoint_id IN ({','.join([ph] * len(ids))}) GROUP BY endpoint_id, status",
                (since, *ids))
            for r in cursor.fetchall():
                eid, st, n = (r["endpoint_id"], r["status"], r["n"]) if isinstance(r, dict) else r
                if st in ("delivered", "failed", "pending") and int(eid) in out:
                    out[int(eid)][st] += int(n)
    return list(out.values())


def group_usage(chat_id: int, days=7) -> dict:
    """The admins' dashboard: every app key and /gentoken token of the group."""
    from services.partner_api import list_keys
    flush()
    days = _days(days)
    since = _hour() - (days - 1) * 86400 - (_hour() % 86400)
    keys = list_keys(chat_id)
    shown = {k["key_id"] for k in keys}
    hashes = [r["token_hash"] for r in db.list_api_tokens(chat_id) if r["token_hash"][:16] in shown]
    rep = _report(_rows(hashes, since), days, lambda th: th[:16])
    per_key = rep.pop("per_key")
    fields = ("key_id", "kind", "label", "partner", "status", "scopes",
              "expires_at", "last_used_at", "last_used_ip")
    rep["keys"] = [{**{f: k.get(f) for f in fields}, **per_key.get(k["key_id"], _finish(_empty()))}
                   for k in keys]
    rep["keys"].sort(key=lambda k: (k["status"] != "active", -k["requests"]))
    rep["webhooks"] = _webhook_stats(chat_id, since)
    return rep


def usage_for_key(chat_id: int, token_hash: str, days=7) -> dict:
    """A partner's own view: this key only, plus its terms."""
    from services.partner_api import list_keys
    flush()
    days = _days(days)
    since = _hour() - (days - 1) * 86400 - (_hour() % 86400)
    rep = _report(_rows([token_hash], since), days, lambda th: th[:16])
    rep.pop("per_key")
    me = next((k for k in list_keys(chat_id) if k["key_id"] == token_hash[:16]), None) or {}
    rep["key"] = {f: me.get(f) for f in ("key_id", "kind", "label", "partner", "agreement_ref", "scopes",
                                         "status", "expires_at", "allowed_ips", "last_used_at")}
    import os
    rep["rate_limit"] = {"requests": int(os.environ.get("REST_API_RATE_LIMIT_MAX_REQUESTS", "60")),
                         "window_seconds": int(os.environ.get("REST_API_RATE_LIMIT_WINDOW_SECONDS", "60"))}
    return rep


def summary_text(chat_id: int, days=7) -> str:
    """/api_usage in Telegram — the same numbers as the web dashboard."""
    rep = group_usage(chat_id, days)
    t = rep["totals"]
    lines = [f"📊 API usage — last {rep['days']} day{'s' if rep['days'] != 1 else ''}",
             f"{t['requests']} requests · {t['error_rate']}% errors · {t['rate_limited']} rate-limited"
             + (f" · {t['avg_ms']} ms avg" if t["requests"] else "")]
    keys = [k for k in rep["keys"] if k["requests"] or k["status"] == "active"]
    if not keys:
        lines.append("\nNo app keys yet — make one on the group's web page → Admin → 🔌 Integrations.")
    else:
        lines.append("")
        for k in keys[:10]:
            who = k.get("partner") or k.get("label") or k["key_id"]
            state = "" if k["status"] == "active" else f" ({k['status']})"
            lines.append(f"🔑 {who}{state}: {k['requests']} req, {k['errors']} errors"
                         + (f", {k['rate_limited']} limited" if k["rate_limited"] else "")
                         + (f" · last {str(k['last_used_at'])[:16]}" if k.get("last_used_at") else " · never used"))
    if rep["top_routes"]:
        lines.append("\nBusiest endpoints:")
        for r in rep["top_routes"][:5]:
            lines.append(f"• {r['route']} — {r['requests']}")
    hooks = rep["webhooks"]
    if hooks:
        lines.append("\nWebhooks:")
        for w in hooks[:5]:
            lines.append(f"🪝 {w['url'][:60]} — {w['delivered']} delivered, {w['failed']} failed"
                         + (f", {w['pending']} retrying" if w["pending"] else "")
                         + ("" if w["active"] else " (off)"))
    lines.append("\nCharts and per-key details: web page → Admin → 🔌 Integrations → Usage.")
    return "\n".join(lines)
