#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-manual}"

cd "$ROOT"
python3 tools/verify_installation.py --source-only
python3 -m venv .venv
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/python tools/verify_installation.py
./harbor.sh init

if [[ "$MODE" == "manual" ]]; then
  echo "Manual installation complete. No systemd units were installed."
  echo "Now configure the admin user, LLM, and sources, run production-check, then start with ./harbor.sh start."
  exit 0
fi

if [[ "$MODE" == "system" ]]; then
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL=(systemctl)
elif [[ "$MODE" == "user" ]]; then
  UNIT_DIR="${HOME}/.config/systemd/user"
  SYSTEMCTL=(systemctl --user)
else
  echo "Unknown mode: $MODE (allowed: manual, user, system)" >&2
  exit 2
fi

mkdir -p "$UNIT_DIR"
./harbor.sh service install harbor --mode "$MODE" --enable
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-jobs.service.tpl >"$UNIT_DIR/woddi-harbor-jobs.service"
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-backup.service.tpl >"$UNIT_DIR/woddi-harbor-backup.service"
cp systemd/woddi-harbor-backup.timer "$UNIT_DIR/woddi-harbor-backup.timer"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable woddi-harbor-jobs.service woddi-harbor-backup.timer

echo "Installation prepared. Now configure the admin user, LLM, and sources, run production-check, then start the services."
