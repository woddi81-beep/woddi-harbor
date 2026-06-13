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
- lokale Docs- und Maildir-Suche
- optionale Adapter fuer externe NetBox-, OpenStack- und Dokumentdienste
- Standard-MCP ueber HTTP `/mcp`
- interne Worker-Kommunikation mit Bearer-Token
- eigene MCP-Pakete ueber manifestbasierte Lifecycle-Registry

## MCP-Referenzdesign

### NetBox

- Referenz: `netboxlabs/netbox-mcp-server` (Apache-2.0).
- Harbors lokaler Worker ist konsequent read-only und erlaubt auch im
  generischen Endpoint nur `GET`.
- `fields` reduziert NetBox-Antworten vor der Uebergabe an das LLM.
- `discover_object_types` liest Core- und Plugin-Collections aus dem
  laufzeitabhängigen OpenAPI-Schema.
- `describe_object_type` kombiniert Schemafelder, Query-Filter und an einem
  Live-Objekt beobachtete Felder.
- `get_inventory_statistics` nutzt paginierte Collection-Counts ohne das
  gesamte Inventar zu laden.
- Pagination bleibt auf dem konfigurierten Origin und unterhalb von `/api/`.
- Seitenzahl, Seitengroesse, Gesamtresultate und Antwortgroesse sind begrenzt.

### OpenStack

- Referenzen: offizielles `openstacksdk` und die Sicherheitsmuster aus
  `call518/MCP-OpenStack-Ops`.
- `discover_resources` prueft jeden erlaubten Service isoliert und meldet
  nicht aktivierte Dienste, ohne die gesamte Discovery abzubrechen.
- `get_storage_statistics` normalisiert Cinder-Limits in `used`, `limit`,
  `available` und `percent`; unlimitierte Quoten werden explizit markiert.
- `get_project_statistics` aggregiert Statusverteilungen und Compute- sowie
  Storage-Quoten fuer das konfigurierte Projekt.
- Credentials werden projektgebunden verwendet; ungescopte Tokens werden nur
  bei genau einem erreichbaren Projekt automatisch gescoped.
- Alle registrierten Tools sind read-only. Mutationen werden nicht dynamisch
  aus SDK-Methoden abgeleitet.
- Ergebnisse werden begrenzt, optional per `fields` komprimiert und bekannte
  Secret-Felder vor der MCP-Antwort redigiert.

## Caching und Performance

- Dokument- und Mail-Suche: persistenter Query-Cache plus vorbereitete
  In-Memory-Suchindizes.
- Modul-Health: kurzer In-Memory-TTL, damit Dashboard-Polling keine Worker-Flut
  erzeugt.
- NetBox/OpenStack: thread-sicherer, begrenzter LRU/TTL-Cache; keine
  unbegrenzten Prozess-Caches.
- Dashboard: zwei Sekunden TTL fuer gebuendelte LLM-, Modul- und Systemdaten.
- HTTP: GZip fuer geeignete Antworten, revalidierbares Browser-Caching fuer
  statische Assets und `no-store` fuer API-/Metrikdaten.
- MCP-Aufrufe laufen aus den async Endpoints im Threadpool, damit synchrone
  SDK-/HTTP-Aufrufe den Event Loop nicht blockieren.

## Weboberflaechen

- Vanilla HTML/CSS/JavaScript ohne zusaetzlichen Build-Schritt.
- Gemeinsames responsives Designsystem fuer Chat und Administration.
- Token-Streaming wird pro Animation Frame gebuendelt, statt die komplette
  Antwort fuer jedes einzelne Token neu zu rendern.
- Sicherheitsheader erlauben nur lokale Skripte, Styles und Verbindungen.

## Prozessmodell

```text
Reverse Proxy/TLS
       |
Harbor API Worker (N)
       |
       +-- SQLite WAL: Control State
       +-- Remote LLM
       +-- lokale Worker oder MCP-Prozesse
       +-- externe MCP HTTP Server
```

## 128 GiB / vier CPU-Sockets

- `api_workers` anhand realer Lasttests festlegen, initial 4 bis 8.
- Indexierung und MCP-Prozesse getrennt von API-Workern betreiben.
- Prozesse bei Bedarf per CPU-Affinity/NUMA-Policy auf Sockets verteilen; systemd
  ist dafuer eine optionale Implementierung.
- Grosse Dokumentbestaende nicht in jedem API-Prozess laden. Suchindizes gehoeren
  in dedizierte Worker; bei weiterem Wachstum ist ein FTS-/Vektorbackend einzusetzen.
- Das externe LLM verhindert, dass Modellgewichte den Harbor-RAM belegen.

## Sicherheitsgrenzen

- Kein externer Klartext-HTTP-Betrieb.
- Keine Secrets in Modulstatus oder API-Antworten.
- Keine Shell-Ausfuehrung fuer MCP-Prozesspakete.
- Relative MCP-Executables duerfen das Paketverzeichnis nicht verlassen.
- Schreibende Browser-Requests muessen same-origin sein.
