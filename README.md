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
- generische MCP-HTTP-Anbindung fuer externe Dienste wie NetBox oder OpenStack
- CLI mit Rich-Ausgabe fuer Status, Konfiguration und Modulsteuerung
- FastAPI-Control-Plane fuer spaetere Oberflaechen und Automatisierung
- jeder lokale Modul-Dienst ist einzeln start-, stopp- und restartbar

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
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

woddi-harbor init
woddi-harbor llm set --base-url http://<LLM-HOST>:<PORT>/v1 --model <MODEL> --api-key-env HARBOR_LLM_API_KEY
woddi-harbor module add-docs docs-local /pfad/zur/dokumentation
woddi-harbor module add-maildir mails-local /pfad/zu/maildirs
woddi-harbor module add-mcp netbox http://127.0.0.1:9010

woddi-harbor module start docs-local
woddi-harbor module start mails-local
woddi-harbor serve --host 127.0.0.1 --port 9680
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
. .venv/bin/activate
pip install -e .
```

Chat:

```bash
woddi-harbor chat "Welche Billing-Hinweise finde ich in der Doku?"
```

Direkter MCP-Aufruf:

```bash
woddi-harbor module call netbox health '{}'
woddi-harbor module call netbox search '{"query":"router"}'
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

## Namensvorschlag

Ich wuerde die AI selbst `Harbor` nennen.

Weitere gute Namen, falls du spaeter anders branden willst:

- `Quay`
- `Helm`
- `Keel`
- `Northstar`

`Harbor` ist fuer dein Setup der beste Fit, weil es nach Kontrollpunkt, Andockstelle und Service-Orchestrierung klingt statt nach Chatbot-Spielzeug.
