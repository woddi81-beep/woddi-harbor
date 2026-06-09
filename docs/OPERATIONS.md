# Harbor Operations

## Installation

```bash
./harbor.sh install
./harbor.sh cli init
./harbor.sh cli init-admin --username admin
./harbor.sh cli production-check
```

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

## Lasttest

```bash
.venv/bin/python tools/load_test.py \
  --url http://127.0.0.1:9680/api/health \
  --requests 10000 --concurrency 64
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
