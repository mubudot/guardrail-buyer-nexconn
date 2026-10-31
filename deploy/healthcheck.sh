#!/bin/sh
# Liveness watchdog for GuardRail Buyer (run by guardrail-healthcheck.timer every 2 minutes).
# Restarts the service after 3 consecutive failed /health/live checks; the count lives in /run
# (cleared on reboot). systemd's Restart=always already covers crashes; this covers a hung process.
SERVICE="${SERVICE:-guardrail.service}"
URL="${URL:-http://127.0.0.1:8710/health/live}"
STATE="${STATE:-/run/guardrail-healthcheck.failures}"
LIMIT="${LIMIT:-3}"

if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then
  echo 0 > "$STATE"
  exit 0
fi
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
echo "health check failed ($n/$LIMIT): $URL"
if [ "$n" -ge "$LIMIT" ]; then
  echo "restarting $SERVICE"
  echo 0 > "$STATE"
  systemctl restart "$SERVICE"
fi
