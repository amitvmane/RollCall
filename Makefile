# RollCall — deployment manager
# Run from: ~/RollCallDB/RollCall/
# Usage:    make <target>

-include .env
export

COMPOSE  := docker compose --profile web
BOT      := rollcall-bot
TUNNEL   := rollcall-cloudflared
DB       := ./data/rollcall.db

.DEFAULT_GOAL := help

.PHONY: help up down restart build logs logs-cf status url notify

help:
	@printf "\nRollCall commands:\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
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

status: ## Show container status
	$(COMPOSE) ps

url: ## Show current tunnel URL and all group voting links
	@URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	if [ -z "$$URL" ]; then echo "Tunnel not running — run: make up"; exit 0; fi; \
	echo "Tunnel:  $$URL"; \
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

notify: ## Send all voting links to Telegram admin (safe to run when banned — prints links if unreachable)
	@URL=$$(docker compose logs $(TUNNEL) 2>/dev/null \
	  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1); \
	if [ -z "$$URL" ]; then echo "Tunnel not running — run: make up first"; exit 1; fi; \
	LINKS=$$(sqlite3 $(DB) \
	  "SELECT chat_id, group_web_token FROM chats WHERE group_web_token IS NOT NULL;" \
	  2>/dev/null | \
	  while IFS='|' read -r cid tok; do \
	    echo "Chat $$cid: $$URL/web/group/$$tok"; \
	  done); \
	MSG="🔗 Web voting links (Telegram-down mode):%0A%0A$$LINKS%0A%0AOpen your group link to vote."; \
	echo "Voting links:"; \
	echo "$$LINKS"; \
	echo ""; \
	curl -sf --max-time 10 \
	  "https://api.telegram.org/bot$(API_KEY)/sendMessage" \
	  -d "chat_id=$(ADMIN1)" \
	  --data-urlencode "text=$$MSG" > /dev/null \
	  && echo "Sent to Telegram admin ($(ADMIN1))" \
	  || echo "(Telegram unreachable — share the links above manually)"
