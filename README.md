# woddi-harbor

`woddi-harbor` ist ein lokaler Control-Hub fuer eine persoenliche AI mit externer LLM-Anbindung, lokalen Suchmodulen und separaten MCP-Diensten.

Zielplattformen fuer den Betrieb sind Linux-Systeme wie `SLES` und `Ubuntu`.

Der Name passt bewusst zum Zielbild:

- ein sicherer zentraler Hafen fuer Modelle, Module und Tools
- klar getrennte Services statt monolithischem Wildwuchs
- spaeter gut erweiterbar um Benutzerrechte, Policies und weitere Oberflaechen

## Kernfunktionen im ersten Stand

- externes OpenAI-kompatibles LLM (`/v1/chat/completions`)
- lokale Dokumentensuche als eigener Modul-Prozess
- lokale Maildir-Suche als eigener Modul-Prozess
- persistente Suchindizes fuer Docs und Maildir unter `data/runtime/indexes/`
- generische MCP-HTTP-Anbindung fuer externe Dienste wie NetBox oder OpenStack
- standardkonforme MCP-HTTP-Anbindung fuer `/mcp`-Server wie `netbox-mcp-server`
- MCP-Discovery/Capability-Handshake fuer tokenlose und tokenbasierte HTTP-Dienste
- zentrale Diagnose- und Statusdaten fuer Startfehler, Health und Reindex-Jobs
- CLI mit Rich-Ausgabe fuer Status, Konfiguration und Modulsteuerung
- FastAPI-Control-Plane fuer spaetere Oberflaechen und Automatisierung
- jeder lokale Modul-Dienst ist einzeln start-, stopp- und restartbar
- fail-closed Benutzer- und Rollenmodell mit Modul-/Tool-Allowlisten
- SQLite-Control-State im WAL-Modus mit Audit-Log, Chat-Sessions und Jobs
- SSE-Streaming fuer Chat-Antworten
- eigenstaendige Chat- und Admin-Web-App mit persistenten Sitzungen
- manifestbasierte MCP-Pakete und Instanzen mit Upgrade/Rollback
- verwaltete lokale/Git-Dokumentquellen mit Qualitaets-Gate
- persistente SQLite-Jobqueue mit separatem Worker
- Prometheus-Metriken, Online-Backup/Restore und Production-Preflight

## Produktionsstatus

Harbor blockiert geschuetzte Endpunkte, solange kein initialer Admin existiert:

```bash
./harbor.sh cli init-admin --username admin
```

Vor einem Rollout:

```bash
./harbor.sh cli production-check
./harbor.sh cli backup create --label pre-release
```

Ein externer Bind ist nur hinter einem TLS-Reverse-Proxy vorgesehen. Lokale Worker
werden mit einem automatisch erzeugten internen Bearer-Token abgesichert.

Architektur und Betrieb:

- `docs/ARCHITECTURE.md`
- `docs/OPERATIONS.md`
- `docs/PRODUCT.md`
- `docs/SLO.md`
- `docs/SECURITY.md`
- `docs/PRIVACY.md`
- `docs/INSTALL.md`
- `docs/HOWTO.md`
- `docs/UPGRADE.md`
- `docs/RUNBOOK.md`
- `docs/RELEASE_NOTES_v0.3.3.md`

## Struktur

```text
woddi-harbor/
  app/
    cli.py
    config.py
    control.py
    llm.py
    modules.py
    search.py
  config/
    harbor.json
    modules.json
    system_prompt.txt
  data/
    logs/
    runtime/
```

## Schnellstart

```bash
cd /srv/http/woddi-harbor
./harbor.sh console
```

Das Skript erledigt:

- OS-Hinweise fuer `Ubuntu` und `SLES`
- Erzeugung der virtuellen Umgebung
- Installation in die venv
- Initialisierung der Harbor-Konfiguration
- Start der interaktiven Harbor-Konsole

Von dort aus kannst du schrittweise:

- das externe LLM konfigurieren
- lokale Docs- und Mail-Module anlegen
- MCP-HTTP-Dienste einbinden
- Module starten, stoppen, restarten und testen
- beim ersten Start ein gefuehrtes Onboarding durchlaufen
- den System-Prompt anpassen
- Host und Port aendern
- systemd-Units fuer Harbor oder lokale Module vorbereiten

Die interaktive Konsole ist das zentrale Operations-Deck:

- linke Navigationsspalte fuer Module und Services
- Karten fuer LLM, Server und Prompt
- Detailpaneel fuer das aktuell ausgewaehlte Modul
- Event-Log im unteren Bereich
- Hotkeys fuer die haeufigen Aktionen

Wichtige Tasten in der Konsole:

- `a` Modul anlegen
- `l` LLM konfigurieren
- `p` Prompt aendern
- `v` Server host/port aendern
- `s` Modul starten
- `x` Modul stoppen
- `d` Modul restarten
- `c` Modul aufrufen
- `g` Logs ansehen
- `u` User-systemd-Unit fuer aktuelles Ziel installieren
- `e` systemd-Service aktivieren
- `z` systemd-Status anzeigen
- `Backspace` Modul entfernen
- `r` Ansicht aktualisieren
- `q` beenden

Wenn du stattdessen direkt nur die API starten willst:

```bash
./harbor.sh start
```

Wenn du lieber manuell arbeiten willst:

```bash
cd /srv/http/woddi-harbor
python3 -m venv .venv
.venv/bin/python -m pip install -e .

woddi-harbor init
woddi-harbor llm set --base-url http://<LLM-HOST>:<PORT>/v1 --model <MODEL> --api-key-env HARBOR_LLM_API_KEY
woddi-harbor module add-docs docs-local /pfad/zur/dokumentation
woddi-harbor module add-maildir mails-local /pfad/zu/maildirs
woddi-harbor module add-mcp netbox http://127.0.0.1:9010

woddi-harbor module start docs-local
woddi-harbor module start mails-local
woddi-harbor serve --host 127.0.0.1 --port 9680
```

Wenn du die virtuelle Umgebung interaktiv aktivieren willst:

```bash
# bash / zsh
. .venv/bin/activate

# fish
source .venv/bin/activate.fish
```

Praktische Wrapper-Aufrufe:

```bash
./harbor.sh bootstrap
./harbor.sh install
./harbor.sh console
./harbor.sh cli init-admin --username admin
./harbor.sh cli onboard --llm-base-url http://<LLM-HOST>:<PORT>/v1 --llm-model <MODEL>
./harbor.sh cli status
./harbor.sh cli module check docs-local
./harbor.sh cli module reindex docs-local
./harbor.sh cli module discover netbox
./harbor.sh cli module diagnose netbox
./harbor.sh cli module test netbox
./harbor.sh cli module add-netbox-mcp netbox --endpoint http://127.0.0.1:8000/mcp
./harbor.sh cli service check harbor
./harbor.sh cli llm set --base-url http://<LLM-HOST>:<PORT>/v1 --model <MODEL>
```

Generische Pflege bestehender Module:

```bash
./harbor.sh cli module set netbox --provider netbox-mcp-server --remote-protocol mcp --base-url http://127.0.0.1:8000/mcp
./harbor.sh cli module set netbox --test-action get_objects --test-payload '{"object_type":"dcim.devices","limit":1}' --test-expect-contains device
./harbor.sh cli module remove netbox
```

## Linux-Kompatibilitaet

`woddi-harbor` ist bewusst auf einen einfachen Linux-Stack reduziert:

- Python `3.10+`
- `venv`
- `git`
- `curl`
- optional `systemd` fuer Service-Betrieb

Schneller Vorab-Check:

```bash
woddi-harbor check-prerequisites
```

Ubuntu/Debian Bootstrap:

```bash
cd /srv/http/woddi-harbor
bash scripts/bootstrap_ubuntu.sh
```

SLES Bootstrap:

```bash
cd /srv/http/woddi-harbor
bash scripts/bootstrap_sles.sh
```

Danach wie gewohnt:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## systemd

Fuer produktionsnahen Betrieb auf `Ubuntu` oder `SLES` kann Harbor User- oder System-Units schreiben.

Profiles ansehen:

```bash
woddi-harbor service list
```

Harbor als User-Service installieren:

```bash
woddi-harbor service install harbor --mode user --enable --start
woddi-harbor service check harbor
```

Lokales Modul als User-Service installieren:

```bash
woddi-harbor service install module:docs-local --mode user --enable --start
woddi-harbor module check docs-local
woddi-harbor service check module:docs-local
```

Der gleiche Flow funktioniert mit `--mode system`, sofern du die noetigen Rechte hast.

## Auth

Die HTTP-Control-Plane kann mit lokalen Benutzern und Rollen abgesichert werden:

- `admin`
- `operator`
- `viewer`

Initialen Admin anlegen:

```bash
woddi-harbor init-admin --username admin
```

Weitere Benutzer verwalten:

```bash
woddi-harbor user list
woddi-harbor user add alice --role operator
woddi-harbor user set-role alice admin
woddi-harbor user disable alice
```

Sobald mindestens ein Benutzer existiert, erwarten die API-Endpunkte HTTP Basic Auth.

Beispiel:

```bash
curl -u admin:SECRET http://127.0.0.1:9680/api/modules
curl -u admin:SECRET -X POST http://127.0.0.1:9680/api/modules/docs-local/start
```

## NetBox MCP Integration

`woddi-harbor` kann standardkonforme HTTP-MCP-Endpunkte unter `/mcp` direkt discovery-en und Tools aufrufen. Das passt zum offiziellen `netbox-mcp-server` von NetBox Labs.

Schnellstart:

```bash
./harbor.sh cli module add-netbox-mcp netbox --endpoint http://127.0.0.1:8000/mcp
./harbor.sh cli module discover netbox
./harbor.sh cli module test netbox
./harbor.sh cli module call netbox get_objects '{"object_type":"dcim.devices","limit":5}'
```

Erwarteter Upstream laut Projekt-README:

- HTTP-Transport aktivieren
- Endpunkt auf `/mcp`
- Tools wie `get_objects`, `get_object_by_id`, `get_changelogs`

Mehr Details zum Upstream-Projekt:

- https://github.com/netboxlabs/netbox-mcp-server

## Eigene MCP-Pakete

Ein lokales Paket enthaelt `mcp-package.json`:

```json
{
  "id": "example-mcp",
  "version": "1.0.0",
  "driver": "process",
  "command": ["bin/example-mcp"],
  "tools": ["search"]
}
```

Lifecycle:

```bash
woddi-harbor mcp install /path/to/example-mcp
woddi-harbor mcp create example --package-id example-mcp --version 1.0.0
woddi-harbor mcp start example
woddi-harbor mcp upgrade example --version 1.1.0
woddi-harbor mcp rollback example
woddi-harbor mcp stop example
```

Die Registry kennt `http`, `process`, `systemd` und `container`. Direkt lokal
ausgefuehrt werden derzeit `http` und `process`; `systemd` und `container`
benoetigen freigegebene Betriebsprofile.

Chat:

```bash
woddi-harbor chat "Welche Billing-Hinweise finde ich in der Doku?"
```

Direkter MCP-Aufruf:

```bash
woddi-harbor module call netbox health '{}'
woddi-harbor module call netbox search '{"query":"router"}'
```

Remote-MCP-Capabilities pruefen:

```bash
woddi-harbor module discover netbox
```

Lokale Suchindizes gezielt neu bauen:

```bash
woddi-harbor module reindex docs-local
woddi-harbor module reindex maildir-local
```

## API

- `GET /api/health`
- `GET /api/modules`
- `POST /api/modules/{module_id}/start`
- `POST /api/modules/{module_id}/stop`
- `POST /api/modules/{module_id}/restart`
- `POST /api/modules/{module_id}/execute`
- `POST /api/chat`

## Modul-Typen

### `docs`

Indexiert Textdateien aus einem lokalen Verzeichnis und beantwortet Suchanfragen performant ueber einen simplen In-Memory-Index.

### `maildir`

Durchsucht lokale Mail-Verzeichnisse (`Maildir` oder einfache `.eml`-Sammlungen) nach Betreff, Absender und Inhalt.

### `mcp_http`

Spricht einen externen MCP- oder MCP-aehnlichen HTTP-Dienst an. Harbor behandelt diese Definitionen als verwaltbare Integrationen; Restart ist hier sinnvollerweise Sache des Ziel-Diensts selbst.

Wichtig:

- ein Token ist optional
- Harbor versucht Discovery und Health sowohl fuer offene als auch fuer geschuetzte HTTP-Dienste
- Discovery prueft mehrere gaengige Muster wie `/health`, `/capabilities`, `/.well-known/mcp` und `POST /execute`

## Namensvorschlag

Ich wuerde die AI selbst `Harbor` nennen.

Weitere gute Namen, falls du spaeter anders branden willst:

- `Quay`
- `Helm`
- `Keel`
- `Northstar`

`Harbor` ist fuer dein Setup der beste Fit, weil es nach Kontrollpunkt, Andockstelle und Service-Orchestrierung klingt statt nach Chatbot-Spielzeug.
