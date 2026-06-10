# Harbor Operations

## Installation

```bash
scripts/install_production.sh user
.venv/bin/woddi-harbor init-admin --username admin
.venv/bin/woddi-harbor production-check
```

Die API, der persistente Job-Worker und der Backup-Timer laufen als getrennte
systemd-Units. TLS wird mit einer Vorlage unter `deploy/` terminiert.

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
NetBox-, OpenStack- oder LLM-Endpunkte.

## Release Gate

Ein Release ist nur zulaessig, wenn:

- `production-check` ohne Fehler endet,
- Tests und Compile-Check erfolgreich sind,
- Backup und Test-Restore geprueft wurden,
- p95/p99-Latenzen und Fehlerrate die vereinbarten SLOs erfuellen,
- Reverse-Proxy, TLS und Monitoring aktiv sind.
