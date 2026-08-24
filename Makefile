# RollCall — deployment manager
# Run from: ~/RollCallDB/RollCall/
# Usage:    make <target>
#
# The Cloudflare Tunnel is no longer managed by this repo — it now runs as
# part of the blobsystems infra repo (container blobsystems-cloudflared),
# which fronts this bot at the stable domain https://rbot.blobsystems.xyz.
# See ../blobsystems/INFRA_PLAN.md.

COMPOSE  := docker compose
BOT      := rollcall-bot
BACKUP   := db-backup

# Services that must come back up together. `make down` stops everything, so
# any target that starts the bot must also restart the backup sidecar —
# otherwise it stays stopped silently and daily snapshots quietly cease.
SERVICES := $(BOT) $(BACKUP)

# Read a value from .env, stripping surrounding quotes
_env = $(shell grep -m1 '^$(1)=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")

# Host port the health endpoint is published on (matches HEALTH_CHECK_HOST_PORT in .env)
HC_PORT := $(or $(call _env,HEALTH_CHECK_HOST_PORT),8080)

# Public URL for the bot's web app — the stable blobsystems.xyz domain, not an
# auto-detected trycloudflare.com URL. Override via WEB_BASE_URL in .env.
WEB_URL := $(or $(call _env,WEB_BASE_URL),https://rbot.blobsystems.xyz)

# Where the database lives on the HOST. Must match DATA_DIR in docker-compose.yml
# (both read it from .env). Defaults outside the git working tree on purpose —
# see the volumes comment in docker-compose.yml for the incident that motivated
# it. Everything inside the container is still /app/data.
DATA_DIR := $(or $(call _env,DATA_DIR),../rollcall-data)
DB       := $(DATA_DIR)/rollcall.db

.DEFAULT_GOAL := help

BACKUP_DIR  := $(DATA_DIR)/backups
# make backup-check fails if the newest snapshot is older than this. The
# sidecar snapshots every 24h, so 48 gives one missed cycle of slack before
# it complains.
BACKUP_MAX_AGE_HOURS ?= 48

.PHONY: help up down restart build rebuild logs logs-cf status url notify token group-token chats \
        backup-now backup-check backup-list backup-remote backup-remote-logs \
        migrate-data check-data-dir

help: ## Show this help
	@printf "\n\033[1mRollCall — deployment manager\033[0m\n"
	@printf "\n\033[4mLIFECYCLE\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make up"      "Start/recreate bot (tunnel is managed by the blobsystems repo)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make down"    "Stop all containers"
	@printf "  \033[36m%-16s\033[0m %s\n" "make restart" "Restart bot (picks up .env changes)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make build"   "Rebuild bot image and restart"
	@printf "  \033[36m%-16s\033[0m %s\n" "make rebuild" "Clean start: down, rebuild image, up (use after git pull)"
	@printf "\n\033[4mOBSERVABILITY\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs"    "Tail bot logs (Ctrl+C to stop)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs-cf" "Tail Cloudflare tunnel logs (blobsystems-cloudflared container)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make status"  "Container status + external service reachability"
	@printf "  \033[36m%-16s\033[0m %s\n" "make url"     "Public URL and all group voting links"
	@printf "  \033[36m%-16s\033[0m %s\n" "make notify"  "Send all voting links to Telegram admin"
	@printf "  \033[36m%-16s\033[0m %s\n" "make chats"   "List all known groups with their chat IDs"
	@printf "\n\033[4mBACKUPS\033[0m\n"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-now"    "Take a snapshot right now"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-list"   "List local snapshots, newest last"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-check"  "Alarm if newest snapshot is stale (exit 1) — put this in cron"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote" "Start off-site rclone sync (needs RCLONE_REMOTE in .env)"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-logs" "Tail the off-site sync sidecar"
	@printf "  \033[36m%-20s\033[0m %s\n" "make migrate-data"  "Move the DB out of the git tree into DATA_DIR"
	@printf "    Options:\n"
	@printf "      BACKUP_MAX_AGE_HOURS=N   staleness limit for backup-check  (default: 48)\n"
	@printf "    Current DATA_DIR: $(DATA_DIR)\n"
	@printf "\n\033[4mTOKENS\033[0m\n"
	@printf "  \033[36mmake token\033[0m\n"
	@printf "    Issue a \033[1mglobal\033[0m admin token (chat-id 0, all scopes — works across all groups)\n"
	@printf "    Options:\n"
	@printf "      LABEL=\"...\"   Friendly name shown in token listings  (default: \"Admin dashboard\")\n"
	@printf "      DAYS=N        Expire after N days                     (default: never)\n"
	@printf "    Examples:\n"
	@printf "      make token\n"
	@printf "      make token LABEL=\"Dashboard\" DAYS=90\n"
	@printf "\n"
	@printf "  \033[36mmake group-token\033[0m\n"
	@printf "    Issue a token scoped to \033[1mone specific group\033[0m only\n"
	@printf "    Run \033[36mmake chats\033[0m first to find the chat ID of your group\n"
	@printf "    Options:\n"
	@printf "      CHAT=<chat_id>     Required: Telegram chat ID (negative number, e.g. -1001234567890)\n"
	@printf "      SCOPES=read,vote   Comma-separated scopes: read, vote, admin  (default: read,vote)\n"
	@printf "      LABEL=\"...\"        Friendly name\n"
	@printf "      DAYS=N             Expire after N days  (default: never)\n"
	@printf "    Examples:\n"
	@printf "      make group-token CHAT=-1001234567890\n"
	@printf "      make group-token CHAT=-1001234567890 SCOPES=read,vote LABEL=\"Webapp\" DAYS=30\n"
	@printf "\n"

# Docker creates a missing bind-mount source as an empty root-owned directory
# rather than failing, so a wrong or unmigrated DATA_DIR would start the bot on
# a blank database and look identical to catastrophic data loss. Refuse instead.
check-data-dir:
	@if [ ! -d "$(DATA_DIR)" ]; then \
	  echo "❌  DATA_DIR does not exist: $(DATA_DIR)"; \
	  echo "    Docker would create it empty and the bot would boot on a blank DB."; \
	  echo "    Run: make migrate-data"; \
	  exit 1; \
	fi
	@if [ ! -f "$(DB)" ] && [ -f ./data/rollcall.db ]; then \
	  echo "❌  $(DB) is missing, but ./data/rollcall.db still exists."; \
	  echo "    Your database has not been migrated out of the git tree yet."; \
	  echo "    Run: make migrate-data"; \
	  exit 1; \
	fi

migrate-data: ## Move the database out of the git tree into DATA_DIR (idempotent)
	@echo "DATA_DIR = $(DATA_DIR)"
	@mkdir -p "$(DATA_DIR)/backups"
	@if [ -f "$(DB)" ]; then \
	  echo "✅  already migrated — $(DB) exists ($$(stat -c %s "$(DB)" 2>/dev/null || stat -f %z "$(DB)") bytes)"; \
	elif [ -f ./data/rollcall.db ]; then \
	  echo "Moving ./data/ → $(DATA_DIR)/ (bot must be stopped)"; \
	  $(COMPOSE) stop $(SERVICES) 2>/dev/null || true; \
	  cp -a ./data/rollcall.db "$(DB)"; \
	  [ -d ./data/backups ] && cp -an ./data/backups/. "$(DATA_DIR)/backups/" 2>/dev/null || true; \
	  rm -f ./data/rollcall.db-wal ./data/rollcall.db-shm; \
	  echo "✅  copied. The originals are left in ./data/ — delete them once you've"; \
	  echo "    confirmed the bot is healthy on the new location."; \
	else \
	  echo "✅  fresh install — created empty $(DATA_DIR), bot will initialise the DB"; \
	fi
	@chmod 777 "$(DATA_DIR)" 2>/dev/null || true
	@[ -f "$(DB)" ] && chmod 666 "$(DB)" || true
	@echo ""
	@echo "Pin it explicitly in .env so it never depends on the working directory:"
	@echo "    DATA_DIR=$$(cd "$(DATA_DIR)" && pwd)"

up: check-data-dir ## Start/recreate bot + backup sidecar (tunnel is managed by the blobsystems repo)
	@echo "Starting bot and backup sidecar..."
	@$(COMPOSE) up -d --force-recreate $(SERVICES)
	@echo ""
	@$(MAKE) -s url

down: ## Stop all containers
	$(COMPOSE) down

restart: ## Restart bot (picks up .env changes)
	$(COMPOSE) restart $(BOT)
	@echo "Bot restarted"

build: check-data-dir ## Rebuild bot image and restart
	$(COMPOSE) up -d --build $(SERVICES)

rebuild: ## Clean start: stop everything, rebuild image from current code, start bot
	@echo "Stopping containers..."
	@$(COMPOSE) down
	@echo "Rebuilding bot image from current code..."
	@$(COMPOSE) build $(BOT)
	@$(MAKE) up

logs: ## Tail bot logs (Ctrl+C to stop)
	docker compose logs -f $(BOT)

logs-cf: ## Tail Cloudflare tunnel logs (blobsystems-cloudflared container)
	docker logs -f blobsystems-cloudflared

status: ## Show container status + external service reachability
	@echo ""
	@echo "=== Containers ==="
	@$(COMPOSE) ps
	@echo ""
	@echo "=== External Services ==="
	@printf "  Telegram API:     "; \
	curl -sf --max-time 5 https://api.telegram.org > /dev/null 2>&1 \
	  && echo "✅  reachable" \
	  || echo "❌  unreachable (ISP ban or outage)"
	@printf "  Cloudflare:       "; \
	curl -sf --max-time 5 https://www.cloudflare.com > /dev/null 2>&1 \
	  && echo "✅  reachable" \
	  || echo "❌  unreachable"
	@printf "  Tunnel endpoint:  "; \
	curl -sf --max-time 8 "$(WEB_URL)/api/v1/health" > /dev/null 2>&1 \
	  && echo "✅  $(WEB_URL)" \
	  || echo "❌  $(WEB_URL) (unreachable — check blobsystems-cloudflared and blobsystems-nginx on the other repo)"
	@echo ""
	@echo "=== Bot Health ==="
	@HEALTH=$$(curl -sf --max-time 5 http://localhost:$(HC_PORT)/health 2>/dev/null); \
	if [ -z "$$HEALTH" ]; then \
	  echo "  ❌  health endpoint not responding (bot down?)"; \
	else \
	  echo "  $$HEALTH" | fold -s -w 100; \
	fi
	@echo ""
	@echo "=== Backups ==="
	@printf "  "; $(MAKE) -s backup-check || true
	@echo ""

backup-now: ## Take a snapshot immediately (does not wait for the 24h cycle)
	@$(COMPOSE) exec $(BACKUP) /app/scripts/backup_db.sh

backup-list: ## List local snapshots, newest last
	@ls -ltr $(BACKUP_DIR)/rollcall-*.db.gz 2>/dev/null || echo "no snapshots in $(BACKUP_DIR)"

# Exits non-zero when the newest snapshot is stale or missing, so cron mails
# you / a monitor alerts. The silent failure this guards against is real: the
# db-backup sidecar sat stopped from 2026-08-03 to 2026-08-24 without emitting
# a single signal, and was three weeks stale at the moment it was needed.
backup-check: ## Verify a recent snapshot exists (exit 1 if stale) — run from cron
	@newest=$$(ls -t $(BACKUP_DIR)/rollcall-*.db.gz 2>/dev/null | head -1); \
	if [ -z "$$newest" ]; then \
	  echo "❌  BACKUP MISSING — no snapshots at all in $(BACKUP_DIR)"; \
	  $(COMPOSE) ps $(BACKUP); \
	  exit 1; \
	fi; \
	mtime=$$(stat -c %Y "$$newest" 2>/dev/null || stat -f %m "$$newest"); \
	size=$$(stat -c %s "$$newest" 2>/dev/null || stat -f %z "$$newest"); \
	age_h=$$(( ( $$(date +%s) - $$mtime ) / 3600 )); \
	if [ "$$size" -lt 1024 ]; then \
	  echo "❌  BACKUP CORRUPT — newest snapshot is only $$size bytes: $$newest"; \
	  exit 1; \
	fi; \
	if [ "$$age_h" -ge "$(BACKUP_MAX_AGE_HOURS)" ]; then \
	  echo "❌  BACKUP STALE — newest is $${age_h}h old (limit $(BACKUP_MAX_AGE_HOURS)h): $$newest"; \
	  echo "    the db-backup sidecar is probably not running:"; \
	  $(COMPOSE) ps $(BACKUP); \
	  exit 1; \
	fi; \
	echo "✅  backup ok — $${age_h}h old, $$size bytes: $$newest"

backup-remote: ## Start the off-site rclone sync sidecar (needs RCLONE_REMOTE in .env)
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; \
	  echo "    1. rclone config                       (create a remote on the host)"; \
	  echo "    2. echo 'RCLONE_REMOTE=gdrive:rollcall-backups' >> .env"; \
	  echo "    3. make backup-remote"; \
	  exit 1; \
	fi
	@echo "Starting off-site sync → $(call _env,RCLONE_REMOTE)"
	@$(COMPOSE) --profile backup-remote up -d backup-sync
	@echo "Verify with: make backup-remote-logs"

backup-remote-logs: ## Tail the off-site sync sidecar
	@docker logs -f rollcall-backup-sync

url: ## Show public URL and all group voting links
	@URL="$(WEB_URL)"; \
	echo "Public URL: $$URL"; \
	echo "API docs:   $$URL/api/docs"; \
	echo ""; \
	echo "Group voting links:"; \
	sqlite3 $(DB) \
	  "SELECT chat_id, group_web_token FROM chats WHERE group_web_token IS NOT NULL;" \
	  2>/dev/null | \
	while IFS='|' read -r cid tok; do \
	  printf "  Chat %-22s %s\n" "$$cid:" "$$URL/web/group/$$tok"; \
	done; \
	echo ""

token: ## Issue a global admin API token. Usage: make token [LABEL="my label"] [DAYS=90]
	@LABEL=$${LABEL:-"Admin dashboard"}; \
	EXTRA=""; \
	if [ -n "$$DAYS" ]; then EXTRA="--expires-days $$DAYS"; fi; \
	docker exec $(BOT) python /app/scripts/issue_api_token.py \
	  --chat-id 0 \
	  --scopes read,vote,admin \
	  --label "$$LABEL" $$EXTRA

group-token: ## Issue a token scoped to one group. Usage: make group-token CHAT=<chat_id> [SCOPES=read,vote] [LABEL="..."] [DAYS=N]
	@if [ -z "$(CHAT)" ]; then \
	  echo "ERROR: CHAT is required. Run 'make chats' to list group IDs."; \
	  echo "Usage: make group-token CHAT=-1001234567890 [SCOPES=read,vote] [LABEL=\"...\"] [DAYS=N]"; \
	  exit 1; \
	fi; \
	SCOPES_VAL=$${SCOPES:-"read,vote"}; \
	LABEL_VAL=$${LABEL:-""}; \
	EXTRA=""; \
	if [ -n "$$DAYS" ]; then EXTRA="--expires-days $$DAYS"; fi; \
	LABEL_ARG=""; \
	if [ -n "$$LABEL_VAL" ]; then LABEL_ARG="--label \"$$LABEL_VAL\""; fi; \
	docker exec $(BOT) python /app/scripts/issue_api_token.py \
	  --chat-id $(CHAT) \
	  --scopes "$$SCOPES_VAL" \
	  $$LABEL_ARG $$EXTRA

chats: ## List all known groups with chat IDs (use these with make group-token)
	@echo ""
	@echo "Known groups:"
	@sqlite3 $(DB) \
	  "SELECT chat_id, COALESCE(group_name, '(no name)') FROM chats ORDER BY group_name;" \
	  2>/dev/null | \
	while IFS='|' read -r cid name; do \
	  printf "  %-30s %s\n" "$$name" "$$cid"; \
	done
	@echo ""

notify: ## Send all voting links to Telegram admin (safe to run when banned — prints links if unreachable)
	@URL="$(WEB_URL)"; \
	API_KEY=$$(grep -m1 '^API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'"); \
	ADMIN1=$$(grep -m1 '^ADMIN1=' .env | cut -d= -f2- | tr -d '"' | tr -d "'"); \
	LINKS=$$(sqlite3 $(DB) \
	  "SELECT chat_id, group_web_token FROM chats WHERE group_web_token IS NOT NULL;" \
	  2>/dev/null | \
	  while IFS='|' read -r cid tok; do \
	    echo "Chat $$cid: $$URL/web/group/$$tok"; \
	  done); \
	echo "Voting links:"; \
	echo "$$LINKS"; \
	echo ""; \
	curl -sf --max-time 10 \
	  "https://api.telegram.org/bot$$API_KEY/sendMessage" \
	  -d "chat_id=$$ADMIN1" \
	  --data-urlencode "text=🔗 Web voting links:%0A%0A$$LINKS%0A%0AOpen your group link to vote." > /dev/null \
	  && echo "Sent to Telegram admin ($$ADMIN1)" \
	  || echo "(Telegram unreachable — share the links above manually)"
