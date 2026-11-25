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
| 7 | Hand `docs/API.md` to the first partner; watch their webhook deliveries under Integrations. | you | Ready |
| 8 | Old number-based REST paths: **kept, backward compatible — not being removed** (decision 2026-11-25). They answer with `Deprecation`/`Link` headers only. | — | Decided |
| 9 | CAPTCHA on sign-in email, only if the new per-IP / hourly caps prove not enough. | code | Only if needed |

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
