# Production Installation

## Voraussetzungen

Linux, Python 3.10+, systemd, lokaler SSD/NVMe-Speicher, Reverse-Proxy und ein
erreichbarer OpenAI-kompatibler oder Ollama-LLM-Endpunkt.

```bash
cd /srv/http/woddi-harbor
scripts/install_production.sh user
.venv/bin/woddi-harbor init-admin --username admin
.venv/bin/woddi-harbor llm set \
  --base-url http://LLM-SERVER:11434 --model MODEL
.venv/bin/woddi-harbor source list
.venv/bin/woddi-harbor production-check
```

Installiere anschließend `woddi-harbor-jobs.service.tpl` und den Backup-Timer im
gleichen systemd-Scope. Rendere entweder `deploy/nginx.conf.tpl` oder
`deploy/Caddyfile`, aktiviere TLS und importiere die Prometheus-/Grafana-Vorlagen.

Der API-Prozess bleibt auf `127.0.0.1:9680`. Ein direktes öffentliches Binding ist
nicht vorgesehen.
