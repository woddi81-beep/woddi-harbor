# Product Definition

## Zweck

Woddi Harbor ist eine selbst betriebene AI-Control-Plane fuer Organisationen, die ein
externes LLM mit internen Dokumenten, Infrastruktur-APIs und kontrollierten MCP-Tools
verbinden wollen.

## Nutzer

- Viewer fuehren Chats mit freigegebenen Modulen und Tools.
- Operatoren starten, stoppen, testen und reindexieren Module und MCP-Instanzen.
- Administratoren verwalten Benutzer, Berechtigungen, Quellen, Pakete, Backups,
  Services und Audit-Daten.

## Produktgrenzen

- Harbor betreibt kein LLM. Das Modell liegt auf einem separaten Server.
- Harbor ist kein Dokumentenmanagementsystem. Quellen werden importiert und indexiert.
- Harbor ist kein allgemeiner Container-Orchestrator. MCP-Lifecycle ist auf validierte
  Manifeste und die Driver `http`, `process`, `systemd` und `container` begrenzt.
- Hochverfuegbarkeit ueber mehrere Harbor-Hosts benoetigt eine spaetere externe
  Datenbank. Der aktuelle Produktionsmodus ist ein einzelner Host mit mehreren API-
  Prozessen.

## Definition of Done

Ein Standort gilt als produktiv, wenn `production-check` ohne Fehler endet, TLS und
Monitoring aktiv sind, alle benoetigten Quellen gesund sind, Backup und Restore
getestet wurden und die SLO-Lastprofile bestanden sind.
