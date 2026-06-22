# RollCall — deployment manager
# Run from: ~/RollCallDB/RollCall/
# Usage:    make <target>

COMPOSE  := docker compose --profile web
BOT      := rollcall-bot
TUNNEL   := cloudflared
DB       := ./data/rollcall.db

# Read a value from .env, stripping surrounding quotes
_env = $(shell grep -m1 '^$(1)=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")

.DEFAULT_GOAL := help

.PHONY: help up down restart build logs logs-cf status url notify token

help: ## Show this help
	@printf "\nRollCall commands:\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
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
	@HEALTH=$$(curl -sf --max-time 5 http://localhost:8080/health 2>/dev/null); \
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
