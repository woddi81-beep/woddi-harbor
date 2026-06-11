#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT="$UNIT_DIR/woddi-harbor-tls.service"

command -v caddy >/dev/null 2>&1 || {
  echo "caddy is required" >&2
  exit 1
}

mkdir -p "$UNIT_DIR" "${HOME}/.local/share/caddy" "${HOME}/.config/caddy"
sed "s|__HARBOR_WORKDIR__|$ROOT|g" "$ROOT/systemd/woddi-harbor-tls.service.tpl" >"$UNIT"
caddy validate --config "$ROOT/deploy/Caddyfile.local" --adapter caddyfile
systemctl --user daemon-reload
systemctl --user enable --now woddi-harbor-tls.service
