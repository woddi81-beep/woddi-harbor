#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTOSTART_DIR="${HOME}/.config/autostart"
APPLICATION_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${APPLICATION_DIR}/woddi-harbor-tray.desktop"

if ! /usr/bin/python3 -c 'import PyQt6' >/dev/null 2>&1; then
  printf 'Fehler: PyQt6 fehlt im System-Python. Installiere das Paket python3-pyqt6.\n' >&2
  exit 1
fi

mkdir -p "$AUTOSTART_DIR" "$APPLICATION_DIR"
{
  printf '%s\n' \
    '[Desktop Entry]' \
    'Type=Application' \
    'Name=Woddi Harbor Ampel' \
    'Comment=Harbor-Dienste starten und fuer den Spielemodus beenden' \
    "Exec=/usr/bin/python3 ${ROOT}/tools/harbor_tray.py" \
    "Icon=${ROOT}/assets/tray/harbor-stopped.svg" \
    'Terminal=false' \
    'Categories=System;Utility;' \
    'X-GNOME-Autostart-enabled=true' \
    'X-KDE-autostart-after=panel'
} >"$DESKTOP_FILE"
chmod 755 "$DESKTOP_FILE"
cp "$DESKTOP_FILE" "$AUTOSTART_DIR/woddi-harbor-tray.desktop"

printf 'Woddi Harbor Ampel installiert.\n'
printf 'Start: /usr/bin/python3 %s/tools/harbor_tray.py\n' "$ROOT"
