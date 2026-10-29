#!/usr/bin/env bash
# Two-namespace Temporal dev server (CLI 1.9.1, server 1.32.0).
#   scripts/dev-server.sh run     foreground (make dev-server, Procfile.dev)
#   scripts/dev-server.sh start   background; waits until both namespaces answer
#   scripts/dev-server.sh stop    stops the background server
#   scripts/dev-server.sh status
# State persists in .temporal/dev.db (gitignored); `make dev-server-reset` wipes it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

STATE_DIR=.temporal
PID_FILE="$STATE_DIR/dev-server.pid"
LOG_FILE="$STATE_DIR/dev-server.log"
IP=127.0.0.1
PORT="${TEMPORAL_DEV_PORT:-7233}"
UI_PORT="${TEMPORAL_DEV_UI_PORT:-8233}"
NAMESPACES=(agents agent-services)

args=(server start-dev --ip "$IP" --port "$PORT" --ui-port "$UI_PORT"
      --db-filename "$STATE_DIR/dev.db" --log-level warn)
for ns in "${NAMESPACES[@]}"; do args+=(--namespace "$ns"); done

running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

ready() {
  for ns in "${NAMESPACES[@]}"; do
    # Explicit address and --tls=false: independent of any user profile.
    temporal operator namespace describe --address "$IP:$PORT" --tls=false \
      --namespace "$ns" >/dev/null 2>&1 || return 1
  done
}

mkdir -p "$STATE_DIR"
case "${1:-run}" in
  run)
    exec temporal "${args[@]}"
    ;;
  start)
    if running; then echo "dev-server: already running (pid $(cat "$PID_FILE"))"; exit 0; fi
    nohup temporal "${args[@]}" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 60); do
      if ready; then
        echo "dev-server: up on $IP:$PORT (UI http://$IP:$UI_PORT), namespaces: ${NAMESPACES[*]}"
        exit 0
      fi
      running || { echo "dev-server: exited; see $LOG_FILE" >&2; exit 1; }
      sleep 0.5
    done
    echo "dev-server: not ready after 30s; see $LOG_FILE" >&2
    exit 1
    ;;
  stop)
    if running; then
      pid="$(cat "$PID_FILE")"
      kill "$pid"
      for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
      echo "dev-server: stopped (pid $pid)"
    else
      echo "dev-server: not running"
    fi
    rm -f "$PID_FILE"
    ;;
  status)
    if running; then echo "dev-server: running (pid $(cat "$PID_FILE"))"; else echo "dev-server: not running"; exit 1; fi
    ;;
  *)
    echo "usage: $0 run|start|stop|status" >&2; exit 2 ;;
esac
