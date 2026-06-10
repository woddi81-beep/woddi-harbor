# Operations Runbook

## Harbor nicht bereit

`woddi-harbor production-check` und `/api/ready` pruefen. Fehlende Benutzer, LLM-
Konfiguration, ungesunde Quellen oder Dateirechte zuerst beheben.

## Jobs bleiben queued

Status von `woddi-harbor-jobs.service` und `/api/jobs` pruefen. Ein abgestuerzter
`running`-Job wird nach 15 Minuten erneut freigegeben.

## Modul antwortet nicht

Im Admin-Portal Diagnose, Test und Logs aufrufen. Danach den einzelnen Modul-Service
neu starten; die Harbor-API muss dafuer nicht beendet werden.

## LLM nicht erreichbar

Netzpfad zum separaten LLM-Server, Modellname, Timeout und API-Key-Umgebung pruefen.
Harbor bleibt administrierbar, Chat-Anfragen liefern einen kontrollierten Fehler.

## Restore

API und Job-Worker stoppen, `woddi-harbor backup restore DATEI --yes` ausfuehren,
`production-check` starten und danach alle Services kontrolliert hochfahren.
