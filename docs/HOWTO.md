# Harbor How-to

## 1. Installieren

```bash
git clone https://github.com/woddi81-beep/woddi-harbor.git
cd woddi-harbor
git checkout main
git pull --ff-only
./harbor.sh version
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
./harbor.sh init-admin --username admin
```

Ohne aktiven Administrator bleiben geschützte Endpunkte gesperrt.

## 3. Externes LLM konfigurieren

Ollama:

```bash
./harbor.sh llm set \
  --base-url http://LLM-SERVER:11434 \
  --model llama3:8b
```

OpenAI-kompatibler Endpunkt mit Secret aus einer Umgebungsvariable:

```bash
export HARBOR_LLM_API_KEY='...'
./harbor.sh llm set \
  --base-url http://LLM-SERVER:8000/v1 \
  --model MODEL \
  --api-key-env HARBOR_LLM_API_KEY
```

## 4. Dokumentquellen importieren

Die beiden produktiven Markdown-Repositories werden direkt als lokale Quellen
konfiguriert:

```bash
./harbor.sh source configure-docs \
  --operations-path /opt/woddi-ai/doku/documentation-operation-main \
  --customer-path /opt/woddi-ai/doku/documentation-customer-main
./harbor.sh source sync operation-docs
./harbor.sh source sync customer-docs
```

Harbor kopiert `.md`, `.markdown`, `.html`, `.htm` und `.png` in seine verwalteten
Dokumentverzeichnisse. HTML wird als sichtbarer Text indexiert; PNG-Dateien bleiben
als zugehoerige Assets erhalten und werden nicht als Binaertext indexiert. Die
hostspezifischen Pfade werden in der nicht versionierten Datei
`config/sources.local.json` gespeichert. Anschliessend:

```bash
./harbor.sh source list
```

Der Reindex lokaler Dokumentmodule erfolgt direkt und benoetigt keinen laufenden
Harbor- oder Modul-Worker. Das gilt ebenfalls fuer `search`, `stats` und andere
Aufrufe lokaler Docs- und Maildir-Module.

Für eine Git-Quelle wird in `config/sources.json` ein Eintrag mit `kind: "git"`,
`repository`, `branch`, `target_path` und der zugehörigen `module_id` angelegt.

## 5. Benutzer verwalten

```bash
./harbor.sh user add alice --role viewer
./harbor.sh user set-role alice operator
./harbor.sh user set-permissions alice \
  --modules 10,11 \
  --tools search
./harbor.sh user passwd alice
./harbor.sh user disable alice
```

Alternativ stehen diese Funktionen im Admin-Portal unter `/admin` bereit.

## 6. NetBox und OpenStack prüfen

NetBox ohne Token:

```bash
./harbor.sh module add-netbox-mcp netbox \
  --netbox-url http://NETBOX-SERVER
./harbor.sh module start netbox
./harbor.sh module diagnose netbox
```

Bei `Errno 111` den Log-Auszug in der strukturierten Diagnose prüfen. Der Fehler
bedeutet, dass der lokale Worker nicht lauscht oder beim Start beendet wurde.

OpenStack wird im Admin-Portal unter **Module**, **OpenStack einbinden**
konfiguriert. Ein bereits projektgescoptes User-Token und die Identity/Auth URL
sind Pflichtfelder. Harbor liest Projekt-ID und Projektname ausschließlich aus
dem Token und führt kein Rescoping durch. Ungescopte Tokens werden abgewiesen.
Der Timeout gilt für Authentifizierung und Service-Abfragen. Danach:

```bash
.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("openstacksdk"))'
./harbor.sh module start openstack
./harbor.sh module discover openstack
./harbor.sh module test openstack
```

## 7. Module verwalten

```bash
./harbor.sh module list
./harbor.sh module start 10
./harbor.sh module test 10
./harbor.sh module reindex 10
./harbor.sh module diagnose 10
./harbor.sh module stop 10
```

## 8. MCP-Paket installieren und steuern

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
./harbor.sh mcp install /pfad/example-mcp
./harbor.sh mcp create example \
  --package-id example-mcp \
  --version 1.0.0
./harbor.sh mcp start example
./harbor.sh mcp restart example
./harbor.sh mcp stop example
```

Das mitgelieferte eigene MCP kann ohne weitere Python-Abhaengigkeiten
End-to-End betrieben werden:

```bash
./harbor.sh mcp install examples/harbor-ops-mcp
./harbor.sh mcp create harbor-ops \
  --package-id harbor-ops-mcp --version 1.0.0 \
  --config-json '{"env":{"MCP_PORT":"61000"}}'
./harbor.sh mcp start harbor-ops
./harbor.sh module add-mcp harbor-ops-tools \
  http://127.0.0.1:61000/mcp --remote-protocol mcp
./harbor.sh module discover harbor-ops-tools
./harbor.sh module call harbor-ops-tools harbor_echo \
  --payload '{"message":"Harbor MCP E2E"}'
```

Upgrade und Rollback:

```bash
./harbor.sh mcp install /pfad/example-mcp-1.1.0
./harbor.sh mcp upgrade example --version 1.1.0
./harbor.sh mcp rollback example
```

## 8. Services starten

systemd ist optional. Fuer manuellen Betrieb genuegen `./harbor.sh start` und
`./harbor.sh console`. Eine User-Installation fuer Dauerbetrieb:

Fuer direkten Zugriff aus einem geschuetzten Netz:

```bash
./harbor.sh server set --host 0.0.0.0 --port 9680
./harbor.sh server show
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
./harbor.sh service check harbor
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
`__HARBOR_HOSTNAME__` ersetzen. `./harbor.sh start` bindet Harbor standardmäßig
auf `127.0.0.1:9680`. Ein externer Bind muss bewusst konfiguriert und durch
Firewall sowie TLS-Reverse-Proxy geschützt werden.

Ohne oeffentlichen DNS-Namen kann `deploy/Caddyfile.local` fuer
`https://localhost:9443` verwendet werden. Es nutzt eine interne Caddy-CA; ohne
lokal importierte CA zeigt der Browser erwartungsgemaess eine Zertifikatswarnung.

Nach Aktivierung prüfen:

```bash
curl -fsS https://HARBOR-HOST/api/health
```

## 10. Backup und Restore

```bash
./harbor.sh backup create --label manual
./harbor.sh backup restore \
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
./harbor.sh production-check
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/ruff check app tests tools
.venv/bin/python tools/security_check.py
```

Ein fehlerhafter `production-check` blockiert den Rollout. Insbesondere müssen reale
Dokumentquellen vorhanden und gesund sein.
