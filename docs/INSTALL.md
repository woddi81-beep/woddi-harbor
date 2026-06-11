# Production Installation

## Voraussetzungen

Linux, Python 3.10+, lokaler SSD/NVMe-Speicher und ein erreichbarer
OpenAI-kompatibler oder Ollama-LLM-Endpunkt. systemd, Reverse-Proxy und Monitoring
sind optionale Betriebsbausteine.

```bash
cd /srv/http/woddi-harbor
scripts/install_production.sh manual
.venv/bin/woddi-harbor init-admin --username admin
.venv/bin/woddi-harbor llm set \
  --base-url http://LLM-SERVER:11434 --model MODEL
.venv/bin/woddi-harbor source list
.venv/bin/woddi-harbor production-check
./harbor.sh start
```

Die manuelle Installation schreibt und startet keine systemd-Units. Fuer einen
explizit gewuenschten Dauerbetrieb stehen die Modi `user` und `system` bereit.
Reverse-Proxy/TLS und Prometheus/Grafana koennen unabhaengig davon aktiviert werden.

Der API-Prozess bleibt auf `127.0.0.1:9680`. Ein direktes öffentliches Binding ist
nicht vorgesehen.
