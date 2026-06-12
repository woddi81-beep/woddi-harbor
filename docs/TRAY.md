# Woddi Harbor Ampel

Die Desktop-Ampel steuert Harbor ohne systemd:

- Grün: Harbor läuft.
- Gelb: Harbor startet, stoppt oder wird neu gestartet.
- Rot: Harbor ist gestoppt.

## Installation

```bash
cd /srv/http/woddi-harbor
./harbor.sh install
bash scripts/install_tray.sh
```

Danach kann die Ampel sofort gestartet werden:

```bash
/usr/bin/python3 tools/harbor_tray.py
```

Beim nächsten Desktop-Login startet sie automatisch. Über das Rechtsklick-Menü stehen
`Harbor starten`, `Alles beenden (Spielemodus)` und `Harbor neu starten` zur Verfügung.
Der Spielemodus beendet den Harbor-Webserver, lokale Modul-Worker, verwaltete
MCP-Prozesse, vorhandene Harbor-Benutzerdienste und den optionalen Prometheus-Container.

Das Häkchen `Beim Anmelden starten` schaltet den Desktop-Autostart direkt ein oder aus.
