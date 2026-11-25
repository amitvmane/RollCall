# RollCall partner API — v1

For developers connecting another app or service to a RollCall group: read its
rollcalls, take votes, and hear about changes as they happen.

**Interactive reference** (every endpoint, request and response, with *Authorize*
for your key and *Try it out*): [/help/api/reference](/help/api/reference) ·
machine-readable OpenAPI: [/api/v1/partner/openapi.json](/api/v1/partner/openapi.json)

## 1. Get a key

A group admin opens the group's web page → **Admin → 🔌 Integrations → Make key**,
names your app, and picks scopes. The key (`rc_…`) is shown once.

| Scope | Allows |
|---|---|
| `read` | list and read rollcalls |
| `vote` | cast votes for any member of the group (`in` / `out` / `maybe`) |
| `admin` | everything an admin can do: start/end rollcalls, settings, proxy (guest) votes, remove/move members. Only a group **owner** can make these. |

A key belongs to **one group**. Every request names that group's `chat_id` in the
path; a different chat is refused (403). Keys expire (default 365 days) and can
be revoked on the same screen. An `admin` key stops working if the admin who
made it stops being an admin.

```
Authorization: Bearer rc_…
```

Base URL: `https://<rollcall-host>/api/v1`

## 2. Name rollcalls by `id`

Every rollcall has a permanent `id` (in every rollcall response). Use the
`by-id` paths:

| Method | Path | Scope |
|---|---|---|
| GET | `/chats/{chat_id}/rollcalls` | read — open rollcalls |
| POST | `/chats/{chat_id}/rollcalls` | admin — start one |
| GET | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}` | read |
| DELETE | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}` | admin — end it |
| POST | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/votes` | vote |
| POST | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/proxy-votes` | admin — a guest without an account |
| DELETE | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/users/{name}` | admin |
| PATCH | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/users/{name}/status` | admin |
| PUT | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/settings/{limit,location,fee,title,when,reminder}` | admin |

The older `/rollcalls/{rc_number}/…` paths (the 1-based display number) keep
working — they are not being removed — but that number shifts when an earlier
rollcall ends, so a call can reach a different rollcall than the one you read.
Their responses carry `Deprecation: true` and a `Link` to the `by-id` path.
Every rollcall response carries `X-Rollcall-Id`.

### Vote

```http
POST /api/v1/chats/-1001234567890/rollcalls/by-id/42/votes
Authorization: Bearer rc_…
Content-Type: application/json

{"vote": "in", "user_id": 168415137, "first_name": "Priya", "comment": "bringing a ball"}
```

`user_id` is the member's id as RollCall knows it: a Telegram id for Telegram
members, or the id RollCall gave a member without Telegram (from `vote.changed`
webhooks or rollcall responses). Votes for people with no account at all go
through `proxy-votes` with an `admin` key.

Errors are JSON `{"error": "...", "detail": "..."}`. 401 bad key · 403 wrong
group or scope · 404 no such open rollcall · 409 already voted that way /
duplicate guest · 422 invalid input · 429 rate limited.

**Rate limit:** 60 requests per minute per key (deployment setting). On 429,
wait and retry.

## 3. Webhooks

The admin adds your HTTPS URL under **Integrations → Add webhook** and picks
events. A signing secret (`whsec_…`) is shown once.

| Event | When |
|---|---|
| `rollcall.opened` | a rollcall started — from Telegram, the web, a schedule, or the API |
| `rollcall.closed` | it ended; `"cancelled": true` if it was called off |
| `vote.changed` | a member's entry was written (`in`/`out`/`maybe`/`waitlist`). Also sent for a rename or a new comment, so the same status can repeat — treat it as "this entry is now X" |
| `vote.removed` | a member was taken off the rollcall |
| `dues.posted` | a dues ledger entry was added (charge, payment, adjustment) |
| `ping` | the Test button |

Each delivery is a `POST` with a JSON body:

```json
{
  "id": 1187,
  "type": "vote.changed",
  "chat_id": -1001234567890,
  "created_at": 1764057600,
  "data": {
    "rollcall_id": 42,
    "member": {"user_id": 168415137, "name": "Priya", "username": "priya", "guest": false},
    "status": "in",
    "comment": "bringing a ball"
  }
}
```

Headers: `X-RollCall-Event`, `X-RollCall-Delivery` (same as `id` — use it to
ignore duplicates), and `X-RollCall-Signature: t=<unix seconds>,v1=<hex>`.

**Verify every delivery:** compute `HMAC-SHA256(secret, "<t>.<raw body>")` and
compare with `v1` in constant time; reject if `t` is more than 5 minutes old.

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, sig = int(parts["t"]), parts["v1"]
    if abs(time.time() - t) > 300:
        return False
    want = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)
```

**Delivery:** answer any `2xx` within 5 seconds. Anything else is retried with
backoff (1, 2, 5, 15, 30, 60, 120 minutes), 8 attempts in all; redirects are
not followed. After 20 deliveries in a row give up, the webhook is switched
off and the admin sees why. Order is not guaranteed — use `created_at` and the
rollcall `id`.

## 4. Changes

This is v1. Fields may be added to responses and payloads at any time; nothing
is removed or renamed within v1.
