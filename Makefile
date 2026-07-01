# RollCall — deployment manager
# Run from: ~/RollCallDB/RollCall/
# Usage:    make <target>

COMPOSE  := docker compose --profile web
BOT      := rollcall-bot
TUNNEL   := cloudflared
DB       := ./data/rollcall.db

# Read a value from .env, stripping surrounding quotes
_env = $(shell grep -m1 '^$(1)=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")

# Host port the health endpoint is published on (matches HEALTH_CHECK_HOST_PORT in .env)
HC_PORT := $(or $(call _env,HEALTH_CHECK_HOST_PORT),8080)

.DEFAULT_GOAL := help

.PHONY: help up down restart build logs logs-cf status url notify token group-token chats

help: ## Show this help
	@printf "\n\033[1mRollCall — deployment manager\033[0m\n"
	@printf "\n\033[4mLIFECYCLE\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make up"      "Start tunnel + bot; detect URL and update .env"
	@printf "  \033[36m%-16s\033[0m %s\n" "make down"    "Stop all containers"
	@printf "  \033[36m%-16s\033[0m %s\n" "make restart" "Restart bot (picks up .env changes)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make build"   "Rebuild bot image and restart"
	@printf "\n\033[4mOBSERVABILITY\033[0m\n"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs"    "Tail bot logs (Ctrl+C to stop)"
	@printf "  \033[36m%-16s\033[0m %s\n" "make logs-cf" "Tail Cloudflare tunnel logs"
	@printf "  \033[36m%-16s\033[0m %s\n" "make status"  "Container status + external service reachability"
	@printf "  \033[36m%-16s\033[0m %s\n" "make url"     "Current tunnel URL and all group voting links"
	@printf "  \033[36m%-16s\033[0m %s\n" "make notify"  "Send all voting links to Telegram admin"
	@printf "  \033[36m%-16s\033[0m %s\n" "make chats"   "List all known groups with their chat IDs"
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

up: ## Start tunnel + bot; auto-detect URL and update .env
	@echo "Starting Cloudflare tunnel..."
	@$(COMPOSE) up -d $(TUNNEL)
	@printf "Waiting for tunnel URL"
	@URL=""; \
	for i in $$(seq 1 20); do \
	  URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	    | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	  [ -n "$$URL" ] && break; \
	  printf "."; sleep 2; \
	done; \
	echo ""; \
	if [ -z "$$URL" ]; then echo "ERROR: timed out waiting for tunnel URL"; exit 1; fi; \
	echo "Tunnel: $$URL"; \
	if grep -q "^WEB_BASE_URL=" .env; then \
	  sed -i "s|^WEB_BASE_URL=.*|WEB_BASE_URL=$$URL|" .env; \
	else \
	  echo "WEB_BASE_URL=$$URL" >> .env; \
	fi; \
	echo "Updated WEB_BASE_URL in .env"; \
	echo "Starting bot..."; \
	$(COMPOSE) up -d --force-recreate $(BOT); \
	echo ""; \
	$(MAKE) -s url

down: ## Stop all containers
	$(COMPOSE) down

restart: ## Restart bot (picks up .env changes)
	$(COMPOSE) restart $(BOT)
	@echo "Bot restarted"

build: ## Rebuild bot image and restart
	$(COMPOSE) up -d --build $(BOT)

logs: ## Tail bot logs (Ctrl+C to stop)
	docker compose logs -f $(BOT)

logs-cf: ## Tail Cloudflare tunnel logs (Ctrl+C to stop)
	docker compose logs -f $(TUNNEL)

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
	@URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	printf "  Tunnel endpoint:  "; \
	if [ -z "$$URL" ]; then \
	  echo "❌  no URL (tunnel not started — run: make up)"; \
	else \
	  curl -sf --max-time 8 "$$URL/api/v1/health" > /dev/null 2>&1 \
	    && echo "✅  $$URL" \
	    || echo "❌  $$URL (tunnel started but not reachable yet)"; \
	fi
	@echo ""
	@echo "=== Bot Health ==="
	@HEALTH=$$(curl -sf --max-time 5 http://localhost:$(HC_PORT)/health 2>/dev/null); \
	if [ -z "$$HEALTH" ]; then \
	  echo "  ❌  health endpoint not responding (bot down?)"; \
	else \
	  echo "  $$HEALTH" | fold -s -w 100; \
	fi
	@echo ""

url: ## Show current tunnel URL and all group voting links
	@URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	if [ -z "$$URL" ]; then echo "Tunnel not running — run: make up"; exit 0; fi; \
	echo "Tunnel:   $$URL"; \
	echo "API docs: $$URL/api/docs"; \
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
	@URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	if [ -z "$$URL" ]; then echo "Tunnel not running — run: make up first"; exit 1; fi; \
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
