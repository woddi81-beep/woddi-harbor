# Production Installation

## Voraussetzungen

Linux, Python 3.10+, lokaler SSD/NVMe-Speicher und ein erreichbarer
OpenAI-kompatibler oder Ollama-LLM-Endpunkt. systemd, Reverse-Proxy und Monitoring
sind optionale Betriebsbausteine.

```bash
cd /srv/http/woddi-harbor
scripts/install_production.sh manual
./harbor.sh init-admin --username admin
./harbor.sh llm set \
  --base-url http://LLM-SERVER:11434 --model MODEL
./harbor.sh source list
./harbor.sh production-check
./harbor.sh start
```

Die manuelle Installation schreibt und startet keine systemd-Units. Fuer einen
explizit gewuenschten Dauerbetrieb stehen die Modi `user` und `system` bereit.
Reverse-Proxy/TLS und Prometheus/Grafana koennen unabhaengig davon aktiviert werden.

Standardmaessig bleibt der API-Prozess auf `127.0.0.1:9680`. In einem geschuetzten
Netz kann er explizit mit
`./harbor.sh server set --host 0.0.0.0 --port 9680` auf allen IPv4-Interfaces
bereitgestellt werden.
