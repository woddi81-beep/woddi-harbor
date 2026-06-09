# Harbor Architektur

## Control Plane

- FastAPI API und Weboberflaeche
- fail-closed Basic Auth hinter TLS-Reverse-Proxy
- Rollen `viewer`, `operator`, `admin`
- Modul- und Tool-Allowlisten pro Benutzer
- SQLite im WAL-Modus fuer Audit, Sessions, Jobs und MCP-Registry
- JSON-Dateien als kompatible Bootstrap- und Exportkonfiguration

## Data Plane

- Remote OpenAI-kompatibles LLM
- getrennte lokale Docs-, Maildir-, NetBox-, OpenStack- und SAP-Docs-Worker
- Standard-MCP ueber HTTP `/mcp`
- interne Worker-Kommunikation mit Bearer-Token
- eigene MCP-Pakete ueber manifestbasierte Lifecycle-Registry

## Prozessmodell

```text
Reverse Proxy/TLS
       |
Harbor API Worker (N)
       |
       +-- SQLite WAL: Control State
       +-- Remote LLM
       +-- lokale Worker/systemd
       +-- externe MCP HTTP Server
```

## 128 GiB / vier CPU-Sockets

- `api_workers` anhand realer Lasttests festlegen, initial 4 bis 8.
- Indexierung und MCP-Prozesse getrennt von API-Workern betreiben.
- Prozesse bei Bedarf pro NUMA-Node mit systemd `CPUAffinity` und
  `NUMAPolicy=preferred` verteilen.
- Grosse Dokumentbestaende nicht in jedem API-Prozess laden. Suchindizes gehoeren
  in dedizierte Worker; bei weiterem Wachstum ist ein FTS-/Vektorbackend einzusetzen.
- Das externe LLM verhindert, dass Modellgewichte den Harbor-RAM belegen.

## Sicherheitsgrenzen

- Kein externer Klartext-HTTP-Betrieb.
- Keine Secrets in Modulstatus oder API-Antworten.
- Keine Shell-Ausfuehrung fuer MCP-Prozesspakete.
- Relative MCP-Executables duerfen das Paketverzeichnis nicht verlassen.
- Schreibende Browser-Requests muessen same-origin sein.
