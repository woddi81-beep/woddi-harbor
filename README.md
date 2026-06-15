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
./harbor.sh init-admin --username admin
```

Vor einem Rollout:

```bash
./harbor.sh version
./harbor.sh production-check
./harbor.sh backup create --label pre-release
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
- `docs/RELEASE_NOTES_v0.6.1.md`

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

./harbor.sh init
./harbor.sh llm set --base-url http://<LLM-HOST>:<PORT>/v1 --model <MODEL> --api-key-env HARBOR_LLM_API_KEY
./harbor.sh module add-docs docs-local /pfad/zur/dokumentation
./harbor.sh module add-maildir mails-local /pfad/zu/maildirs
./harbor.sh module add-mcp netbox http://127.0.0.1:9010

./harbor.sh module start docs-local
./harbor.sh module start mails-local
./harbor.sh serve --host 127.0.0.1 --port 9680
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
./harbor.sh init-admin --username admin
./harbor.sh onboard --llm-base-url http://<LLM-HOST>:<PORT>/v1 --llm-model <MODEL>
./harbor.sh status
./harbor.sh module check docs-local
./harbor.sh module reindex docs-local
./harbor.sh module discover netbox
./harbor.sh module diagnose netbox
./harbor.sh module test netbox
./harbor.sh module add-netbox-mcp netbox --netbox-url https://netbox.example.com
./harbor.sh service check harbor
./harbor.sh llm set --base-url http://<LLM-HOST>:<PORT>/v1 --model <MODEL>
```

Generische Pflege bestehender Module:

```bash
./harbor.sh module set netbox --provider netbox-mcp-server --remote-protocol mcp --base-url http://127.0.0.1:8000/mcp
./harbor.sh module set netbox --test-action get_objects --test-payload '{"object_type":"dcim.devices","limit":1}' --test-expect-contains device
./harbor.sh module remove netbox
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
./harbor.sh check-prerequisites
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
./harbor.sh service list
```

Harbor als User-Service installieren:

```bash
./harbor.sh service install harbor --mode user --enable --start
./harbor.sh service check harbor
```

Lokales Modul als User-Service installieren:

```bash
./harbor.sh service install module:docs-local --mode user --enable --start
./harbor.sh module check docs-local
./harbor.sh service check module:docs-local
```

Der gleiche Flow funktioniert mit `--mode system`, sofern du die noetigen Rechte hast.

## Auth

Die HTTP-Control-Plane kann mit lokalen Benutzern und Rollen abgesichert werden:

- `admin`
- `operator`
- `viewer`

Initialen Admin anlegen:

```bash
./harbor.sh init-admin --username admin
```

Weitere Benutzer verwalten:

```bash
./harbor.sh user list
./harbor.sh user add alice --role operator
./harbor.sh user set-role alice admin
./harbor.sh user disable alice
```

Sobald mindestens ein Benutzer existiert, erwarten die API-Endpunkte HTTP Basic Auth.

Beispiel:

```bash
curl -u admin:SECRET http://127.0.0.1:9680/api/modules
curl -u admin:SECRET -X POST http://127.0.0.1:9680/api/modules/docs-local/start
```

## NetBox MCP Integration

`woddi-harbor` kann standardkonforme HTTP-MCP-Endpunkte unter `/mcp` direkt
discovery-en und Tools aufrufen. Der lokale Worker orientiert sich am offiziellen
`netboxlabs/netbox-mcp-server`: read-only, begrenzte Resultsets, native
`fields`-Selektion und optionale Plugin-Endpunkt-Erkennung.

Im Admin-Portal unter `/admin` im Bereich **Module** auf **NetBox einbinden**
klicken. Harbor verwendet bewusst keinen NetBox-Token, weil die Zielinstanz
anonym lesbar ist. Der lokale Adapter akzeptiert ausschließlich GET-Anfragen,
begrenzt Antwortgrößen und folgt keinen Links auf andere Hosts.

Schnellstart:

```bash
./harbor.sh module add-netbox-mcp netbox --netbox-url http://NETBOX-SERVER
./harbor.sh module start netbox
./harbor.sh module discover netbox
./harbor.sh module test netbox
./harbor.sh module call netbox discover_object_types '{}'
./harbor.sh module call netbox describe_object_type '{"object_type":"dcim.devices"}'
./harbor.sh module call netbox get_inventory_statistics '{}'
./harbor.sh module call netbox get_objects '{"object_type":"dcim.devices","limit":5,"fields":["id","name","status","site"]}'
```

`discover_object_types` ermittelt auch Collections installierter NetBox-Plugins.
`describe_object_type` trennt Felder aus dem OpenAPI-Schema von Feldern, die
an einem echten Objekt beobachtet wurden. So bleiben optionale und aktuell
unbelegte Felder erkennbar. `get_inventory_statistics` liefert kostengünstige
Gesamtzahlen für ausgewählte Collections.

Ein `Errno 111: Connection refused` bei der Diagnose bedeutet, dass der lokale
MCP-Worker nicht auf seinem konfigurierten Port antwortet. Ab v0.3.8 liefert
`module diagnose` dafür ein strukturiertes Ergebnis samt Start-Hinweis:

```bash
./harbor.sh module diagnose netbox
./harbor.sh module start netbox
./harbor.sh module diagnose netbox
```

Erwarteter Upstream laut Projekt-README:

- HTTP-Transport aktivieren
- Endpunkt auf `/mcp`
- Tools wie `discover_object_types`, `describe_object_type`, `get_objects`,
  `get_object_by_id`, `get_changelogs`

Mehr Details zum Upstream-Projekt:

- https://github.com/netboxlabs/netbox-mcp-server

## OpenStack MCP Integration

Im Admin-Portal unter `/admin` im Bereich **Module** auf **OpenStack einbinden**
klicken. Die Identity/Auth URL wird gemeinsam konfiguriert; Region und lokaler
Port sind optional. Jeder Harbor-Benutzer hinterlegt danach sein eigenes
projektgescoptes OpenStack User-Token direkt im Chat. Das Token wird nicht in
`config/modules.local.json` gespeichert und nie an den Browser zurückgegeben.

Projekt-ID und Projektname werden ausschließlich aus dem Token gelesen. Harbor
nimmt keine separaten Projektfelder an und führt kein Rescoping durch. Ein
ungescoptes Token oder ein Token ohne Service-Katalog wird klar abgewiesen.
SDK-Verbindungen und OpenStack-Caches sind pro Harbor-Benutzer getrennt.

Der OpenStack-Dialog enthält einen Timeout für Keystone- und Service-Aufrufe.
Für langsam erreichbare private Clouds sind `60` bis `120` Sekunden sinnvoll.

Harbor verwendet das OpenStack Python SDK direkt. Es gibt keinen externen
CLI-Prozess und keine Abhängigkeit von `PATH` oder Symlinks:

```bash
.venv/bin/python -m pip install -e . --no-build-isolation
.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("openstacksdk"))'
./harbor.sh module start openstack
export OS_TOKEN='PROJEKTGESCOPTES_USER_TOKEN'
./harbor.sh module discover openstack
./harbor.sh module test openstack
./harbor.sh module call openstack discover_resources '{}'
./harbor.sh module call openstack get_storage_statistics '{}'
./harbor.sh module call openstack get_project_statistics '{}'
./harbor.sh module call openstack list_servers '{}'
unset OS_TOKEN
```

Die OpenStack-Werkzeuge sind auf lesende `list`- und `show`-Operationen begrenzt.
Der Funktionsumfang orientiert sich an den sicheren Mustern von
`call518/MCP-OpenStack-Ops`, verwendet aber Harbors eigene kleine,
projektgebundene Implementierung auf Basis des offiziellen `openstacksdk`.
Abgedeckt sind Compute, Netzwerk, Floating IPs, Security Groups, Block Storage,
Heat, Octavia, Availability Zones und Compute Limits. `discover_resources`
zeigt pro verfügbarem Service die tatsächlich beobachteten Felder.
`get_storage_statistics` berechnet Cinder-Auslastung in Prozent und ergänzt
Volumen-, Snapshot- und Backup-Status sowie provisionierte GiB.
`get_project_statistics` fasst Inventar und Compute-/Storage-Quoten zusammen.

Im Admin-Portal zeigt die Aktion **Discovery** auf jeder Modulkarte die
erkannten MCP-Tools und Capabilities direkt an.

Mehr Details zu den Referenzen:

- https://github.com/call518/MCP-OpenStack-Ops
- https://github.com/openstack/openstacksdk

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
./harbor.sh mcp install /path/to/example-mcp
./harbor.sh mcp create example --package-id example-mcp --version 1.0.0
./harbor.sh mcp start example
./harbor.sh mcp upgrade example --version 1.1.0
./harbor.sh mcp rollback example
./harbor.sh mcp stop example
```

Die Registry kennt `http`, `process`, `systemd` und `container`. Direkt lokal
ausgefuehrt werden derzeit `http` und `process`; `systemd` und `container`
benoetigen freigegebene Betriebsprofile.

Chat:

```bash
./harbor.sh chat "Welche Billing-Hinweise finde ich in der Doku?"
```

Direkter MCP-Aufruf:

```bash
./harbor.sh module call netbox health '{}'
./harbor.sh module call netbox search '{"query":"router"}'
```

Remote-MCP-Capabilities pruefen:

```bash
./harbor.sh module discover netbox
```

Lokale Suchindizes gezielt neu bauen:

```bash
./harbor.sh module reindex docs-local
./harbor.sh module reindex maildir-local
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
