#!/usr/bin/env bash
# Shared helpers for NeuralNexus MCP install/manage scripts (macOS / launchd).

MCP_MODE="prod"
SERVICE_NAME=""
ENV_FILE=""
DEFAULT_CONFIG_DIR=""

_resolve_repo_paths() {
  local source="${BASH_SOURCE[2]:-${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}}"
  SCRIPT_DIR="$(cd "$(dirname "$source")" && pwd)"
  if [[ "$(basename "$SCRIPT_DIR")" == "scripts" ]]; then
    SCRIPTS_DIR="$SCRIPT_DIR"
    ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  else
    ROOT_DIR="$SCRIPT_DIR"
    SCRIPTS_DIR="$ROOT_DIR/scripts"
  fi
}

init_mcp_mode() {
  local mode="${1:-prod}"
  MCP_MODE="$mode"
  if [[ "$MCP_MODE" == "dev" ]]; then
    SERVICE_NAME="com.neuralnexus.mcp.dev"
    ENV_FILE="${ROOT_DIR}/.env.dev"
    DEFAULT_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/neuralnexus-mcp-dev"
    LOG_DIR="$HOME/Library/Logs/neuralnexus-mcp-dev"
  else
    SERVICE_NAME="com.neuralnexus.mcp"
    ENV_FILE="${ROOT_DIR}/.env"
    DEFAULT_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/neuralnexus-mcp"
    LOG_DIR="$HOME/Library/Logs/neuralnexus-mcp"
  fi
  CONFIG_DIR="${NEURALNEXUS_MCP_CONFIG_DIR:-$DEFAULT_CONFIG_DIR}"

  # Plist path is mode-specific and must be recomputed whenever the mode
  # changes, otherwise a dev invocation would write to the production agent.
  PLIST_DIR="$HOME/Library/LaunchAgents"
  PLIST_PATH="${PLIST_DIR}/${SERVICE_NAME}.plist"
  LOG_FILE="${LOG_DIR}/daemon.log"
  LAUNCHD_DOMAIN="gui/$(id -u)"
}

_resolve_repo_paths
init_mcp_mode prod

# True when the given interpreter exists and is Python 3.11+.
_python_ok() {
  command -v "$1" >/dev/null 2>&1 \
    && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

# Locate uv, installing it to ~/.local/bin if needed. uv can download a
# managed Python, which lets the one-click install work on Macs that ship
# only the 3.9 Command Line Tools python3 — or no python3 at all.
ensure_uv() {
  local candidate
  for candidate in uv "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if command -v "$candidate" >/dev/null 2>&1; then
      UV_BIN="$(command -v "$candidate")"
      return 0
    fi
  done
  echo "Installing uv (Python runtime manager) into ~/.local/bin..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV_BIN="$HOME/.local/bin/uv"
  if [[ ! -x "$UV_BIN" ]]; then
    echo "Failed to install uv. Install Python 3.11+ manually and re-run." >&2
    exit 1
  fi
}

require_python3() {
  if _python_ok python3; then
    return 0
  fi
  ensure_uv
}

require_launchd() {
  if ! command -v launchctl >/dev/null 2>&1; then
    echo "launchctl is required for service management." >&2
    exit 1
  fi
  if ! launchctl print "$LAUNCHD_DOMAIN" >/dev/null 2>&1; then
    echo "Could not reach the launchd user session (${LAUNCHD_DOMAIN})." >&2
    echo "Run this from a logged-in macOS session (not a bare SSH shell)." >&2
    exit 1
  fi
}

require_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing ${ENV_FILE}." >&2
    if [[ "$MCP_MODE" == "dev" ]]; then
      echo "Copy .env.dev.example to .env.dev and adjust for local Anubis." >&2
    fi
    exit 1
  fi
}

load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  elif [[ "$MCP_MODE" == "dev" ]]; then
    require_env_file
  fi
  CONFIG_DIR="${NEURALNEXUS_MCP_CONFIG_DIR:-$DEFAULT_CONFIG_DIR}"
}

install_dependencies() {
  require_python3
  cd "$ROOT_DIR"

  if [[ ! -d .venv ]]; then
    if _python_ok python3; then
      python3 -m venv .venv
    else
      "$UV_BIN" venv --seed --python 3.12 .venv
    fi
  fi

  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q
}

is_first_run() {
  load_env_file
  "${ROOT_DIR}/.venv/bin/python" -c "from src.daemon.setup import is_first_run; import sys; sys.exit(0 if is_first_run() else 1)"
}

run_first_run_setup() {
  load_env_file
  if ! is_first_run; then
    return 0
  fi

  echo "First run — configuring daemon (${MCP_MODE})..."
  if [[ -t 0 ]]; then
    "${ROOT_DIR}/.venv/bin/python" -m src.daemon setup "$@"
  elif [[ -n "${NEURALNEXUS_API_KEY:-}" ]]; then
    "${ROOT_DIR}/.venv/bin/python" -m src.daemon setup --non-interactive "$@"
  else
    echo "First run requires an interactive terminal or NEURALNEXUS_API_KEY." >&2
    return 1
  fi
}

apply_env_to_config() {
  load_env_file
  if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    return 0
  fi

  local args=()
  if [[ -n "${NEURALNEXUS_API_BASE_URL:-}" ]]; then
    args+=(--api-base-url "$NEURALNEXUS_API_BASE_URL")
  fi
  if [[ -n "${PORT:-}" ]]; then
    args+=(--port "$PORT")
  fi
  if [[ ${#args[@]} -gt 0 ]]; then
    "${ROOT_DIR}/.venv/bin/python" -m src.daemon configure "${args[@]}"
  fi
}

install_launchd_service() {
  load_env_file
  # Resolve the config dir to an absolute path here. launchd performs no tilde
  # or variable expansion in plists, so a value like
  # "~/.config/neuralnexus-mcp-dev" in .env.dev would otherwise be created
  # literally under the working directory. Inject the expanded path directly.
  local config_dir="${CONFIG_DIR/#\~/$HOME}"

  mkdir -p "$PLIST_DIR" "$LOG_DIR"
  cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SERVICE_NAME}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>set -a; [ -f "${ENV_FILE}" ] &amp;&amp; . "${ENV_FILE}"; set +a; exec "${ROOT_DIR}/.venv/bin/python" -m src.daemon start</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NEURALNEXUS_MCP_CONFIG_DIR</key>
    <string>${config_dir}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
EOF
}

remove_launchd_service() {
  launchctl bootout "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$PLIST_PATH"
}

service_is_installed() {
  [[ -f "$PLIST_PATH" ]]
}

service_is_loaded() {
  launchctl print "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" >/dev/null 2>&1
}

service_is_running() {
  launchctl print "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null | grep -q "state = running"
}

start_service() {
  require_launchd
  # Re-bootstrap so plist edits take effect (the daemon-reload analogue).
  if service_is_loaded; then
    launchctl bootout "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null || true
  fi
  launchctl enable "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null || true
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_PATH"
}

stop_service() {
  require_launchd
  if service_is_installed; then
    launchctl bootout "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null || true
    return 0
  fi

  stop_foreground_daemon
}

restart_service() {
  require_launchd
  if service_is_loaded; then
    launchctl kickstart -k "${LAUNCHD_DOMAIN}/${SERVICE_NAME}"
  else
    start_service
  fi
}

follow_logs() {
  mkdir -p "$LOG_DIR"
  touch "$LOG_FILE"
  exec tail -n 50 -F "$LOG_FILE"
}

stop_foreground_daemon() {
  load_env_file
  local port pid
  port="$("${ROOT_DIR}/.venv/bin/python" -c "from src.daemon.config import DaemonConfig; print(DaemonConfig.load().local_port)" 2>/dev/null || echo "${PORT:-8000}")"
  pid="$(lsof -ti ":${port}" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    echo "No NeuralNexus MCP daemon (${MCP_MODE}) found on port ${port}."
    return 0
  fi
  echo "Stopping ${MCP_MODE} process on port ${port} (PID ${pid})..."
  kill -TERM $pid 2>/dev/null || true
  sleep 2
  if kill -0 $pid 2>/dev/null; then
    echo "Process did not exit; sending SIGKILL..."
    kill -KILL $pid 2>/dev/null || true
  fi
}

install_and_start_service() {
  run_first_run_setup "$@"
  apply_env_to_config
  install_launchd_service
  start_service
}

show_service_status() {
  require_launchd
  echo "Mode: ${MCP_MODE}"
  echo "Env:  ${ENV_FILE}"
  echo "Config: ${CONFIG_DIR}"
  echo
  if service_is_installed; then
    if service_is_loaded; then
      launchctl print "${LAUNCHD_DOMAIN}/${SERVICE_NAME}" 2>/dev/null \
        | grep -E "(state|pid|last exit code) = " || true
    else
      echo "LaunchAgent installed but not running (${PLIST_PATH})."
      echo "Start it with: ./neuralnexus-mcp.sh start"
    fi
    echo
  else
    echo "LaunchAgent not installed (${PLIST_PATH})."
    echo
  fi
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    load_env_file
    "${ROOT_DIR}/.venv/bin/python" -m src.daemon status || true
  fi
}
