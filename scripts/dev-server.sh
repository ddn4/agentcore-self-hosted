#!/usr/bin/env bash
# Two-namespace Temporal dev server (CLI 1.9.1, server 1.32.0).
#   scripts/dev-server.sh run     foreground (make dev-server, Procfile.dev)
#   scripts/dev-server.sh start   background; waits until both namespaces answer
#   scripts/dev-server.sh stop    stops the background server
#   scripts/dev-server.sh status
# State persists in .temporal/dev.db (gitignored); `make dev-server-reset` wipes it.
#
# v0.7: WCI=1 (make dev-server WCI=1) also enables the Worker Controller Instance, which the
# dev server contains (ledger 23). `start-dev` takes only --dynamic-config-value KEY=VALUE
# (no file, no constraints), so it applies to EVERY namespace here; the "agents only"
# constraint is first verified on kind (v0.9). Modes:
#   WCI=1 | WCI=local  the WCI's AWS SDK is pointed at scripts/agentcore_emulator.py
#                      (AWS_ENDPOINT_URL, dummy credentials, no shared AWS config), and the
#                      role/External ID requirement is off: nothing can reach AWS.
#   WCI=aws            real AWS from the caller's environment (v0.8: AWS_PROFILE=temporal-dev-wci);
#                      require_role_and_external_id stays at its default (true).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

STATE_DIR=.temporal
PID_FILE="$STATE_DIR/dev-server.pid"
LOG_FILE="$STATE_DIR/dev-server.log"
IP=127.0.0.1
PORT="${TEMPORAL_DEV_PORT:-7233}"
UI_PORT="${TEMPORAL_DEV_UI_PORT:-8233}"
NAMESPACES=(agents agent-services)

# Standalone Nexus Operations (`temporal nexus operation ...`, Q16) are OFF in
# CLI 1.9.1's dev server unless enabled explicitly (verified at v0.4).
args=(server start-dev --ip "$IP" --port "$PORT" --ui-port "$UI_PORT"
      --db-filename "$STATE_DIR/dev.db" --log-level warn
      --dynamic-config-value nexusoperation.enableStandalone=true)
for ns in "${NAMESPACES[@]}"; do args+=(--namespace "$ns"); done

WCI="${WCI:-0}"
envs=()
case "$WCI" in
  0|"") ;;
  1|local|aws)
    args+=(--dynamic-config-value workercontroller.enabled=true
           --dynamic-config-value 'workercontroller.compute_providers.enabled=["aws-agentcore"]'
           --dynamic-config-value 'workercontroller.scaling_algorithms.enabled=["no-sync"]')
    if [[ "$WCI" != aws ]]; then
      args+=(--dynamic-config-value workercontroller.compute_providers.aws.require_role_and_external_id=false)
      envs=(-u AWS_PROFILE -u AWS_SESSION_TOKEN
            AWS_ENDPOINT_URL="http://127.0.0.1:${AGENTCORE_EMULATOR_PORT:-4599}"
            AWS_ACCESS_KEY_ID=local-emulator AWS_SECRET_ACCESS_KEY=local-emulator
            AWS_REGION="${AWS_REGION:-us-east-1}" AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null)
    fi
    ;;
  *) echo "WCI must be 0, 1/local or aws (got '$WCI')" >&2; exit 2 ;;
esac
launch=(env ${envs[@]+"${envs[@]}"} temporal "${args[@]}")

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
    echo "dev-server: WCI=$WCI"
    exec "${launch[@]}"
    ;;
  start)
    if running; then echo "dev-server: already running (pid $(cat "$PID_FILE"))"; exit 0; fi
    nohup "${launch[@]}" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    echo "$WCI" >"$STATE_DIR/dev-server.wci"
    for _ in $(seq 1 60); do
      if ready; then
        echo "dev-server: up on $IP:$PORT (UI http://$IP:$UI_PORT), namespaces: ${NAMESPACES[*]}, WCI=$WCI"
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
    rm -f "$PID_FILE" "$STATE_DIR/dev-server.wci"
    ;;
  status)
    if running; then echo "dev-server: running (pid $(cat "$PID_FILE"), WCI=$(cat "$STATE_DIR/dev-server.wci" 2>/dev/null || echo 0))"; else echo "dev-server: not running"; exit 1; fi
    ;;
  *)
    echo "usage: $0 run|start|stop|status" >&2; exit 2 ;;
esac
