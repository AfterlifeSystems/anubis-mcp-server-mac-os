#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib.sh"

usage() {
  cat <<EOF
NeuralNexus MCP manager

Usage:
  ./neuralnexus-mcp.sh                    Interactive menu
  ./neuralnexus-mcp.sh install            Install deps and start the LaunchAgent
  ./neuralnexus-mcp.sh start              Load and start the LaunchAgent
  ./neuralnexus-mcp.sh stop               Stop the LaunchAgent
  ./neuralnexus-mcp.sh restart            Restart the LaunchAgent
  ./neuralnexus-mcp.sh status             Show service and config status
  ./neuralnexus-mcp.sh folders            List shared folders
  ./neuralnexus-mcp.sh add-folder DIR...     Share more folders with NeuralNexus
  ./neuralnexus-mcp.sh remove-folder DIR...  Stop sharing folders
  ./neuralnexus-mcp.sh logs               Follow service logs
  ./neuralnexus-mcp.sh uninstall          Remove the LaunchAgent
  ./neuralnexus-mcp.sh uninstall --purge  Also remove .venv, config, and logs

Scripts (also runnable directly):
  ./scripts/install.sh
  ./scripts/stop.sh
  ./scripts/uninstall.sh

Local development (separate LaunchAgent, uses .env.dev):
  ./scripts/dev.sh start
  ./scripts/dev.sh stop
EOF
}

cmd_install() {
  exec "$SCRIPTS_DIR/install.sh" "$@"
}

cmd_start() {
  require_launchd
  if ! service_is_installed; then
    echo "Service is not installed. Run ./neuralnexus-mcp.sh install first." >&2
    exit 1
  fi
  install_launchd_service
  start_service
  echo "Started ${SERVICE_NAME}."
}

cmd_stop() {
  exec "$SCRIPTS_DIR/stop.sh"
}

cmd_restart() {
  require_launchd
  if ! service_is_installed; then
    echo "Service is not installed. Run ./neuralnexus-mcp.sh install first." >&2
    exit 1
  fi
  restart_service
  echo "Restarted ${SERVICE_NAME}."
}

cmd_status() {
  show_service_status
}

# Loads the env file and sets PYTHON_BIN to the venv interpreter.
require_venv_python() {
  load_env_file
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python environment not found. Run ./neuralnexus-mcp.sh install first." >&2
    exit 1
  fi
}

restart_service_if_running() {
  if command -v launchctl >/dev/null 2>&1 \
    && service_is_installed \
    && service_is_running; then
    restart_service
    echo "Restarted ${SERVICE_NAME} so the change takes effect."
  else
    echo "Note: restart the daemon for the change to take effect."
  fi
}

cmd_folders() {
  require_venv_python
  "$PYTHON_BIN" - <<'PYEOF'
from src.daemon.config import DaemonConfig

watched_roots = DaemonConfig.load().watched_roots
if not watched_roots:
    print("No folders are shared yet. Add one with:")
    print("  ./neuralnexus-mcp.sh add-folder /path/to/folder")
else:
    print("Shared folders:")
    for root in watched_roots:
        print(f"  {root}")
PYEOF
}

cmd_add_folder() {
  if [[ $# -eq 0 ]]; then
    echo "Usage: ./neuralnexus-mcp.sh add-folder /path/to/folder [...]" >&2
    exit 1
  fi
  local folder
  local expanded_folders=()
  for folder in "$@"; do
    folder="${folder/#\~/$HOME}"
    if [[ ! -d "$folder" ]]; then
      echo "Not a directory: $folder" >&2
      exit 1
    fi
    expanded_folders+=("$folder")
  done
  require_venv_python
  "$PYTHON_BIN" -m src.daemon configure --add-watch "${expanded_folders[@]}"
  restart_service_if_running
}

cmd_remove_folder() {
  if [[ $# -eq 0 ]]; then
    echo "Usage: ./neuralnexus-mcp.sh remove-folder /path/to/folder [...]" >&2
    exit 1
  fi
  require_venv_python
  "$PYTHON_BIN" -m src.daemon configure --remove-watch "$@"
  restart_service_if_running
}

cmd_logs() {
  follow_logs
}

cmd_uninstall() {
  exec "$SCRIPTS_DIR/uninstall.sh" "$@"
}

folders_menu() {
  cmd_folders
  echo
  echo "  a) Add a folder"
  echo "  r) Remove a folder"
  echo "  b) Back"
  echo
  read -r -p "Choose an option [a/r/b]: " folder_choice
  case "$folder_choice" in
    a | A)
      read -r -p "Folder to share: " folder_path
      [[ -n "$folder_path" ]] && cmd_add_folder "$folder_path"
      ;;
    r | R)
      read -r -p "Folder to stop sharing: " folder_path
      [[ -n "$folder_path" ]] && cmd_remove_folder "$folder_path"
      ;;
    *) ;;
  esac
}

interactive_menu() {
  while true; do
    echo
    echo "NeuralNexus MCP"
    echo "  1) Install"
    echo "  2) Start"
    echo "  3) Stop"
    echo "  4) Restart"
    echo "  5) Status"
    echo "  6) Shared folders (list / add / remove)"
    echo "  7) Logs"
    echo "  8) Uninstall"
    echo "  9) Exit"
    echo
    read -r -p "Choose an option [1-9]: " choice
    case "$choice" in
      1) cmd_install ;;
      2) cmd_start ;;
      3) cmd_stop ;;
      4) cmd_restart ;;
      5) cmd_status ;;
      6) folders_menu ;;
      7) cmd_logs ;;
      8) cmd_uninstall ;;
      9) exit 0 ;;
      *)
        echo "Invalid choice."
        ;;
    esac
  done
}

if [[ $# -eq 0 ]]; then
  if [[ -t 0 ]]; then
    interactive_menu
  else
    usage
    exit 1
  fi
fi

case "$1" in
  install) shift; cmd_install "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  folders) cmd_folders ;;
  add-folder) shift; cmd_add_folder "$@" ;;
  remove-folder) shift; cmd_remove_folder "$@" ;;
  logs) cmd_logs ;;
  uninstall) shift; cmd_uninstall "$@" ;;
  -h | --help | help) usage ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 1
    ;;
esac
