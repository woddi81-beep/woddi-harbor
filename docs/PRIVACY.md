# Privacy and Data Handling

Harbor speichert Benutzerkonten, Audit-Ereignisse, Jobdaten und Chatverlaeufe lokal in
`data/runtime/harbor.db`. Dokumentquellen und Suchindizes liegen unter `data/`.

Chat-Inhalte und ausgewaehlter Modulkontext werden an den konfigurierten LLM-Server
gesendet. Betreiber muessen vor Produktivbetrieb klaeren, welche Daten dieser Server
verarbeiten darf.

Empfohlene Aufbewahrung:

- Chat-Sitzungen: 90 Tage oder kuerzer.
- Audit-Daten: 180 Tage.
- Jobdaten: 30 Tage.
- Backups: 14 Tagesstaende und 3 Monatsstaende.

Loeschanfragen werden durch Entfernen der betroffenen Chat-Sitzungen und anschliessende
Backup-Rotation umgesetzt. Quellenverantwortliche muessen personenbezogene Daten vor
dem Import minimieren.
