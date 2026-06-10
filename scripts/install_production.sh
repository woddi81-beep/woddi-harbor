#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-user}"

cd "$ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/woddi-harbor init

if [[ "$MODE" == "system" ]]; then
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL=(systemctl)
else
  UNIT_DIR="${HOME}/.config/systemd/user"
  SYSTEMCTL=(systemctl --user)
fi

mkdir -p "$UNIT_DIR"
.venv/bin/woddi-harbor service install harbor --mode "$MODE" --enable
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-jobs.service.tpl >"$UNIT_DIR/woddi-harbor-jobs.service"
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-backup.service.tpl >"$UNIT_DIR/woddi-harbor-backup.service"
cp systemd/woddi-harbor-backup.timer "$UNIT_DIR/woddi-harbor-backup.timer"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable woddi-harbor-jobs.service woddi-harbor-backup.timer

echo "Installation vorbereitet. Jetzt Admin, LLM und Quellen konfigurieren, production-check ausfuehren und Services starten."
