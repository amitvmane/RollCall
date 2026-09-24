#!/bin/sh
# Independent watchdog: polls the bot's /health and DMs an admin when it breaks.
#
# Why this exists: every failure mode this project has actually hit was SILENT.
# The db-backup sidecar died on 2026-08-03 and nobody noticed until 2026-08-24.
# `restart: unless-stopped` and the container healthcheck recover a crashed
# bot, but neither one tells a human anything, and a stale backup or a dead
# scheduler task deliberately does NOT fail the healthcheck (a 503 there would
# make Docker restart a bot that is serving fine). So nothing was watching.
#
# This runs as its own container and talks to the Telegram API directly, so it
# still works when the bot process is the thing that is down — which is exactly
# when it matters. /health already aggregates DB, Telegram, scheduler liveness,
# prune liveness, pool saturation and backup freshness, so reading that one
# endpoint covers every signal the bot knows how to report about itself.
#
# Config (all via environment):
#   TELEGRAM_TOKEN / API_KEY   bot token used to send the alert DM
#   WATCHDOG_CHAT_ID           who to alert; falls back to ADMIN1
#   HEALTH_URL                 default http://rollcall-bot:8080/health
#   WATCHDOG_INTERVAL_SECONDS  poll cadence (default 300)
#   WATCHDOG_FAILURES_BEFORE_ALERT  consecutive bad polls before alerting
#                              (default 3 — rides out a normal restart)
#   WATCHDOG_REPEAT_HOURS      re-nag cadence while still broken (default 12)
#   WATCHDOG_HEARTBEAT_FILE    stamp touched once per loop iteration; the
#                              container healthcheck below reads its age
#                              (default /tmp/watchdog-heartbeat)

set -u

TOKEN="${TELEGRAM_TOKEN:-${API_KEY:-}}"
CHAT_ID="${WATCHDOG_CHAT_ID:-${ADMIN1:-}}"
HEALTH_URL="${HEALTH_URL:-http://rollcall-bot:8080/health}"
INTERVAL="${WATCHDOG_INTERVAL_SECONDS:-300}"
FAILURES_BEFORE_ALERT="${WATCHDOG_FAILURES_BEFORE_ALERT:-3}"
REPEAT_SECONDS=$(( ${WATCHDOG_REPEAT_HOURS:-12} * 3600 ))
HEARTBEAT_FILE="${WATCHDOG_HEARTBEAT_FILE:-/tmp/watchdog-heartbeat}"

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
  # Deliberately not an error: the watchdog ships enabled by default, and an
  # operator who has not set ADMIN1 should get a clear explanation on a loop
  # rather than a container that crash-loops or, worse, one that looks healthy
  # while silently alerting nobody.
  #
  # This branch never touches HEARTBEAT_FILE, so the container healthcheck
  # below stays unhealthy the whole time it runs. That's deliberate, not a
  # bug: a watchdog with no admin configured genuinely isn't watching
  # anything, and `docker ps`/`make status` should say so rather than show
  # green for a container quietly doing nothing.
  while true; do
    echo "[watchdog] DISABLED — need a bot token and WATCHDOG_CHAT_ID (or ADMIN1) to send alerts."
    echo "[watchdog] Set ADMIN1=<your numeric Telegram user id> in .env, and make sure"
    echo "[watchdog] you have sent the bot a DM at least once (Telegram forbids bots from"
    echo "[watchdog] opening a conversation first, so an un-DMed admin cannot be alerted)."
    sleep 3600
  done
fi

notify() {
  # --max-time so a hung Telegram API call can't wedge the poll loop.
  curl -s --max-time 20 -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${CHAT_ID}" \
    -d "disable_web_page_preview=true" \
    --data-urlencode "text=$1" >/dev/null 2>&1 \
    || echo "[watchdog] failed to deliver alert (Telegram unreachable from watchdog)"
}

# Seconds -> "3h 12m" / "2d 4h" / "45s". Two units is always enough, and the
# alert is read on a phone.
human_duration() {
  s=$1
  if [ "$s" -lt 60 ]; then echo "${s}s"; return; fi
  m=$(( s / 60 ))
  if [ "$m" -lt 60 ]; then echo "${m}m"; return; fi
  h=$(( m / 60 )); m=$(( m % 60 ))
  if [ "$h" -lt 24 ]; then
    if [ "$m" -eq 0 ]; then echo "${h}h"; else echo "${h}h ${m}m"; fi
    return
  fi
  d=$(( h / 24 )); h=$(( h % 24 ))
  if [ "$h" -eq 0 ]; then echo "${d}d"; else echo "${d}d ${h}h"; fi
}

consecutive_failures=0
last_state="OK"
last_alert_at=0
# When the current run of bad polls began — the basis for the downtime figure
# in the recovery message. 0 means "not currently down".
#
# This is the watchdog's OWN view, bounded by its poll interval and lost if
# this container restarts. The bot keeps an independent, durable measurement
# in its database (see rollCall/uptime.py) and reports that when it reconnects
# — which is the one that survives an outage that took this container down
# too. Two observers on purpose: neither one can see every kind of outage.
first_failure_at=0

echo "[watchdog] started — polling ${HEALTH_URL} every ${INTERVAL}s, alerting chat ${CHAT_ID}"
notify "🐕 RollCall watchdog started — monitoring $(echo "$HEALTH_URL" | sed 's#http://##'). You'll get a message here if the bot stops responding or reports a problem."
touch "$HEARTBEAT_FILE" 2>/dev/null

while true; do
  body="$(curl -s --max-time 15 "$HEALTH_URL" 2>/dev/null)"
  rc=$?

  if [ $rc -ne 0 ] || [ -z "$body" ]; then
    state="UNREACHABLE"
    detail="/health did not respond (curl exit ${rc}). The bot container is down, restarting, or wedged."
  else
    case "$body" in
      DEGRADED*)
        state="DEGRADED"
        detail="$body"
        ;;
      *)
        state="OK"
        detail="$body"
        ;;
    esac
  fi

  now="$(date +%s)"

  if [ "$state" = "OK" ]; then
    if [ "$last_state" != "OK" ]; then
      # Down for at least (now - first bad poll). "at least" because the bot
      # can have failed at any point in the interval before that poll, and
      # recovered at any point in the one before this — both ends are fuzzy
      # by up to INTERVAL seconds, so never claim more precision than that.
      # first_failure_at is always set here: last_state only leaves "OK" in
      # the failure branch below, which stamps it.
      down_for=$(( now - first_failure_at ))
      # GNU date in the bot image; the -r form is the BSD fallback for anyone
      # running this script directly on a Mac.
      went_at="$(date -d "@${first_failure_at}" '+%H:%M' 2>/dev/null \
                 || date -r "${first_failure_at}" '+%H:%M' 2>/dev/null \
                 || echo '?')"
      notify "✅ RollCall recovered — $(date '+%Y-%m-%d %H:%M %Z')

Down for about $(human_duration "$down_for") — first bad check at ${went_at}, ±$(human_duration "$INTERVAL") either side.

${detail}"
      echo "[watchdog] recovered after ${down_for}s: ${detail}"
    fi
    consecutive_failures=0
    last_state="OK"
    last_alert_at=0
    first_failure_at=0
  else
    consecutive_failures=$(( consecutive_failures + 1 ))
    if [ "$first_failure_at" -eq 0 ]; then
      first_failure_at="$now"
    fi
    # Alert on the Nth consecutive bad poll, then re-nag on the repeat cadence.
    # Both conditions are needed: the first stops a routine restart from paging
    # anyone, the second stops a real outage from being announced once at 3am
    # and never mentioned again.
    should_alert=0
    if [ "$consecutive_failures" -eq "$FAILURES_BEFORE_ALERT" ]; then
      should_alert=1
    elif [ "$consecutive_failures" -gt "$FAILURES_BEFORE_ALERT" ] && \
         [ $(( now - last_alert_at )) -ge "$REPEAT_SECONDS" ]; then
      should_alert=1
    fi

    if [ "$should_alert" -eq 1 ]; then
      notify "🚨 RollCall ${state} — $(date '+%Y-%m-%d %H:%M %Z')

${detail}

Failed ${consecutive_failures} check(s) in a row, over $(human_duration $(( now - first_failure_at ))).
On the server: make status && make logs"
      last_alert_at="$now"
      echo "[watchdog] ALERT (${state}, ${consecutive_failures} consecutive): ${detail}"
    else
      echo "[watchdog] ${state} (${consecutive_failures} consecutive, alert at ${FAILURES_BEFORE_ALERT}): ${detail}"
    fi
    last_state="$state"
  fi

  # Marks "the poll loop is still turning" — independent of whether the last
  # poll was OK, DEGRADED or UNREACHABLE. An unreachable bot is exactly the
  # case this file must stay fresh through, or the healthcheck would confuse
  # "bot is down" (the thing watchdog exists to report) with "watchdog is
  # down" (the thing nothing else was reporting).
  touch "$HEARTBEAT_FILE" 2>/dev/null

  sleep "$INTERVAL"
done
