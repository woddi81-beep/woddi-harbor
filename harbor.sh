#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HARBOR_VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

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
  version [--short]   Show the checked-out Harbor version
  bootstrap           Show distro-specific dependency hints
  install             Create venv and install woddi-harbor into it
  init                Initialize Harbor config/layout
  start               Ensure install, init if needed, then start API
  console             Open the interactive Harbor console
  stop                Stop all Harbor runtime components
  uninstall-runtime   Remove managed services; preserve data and configuration
  cli [args...]       Compatibility form for forwarding CLI commands
  activate-hint       Print the correct activation command for the current shell
  help                Show this help

Any other command is forwarded to the Harbor CLI inside the virtual environment.

Examples:
  ./harbor.sh version
  ./harbor.sh start
  ./harbor.sh console
  ./harbor.sh status
  ./harbor.sh llm set --base-url http://llm:8000/v1 --model my-model
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

show_version() {
  if [[ $# -gt 1 || ($# -eq 1 && "$1" != "--short") ]]; then
    fail "Usage: ./harbor.sh version [--short]"
  fi

  ensure_python
  local version
  version="$(cd "$ROOT_DIR" && "$PYTHON_BIN" -c 'from app.version import __version__; print(__version__)')" \
    || fail "Version konnte nicht aus app/version.py gelesen werden."

  if [[ "${1:-}" == "--short" ]]; then
    printf '%s\n' "$version"
  else
    printf 'woddi-harbor %s\n' "$version"
  fi
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
  "$PYTHON_BIN" "$ROOT_DIR/tools/verify_installation.py" --source-only
  log "Installiere woddi-harbor in die virtuelle Umgebung"
  if "$VENV_DIR/bin/python" -c 'import setuptools.build_meta, wheel' >/dev/null 2>&1; then
    "$VENV_DIR/bin/python" -m pip install --no-build-isolation -e "$ROOT_DIR"
  else
    log "Initialisiere die Python-Build-Umgebung"
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"
  fi
  "$VENV_DIR/bin/python" "$ROOT_DIR/tools/verify_installation.py"
}

run_cli() {
  ensure_venv
  if [[ ! -x "$VENV_DIR/bin/woddi-harbor" ]]; then
    log "Harbor CLI fehlt; fuehre die Erstinstallation automatisch aus"
    install_project
  elif ! "$VENV_DIR/bin/python" "$ROOT_DIR/tools/verify_installation.py" >/dev/null 2>&1; then
    log "Installierte Harbor-Version ist nicht aktuell; aktualisiere die virtuelle Umgebung"
    install_project
  fi
  exec "$VENV_DIR/bin/woddi-harbor" "$@"
}

start_harbor() {
  install_project
  "$VENV_DIR/bin/woddi-harbor" init >/dev/null
  local host="${HARBOR_HOST:-127.0.0.1}"
  local port="${HARBOR_PORT:-9680}"
  log "Starte woddi-harbor auf ${host}:${port}"
  exec "$VENV_DIR/bin/woddi-harbor" serve --host "$host" --port "$port"
}

cmd="${1:-start}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$cmd" in
  version)
    show_version "$@"
    ;;
  --version|-V)
    [[ $# -eq 0 ]] || fail "Usage: ./harbor.sh $cmd"
    show_version
    ;;
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
    exec "$VENV_DIR/bin/woddi-harbor" console "$@"
    ;;
  stop)
    run_cli runtime stop-all
    ;;
  uninstall-runtime)
    run_cli runtime uninstall --yes
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
    run_cli "$cmd" "$@"
    ;;
esac
