# Security Model

- Alle geschuetzten Endpunkte verwenden lokale Benutzer, PBKDF2-Hashes und Rollen.
- Harbor bindet produktiv nur an Loopback und wird ueber einen TLS-Reverse-Proxy
  veroeffentlicht.
- Lokale Worker benoetigen einen internen Bearer-Token mit Dateimodus `0600`.
- OpenStack User-Tokens werden pro Harbor-Benutzer getrennt gespeichert, nur
  fuer dessen interne Requests an den Worker weitergegeben und nie von Status-
  Endpunkten zurueckgegeben.
- OpenStack SDK-Verbindungen und Antwort-Caches sind pro Harbor-Benutzer
  getrennt; eine Tokenrotation verwirft nur den betroffenen Benutzerkontext.
- Modul- und Tool-Allowlisten werden serverseitig erzwungen.
- MCP-Pakete werden vor Installation validiert; absolute Process-Executables und
  Pfadfluchten sind verboten.
- Secrets gehoeren in Umgebungsvariablen oder lokale, nicht versionierte Dateien.
- Schreibende Browser-Aufrufe mit fremdem Origin werden blockiert.
- Audit-Ereignisse dokumentieren administrative Aenderungen.

## Meldungen

Sicherheitsluecken werden privat an den Repository-Eigentuemer gemeldet. Ein Report
enthaelt betroffene Version, Reproduktion, Auswirkung und vorgeschlagene Abhilfe.

## Bekannte Grenze

HTTP Basic ist nur innerhalb einer TLS-Verbindung zulaessig. SSO/OIDC und zentraler
Secret-Store sind geplante Erweiterungen fuer groessere Installationen.
