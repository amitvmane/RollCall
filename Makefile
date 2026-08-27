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
# Host dir holding rclone.conf, mounted read-only into the sync sidecar.
RCLONE_CONFIG_PATH := $(or $(call _env,RCLONE_CONFIG_PATH),$(HOME)/.config/rclone)
DB       := $(DATA_DIR)/rollcall.db

# Postgres deployments have no SQLite file, so the snapshot sidecar no-ops and
# the local-backup targets have nothing to check. Detect it once here so those
# targets can say "not applicable" instead of raising a false alarm.
IS_POSTGRES := $(shell grep -q '^DATABASE_URL=postgres' .env 2>/dev/null && echo yes)

.DEFAULT_GOAL := help

BACKUP_DIR  := $(DATA_DIR)/backups
# make backup-check fails if the newest snapshot is older than this. The
# sidecar snapshots every 24h, so 48 gives one missed cycle of slack before
# it complains.
BACKUP_MAX_AGE_HOURS ?= 48

.PHONY: help up down restart build rebuild logs logs-cf status url notify token group-token chats \
        backup-now backup-check backup-list backup-remote backup-remote-logs backup-remote-ls \
        migrate-data check-data-dir restore backup-remote-get backup-remote-latest restore-remote \
        up-postgres db-shell db-counts backup-remote-prune backup-remote-size

help: ## Show this help
	@printf "\n\033[1mRollCall — deployment manager\033[0m\n"
	@printf "\n\033[4mLIFECYCLE\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make up"      "Start/recreate bot (tunnel is managed by the blobsystems repo)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make down"    "Stop all containers"
	@printf "  \033[36m%-16s\033[0m %s\n" "make restart" "Restart bot (picks up .env changes)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make build"   "Rebuild bot image and restart"
	@printf "  \033[36m%-16s\033[0m %s\n" "make rebuild" "Clean start: down, rebuild image, up (use after git pull)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make up-postgres" "Start PostgreSQL, wait for it, then start the bot"
	@printf "\n\033[4mOBSERVABILITY\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs"    "Tail bot logs (Ctrl+C to stop)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs-cf" "Tail Cloudflare tunnel logs (blobsystems-cloudflared container)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make status"  "Container status + external service reachability"
	@printf "  \033[36m%-16s\033[0m %s\n" "make url"     "Public URL and all group voting links"
	@printf "  \033[36m%-16s\033[0m %s\n" "make notify"  "Send all voting links to Telegram admin"
	@printf "  \033[36m%-16s\033[0m %s\n" "make chats"   "List all known groups with their chat IDs"
	@printf "  \033[36m%-16s\033[0m %s\n" "make db-counts" "Row counts for the main tables (works on SQLite or Postgres)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make db-shell" "Open a SQL shell on whichever DB this deployment uses"
	@printf "\n\033[4mBACKUPS\033[0m\n"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-now"    "Take a snapshot right now"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-list"   "List local snapshots, newest last"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-check"  "Alarm if newest snapshot is stale (exit 1) — put this in cron"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote" "Start off-site rclone sync (needs RCLONE_REMOTE in .env)"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-logs" "Tail the off-site sync sidecar"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-ls" "List what's actually on the remote (proof it works)"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-get" "Download one snapshot from the remote  FILE=<name>"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-latest" "Print the newest snapshot name on the remote"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-size" "How much space the off-site backups use"
	@printf "  \033[36m%-20s\033[0m %s\n" "make backup-remote-prune" "Delete remote snapshots older than REMOTE_RETENTION_DAYS"
	@printf "  \033[36m%-20s\033[0m %s\n" "make restore"       "Restore a snapshot  FILE=<path>  (stops the bot first)"
	@printf "  \033[36m%-20s\033[0m %s\n" "make restore-remote" "Fetch the NEWEST off-site snapshot and restore it"
	@printf "  \033[36m%-20s\033[0m %s\n" "make migrate-data"  "Move the DB out of the git tree into DATA_DIR"
	@printf "    Options:\n"
	@printf "      BACKUP_MAX_AGE_HOURS=N   staleness limit for backup-check  (default: 48)\n"
	@printf "      REMOTE_RETENTION_DAYS=N  age cutoff for backup-remote-prune  (default: 90)\n"
	@printf "      YES=1                    skip the confirm/dry-run on prune and restore-remote\n"
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

up-postgres: ## Start PostgreSQL, wait for it, then start the bot
	@if ! grep -q '^DATABASE_URL=postgres' .env 2>/dev/null; then \
	  echo "⚠️   .env has no postgres DATABASE_URL — the bot will still use SQLite."; \
	  echo "    Add this line to .env first:"; \
	  echo "    DATABASE_URL=postgresql://rollcall:rollcall@postgres:5432/rollcall"; \
	  echo ""; \
	fi
	@$(COMPOSE) --profile postgres up -d postgres
	@printf "Waiting for Postgres to accept connections"
	@for i in $$(seq 1 30); do \
	  if [ "$$(docker inspect -f '{{.State.Health.Status}}' rollcall-postgres 2>/dev/null)" = "healthy" ]; then \
	    echo " ✅"; break; \
	  fi; \
	  printf "."; sleep 2; \
	  if [ "$$i" = "30" ]; then echo " ❌ timed out"; docker logs --tail 20 rollcall-postgres; exit 1; fi; \
	done
	@$(MAKE) -s up

up: check-data-dir ## Start/recreate bot + backup sidecars (tunnel is managed by the blobsystems repo)
	@echo "Starting bot and backup sidecar..."
	@$(COMPOSE) up -d --force-recreate $(SERVICES)
	# backup-sync sits behind the backup-remote profile, so a plain `up` skips
	# it while `down` still stops it — the same silent-death trap that left
	# db-backup stopped for three weeks. If the remote is configured, the
	# sidecar is not optional; bring it back every time.
	@if [ -n "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "Starting off-site sync → $(call _env,RCLONE_REMOTE)"; \
	  $(COMPOSE) --profile backup-remote up -d --no-deps backup-sync; \
	fi
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
	@printf "  off-site:  "; \
	if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "⚠️   not configured — everything lives on this one disk (make backup-remote)"; \
	elif [ -n "$$(docker ps -q -f name=rollcall-backup-sync -f status=running)" ]; then \
	  echo "✅  syncing to $(call _env,RCLONE_REMOTE)  (verify: make backup-remote-ls)"; \
	else \
	  echo "❌  RCLONE_REMOTE is set but rollcall-backup-sync is NOT running — run: make up"; \
	fi
	@echo ""

db-shell: ## Open a SQL shell on whichever database this deployment uses
	@if [ -n "$(IS_POSTGRES)" ]; then \
	  $(COMPOSE) --profile postgres exec postgres \
	    psql -U "$(or $(call _env,POSTGRES_USER),rollcall)" -d "$(or $(call _env,POSTGRES_DB),rollcall)"; \
	else \
	  echo "SQLite: $(DB)"; sqlite3 "$(DB)"; \
	fi

db-counts: ## Row counts for the main tables — quick "is my setup working" check
	@if [ -n "$(IS_POSTGRES)" ]; then \
	  $(COMPOSE) --profile postgres exec -T postgres \
	    psql -U "$(or $(call _env,POSTGRES_USER),rollcall)" -d "$(or $(call _env,POSTGRES_DB),rollcall)" -c \
	    "select 'users' t, count(*) from users union all select 'rollcalls', count(*) from rollcalls union all select 'chats', count(*) from chats union all select 'dues_entries', count(*) from dues_entries union all select 'fund_transactions', count(*) from fund_transactions;"; \
	else \
	  sqlite3 "$(DB)" \
	    'select "users", count(*) from users union all select "rollcalls", count(*) from rollcalls union all select "chats", count(*) from chats union all select "dues_entries", count(*) from dues_entries union all select "fund_transactions", count(*) from fund_transactions;'; \
	fi

backup-now: ## Take a snapshot immediately (does not wait for the 24h cycle)
	@$(COMPOSE) exec $(BACKUP) /app/scripts/backup_db.sh

backup-list: ## List local snapshots, newest last
	@ls -ltr $(BACKUP_DIR)/rollcall-*.db.gz 2>/dev/null || echo "no snapshots in $(BACKUP_DIR)"

# Exits non-zero when the newest snapshot is stale or missing, so cron mails
# you / a monitor alerts. The silent failure this guards against is real: the
# db-backup sidecar sat stopped from 2026-08-03 to 2026-08-24 without emitting
# a single signal, and was three weeks stale at the moment it was needed.
backup-check: ## Verify a recent snapshot exists (exit 1 if stale) — run from cron
	# One shell for the whole check: `exit` in a make recipe only ends that
	# line's shell, so an early return has to live in the same invocation as
	# everything it is short-circuiting.
	@if [ -n "$(IS_POSTGRES)" ]; then \
	  echo "ℹ️   Postgres deployment — SQLite snapshots not applicable (use pg_dump)"; \
	  exit 0; \
	fi; \
	newest=$$(ls -t $(BACKUP_DIR)/rollcall-*.db.gz 2>/dev/null | head -1); \
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

# Remote retention is OFF by default and stays that way: the sidecar uses
# `rclone copy`, never `sync`, precisely so local 7-day pruning can't reach the
# off-site history. This target is the deliberate, manual exception.
#
# Safe to delete old snapshots because each one is a FULL database image, not
# an increment — the newest snapshot already contains the entire append-only
# dues/fund ledger. Retention limits how far back you can rewind, not what data
# you hold. Dry-run unless YES=1.
REMOTE_RETENTION_DAYS ?= 90

backup-remote-prune: ## Delete remote snapshots older than REMOTE_RETENTION_DAYS (dry-run unless YES=1)
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; exit 1; fi
	@remaining=$$(docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	   lsf --files-only --include 'rollcall-*.db.gz' --max-age $(REMOTE_RETENTION_DAYS)d \
	   "$(call _env,RCLONE_REMOTE)" --config /config/rclone/rclone.conf 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$remaining" = "0" ]; then \
	  echo "❌  refusing to prune — nothing on the remote is NEWER than $(REMOTE_RETENTION_DAYS)d."; \
	  echo "    That would delete every off-site copy you have."; \
	  exit 1; \
	fi; \
	echo "$$remaining snapshot(s) newer than $(REMOTE_RETENTION_DAYS)d will be kept."
	@if [ -z "$(YES)" ]; then \
	  echo "DRY RUN — would delete (re-run with YES=1 to apply):"; \
	  docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	    delete --dry-run --min-age $(REMOTE_RETENTION_DAYS)d --include 'rollcall-*.db.gz' \
	    "$(call _env,RCLONE_REMOTE)" --config /config/rclone/rclone.conf; \
	else \
	  docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	    delete --min-age $(REMOTE_RETENTION_DAYS)d --include 'rollcall-*.db.gz' \
	    "$(call _env,RCLONE_REMOTE)" --config /config/rclone/rclone.conf; \
	  echo "✅  pruned. Remaining:"; $(MAKE) -s backup-remote-ls; \
	fi

backup-remote-size: ## How much space the off-site backups use
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; exit 1; fi
	@docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	  size "$(call _env,RCLONE_REMOTE)" --config /config/rclone/rclone.conf

backup-remote: ## Start the off-site rclone sync sidecar (needs RCLONE_REMOTE in .env)
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; \
	  echo "    1. mkdir -p $(RCLONE_CONFIG_PATH)"; \
	  echo "    2. docker run --rm -it -v $(RCLONE_CONFIG_PATH):/config/rclone \\"; \
	  echo "         rclone/rclone config --config /config/rclone/rclone.conf"; \
	  echo "       (the remote's NAME is what goes before the colon below —"; \
	  echo "        name it 'b2', not 'storage b2')"; \
	  echo "       (for B2, 'account' is the keyID — the ~25-char string, NOT"; \
	  echo "        the friendly name you gave the key. Wrong value => 401)"; \
	  echo "    3. sudo chown -R \$$(id -un): $(RCLONE_CONFIG_PATH)   # docker wrote it as root"; \
	  echo "    4. echo 'RCLONE_REMOTE=b2:rollcall-backups' >> .env"; \
	  echo "    5. make backup-remote"; \
	  exit 1; \
	fi
	@echo "Starting off-site sync → $(call _env,RCLONE_REMOTE)"
	# --no-deps: backup-sync depends_on db-backup, which in turn depends_on the
	# bot, so without this compose recreates the running bot — a pointless
	# production restart just to start a backup sidecar. `make up` is what owns
	# starting those two.
	@$(COMPOSE) --profile backup-remote up -d --no-deps backup-sync
	@echo "Verify with: make backup-remote-logs"

backup-remote-logs: ## Tail the off-site sync sidecar
	@docker logs -f rollcall-backup-sync

# Snapshot names are rollcall-YYYYmmdd-HHMMSS.db.gz in UTC, so a plain
# lexicographic sort is a chronological sort. That's deliberately preferred
# over the remote's modification times, which reflect when rclone uploaded a
# file rather than when the snapshot was actually taken.
_remote_latest = docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	  lsf --files-only --include 'rollcall-*.db.gz' "$(call _env,RCLONE_REMOTE)" \
	  --config /config/rclone/rclone.conf 2>/dev/null | sort | tail -1

backup-remote-latest: ## Print the newest snapshot name on the remote
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; exit 1; fi
	@newest=$$($(_remote_latest)); \
	if [ -z "$$newest" ]; then \
	  echo "❌  no rollcall-*.db.gz found on $(call _env,RCLONE_REMOTE)"; exit 1; \
	fi; \
	echo "$$newest"

restore-remote: ## Download the NEWEST off-site snapshot and restore it (YES=1 to skip prompt)
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env"; exit 1; fi
	@newest=$$($(_remote_latest)); \
	if [ -z "$$newest" ]; then \
	  echo "❌  no rollcall-*.db.gz found on $(call _env,RCLONE_REMOTE)"; exit 1; \
	fi; \
	echo "Newest off-site snapshot: $$newest"; \
	if [ -z "$(YES)" ]; then \
	  printf "Restore it over %s? [y/N] " "$(DB)"; \
	  read ans; case "$$ans" in y|Y|yes|YES) ;; *) echo "aborted"; exit 1 ;; esac; \
	fi; \
	$(MAKE) -s backup-remote-get FILE="$$newest" DEST="$(BACKUP_DIR)" && \
	$(MAKE) -s restore FILE="$(BACKUP_DIR)/$$newest" YES=1

restore: ## Restore a snapshot: make restore FILE=<path-to-.db.gz>
	@if [ -z "$(FILE)" ]; then \
	  echo "❌  usage: make restore FILE=<snapshot>"; \
	  echo ""; \
	  echo "Available local snapshots:"; \
	  $(MAKE) -s backup-list; \
	  echo ""; \
	  echo "To restore one from off-site first: make backup-remote-get FILE=<name>"; \
	  exit 1; \
	fi
	@if [ ! -f "$(FILE)" ]; then echo "❌  no such file: $(FILE)"; exit 1; fi
	@echo "Restoring $(FILE) → $(DB)"
	@echo "(the current database is copied to *.pre-restore-* first)"
	@$(COMPOSE) stop $(SERVICES)
	@DATA_DIR="$(DATA_DIR)" ./scripts/restore_db.sh "$(FILE)"
	@echo ""
	@printf "integrity_check: "; sqlite3 "$(DB)" 'PRAGMA integrity_check;'
	@sqlite3 "$(DB)" 'select "  users="||count(*) from users;' 2>/dev/null || true
	@echo ""
	@echo "If that says 'ok' and the counts look right:  make up"

backup-remote-get: ## Download one snapshot from the remote: make backup-remote-get FILE=<name> [DEST=<dir>]
	@if [ -z "$(FILE)" ]; then \
	  echo "❌  usage: make backup-remote-get FILE=<name-from-backup-remote-ls> [DEST=<dir>]"; exit 1; \
	fi
	@dest="$(or $(DEST),.)"; mkdir -p "$$dest"; \
	docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" \
	  -v "$$(cd "$$dest" && pwd):/out" \
	  rclone/rclone copy "$(call _env,RCLONE_REMOTE)/$(FILE)" /out \
	  --config /config/rclone/rclone.conf; \
	echo "✅  downloaded $$dest/$(FILE)"; \
	[ -n "$(DEST)" ] || echo "   restore with: make restore FILE=./$(FILE)"

# An off-site backup you haven't listed is an off-site backup you don't have.
# Uses the sidecar's own image so there's nothing to install on the host.
backup-remote-ls: ## List what's actually on the remote (the real proof it works)
	@if [ -z "$(call _env,RCLONE_REMOTE)" ]; then \
	  echo "❌  RCLONE_REMOTE is not set in .env — run: make backup-remote"; exit 1; \
	fi
	@echo "Listing $(call _env,RCLONE_REMOTE) ..."
	@docker run --rm -v "$(RCLONE_CONFIG_PATH):/config/rclone:ro" rclone/rclone \
	  ls "$(call _env,RCLONE_REMOTE)" --config /config/rclone/rclone.conf \
	  || { echo "❌  could not list the remote — check 'make backup-remote-logs'"; exit 1; }

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
