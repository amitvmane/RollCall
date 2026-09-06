# RollCall — Setup Guide

End-to-end instructions for running your own RollCall instance, from nothing to
a bot responding in a Telegram group. Works with either database backend.

If you just want the short version, the README's
[Quick Start](../README.md#quick-start) is two commands. This guide is for
setting it up properly, and for understanding what you've set up.

**Contents**

- [Prerequisites](#prerequisites)
- [Step 1 — Get a bot token](#step-1--get-a-bot-token)
- [Step 2 — Get the code and configure it](#step-2--get-the-code-and-configure-it)
- [Step 3 — Pick a database](#step-3--pick-a-database)
  - [Option A — Docker + SQLite (recommended)](#option-a--docker--sqlite-recommended)
  - [Option B — Docker + PostgreSQL](#option-b--docker--postgresql)
  - [Option C — Bare Python, no Docker](#option-c--bare-python-no-docker)
- [Step 4 — Add the bot to a group](#step-4--add-the-bot-to-a-group)
- [Step 5 — Verify the install](#step-5--verify-the-install)
- [Step 6 — Set up backups](#step-6--set-up-backups)
- [Optional — web voting and the Mini App](#optional--web-voting-and-the-mini-app)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

---

## Prerequisites

| | Needed for | Notes |
|---|---|---|
| **Docker** 20.10+ with the Compose **v2** plugin | Options A and B | Check with `docker compose version`. The older `docker-compose` (v1, with a hyphen) is not supported — this repo uses profiles. |
| **Python 3.12** | Option C, and running tests | 3.10 and 3.11 run the bot, but the test suite and CI target 3.12. On 3.9 or older it will not start. |
| **`sqlite3`** CLI | SQLite deployments | Used by several `make` targets and by the backup sidecar. Preinstalled on macOS; `apt install sqlite3` on Debian/Ubuntu. |
| **`make`** | All the convenience commands | `apt install build-essential`, or Xcode Command Line Tools on macOS. |
| **A Telegram account** | Everything | To talk to @BotFather and to admin the group. |

You do **not** need a public server, a domain, or HTTPS to run the bot in
Telegram. Those are only for the optional web app.

---

## Step 1 — Get a bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (a display name, then a username
   ending in `bot`).
3. BotFather replies with a token like `123456789:AAF...`. **That token is a
   password for your bot** — anyone holding it controls it. Keep it out of git
   and out of screenshots.
4. Send `/setprivacy` → pick your bot → **Disable**.

That last step is not optional. With privacy mode enabled (the default) the bot
cannot see normal group messages, so commands like `/in` will silently do
nothing.

While you're here, you'll also want your own numeric Telegram user ID for the
super-admin setting. Message [@userinfobot](https://t.me/userinfobot) and it
replies with your ID.

---

## Step 2 — Get the code and configure it

```bash
git clone https://github.com/amitvmane/RollCall.git
cd RollCall
cp .env.example .env
```

Open `.env` and set the two values that matter to start with:

```bash
API_KEY=123456789:AAF...     # the token from BotFather
ADMIN1=987654321             # your numeric Telegram user ID
```

`.env.example` documents every supported variable with its default. Everything
except `API_KEY` is optional — leave a line commented to keep the default.

`.env` is gitignored. Never commit it.

---

## Step 3 — Pick a database

**SQLite** is the default and is the right choice for a single group or a
handful of them. It's one file, no server, and the backup tooling in this repo
is built around it.

**PostgreSQL** is worth it if you're running many groups, want concurrent
access from other tools, or already operate a Postgres instance.

You can switch later — but there is no automatic migration between the two, so
if you have real data, pick deliberately now.

### Option A — Docker + SQLite (recommended)

```bash
make migrate-data     # creates the data directory outside the git tree
make up               # starts the bot + the backup sidecar
make logs             # Ctrl+C once you see "Bot is now running"
```

`make migrate-data` prints an absolute path — pin it in `.env` so the location
never depends on which directory you ran `make` from:

```bash
DATA_DIR=/home/you/rollcall-data
```

**Where your data lives.** The database is at `$DATA_DIR/rollcall.db` on the
host, bind-mounted to `/app/data/rollcall.db` in the container. `DATA_DIR`
defaults to `../rollcall-data` — deliberately *outside* the git working tree,
so no `git checkout`, `git clean -fdx`, or stash can ever touch production
data. `make up` refuses to start if `DATA_DIR` doesn't exist, because Docker
would otherwise silently create it empty and the bot would boot on a blank
database that looks exactly like total data loss.

### Option B — Docker + PostgreSQL

Add to `.env`:

```bash
DATABASE_URL=postgresql://rollcall:rollcall@postgres:5432/rollcall
# optional — override the defaults baked into docker-compose.yml
# POSTGRES_USER=rollcall
# POSTGRES_PASSWORD=use-something-better-than-this
# POSTGRES_DB=rollcall
# POSTGRES_HOST_PORT=5432
```

The hostname is `postgres` — the compose service name, resolved on the compose
network. It is not `localhost`; that would point the container at itself.

```bash
make migrate-data     # still needed: /app/data holds logs and scratch space
make up-postgres      # starts Postgres, waits for it to be healthy, then the bot
make logs
```

Postgres data lives in a Docker named volume (`pgdata`), not in `DATA_DIR`. It
survives `make down` and `docker compose down`, but **not**
`docker compose down -v`.

Postgres is published on `127.0.0.1:5432` only, so a local `psql` or GUI client
works while nothing outside the machine can reach it. If 5432 is already taken,
set `POSTGRES_HOST_PORT=15432`.

Change `POSTGRES_PASSWORD` before doing anything real with this.

> **Backups on Postgres:** the snapshot sidecar in this repo is SQLite-specific
> and no-ops when `DATABASE_URL` points at Postgres — `make backup-check` will
> tell you so rather than raising a false alarm. Use `pg_dump` on a schedule
> instead. Postgres deployments are otherwise fully supported.

### Option C — Bare Python, no Docker

Useful for development and for stepping through code.

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # or requirements.lock for exact pins

cd rollCall
python runner.py
```

The bot must run from inside `rollCall/`. Without `DATABASE_URL` set it creates
`rollcall.db` in the current directory.

`requirements.txt` pins direct dependencies; `requirements.lock` pins every
transitive one and is what the Docker image installs. Use the lock file if you
want to reproduce CI exactly.

---

## Step 4 — Add the bot to a group

1. Create a Telegram group (or use an existing one).
2. Add your bot to it.
3. **Promote the bot to admin.** Group Settings → Administrators → Add. It
   needs Delete Messages and Pin Messages to manage panels properly.
4. In the group, send `/start_rollcall Test Game` (or `/src Test Game`).

If the bot doesn't respond, it's almost always privacy mode — go back to
[Step 1](#step-1--get-a-bot-token).

---

## Step 5 — Verify the install

```bash
make status
```

Healthy output looks like:

```
=== Containers ===
rollcall-bot         Up 2 minutes (healthy)
rollcall-db-backup   Up 2 minutes

=== External Services ===
  Telegram API:     ✅  reachable

=== Bot Health ===
  bot=@YourBot db=ok scheduler=ok prune=ok chats=1 reminder_loops=0

=== Backups ===
  ✅  backup ok — 0h old, 4096 bytes: ../rollcall-data/backups/rollcall-….db.gz
  off-site:  ⚠️   not configured — everything lives on this one disk
```

Then confirm the database is actually being written:

```bash
make db-counts      # works on SQLite or Postgres
```

After starting one rollcall you should see `chats=1` and `rollcalls=1`. If
`chats=0`, the bot isn't seeing your group's messages — privacy mode again.

In Telegram, `/version` and `/help` should both respond.

---

## Step 6 — Set up backups

Skip this and you are one bad day away from losing everything. It takes five
minutes.

**Local snapshots** are already running — the `db-backup` sidecar starts with
`make up`, takes a gzipped snapshot immediately and then every 24 hours, and
prunes local copies after 7 days.

```bash
make backup-list      # what's on disk
make backup-now       # snapshot on demand
```

Tighten the interval if a day of loss is too much (snapshots are tiny):

```bash
echo 'BACKUP_INTERVAL_SECONDS=21600' >> .env    # every 6h
make up
```

**Alarm on stale backups.** The dangerous failure is silent — backups stopping
without anyone noticing. This is not hypothetical: the sidecar died on
2026-08-03 and nobody found out until 2026-08-24.

The `watchdog` sidecar now covers this automatically — `/health` reports
backup freshness, and the watchdog DMs you when it goes stale. See
[Alerting](#alerting) below; if the watchdog is running you do not need the
cron job.

For a belt-and-braces check independent of Docker (or if you have the
watchdog switched off), `make backup-check` exits non-zero when the newest
snapshot is missing, older than 48h, or truncated:

```
0 9 * * * cd /home/you/RollCall && make -s backup-check
```

### Alerting

Everything else in this document makes the bot *recover*. Nothing in it makes
anyone *find out*. `restart: unless-stopped` brings a crashed bot back
silently, and a stale backup or a dead scheduler task deliberately does not
fail the container healthcheck at all — returning 503 there would make Docker
restart a bot that is serving traffic perfectly well. So those conditions had
nothing watching them.

The `watchdog` sidecar closes that gap. It starts automatically with
`make up`, polls the bot's `/health` every 5 minutes from its own container,
and DMs you when the bot stops responding or reports a problem. Because it
talks to the Telegram API directly it still works when the bot process is the
thing that is down — which is the case that matters.

It reports on everything `/health` aggregates: database reachability,
Telegram reachability, scheduler and prune task liveness, connection-pool
saturation, and backup freshness.

Two requirements, and the second one is the one people miss:

1. `ADMIN1` in `.env` must be your **numeric** Telegram user id (or set
   `WATCHDOG_CHAT_ID` to send alerts elsewhere, e.g. a private ops group).
2. That account must have **sent the bot a DM at least once**. Telegram does
   not allow a bot to open a conversation, so an admin who has never messaged
   the bot cannot be alerted — and the failure is silent, which is exactly the
   thing we are trying to eliminate.

Prove the whole path end to end before you rely on it:

```bash
make watchdog-test     # sends a real DM; fails loudly if it cannot reach you
make watchdog-logs     # tail the sidecar
```

`make watchdog-test` is worth running again any time you change `ADMIN1`, the
bot token, or the alert target.

Tuning (all optional, in `.env`):

```bash
WATCHDOG_INTERVAL_SECONDS=300        # poll cadence
WATCHDOG_FAILURES_BEFORE_ALERT=3     # consecutive bad polls before alerting
WATCHDOG_REPEAT_HOURS=12             # re-nag cadence while still broken
```

The defaults mean a routine restart never pages you (three consecutive
failures at 5-minute intervals is ~15 minutes of genuine downtime), while a
real outage is re-announced twice a day rather than mentioned once and
forgotten.

**Sentry (optional).** For full stack traces rather than a one-line health
summary, set `SENTRY_DSN` in `.env` and add `sentry-sdk` to the image. The
two are complementary: Sentry tells you what threw, the watchdog tells you the
bot stopped answering at all.

**Off-site copies.** Everything above still lives on one disk. The
`backup-sync` sidecar copies snapshots to any rclone remote (Backblaze B2, S3,
Google Drive, …) every hour, using `rclone copy` so the remote keeps history
forever even after local pruning.

```bash
mkdir -p ~/.config/rclone
docker run --rm -it -v ~/.config/rclone:/config/rclone \
  rclone/rclone config --config /config/rclone/rclone.conf
# name the remote e.g. "b2"; for B2, 'account' is the keyID, not the key's name
sudo chown -R "$(id -un):" ~/.config/rclone

echo 'RCLONE_REMOTE=b2:rollcall-backups' >> .env
make backup-remote
make backup-remote-ls        # the only real proof it worked
```

**Restoring.** Practise this once before you need it:

```bash
make restore FILE=../rollcall-data/backups/rollcall-20260825-105618.db.gz
make restore-remote          # or: fetch the newest off-site snapshot and restore it
```

Either way the current database is copied aside as `*.pre-restore-<timestamp>`
first, stale WAL files are cleared, and `integrity_check` plus row counts are
printed. Neither command starts the bot — you check the numbers, then `make up`.

---

## Optional — web voting and the Mini App

Not required for Telegram use. Both need the REST API on and a public HTTPS URL
(a Cloudflare Tunnel is the usual way to get one without opening ports).

```bash
REST_API_ENABLED=true
WEB_BASE_URL=https://your-domain.example
MINIAPP_URL=https://your-domain.example/miniapp/
```

See [Deployment](../README.md#deployment) in the README for the full walkthrough
including the tunnel.

---

## Troubleshooting

**The bot doesn't respond to commands in the group.**
Privacy mode. `/setprivacy` → your bot → Disable in @BotFather, then remove and
re-add the bot to the group.

**`make up` says `DATA_DIR does not exist`.**
Working as intended — it's refusing to let Docker create an empty directory and
boot on a blank database. Run `make migrate-data`.

**`attempt to write a readonly database`.**
The container (root, but with all Linux capabilities dropped) can't write the
file. Fix ownership and modes:
```bash
chmod 777 "$DATA_DIR" && chmod 666 "$DATA_DIR/rollcall.db"
```

**`database disk image is malformed`.**
Usually a stale `-wal`/`-shm` pair left beside a database they don't belong to.
Stop the bot, **copy the whole data directory somewhere safe first**, then
restore from a snapshot with `make restore`. If the main database is gone but a
`-wal` survives, `scripts/wal_recover.py` can rebuild a database directly from
its frames.

**Postgres: `could not translate host name "postgres"`.**
Either the Postgres container isn't running (`make up-postgres`, not `make up`),
or `DATABASE_URL` says `localhost` instead of `postgres`.

**`docker compose` reports an unknown flag or ignores profiles.**
You're on Compose v1. Install the v2 plugin; `docker compose version` should
report 2.x.

**Tests fail with hundreds of errors on your machine.**
Wrong Python. The suite targets 3.12; older interpreters produce a flood of
unrelated failures.
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock
pytest
```

**Something imports fine in tests but crashes on boot.**
`conftest.py` mocks telebot, so unit tests can't catch signature mismatches
against the real library. Run the real-import check:
```bash
python scripts/smoke_test.py
```

---

## Uninstalling

```bash
make down                       # stop everything
docker compose down -v          # also delete the Postgres volume, if you used it
```

Your SQLite database and snapshots are in `$DATA_DIR` and are **not** removed
by either command — delete that directory by hand if you really mean it.
