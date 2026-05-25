#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DEFAULT_HOST="${HARBOR_HOST:-127.0.0.1}"
DEFAULT_PORT="${HARBOR_PORT:-9680}"

detect_os() {
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    echo "${ID:-linux}"
    return
  fi
  uname -s | tr '[:upper:]' '[:lower:]'
}

detect_shell_name() {
  if [[ -n "${SHELL:-}" ]]; then
    basename "$SHELL"
    return
  fi
  echo "sh"
}

log() {
  printf '[harbor] %s\n' "$*"
}

fail() {
  printf '[harbor][error] %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./harbor.sh [command] [args...]

Commands:
  bootstrap           Show distro-specific dependency hints
  install             Create venv and install woddi-harbor into it
  init                Initialize Harbor config/layout
  start               Ensure install, init if needed, then start API
  console             Open the richer Harbor TUI, fallback to the simple console
  cli [args...]       Run woddi-harbor CLI inside the venv
  activate-hint       Print the correct activation command for the current shell
  help                Show this help

Examples:
  ./harbor.sh start
  ./harbor.sh console
  ./harbor.sh cli status
  ./harbor.sh cli llm set --base-url http://llm:8000/v1 --model my-model
EOF
}

bootstrap_hint() {
  local os_id
  os_id="$(detect_os)"
  case "$os_id" in
    ubuntu|debian)
      cat <<EOF
Detected OS: $os_id
Run:
  bash scripts/bootstrap_ubuntu.sh
EOF
      ;;
    sles|sled|opensuse-leap|opensuse-tumbleweed|opensuse*)
      cat <<EOF
Detected OS: $os_id
Run:
  bash scripts/bootstrap_sles.sh
EOF
      ;;
    *)
      cat <<EOF
Detected OS: $os_id
Install at least:
  python3 python3-venv python3-pip git curl ca-certificates
EOF
      ;;
  esac
}

activate_hint() {
  local shell_name
  shell_name="$(detect_shell_name)"
  case "$shell_name" in
    fish)
      echo "source .venv/bin/activate.fish"
      ;;
    csh|tcsh)
      echo "source .venv/bin/activate.csh"
      ;;
    *)
      echo ". .venv/bin/activate"
      ;;
  esac
}

ensure_python() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python3 nicht gefunden. $(bootstrap_hint)"
}

ensure_venv() {
  ensure_python
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log "Erzeuge virtuelle Umgebung unter $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "venv-Erzeugung fehlgeschlagen. Unter Ubuntu fehlt oft python3-venv, unter SLES python3-virtualenv."
  fi
}

install_project() {
  ensure_venv
  log "Installiere woddi-harbor in die virtuelle Umgebung"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"
}

run_cli() {
  ensure_venv
  exec "$VENV_DIR/bin/woddi-harbor" "$@"
}

start_harbor() {
  install_project
  "$VENV_DIR/bin/woddi-harbor" init >/dev/null
  log "Starte woddi-harbor auf ${DEFAULT_HOST}:${DEFAULT_PORT}"
  exec "$VENV_DIR/bin/woddi-harbor" serve --host "$DEFAULT_HOST" --port "$DEFAULT_PORT"
}

cmd="${1:-start}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$cmd" in
  bootstrap)
    bootstrap_hint
    ;;
  install)
    install_project
    log "Fertig. Aktivierung fuer $(detect_shell_name): $(activate_hint)"
    ;;
  init)
    install_project
    exec "$VENV_DIR/bin/woddi-harbor" init
    ;;
  start)
    start_harbor
    ;;
  console)
    install_project
    "$VENV_DIR/bin/woddi-harbor" init >/dev/null
    if "$VENV_DIR/bin/woddi-harbor" tui; then
      exit 0
    fi
    log "TUI konnte nicht gestartet werden, wechsle auf einfache Konsole"
    exec "$VENV_DIR/bin/woddi-harbor" console-ui
    ;;
  cli)
    run_cli "$@"
    ;;
  activate-hint)
    activate_hint
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    fail "Unbekannter Befehl: $cmd"
    ;;
esac
