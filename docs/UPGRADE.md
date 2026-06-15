# Upgrade Procedure

1. Release Notes und Datenbankmigrationen lesen.
2. `./harbor.sh backup create --label pre-upgrade` ausfuehren.
3. Services stoppen und das neue Release installieren.
4. `./harbor.sh production-check` ausfuehren. Dabei werden Migrationen atomar
   angewendet.
5. Job-Worker, Modul-Services und API starten.
6. Readiness, Metriken, Chat-Streaming, Quellensuche und MCP-Tests pruefen.

Ein Rollback erfolgt auf die vorherige Anwendungsversion und das Pre-Upgrade-Backup.
Vor einem Restore wird automatisch ein weiteres Safety-Backup erzeugt.
