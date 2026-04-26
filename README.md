# RollCall Bot

A feature-rich Telegram bot for tracking event attendance in group chats. Members can mark themselves as **in**, **out**, or **maybe** for any event — with support for multiple simultaneous roll calls, waitlists, fee splitting, reminders, templates, and more.

[![CI](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml/badge.svg)](https://github.com/amitvmane/RollCall/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.5-green)](rollCall/version.json)

---

## Features

- **Attendance tracking** — in / out / maybe with optional comments
- **Multiple roll calls** — run several events simultaneously in one group
- **Attendance limits & waitlists** — cap attendees; overflow goes on a waitlist
- **Proxy responses** — mark attendance on behalf of another user
- **Event details** — title, date/time, location, and fee with automatic cost splitting
- **Templates** — save and reuse roll call configurations
- **Reminders** — scheduled notifications for upcoming events
- **Statistics** — per-user attendance stats for insights
- **Ghost tracking** — track no-shows and display ghost leaderboard
- **Admin controls** — restrict commands to designated admins
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
# Edit .env and set your API_KEY and other options

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
| `API_KEY` | Yes | Telegram Bot Token from @BotFather |
| `ADMIN1` | No | Telegram user ID of first admin |
| `ADMIN2` | No | Telegram user ID of second admin |
| `DATABASE_URL` | No | PostgreSQL URL (omit to use SQLite) |
| `HEALTH_CHECK_PORT` | No | Health check HTTP port (default: `8080`) |

**SQLite** (default) stores the database at `data/database.db`.  
**PostgreSQL** example: `postgresql://user:password@host:5432/dbname`

---

## Commands

### Core Commands

| Command | Alias | Description |
|---|---|---|
| `/start_roll_call [title]` | `/src` | Start a new roll call with optional title |
| `/end_roll_call` | `/erc` | End the current roll call |
| `/in [comment]` | | Mark yourself as attending |
| `/out [comment]` | | Mark yourself as not attending |
| `/maybe [comment]` | | Mark yourself as undecided |
| `/whos_in` | | List everyone who is in |
| `/whos_out` | | List everyone who is out |

### Roll Call Settings

| Command | Alias | Description |
|---|---|---|
| `/set_title {title}` | `/st` | Set or update the roll call title |
| `/shh` | | Hide the attendance list after each response |
| `/louder` | | Show the attendance list after each response |
| `/set_limit {n}` | | Set a maximum number of attendees (enables waitlist) |

### Event Details

| Command | Description |
|---|---|
| `/location {location}` | Set the event location |
| `/fee {amount}` | Set the event fee (auto-splits among attendees) |
| `/individual_fee` | Show each attendee's share of the fee |
| `/version` | Display the current bot version |

### Proxy Commands (respond on behalf of others)

| Command | Alias | Description |
|---|---|---|
| `/set_in_for {name}` | `/sif` | Mark another user as in |
| `/set_out_for {name}` | `/sof` | Mark another user as out |
| `/set_maybe_for {name}` | `/smf` | Mark another user as maybe |

### Multiple Roll Calls

Append `::{number}` to any command to target a specific roll call when multiple are active:

```
/in::2        → Mark yourself in for roll call #2
/whos_in::1   → Show attendees for roll call #1
```

### Templates

| Command | Description |
|---|---|
| `/save_template {name}` | Save current roll call as a template |
| `/load_template {name}` | Start a new roll call from a saved template |
| `/list_templates` | List all available templates |

### Admin Commands

| Command | Description |
|---|---|
| `/delete_user {name}` | Remove a user from the roll call |
| `/stats` | View attendance statistics |
| `/stats ghost` | View ghost (no-show) leaderboard |
| `/toggle_ghost_tracking` | Enable or disable ghost tracking |
| `/set_absent_limit {n}` | Set threshold for reconfirmation prompts |
| `/mark_absent` | Manually mark a user as absent |
| `/clear_absent {name}` | Clear ghost count for a user |

---

## Architecture

```
RollCall/
├── rollCall/
│   ├── runner.py              # Entry point & async health check server
│   ├── telegram_helper.py     # All Telegram command handlers
│   ├── models.py              # RollCall and User data models
│   ├── rollcall_manager.py    # In-memory cache + DB sync layer
│   ├── db.py                  # Database abstraction (SQLite / PostgreSQL)
│   ├── config.py              # Environment variable parsing
│   ├── functions.py           # Shared helpers (timezone, admin checks)
│   ├── check_reminders.py     # Reminder scheduler
│   └── exceptions.py          # Custom exception types
├── tests/                     # pytest test suite
├── .github/workflows/         # GitHub Actions CI/CD
├── dockerfile
├── docker-compose.yml
└── requirements.txt
```

**Key design decisions:**

- **Manager pattern** — `RollCallManager` provides an in-memory cache per chat with lazy loading from the database, minimising repeated DB queries.
- **Async throughout** — uses `AsyncTeleBot` (pyTelegramBotAPI) with `asyncio` for non-blocking Telegram API calls and the health check server.
- **Dual DB backend** — the same `db.py` layer supports both SQLite (zero-config) and PostgreSQL (production-scale) via a `DATABASE_URL` environment variable.

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

The bot exposes two HTTP endpoints (default port `8080`):

| Endpoint | Response |
|---|---|
| `GET /health` | JSON with bot status and active cache size |
| `GET /ping` | `pong` |

These integrate directly with Docker health checks and container orchestration platforms.

### Production with PostgreSQL

```bash
# Set DATABASE_URL in your .env or environment
DATABASE_URL=postgresql://user:password@db-host:5432/rollcall

docker-compose up -d
```

Database tables are created automatically on first startup.

---

## Changelog

See [version.json](rollCall/version.json) for the full version history.

| Version | Highlights |
|---|---|
| **4.5** | Ghost tracking (no-show detection), reconfirmation prompts, ghost leaderboard *(current)* |
| **4.4** | Removed duplicate check, `deleteuser` supports username, Claude/OpenAI prompt support |
| **4.3** | Major attendance ordering revamp, stats collection |
| **4.2** | Templates for quick roll call creation, end confirmation message |
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
