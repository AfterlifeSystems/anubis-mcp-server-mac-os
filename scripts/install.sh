#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
init_mcp_mode prod
cd "$ROOT_DIR"

load_env_file
install_dependencies
install_and_start_service "$@"

echo "NeuralNexus MCP installed."
echo
echo "NeuralNexus MCP is running as a background service."
echo
echo "  Manage:  ./neuralnexus-mcp.sh"
echo "  Status:  ./neuralnexus-mcp.sh status"
echo "  Stop:    ./scripts/stop.sh"
echo "  Logs:    tail -f ${LOG_FILE}"
echo
echo "The service starts automatically when you log in."
echo "Note: like all macOS LaunchAgents, it runs only while you are logged in."
echo
echo "If macOS asks for permission to access folders (Documents, Downloads, ...),"
echo "click Allow so NeuralNexus can read the files you chose to share."
echo
echo "Local development against Anubis uses a separate dev service:"
echo "  cp .env.dev.example .env.dev && ./scripts/dev.sh start"
