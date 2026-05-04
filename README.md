# RollCall Bot

A feature-rich Telegram bot for tracking event attendance in group chats. Members can mark themselves as **in**, **out**, or **maybe** for any event — with support for multiple simultaneous roll calls, waitlists, fee splitting, reminders, templates, ghost tracking, and more.

[![CI](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml/badge.svg)](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-6.2-green)](rollCall/version.json)

---

## Features

- **Attendance tracking** — in / out / maybe with optional comments
- **Multiple roll calls** — run up to 3 events simultaneously in one group
- **Attendance limits & waitlists** — cap attendees; overflow goes on a waitlist
- **Proxy responses** — mark attendance on behalf of non-Telegram members (`/sif`, `/sof`, `/smf`)
- **Event details** — title, date/time, location, and fee with automatic per-person cost splitting
- **Templates** — save and reuse roll call configurations
- **Reminders** — scheduled notifications before events; auto-closes at event time
- **Ghost tracking** — record no-shows per user, show leaderboard, prompt for reconfirmation on repeat offenders
- **Buzz** — ping members who haven't voted yet; auto-removes members who have left the group
- **Attendance streaks** — track current and best consecutive-session streaks per user
- **In-place panel editing** — votes update the panel message instead of flooding the chat
- **Statistics** — per-user attendance rate, streaks, IN/OUT/MAYBE counts
- **History** — view the last N ended rollcalls with participant and ghost counts
- **Admin controls** — restrict commands to designated group admins
- **Webhook mode** — opt-in webhook support via `WEBHOOK_URL` env var (falls back to long-polling)
- **Dual database support** — SQLite (default) or PostgreSQL
- **Docker-ready** — ships with a Dockerfile and Docker Compose configuration
- **Health checks** — HTTP `/health` and `/ping` endpoints on port 8080

---

## Quick Start

### Prerequisites

- Python 3.10 or higher
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- The bot must be added as an **admin** in your Telegram group

### Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/amitvmane/RollCall.git
cd RollCall

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
# Edit .env and set your TELEGRAM_TOKEN and other options

# 4. Run the bot
cd rollCall
python runner.py
```

### Docker (Recommended)

```bash
# Build and start with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

---

## Configuration

Copy `.env.example` to `.env` and set the following variables:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` (or `API_KEY`) | Yes | Telegram Bot Token from @BotFather |
| `ADMIN1` | No | Telegram user ID of first super-admin |
| `ADMIN2` | No | Telegram user ID of second super-admin |
| `DATABASE_URL` | No | PostgreSQL URL — omit to use SQLite |
| `WEBHOOK_URL` | No | Public HTTPS URL to enable webhook mode (e.g. `https://mybot.example.com/webhook`) |
| `HEALTH_CHECK_PORT` | No | HTTP port for health endpoints (default: `8080`) |

**SQLite** (default) stores the database at `/app/data/rollcall.db`.  
**PostgreSQL** example: `postgresql://user:password@host:5432/dbname`

**Webhook mode:** set `WEBHOOK_URL` to switch from long-polling to webhook delivery. Leave unset (default) to use long-polling.

---

## Commands

Append `::N` to most commands to target a specific rollcall when multiple are active (e.g. `/in ::2`).

### Core

| Command | Alias | Description |
|---|---|---|
| `/start_roll_call [title]` | `/src` | Start a new roll call |
| `/end_roll_call [::N]` | `/erc` | End rollcall #N |
| `/rollcalls` | `/r` | List all active rollcalls |
| `/panel [::N]` | | Show inline control panel for rollcall #N |
| `/in [comment] [::N]` | | Mark yourself IN |
| `/out [comment] [::N]` | | Mark yourself OUT |
| `/maybe [comment] [::N]` | | Mark yourself MAYBE |

### Lists

| Command | Alias | Description |
|---|---|---|
| `/whos_in [::N]` | `/wi` | Show IN list |
| `/whos_out [::N]` | `/wo` | Show OUT list |
| `/whos_maybe [::N]` | `/wm` | Show MAYBE list |
| `/whos_waiting [::N]` | `/ww` | Show waitlist |

### Event Settings (admin only)

| Command | Alias | Description |
|---|---|---|
| `/set_title title [::N]` | `/st` | Set rollcall title |
| `/set_limit N [::N]` | `/sl` | Set max attendees (enables waitlist) |
| `/set_rollcall_time DD-MM-YYYY H:M [::N]` | `/srt` | Set event date/time (`cancel` to clear) |
| `/set_rollcall_reminder hours [::N]` | `/srr` | Reminder hours before event (`cancel` to clear) |
| `/event_fee amount [::N]` | `/ef` | Set total event fee |
| `/individual_fee [::N]` | `/if` | Show per-person fee split |
| `/location place [::N]` | `/loc` | Set event location |
| `/when [::N]` | `/w` | Show event time |

### Proxy Voting (admin only)

For adding non-Telegram members to a rollcall:

| Command | Alias | Description |
|---|---|---|
| `/set_in_for name [::N]` | `/sif` | Add proxy member as IN |
| `/set_out_for name [::N]` | `/sof` | Add proxy member as OUT |
| `/set_maybe_for name [::N]` | `/smf` | Add proxy member as MAYBE |

### Templates (admin only)

| Command | Description |
|---|---|
| `/set_template name "Title" [limit=N] [location=X] [fee=X] [offset_days=D] [event_day=weekday] [event_time=HH:MM]` | Save a template (`event_day`/`event_time` set when the rollcall auto-closes) |
| `/templates` | List saved templates (shows schedule status) |
| `/start_template name [extra title]` | Start a rollcall from a template |
| `/delete_template name` | Delete a template |
| `/schedule_template name <weekday> <HH:MM>` | Enable auto-start for a template on a recurring weekly schedule (must be before `event_time`) |
| `/schedule_template name off` | Disable auto-start for a template |
| `/schedule_template name` | Show current schedule for a template |

### Ghost Tracking (admin only)

| Command | Description |
|---|---|
| `/toggle_ghost_tracking` | Enable or disable no-show tracking |
| `/set_absent_limit N` | Set no-show threshold for reconfirmation prompts |
| `/absent_stats` | Show ghost leaderboard |
| `/mark_absent` | Manually mark no-shows from a past session |
| `/clear_absent name` | Clear ghost count for a user |

### Admin Tools

| Command | Description |
|---|---|
| `/delete_user name [::N]` | Remove a user (shows confirmation prompt) |
| `/buzz [message] [::N]` | Ping members who haven't voted; pings all known members if no rollcall is active |
| `/set_admins` | Enable admin-only mode (group admins only) |
| `/unset_admins` | Disable admin-only mode |

### Info & Stats

| Command | Alias | Description |
|---|---|---|
| `/stats [name\|@user\|group\|top\|bot]` | `/s` | Attendance rate, streaks, IN/OUT/MAYBE counts |
| `/history [N]` | | Last N ended rollcalls with counts (default 10) |
| `/version` | `/v` | Show bot version |

### Chat Settings

| Command | Description |
|---|---|
| `/shh` | Silent mode — no panel update after each vote |
| `/louder` | Resume full panel output after votes |
| `/timezone Region/City` | Set timezone (e.g. `Asia/Kolkata`) |

### Super Admin

| Command | Description |
|---|---|
| `/broadcast "message"` | Send a message to all chats the bot is in |

---

## Architecture

```
RollCall/
├── rollCall/
│   ├── runner.py              # Entry point, health server, webhook/polling setup
│   ├── telegram_helper.py     # All Telegram command and callback handlers
│   ├── models.py              # RollCall and User data models
│   ├── rollcall_manager.py    # In-memory cache + DB sync layer
│   ├── db.py                  # Database abstraction (SQLite / PostgreSQL)
│   ├── config.py              # Environment variable parsing
│   ├── functions.py           # Shared helpers (timezone, admin checks)
│   ├── check_reminders.py     # Timed reminder and auto-close scheduler
│   ├── version.json           # Version history
│   └── exceptions.py          # Custom exception types
├── tests/                     # pytest test suite (295+ tests)
├── .github/workflows/         # GitHub Actions CI/CD
├── dockerfile
├── docker-compose.yml
└── requirements.txt
```

**Key design decisions:**

- **Manager pattern** — `RollCallManager` provides an in-memory cache per chat with lazy loading from the database, minimising repeated DB queries.
- **Async throughout** — uses `AsyncTeleBot` (pyTelegramBotAPI) with `asyncio` for non-blocking Telegram API calls and the health check server.
- **Dual DB backend** — the same `db.py` layer supports both SQLite (zero-config) and PostgreSQL (production-scale) via a `DATABASE_URL` environment variable.
- **In-place panel editing** — votes via commands edit the existing panel message rather than posting a new one, keeping the chat clean.

---

## Development

### Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Tests mock all external dependencies (Telegram API, database) so they run fully offline.

### Linting

```bash
pip install flake8
flake8 rollCall/ --max-line-length=120
```

### CI/CD

GitHub Actions runs automatically on every push and pull request:

- **Tests** — Python 3.10, 3.11, 3.12
- **Lint** — flake8
- **Docker build** — validates the image builds successfully
- **PR checks** — conventional commit format enforcement and auto-labelling

---

## Deployment

### Health Checks

The bot exposes HTTP endpoints (default port `8080`):

| Endpoint | Response |
|---|---|
| `GET /health` | Bot status, username, and cache size |
| `GET /ping` | `pong` |

These integrate directly with Docker health checks and container orchestration platforms.

### Production with PostgreSQL

```bash
DATABASE_URL=postgresql://user:password@db-host:5432/rollcall docker-compose up -d
```

Database tables are created automatically on first startup.

### Webhook Mode

```bash
# Set WEBHOOK_URL in .env to enable webhook delivery
WEBHOOK_URL=https://mybot.example.com/webhook
```

The bot registers the webhook with Telegram on startup and removes it cleanly on shutdown. Leave `WEBHOOK_URL` unset to use long-polling (default).

---

## Changelog

See [version.json](rollCall/version.json) for the full version history.

| Version | Highlights |
|---|---|
| **6.2** | Bug fixes — bare except cleanup, IN-position reset on re-vote, status validation, per-template auto-start error handling |
| **6.1** | Bug fixes — concurrent /erc lock, proxy delete cleans ghost record, proxy ghost events audit trail |
| **6.0** | Code review hardening — background task exceptions surfaced, buzz timeout, duplicate proxy guard, partial template update, improved renumber message |
| **5.9** | Scheduled templates — weekly auto-start per template via `/schedule_template` |
| **5.8** | `/buzz` rework — DB-persisted member list, concurrent membership check, auto-remove leavers |
| **5.7** | In-place panel editing — votes update the panel instead of flooding the chat |
| **5.6** | `/buzz` command — ping unvoted members or all known members |
| **5.5** | Webhook mode, attendance rate + streaks in `/stats`, `/history`, `/delete_user` confirmation, rate limiting |
| **5.4** | Ghost feature fixes — cache key mismatch, panel-end ghost prompt, ghost selections restored on restart |
| **5.0** | Ghost tracking — no-show recording, reconfirmation prompts, leaderboard |
| **4.6** | Bug fixes and security hardening |
| **4.3** | Attendance ordering revamp, stats collection |
| **4.2** | Templates, end confirmation |
| **4.0** | SQLite / PostgreSQL support, major UI/UX overhaul |
| **3.0** | Docker support |
| **2.2** | Multiple Roll Call (MRC) support |

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository and create a feature branch.
2. Follow [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (enforced by CI).
3. Ensure all tests pass (`pytest tests/ -v`).
4. Open a pull request with a clear description of your changes.

Bug reports and feature requests can be filed via [GitHub Issues](../../issues).

---

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE) for details.
