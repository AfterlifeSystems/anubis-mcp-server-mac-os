#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
init_mcp_mode prod
cd "$ROOT_DIR"

if service_is_installed; then
  require_launchd
  echo "Stopping ${SERVICE_NAME}..."
  stop_service
  echo "Stopped."
else
  load_env_file
  stop_foreground_daemon
fi
