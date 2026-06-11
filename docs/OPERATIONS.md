# Harbor Operations

## Installation

```bash
scripts/install_production.sh manual
.venv/bin/woddi-harbor init-admin --username admin
.venv/bin/woddi-harbor production-check
./harbor.sh start
```

Im Standardbetrieb werden keine systemd-Units installiert. API und Konsole werden
mit `harbor.sh` gesteuert; `./harbor.sh stop` beendet auch Module, MCP-Prozesse und
Monitoring. User- oder System-Units sowie TLS und Monitoring sind explizite
Deployment-Optionen.

## Backup und Restore

```bash
./harbor.sh cli backup create --label daily
./harbor.sh cli backup restore data/backups/harbor-...tar.gz --yes
```

Vor Restore wird automatisch ein Safety-Backup erzeugt.

## Observability

- Liveness: `GET /api/health`
- Readiness: `GET /api/ready`
- Prometheus: `GET /metrics`, Admin-Authentifizierung erforderlich
- Audit: `GET /api/audit`
- Jobs: `GET /api/jobs`
- Quellen: `GET /api/sources`

## Lasttest

```bash
.venv/bin/python tools/load_test.py \
  --url http://127.0.0.1:9680/api/health \
  --requests 10000 --concurrency 64 \
  --max-error-rate 0.01 --max-p95-ms 100
```

Chat- und MCP-Lasttests muessen gegen Test-Upstreams laufen, nicht gegen produktive
externe Dienste oder LLM-Endpunkte.

## Release Gate

Ein Release ist nur zulaessig, wenn:

- `production-check` ohne Fehler endet,
- Tests und Compile-Check erfolgreich sind,
- Backup und Test-Restore geprueft wurden,
- p95/p99-Latenzen und Fehlerrate die vereinbarten SLOs erfuellen,
- die fuer das konkrete Deployment freigegebenen TLS- und Monitoring-Optionen
  geprueft wurden.
