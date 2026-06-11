# Harbor How-to

## 1. Installieren

```bash
git clone https://github.com/woddi81-beep/woddi-harbor.git
cd woddi-harbor
git checkout v0.2.0
scripts/install_production.sh user
```

Für systemweite Units wird das Skript mit ausreichenden Rechten und `system`
aufgerufen:

```bash
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

Lokale Inhalte in die konfigurierten Verzeichnisse kopieren:

```bash
rsync -a /pfad/operations-doku/ data/sources/documentation-operation/
rsync -a /pfad/kunden-doku/ data/sources/documentation-customer/
.venv/bin/woddi-harbor source list
.venv/bin/woddi-harbor source sync operation-docs
.venv/bin/woddi-harbor source sync customer-docs
```

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

Upgrade und Rollback:

```bash
.venv/bin/woddi-harbor mcp install /pfad/example-mcp-1.1.0
.venv/bin/woddi-harbor mcp upgrade example --version 1.1.0
.venv/bin/woddi-harbor mcp rollback example
```

## 8. Services starten

User-Installation:

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

## 9. TLS-Reverse-Proxy aktivieren

In `deploy/nginx.conf.tpl` oder `deploy/Caddyfile` den Platzhalter
`__HARBOR_HOSTNAME__` ersetzen. Harbor selbst bleibt auf `127.0.0.1:9680`.

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
