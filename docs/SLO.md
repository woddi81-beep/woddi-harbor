# Service Level Objectives

## Zielwerte

- Verfuegbarkeit der Harbor-Control-Plane: 99,5 Prozent pro Kalendermonat.
- `GET /api/health`: p95 unter 100 ms, p99 unter 250 ms.
- Nicht-LLM-API-Aufrufe: p95 unter 500 ms bei 64 parallelen Verbindungen.
- Harbor-interne Fehlerrate: unter 1 Prozent.
- Job-Annahme: innerhalb von 5 Sekunden; Start eines Jobs innerhalb von 60 Sekunden.
- Recovery Point Objective: 24 Stunden.
- Recovery Time Objective: 2 Stunden.

LLM-Tokenlatenz und externe MCP-Laufzeiten werden getrennt ausgewiesen, da Harbor
diese Upstreams nicht kontrolliert.

## Kapazitaetsprofil

Fuer den Zielhost mit 128 GB RAM und vier CPU-Sockets startet Harbor mit vier
API-Workern und einem Job-Worker. Die Workerzahl wird erst nach NUMA- und Lastmessung
erhoeht. Suchindizes liegen auf lokalem SSD/NVMe-Speicher; das LLM laeuft extern.

## Messung

Prometheus liest `/metrics`. Das Lastprofil in `tools/load_profiles.sh` ist vor jedem
Produktionsrelease auf dem Zielhost auszufuehren. Das Profil beginnt mit einem
Hardware-Gate fuer mindestens 128 GiB RAM und vier CPU-Sockets. Ein verletzter
Grenzwert blockiert das Release und darf nicht durch einen Test auf Ersatzhardware
ersetzt werden.
