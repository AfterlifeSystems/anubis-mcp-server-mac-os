#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
init_mcp_mode dev
cd "$ROOT_DIR"

usage() {
  cat <<EOF
NeuralNexus MCP — local development (uses .env.dev, separate from production)

Usage:
  ./scripts/dev.sh start     Install deps if needed, configure from .env.dev, start dev service
  ./scripts/dev.sh stop      Stop the dev service only
  ./scripts/dev.sh restart   Restart the dev service
  ./scripts/dev.sh status    Show dev service and config status
  ./scripts/dev.sh logs      Follow dev service logs

Production install/manage (unchanged):
  ./neuralnexus-mcp.sh install
  ./neuralnexus-mcp.sh stop

Setup:
  cp .env.dev.example .env.dev
  # edit NEURALNEXUS_API_BASE_URL, PORT, NEURALNEXUS_MCP_CONFIG_DIR, etc.
EOF
}

cmd_start() {
  require_env_file
  load_env_file
  install_dependencies
  install_and_start_service "$@"

  echo "NeuralNexus MCP dev service is running."
  echo
  echo "  API:     ${NEURALNEXUS_API_BASE_URL:-http://localhost:8123}"
  echo "  MCP:     http://127.0.0.1:${PORT:-9990}/mcp"
  echo "  Config:  ${CONFIG_DIR}"
  echo "  Status:  ./scripts/dev.sh status"
  echo "  Stop:    ./scripts/dev.sh stop"
  echo "  Logs:    ./scripts/dev.sh logs"
}

cmd_stop() {
  require_env_file
  load_env_file
  if service_is_installed; then
    require_launchd
    echo "Stopping ${SERVICE_NAME}..."
    stop_service
    echo "Dev service stopped."
  else
    stop_foreground_daemon
  fi
}

cmd_restart() {
  cmd_stop
  cmd_start "$@"
}

cmd_status() {
  require_env_file
  show_service_status
}

cmd_logs() {
  require_env_file
  follow_logs
}

if [[ $# -eq 0 ]]; then
  set -- start
fi

case "$1" in
  start) shift; cmd_start "$@" ;;
  stop) cmd_stop ;;
  restart) shift; cmd_restart "$@" ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  -h | --help | help) usage ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 1
    ;;
esac
