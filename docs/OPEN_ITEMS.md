# RollCall — open items

The one maintained list of open work. Update it in the same commit as the
change that opens or closes an item (CLAUDE.md, "Open items"). Newest status
first; dates are when the status was last confirmed.

Legend: **you** = needs the owner (server, accounts, decisions) · **code** = can be built here.

## Now

| # | Item | Who | Status |
|---|------|-----|--------|
| 1 | **Rotate the production bot token.** It is in public git history (found in the 2026-11-09 security audit) and can forge web logins. Set `IDENTITY_SECRET` first so nobody is signed out, then `/revoke` in BotFather and update `TELEGRAM_TOKEN`. | you | Open — last confirmed still live 2026-11-12 |
| 2 | **Deploy 11.0** (Teams, guests, email sign-in, web-only groups, merge, join approval, REST ids). Add `SMTP_HOST`/`SMTP_FROM` (+ `SMTP_USER`/`SMTP_PASSWORD`) to `.env`, `make build`, compare live `/web/` assets with the repo, try one real email sign-in, check `/health`. Schema changes are additive and apply on boot. | you | Open — CI green on main |
| 3 | Install the Renovate GitHub app (config already in `renovate.json`). | you | Open |
| 4 | Run the server as non-root: `APP_UID`/`APP_GID=10001` in `.env` → `make fix-perms` → `make build`. | you | Open |
| 5 | Run `make watchdog-test` once on the server, to prove alerts reach you. | you | Open |
| 6 | Test web push on a real iPhone (installed app). | you | Open |

## Next

| # | Item | Who | Status |
|---|------|-----|--------|
| 7 | **Partner API**: per-app keys with scopes, signed outbound webhooks with retries (vote changed, rollcall opened/closed, promoted, dues posted), versioned public docs. Builds on the stable rollcall ids (done 2026-11-25). | code | Not started |
| 8 | **Retire the deprecated number-based REST paths** (`/rollcalls/{rc_number}/…`). They still work and announce `Deprecation: true` + a `Link` to the `by-id` path. Pick a sunset date once partners exist, add a `Sunset` header, then remove them. | code + you (date) | Waiting on #7 |
| 9 | Account merge started from an email session (today it starts only from "add email" on a Telegram account), plus an admin tool to merge two people. | code | Not started |
| 10 | Sign-in email abuse: per-IP cap (and maybe a CAPTCHA) on sending sign-in emails. Today: 3 per address per 15 min + the general per-IP limit. | code | Not started |
| 11 | "API hits" peak stat (Telegram updates + REST requests per hour). Needs a new counter table — ask before adding it. | code | Not started |

## Later / parked by you

| Item | Status |
|------|--------|
| High availability (multiple live instances) — 3 code blockers: long-poll vs webhook, in-memory session dicts, no scheduler leader election. | Parked 2026-10-20 |
| Standing / advance votes (`/in_next <template>`, one-shot design settled). | Backlog, re-parked 2026-11-23 — don't build or re-pitch |
| Ground suggestions / booking monitoring (admin-driven grounds list first). | Parked 2026-11-17 |
| Late drops: a penalty ("teeth"). Late drops are recorded and shown since 738dfed, deliberately with no charge. | Watch the data first |
| Embeddable voting widget and SDK for other sites. | After #7 |

## Dropped — don't resurrect

- Discord adapter (dropped 2026-08-17; a future second platform would come in through principals, with app-issued ids).
- Reaction-based voting; `/myvote`; `/ping in|out` split; the old DB-merge cutover.

## Recently done

- 2026-11-25 — REST rollcalls by permanent id: every rollcall route has a `/rollcalls/by-id/{rollcall_id}/…` twin, re-finds the rollcall by id inside the chat lock; number paths kept, deprecated, same errors as before. `settings/location|fee|when|reminder` now take the chat lock. Ratchet: `tests/test_rest_rollcall_ids.py`.
- 2026-11-25 — Account merge (email-only → Telegram), link-hijack fix, join approval, quiet logs for web-only groups, guest dues alerts (2155733).
- 2026-11-25 — Local id space, email sign-in, link email to Telegram, web-only groups (fc87511).
- 2026-11-25 — Guest web push: waitlist promotion, vote reminders (5502907); vote card waitlist/namesake fixes (44bd535).
- 2026-11-25 — Upgrade rehearsal: a DB built with the pre-11.0 code, opened with today's code — every stat, balance, page and open list identical.
