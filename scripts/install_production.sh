#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-manual}"

cd "$ROOT"
python3 tools/verify_installation.py --source-only
python3 -m venv .venv
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/python tools/verify_installation.py
.venv/bin/woddi-harbor init

if [[ "$MODE" == "manual" ]]; then
  echo "Manuelle Installation abgeschlossen. Es wurden keine systemd-Units installiert."
  echo "Jetzt Admin, LLM und Quellen konfigurieren, production-check ausfuehren und mit ./harbor.sh start starten."
  exit 0
fi

if [[ "$MODE" == "system" ]]; then
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL=(systemctl)
elif [[ "$MODE" == "user" ]]; then
  UNIT_DIR="${HOME}/.config/systemd/user"
  SYSTEMCTL=(systemctl --user)
else
  echo "Unbekannter Modus: $MODE (erlaubt: manual, user, system)" >&2
  exit 2
fi

mkdir -p "$UNIT_DIR"
.venv/bin/woddi-harbor service install harbor --mode "$MODE" --enable
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-jobs.service.tpl >"$UNIT_DIR/woddi-harbor-jobs.service"
sed "s|__HARBOR_WORKDIR__|$ROOT|g" systemd/woddi-harbor-backup.service.tpl >"$UNIT_DIR/woddi-harbor-backup.service"
cp systemd/woddi-harbor-backup.timer "$UNIT_DIR/woddi-harbor-backup.timer"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable woddi-harbor-jobs.service woddi-harbor-backup.timer

echo "Installation vorbereitet. Jetzt Admin, LLM und Quellen konfigurieren, production-check ausfuehren und Services starten."
