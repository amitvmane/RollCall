# RollCall partner API — v1

For developers connecting another app or service to a RollCall group: read its
rollcalls, take votes, run games, and hear about changes as they happen.

**Interactive reference** (every endpoint, request and response, with *Authorize*
for your key and *Try it out*): [/help/api/reference](/help/api/reference) ·
machine-readable OpenAPI: [/api/v1/partner/openapi.json](/api/v1/partner/openapi.json)

## 1. Access is issued, not signed up for

There is no self-service sign-up. Access works like a bank's partner API: the
group's owner agrees terms with you **off the platform** (what you may do,
for how long, who to call), then issues a key under that agreement.

1. You and the group's owner agree the terms: scopes, lifetime, the addresses
   you'll call from, and a contact on your side.
2. The owner (or an admin, for `read`/`vote`) opens the group's web page →
   **Admin → 🔌 Integrations → Make key**, names your app, picks scopes and,
   under *Agreement & limits*, records your organisation, contact, the
   agreement reference, an optional **IP allowlist** and the lifetime.
3. The key (`rc_…`) is shown **once**. Only its hash is stored — if it's lost,
   the owner rotates or makes a new one. Store it in a secret manager, never in
   a client app, a URL or a repository.

| Scope | Allows |
|---|---|
| `read` | list and read rollcalls |
| `vote` | cast votes for any **current** member of the group (`in` / `out` / `maybe`) |
| `admin` | everything an admin can do: start/end rollcalls, settings, proxy (guest) votes, remove/move members. Only a group **owner** can make these. |

### Key lifecycle

| Stage | What happens |
|---|---|
| **Issued** | Bound to **one group** (every path names its `chat_id`; another chat → 403), with scopes, the agreement details and a lifetime of 1–730 days (default 365). |
| **In use** | Every response to your key carries `X-RollCall-Key-Expires` (UTC). Admins see when and from which address it was last used. |
| **Expiring** | The issuer and the group's owners are reminded **14 and 3 days** before it expires. Monitor the header too. |
| **Rotated** | The owner presses *Rotate*: a new key on the same terms. The old key keeps working for **7 days** (or until it would have expired, if sooner), so you can deploy the new one without downtime. |
| **Revoked / expired** | Refused at once with 401. |
| **Issuer's standing** | A key stays tied to the admin who made it. If they stop being an admin of the group — or delete their account — **every** key they issued stops working (403), whatever its scope. |
| **IP allowlist** | If set, calls from any other address get 403 `This key can't be used from this address`. |

```
Authorization: Bearer rc_…
```

Base URL: `https://<rollcall-host>/api/v1` — HTTPS only.

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
| GET | `/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/lineup` | read — the teams by position (see below) |

**Only the `by-id` paths exist for app keys.** The old `/rollcalls/{rc_number}/…`
paths (a 1-based display number, which shifts when an earlier rollcall ends —
so a call could reach a different rollcall than the one you read) answer an
app key with **410 Gone** and a `Link` to the list. They remain for older
personal tokens only, with `Deprecation: true`, `Sunset: Mon, 01 Mar 2027` and
a `Link` to the `by-id` path, and are **removed on 1 March 2027**. Every
rollcall response carries `X-Rollcall-Id`.

### Vote

```http
POST /api/v1/chats/-1001234567890/rollcalls/by-id/42/votes
Authorization: Bearer rc_…
Idempotency-Key: 7f3c1e0a-vote-priya-42
Content-Type: application/json

{"vote": "in", "user_id": 168415137, "first_name": "Priya", "comment": "bringing a ball"}
```

`user_id` is the member's id as RollCall knows it: a Telegram id for Telegram
members, or the id RollCall gave a member without Telegram (from `vote.changed`
webhooks or rollcall responses). With an app key, the member must currently
belong to the group, and their name is the one the group knows — `first_name`
in your request does not rename anyone. Votes for people with no account at
all go through `proxy-votes` with an `admin` key.

### Lineup

`GET /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/lineup` (scope `read`)
returns who plays where — for a booking or turf screen, a scoring or stats
app, a league table or a stream overlay. It has content when the group has set
a sport and the game has 2, 3 or 4 teams; otherwise `available` is `false`.
`teams` has one entry per team. With 3 or 4 teams the group usually rotates
(two play, the rest wait), so draw each team on its own half-pitch.

```json
{
  "rollcall_id": 42, "available": true, "sport": "football",
  "players_per_side": 7, "locked": true,
  "teams": [{
    "label": "A", "formation": "2-3-1",
    "positions": [
      {"position": "0:0", "role": "GK",  "player": {"user_id": 168415137, "name": "Priya", "guest": false}},
      {"position": "1:0", "role": "DEF", "player": null}
    ],
    "subs": [{"user_id": null, "name": "Ravi K", "guest": true}]
  }]
}
```

- **Football:** `position` is `"<line>:<index>"` — line 0 is the keeper, then
  back to front, index left to right as seen from that team's goal; `formation`
  is the outfield lines (`2-3-1`). `player: null` is an empty spot.
- **Cricket:** no pitch. `formation` is `""`; each entry in `positions` is a
  player with `role` `WK` (wicket-keeper), `BAT`, `AR` (all-rounder) or `BOWL`,
  and `position` is `"<role>:<n>"`, listed WK, BAT, AR, BOWL. `subs` are the
  players who don't have a role yet.
- Treat `locked: false` as provisional — the group can still move people.
  The `lineup.published` webhook tells you when an admin publishes it.

### Your own usage

`GET /chats/{chat_id}/usage?days=7` (any scope) returns what **your key** has
done over the last 1, 7, 30 or 90 days — totals, error rate, rate-limited
calls and average response time, per UTC day and per endpoint — together with
its terms: scopes, expiry, IP allowlist, agreement reference and the rate
limit. Only your own key is ever shown. The group's admins see the same for
every key, plus webhook deliveries, under **Integrations → Usage**.

## 3. Conventions on every call

| | |
|---|---|
| **Errors** | JSON `{"error": "<code>", "detail": "<message>", "request_id": "<id>"}`. `detail` is safe to show a person; branch on the HTTP status and `error`. |
| **Request id** | Every response has `X-Request-Id`. Send your own (1–64 of `A–Z a–z 0–9 . _ : -`) and it is echoed back. Quote it when you contact the group or the operator. |
| **Idempotency** | Send `Idempotency-Key: <1–255 visible ASCII>` on any `POST`/`PUT`/`PATCH`/`DELETE`. A retry with the same key and the same request gets the **first answer** back (with `Idempotent-Replayed: true`) for 24 hours, so a timeout-and-retry never starts a second rollcall. The same key with a different request → 422 `idempotencyKeyReused`; while the first is still running → 409 `idempotencyInProgress`. Server errors (5xx) are not stored, so retrying them really retries. Use a fresh key (a UUID) per intended action. |
| **Rate limit** | 60 requests per minute per key (deployment setting). Every response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds). On 429, wait `Retry-After` seconds. |
| **Times** | Timestamps are UTC: Unix seconds in webhook payloads, ISO 8601 / RFC 3339 elsewhere. |
| **Transport** | HTTPS only. Redirects are never needed — don't follow one to another host with your key. |

| Status | `error` (examples) | Meaning |
|---|---|---|
| 401 | `unauthorized` | missing, wrong, revoked or expired key |
| 403 | `forbidden`, `insufficientPermissions` | wrong group or scope, address not allowed, issuer no longer an admin, not a current member |
| 404 | `notFound`, `rollCallNotStarted` | no such open rollcall |
| 409 | `alreadyInList`, `conflict`, `idempotencyInProgress` | already voted that way, duplicate guest, request still running |
| 422 | `invalidInput`, `incorrectParameter`, `idempotencyKeyReused` | invalid input |
| 429 | `rateLimitExceeded` | slow down |

## 4. Webhooks

The owner adds your HTTPS URL under **Integrations → Add webhook** and picks
events. A signing secret (`whsec_…`) is shown once. Any admin can switch a
webhook off; only an owner can switch it back on.

| Event | When |
|---|---|
| `rollcall.opened` | a rollcall started — from Telegram, the web, a schedule, or the API |
| `rollcall.closed` | it ended; `"cancelled": true` if it was called off |
| `vote.changed` | a member's entry was written (`in`/`out`/`maybe`/`waitlist`). Also sent for a rename or a new comment, so the same status can repeat — treat it as "this entry is now X" |
| `vote.removed` | a member was taken off the rollcall |
| `dues.posted` | a dues ledger entry was added (charge, payment, adjustment) |
| `lineup.published` | an admin published the lineup; `data` is the same object as `GET …/lineup` (without `rollcall_id`, which is on `data` as for every event). Opt in: existing webhooks don't get new event types until the owner ticks them |
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
rollcall `id`. Webhooks are only sent to public HTTPS addresses.

## 5. Your obligations as a partner

- Keep the key server-side, in a secret manager. Never ship it in a mobile or
  browser app, never put it in a URL.
- Call only from the addresses you agreed; tell the owner before they change.
- Rotate when asked, and before `X-RollCall-Key-Expires`.
- Use only the scopes you need; ask for a narrower key rather than share an
  `admin` key across systems.
- Tell the group's owner at once if a key may have leaked — they revoke it on
  the Integrations screen and issue a new one.
- Treat member names and votes as the group's data: use them only for what was
  agreed, and delete them when the agreement ends.

## 6. Versioning and change policy

This is **v1**, in the path. Within v1:

- Fields may be **added** to responses, payloads and error bodies at any time —
  ignore fields you don't know.
- Nothing is **removed or renamed**, and an existing field never changes type
  or meaning.
- New capabilities arrive as new paths, new scopes and new webhook event
  types; a key only ever gains a scope when its owner issues one with it.
- A path being retired is marked first — `Deprecation: true`, a `Sunset` date
  (RFC 8594) at least three months out, and a `Link` to its replacement — and
  is removed on that date. New app keys never get retired paths. An
  incompatible change would be `/v2`, run alongside v1.

### Building on it — e.g. bookings

Future capabilities (such as booking a ground or a slot for a rollcall)
follow the same pattern, so a booking partner integrates the same way:

- a new **scope** (e.g. `booking`), granted per key by the owner under the
  agreement — existing keys don't gain it;
- resources under the same group path, named by permanent ids
  (`/chats/{chat_id}/…/by-id/{id}`), never by shifting display numbers;
- writes accept `Idempotency-Key`, errors use the shape above;
- every state change is also a signed **webhook event** (`<resource>.<verb>`,
  e.g. `booking.confirmed`), delivered with the same retries and signature.

Contact the group's owner to request access; they hold the agreement.
