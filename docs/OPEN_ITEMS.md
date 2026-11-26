# RollCall — open items

The one maintained list of open work. Update it in the same commit as the
change that opens or closes an item (CLAUDE.md, "Open items"). Newest status
first; dates are when the status was last confirmed.

Legend: **you** = needs the owner (server, accounts, decisions) · **code** = can be built here.

## Now

| # | Item | Who | Status |
|---|------|-----|--------|
| 1 | **Rotate the production bot token.** It is in public git history (found in the 2026-11-09 security audit) and can forge web logins. Set `IDENTITY_SECRET` first so nobody is signed out, then `/revoke` in BotFather and update `TELEGRAM_TOKEN`. | you | Open — last confirmed still live 2026-11-12 |
| 2 | **Deploy 11.1** — prod runs 11.0 (`f3414fc`, 23 Nov); 11.1 is everything since (email sign-in, web-only groups, merge, join approval, REST ids, partner API with key lifecycle + idempotency, human check, buzz mute, notification emails, Account page, membership/leave/remove, `repeat=` fix). No code left — checklist: **Before/during:** (a) `.env`: `SMTP_HOST`, `SMTP_FROM` (+ `SMTP_USER`/`SMTP_PASSWORD`), and `WEB_BASE_URL` set — without them email sign-in and emails stay off; (b) SPF, DKIM, DMARC on the `SMTP_FROM` domain, or mail lands in spam; (c) set `IDENTITY_SECRET` now (signs nobody out; needed before #1); (d) set `DeployedDatetime` in `version.json` to the real time (now "26-11-2026 IST", no time); (e) deploy with `make build`, not `make up` — schema changes are additive and apply on boot. **After:** (f) compare live `/web/app.js` on rbot.blobsystems.xyz with the repo; (g) one real email sign-in from a phone, and check `/health`. | you | Open — CI green on `7b9c619` |
| 3 | Install the Renovate GitHub app (config already in `renovate.json`). | you | Open |
| 4 | Run the server as non-root: `APP_UID`/`APP_GID=10001` in `.env` → `make fix-perms` → `make build`. | you | Open |
| 5 | Run `make watchdog-test` once on the server, to prove alerts reach you. | you | Open |
| 6 | Test web push on a real iPhone (installed app). | you | Open |

## Next

| # | Item | Who | Status |
|---|------|-----|--------|
| 7 | Hand `docs/API.md` to the first partner under a written agreement: issue their key with the partner, contact, agreement reference and their server IPs filled in (Integrations → *Agreement & limits*); watch "last used … from" and their webhook deliveries there. | you | Ready |
| 8 | Old number-based REST paths (`/rollcalls/{rc_number}/…`): **retiring** (decision 2026-11-26, replacing the 2026-11-25 "keep"). App keys already get 410; everyone else gets `Deprecation` + `Sunset: 01 Mar 2027`. **On 2027-03-01: delete the number registration in `api/rollcall_ref.dual` (and its tests), update docs/API.md §2.** Before that, check `/api_usage 90` for `/rollcalls/{rc_number}` traffic and warn whoever still uses it. | code | Scheduled 2027-03-01 |
| 9 | Optional, stronger human check: create a Cloudflare Turnstile widget, set `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` and `HUMAN_CHECK=turnstile`. The default self-hosted proof of work needs nothing. | you | Optional |

## Later / parked by you

| Item | Status |
|------|--------|
| High availability (multiple live instances) — 3 code blockers: long-poll vs webhook, in-memory session dicts, no scheduler leader election. | Parked 2026-10-20 |
| Standing / advance votes (`/in_next <template>`, one-shot design settled). | Backlog, re-parked 2026-11-23 — don't build or re-pitch |
| Ground suggestions / booking monitoring (admin-driven grounds list first). | Parked 2026-11-17 |
| Late drops: a penalty ("teeth"). Late drops are recorded and shown since 738dfed, deliberately with no charge. | Watch the data first |
| Embeddable voting widget and SDK for other sites. | After #7 |
| Activity feed for web-only groups (Telegram groups see every change in the chat; a web-only group has only push/email per person). | Not built — ask first |
| `_ph()` placeholder helper is copied in ~15 modules; fold into one `db.ph()` when those files are next touched (not worth a churn-only change). | Refactor, low |

## Dropped — don't resurrect

- Discord adapter (dropped 2026-08-17; a future second platform would come in through principals, with app-issued ids).
- Reaction-based voting; `/myvote`; `/ping in|out` split; the old DB-merge cutover.

## Recently done

- 2026-11-26 — Review (security / function / UX / API): every app key now dies with its issuer's admin standing (was admin-scope only); app-key votes only for current members, under the group's name for them; signed-out or deleted email sessions can't be merged; deleting an account revokes the API keys it issued; only owners switch a webhook back on; a multi-line title no longer breaks a notification email, and unsubscribing stops mail already queued. People without Telegram: membership card (join / waiting / not approved), leave a group, admins remove members, admins notified of join requests; removed/left members no longer count as members. Partner API: agreement fields + IP allowlist on keys, rotation with a 7-day overlap, 14/3-day expiry reminders, `/gentoken` tokens listed and revocable, request ids, rate-limit headers, one error shape, `Idempotency-Key`, key-expiry header; `docs/API.md` rewritten (issuance, lifecycle, obligations, versioning, how future booking APIs will look). Onboarding: landing page has a no-Telegram way in, first-visit welcome, sign-in resumes "create group", 🔗 Invite people, web-first "create your own group". `/weblink` and `/gentoken` help updated.

- 2026-11-26 — Email-only people: notification emails (waitlist, reminders, dues, new games; only when web push is off by default; capped, de-duplicated, retried, one-click unsubscribe) and an Account page (change email, sign out everywhere, delete account).
- 2026-11-26 — Changelog split: 11.0 entry restored to what's live; 25–26 Nov work is 11.1.
- 2026-11-26 — `repeat=` (on /src and /repeat) no longer overwrites a same-named template — it used to rewrite the group's own template's location/fee/limit/title and move its schedule. Checked: `/location` only ever changes the ongoing rollcall.
- 2026-11-25 — Human check (CAPTCHA) on guest votes and sign-in emails: self-hosted proof of work by default, Cloudflare Turnstile optional; 12-hour pass capped at 30 actions. API guide served at `/help/api`, linked from `/help` (🔌), the Integrations panel and the bot's `/help` (`/help api`).
- 2026-11-25 — Review fixes: app keys vote for members only (no guest adds, no invented ids); webhooks owner-only; buzz mute counts buzzes, not rollcalls; merge re-checks groups after locking; webhook send tasks kept referenced.

- 2026-11-25 — Partner API: app keys (Integrations; admin scope owners-only; may vote for members), signed webhooks with retries/backoff/auto-off and private-address refusal at connect time, `docs/API.md` v1.
- 2026-11-25 — Merge from an email session (sign in with Telegram on the same device → offered), owner merge tool (email-only accounts whose history is only in that group).
- 2026-11-25 — Sign-in email limits: 10/hour per visitor, 200/hour per deployment, on top of 3 per address per 15 min.
- 2026-11-25 — "API hits": Telegram updates + REST requests per hour (`api_hits`, flushed each minute, 90-day retention); busiest hour on `/health`.
- 2026-11-25 — Buzz mute: members who ignore N buzzes in a row (default 3) are skipped by /buzz, auto-buzz and reminders; any vote brings them back. `/buzz_mute`, `/buzz_unmute`, web settings. Fixed /buzz saying "no known members" when everyone had voted.

- 2026-11-25 — REST rollcalls by permanent id: every rollcall route has a `/rollcalls/by-id/{rollcall_id}/…` twin, re-finds the rollcall by id inside the chat lock; number paths kept, deprecated, same errors as before. `settings/location|fee|when|reminder` now take the chat lock. Ratchet: `tests/test_rest_rollcall_ids.py`.
- 2026-11-25 — Account merge (email-only → Telegram), link-hijack fix, join approval, quiet logs for web-only groups, guest dues alerts (2155733).
- 2026-11-25 — Local id space, email sign-in, link email to Telegram, web-only groups (fc87511).
- 2026-11-25 — Guest web push: waitlist promotion, vote reminders (5502907); vote card waitlist/namesake fixes (44bd535).
- 2026-11-25 — Upgrade rehearsal: a DB built with the pre-11.0 code, opened with today's code — every stat, balance, page and open list identical.
