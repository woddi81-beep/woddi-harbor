# Harbor How-to

## 1. Installieren

```bash
git clone https://github.com/woddi81-beep/woddi-harbor.git
cd woddi-harbor
git checkout v0.3.7
scripts/install_production.sh manual
```

Die manuelle Installation richtet keine systemd-Units ein. Nur fuer einen bewusst
gewaehlten Dauerbetrieb wird das Skript mit `user` oder mit ausreichenden Rechten
und `system` aufgerufen:

```bash
scripts/install_production.sh user
sudo scripts/install_production.sh system
```

## 2. Ersten Administrator anlegen

```bash
.venv/bin/woddi-harbor init-admin --username admin
```

Ohne aktiven Administrator bleiben geschützte Endpunkte gesperrt.

## 3. Externes LLM konfigurieren

Ollama:

```bash
.venv/bin/woddi-harbor llm set \
  --base-url http://LLM-SERVER:11434 \
  --model llama3:8b
```

OpenAI-kompatibler Endpunkt mit Secret aus einer Umgebungsvariable:

```bash
export HARBOR_LLM_API_KEY='...'
.venv/bin/woddi-harbor llm set \
  --base-url http://LLM-SERVER:8000/v1 \
  --model MODEL \
  --api-key-env HARBOR_LLM_API_KEY
```

## 4. Dokumentquellen importieren

Die beiden produktiven Markdown-Repositories werden direkt als lokale Quellen
konfiguriert:

```bash
.venv/bin/woddi-harbor source configure-docs \
  --operations-path /opt/woddi-ai/doku/documentation-operation-main \
  --customer-path /opt/woddi-ai/doku/documentation-customer-main
.venv/bin/woddi-harbor source sync operation-docs
.venv/bin/woddi-harbor source sync customer-docs
```

Harbor kopiert `.md`, `.markdown`, `.html`, `.htm` und `.png` in seine verwalteten
Dokumentverzeichnisse. HTML wird als sichtbarer Text indexiert; PNG-Dateien bleiben
als zugehoerige Assets erhalten und werden nicht als Binaertext indexiert. Die
hostspezifischen Pfade werden in der nicht versionierten Datei
`config/sources.local.json` gespeichert. Anschliessend:

```bash
.venv/bin/woddi-harbor source list
```

Der Reindex lokaler Dokumentmodule erfolgt direkt und benoetigt keinen laufenden
Harbor- oder Modul-Worker. Das gilt ebenfalls fuer `search`, `stats` und andere
Aufrufe lokaler Docs- und Maildir-Module.

Für eine Git-Quelle wird in `config/sources.json` ein Eintrag mit `kind: "git"`,
`repository`, `branch`, `target_path` und der zugehörigen `module_id` angelegt.

## 5. Benutzer verwalten

```bash
.venv/bin/woddi-harbor user add alice --role viewer
.venv/bin/woddi-harbor user set-role alice operator
.venv/bin/woddi-harbor user set-permissions alice \
  --modules 10,11 \
  --tools search
.venv/bin/woddi-harbor user passwd alice
.venv/bin/woddi-harbor user disable alice
```

Alternativ stehen diese Funktionen im Admin-Portal unter `/admin` bereit.

## 6. Module verwalten

```bash
.venv/bin/woddi-harbor module list
.venv/bin/woddi-harbor module start 10
.venv/bin/woddi-harbor module test 10
.venv/bin/woddi-harbor module reindex 10
.venv/bin/woddi-harbor module diagnose 10
.venv/bin/woddi-harbor module stop 10
```

## 7. MCP-Paket installieren und steuern

Ein eigenes MCP-Paket enthält eine `mcp-package.json`. Beispiel für einen
prozessbasierten Server:

```json
{
  "id": "example-mcp",
  "version": "1.0.0",
  "driver": "process",
  "command": ["bin/example-mcp"],
  "tools": ["search", "status"]
}
```

Installation und Lifecycle:

```bash
.venv/bin/woddi-harbor mcp install /pfad/example-mcp
.venv/bin/woddi-harbor mcp create example \
  --package-id example-mcp \
  --version 1.0.0
.venv/bin/woddi-harbor mcp start example
.venv/bin/woddi-harbor mcp restart example
.venv/bin/woddi-harbor mcp stop example
```

Das mitgelieferte eigene MCP kann ohne weitere Python-Abhaengigkeiten
End-to-End betrieben werden:

```bash
.venv/bin/woddi-harbor mcp install examples/harbor-ops-mcp
.venv/bin/woddi-harbor mcp create harbor-ops \
  --package-id harbor-ops-mcp --version 1.0.0 \
  --config-json '{"env":{"MCP_PORT":"61000"}}'
.venv/bin/woddi-harbor mcp start harbor-ops
.venv/bin/woddi-harbor module add-mcp harbor-ops-tools \
  http://127.0.0.1:61000/mcp --remote-protocol mcp
.venv/bin/woddi-harbor module discover harbor-ops-tools
.venv/bin/woddi-harbor module call harbor-ops-tools harbor_echo \
  --payload '{"message":"Harbor MCP E2E"}'
```

Upgrade und Rollback:

```bash
.venv/bin/woddi-harbor mcp install /pfad/example-mcp-1.1.0
.venv/bin/woddi-harbor mcp upgrade example --version 1.1.0
.venv/bin/woddi-harbor mcp rollback example
```

## 8. Services starten

systemd ist optional. Fuer manuellen Betrieb genuegen `./harbor.sh start` und
`./harbor.sh console`. Eine User-Installation fuer Dauerbetrieb:

Fuer direkten Zugriff aus einem geschuetzten Netz:

```bash
.venv/bin/woddi-harbor server set --host 0.0.0.0 --port 9680
.venv/bin/woddi-harbor server show
./harbor.sh start
```

Der Port `9680/tcp` muss in der Host-Firewall fuer das geschuetzte Quellnetz
freigegeben sein. Ein externes Binding erzeugt im `production-check` bewusst eine
Warnung, aber keinen Fehler.

```bash
systemctl --user start woddi-harbor.service
systemctl --user start woddi-harbor-jobs.service
systemctl --user enable --now woddi-harbor-backup.timer
```

Status:

```bash
systemctl --user status woddi-harbor.service
systemctl --user status woddi-harbor-jobs.service
.venv/bin/woddi-harbor service check harbor
```

Alle Harbor-Komponenten beenden:

```bash
./harbor.sh stop
```

Alle von Harbor verwalteten User-Units, TLS und Monitoring entfernen, ohne
Dokumente, Chats, Konfiguration oder Backups zu loeschen:

```bash
./harbor.sh uninstall-runtime
```

## 9. TLS-Reverse-Proxy aktivieren

In `deploy/nginx.conf.tpl` oder `deploy/Caddyfile` den Platzhalter
`__HARBOR_HOSTNAME__` ersetzen. Harbor selbst bleibt auf `127.0.0.1:9680`.

Ohne oeffentlichen DNS-Namen kann `deploy/Caddyfile.local` fuer
`https://localhost:9443` verwendet werden. Es nutzt eine interne Caddy-CA; ohne
lokal importierte CA zeigt der Browser erwartungsgemaess eine Zertifikatswarnung.

Nach Aktivierung prüfen:

```bash
curl -fsS https://HARBOR-HOST/api/health
```

## 10. Backup und Restore

```bash
.venv/bin/woddi-harbor backup create --label manual
.venv/bin/woddi-harbor backup restore \
  data/backups/harbor-DATUM-manual.tar.gz --yes
```

Vor jedem Restore erstellt Harbor automatisch ein Safety-Backup.

## 11. Monitoring und Lasttest

Prometheus verwendet `deploy/prometheus.yml.tpl`; das Dashboard liegt unter
`deploy/grafana-dashboard.json`.

```bash
tools/load_profiles.sh http://127.0.0.1:9680
```

LLM- und MCP-Lasttests nur gegen dafür vorgesehene Test-Upstreams ausführen.

## 12. Produktionsfreigabe

```bash
.venv/bin/woddi-harbor production-check
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/ruff check app tests tools
.venv/bin/python tools/security_check.py
```

Ein fehlerhafter `production-check` blockiert den Rollout. Insbesondere müssen reale
Dokumentquellen vorhanden und gesund sein.
