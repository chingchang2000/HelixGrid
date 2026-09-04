# HelixGrid – einfache Anleitung auf Deutsch

## Was ist HelixGrid?

HelixGrid führt Workflows mit Abhängigkeiten auf Workern aus. Der **Coordinator** verwaltet Reihenfolge, Zustände, Retries und Leases. Die **Worker** führen die Befehle aus.

## 1. Voraussetzungen

- Git
- Docker mit Docker Compose
- Python 3.11+ für die CLI

## 2. Projekt herunterladen

```bash
git clone https://github.com/chingchang2000/app.git
cd app
```

## 3. HelixGrid starten

```bash
docker compose up --build --scale worker=3
```

Danach öffnen:

```text
http://127.0.0.1:8080/healthz
```

Die Antwort sollte `"status":"ok"` enthalten.

## 4. CLI installieren

Windows:

```powershell
py -m pip install -e .\sdk\python
```

Linux/macOS:

```bash
python3 -m pip install -e ./sdk/python
```

## 5. Beispiel ausführen

Windows:

```powershell
py -m helixgrid.cli submit examples\workflow.json --wait
```

Linux/macOS:

```bash
python3 -m helixgrid.cli submit examples/workflow.json --wait
```

Am Ende sollte `SUCCEEDED` stehen.

## 6. Nützliche Befehle

```bash
python3 -m helixgrid.cli list
python3 -m helixgrid.cli get WORKFLOW_ID
python3 -m helixgrid.cli workers
python3 -m helixgrid.cli watch WORKFLOW_ID
docker compose logs -f
docker compose down
```

Unter Windows kannst du `py` statt `python3` verwenden.

## 7. Eigener Workflow

```json
{
  "name": "mein-workflow",
  "tasks": [
    {
      "id": "hello",
      "command": ["sh", "-lc", "echo Hallo von HelixGrid"]
    },
    {
      "id": "done",
      "depends_on": ["hello"],
      "command": ["sh", "-lc", "echo Fertig"]
    }
  ]
}
```

Ausführen:

```bash
python3 -m helixgrid.cli submit my-workflow.json --wait
```

## 8. Tests

```bash
make test
make check
```

## 9. Speicherung

Der aktive Coordinator verwendet derzeit einen **In-Memory-Store**. Bei einem Neustart des Coordinators gehen Workflow- und Worker-Zustände verloren.

`storage/postgres/schema.sql` enthält das PostgreSQL-Design und wird in CI geprüft, ist aber noch nicht als aktive Coordinator-Datenbank angeschlossen.

## 10. Fehlerbehebung

Worker-Logs:

```bash
docker compose logs worker
```

Workflow prüfen:

```bash
python3 -m helixgrid.cli get WORKFLOW_ID
```

Docker prüfen:

```bash
docker compose config
```

## 11. Sicherheit

Worker führen die Befehle aus, die in Workflows stehen. Der Coordinator besitzt momentan keine Authentifizierung.

Port 8080 sollte deshalb nicht direkt öffentlich ins Internet gestellt werden, solange keine Authentifizierung und Netzwerksicherung ergänzt wurden.
