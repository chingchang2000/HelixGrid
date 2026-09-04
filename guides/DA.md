# HelixGrid – nem guide på dansk

Denne guide viser den letteste måde at få HelixGrid til at køre, også hvis du ikke kender projektet i forvejen.

## Hvad er HelixGrid?

HelixGrid kører workflows med flere opgaver. En **coordinator** holder styr på rækkefølge, dependencies, retries og leases. En eller flere **workers** henter opgaver og kører kommandoerne.

Et workflow kan f.eks. være:

1. `prepare` kører først.
2. `test` venter på `prepare`.
3. `publish` venter på `test`.

## 1. Installer det nødvendige

Den nemmeste metode kræver:

- Git
- Docker med Docker Compose
- Python 3.11 eller nyere, hvis du vil bruge Helix CLI'en

På Windows er Docker Desktop normalt den letteste Docker-løsning.

## 2. Hent projektet

Åbn PowerShell, Terminal eller CMD:

```bash
git clone https://github.com/chingchang2000/app.git
cd app
```

Hvis du allerede har projektet:

```bash
git pull
```

## 3. Start HelixGrid

Kør fra projektmappen:

```bash
docker compose up --build --scale worker=3
```

Det starter:

- 1 coordinator
- 3 workers

Lad terminalen være åben.

Test derefter coordinatoren i browseren:

```text
http://127.0.0.1:8080/healthz
```

Du bør få JSON tilbage med `"status":"ok"`.

## 4. Installer den nemme CLI

Åbn en ny terminal i projektmappen.

### Windows

```powershell
py -m pip install -e .\sdk\python
```

### Linux/macOS

```bash
python3 -m pip install -e ./sdk/python
```

## 5. Kør det medfølgende eksempel

### Windows

```powershell
py -m helixgrid.cli submit examples\workflow.json --wait
```

### Linux/macOS

```bash
python3 -m helixgrid.cli submit examples/workflow.json --wait
```

Hvis alt virker, ender workflowet som `SUCCEEDED`.

## 6. De vigtigste kommandoer

Se alle workflows:

```bash
python3 -m helixgrid.cli list
```

Se ét workflow:

```bash
python3 -m helixgrid.cli get WORKFLOW_ID
```

Se workers:

```bash
python3 -m helixgrid.cli workers
```

Følg events fra et workflow:

```bash
python3 -m helixgrid.cli watch WORKFLOW_ID
```

Annuller et workflow:

```bash
python3 -m helixgrid.cli cancel WORKFLOW_ID
```

På Windows kan du bruge `py` i stedet for `python3`.

## 7. Lav dit eget workflow

Lav en fil der hedder `my-workflow.json`:

```json
{
  "name": "mit-forste-workflow",
  "tasks": [
    {
      "id": "hello",
      "command": ["sh", "-lc", "echo Hello fra HelixGrid"]
    },
    {
      "id": "done",
      "depends_on": ["hello"],
      "command": ["sh", "-lc", "echo Faerdig"]
    }
  ]
}
```

Kør den:

```bash
python3 -m helixgrid.cli submit my-workflow.json --wait
```

## 8. Hvad betyder de normale states?

- `PENDING` – venter på at blive klar.
- `READY` – kan leases af en worker.
- `LEASED` – en worker har fået opgaven.
- `RUNNING` – opgaven kører.
- `RETRY_WAIT` – opgaven fejlede og venter før nyt forsøg.
- `SUCCEEDED` – færdig uden fejl.
- `FAILED` – fejlede endeligt.
- `CANCELLED` – annulleret.

## 9. Se Docker-logs

Alle logs:

```bash
docker compose logs -f
```

Kun coordinator:

```bash
docker compose logs -f coordinator
```

Kun workers:

```bash
docker compose logs -f worker
```

## 10. Stop HelixGrid

```bash
docker compose down
```

## 11. Kør tests

Hvis du har de nødvendige udviklerværktøjer installeret:

```bash
make test
make check
```

GitHub Actions tester også Go, Rust, Python, TypeScript, C++, Java, protokoller, PostgreSQL-schema, Docker og en rigtig end-to-end workflow-kørsel.

## 12. Hvis noget ikke virker

### Port 8080 er optaget

Stop programmet der bruger port 8080, eller ændr port-mappingen i `docker-compose.yml`.

### Ingen worker tager opgaver

Kør:

```bash
docker compose logs worker
python3 -m helixgrid.cli workers
```

### Workflow står som FAILED

Se detaljerne:

```bash
python3 -m helixgrid.cli get WORKFLOW_ID
docker compose logs worker
```

### Docker starter ikke

Kontrollér først:

```bash
docker --version
docker compose version
docker compose config
```

## 13. Vigtigt om data

Den aktive coordinator bruger lige nu en **in-memory store**. Hvis coordinator-containeren genstarter, forsvinder workflow- og worker-state.

`storage/postgres/schema.sql` indeholder et gennemarbejdet PostgreSQL-design, og CI tester at schemaet kan indlæses, men coordinatoren er endnu ikke koblet til PostgreSQL som aktiv database.

## 14. Sikkerhed

HelixGrid-workers **kører de kommandoer, der står i workflows**. Coordinatoren har heller ikke login/API-authentication endnu.

Derfor bør du ikke åbne port 8080 direkte mod internettet på en maskine med vigtige filer. Brug HelixGrid lokalt eller på et beskyttet netværk, medmindre du selv tilføjer authentication og netværksbeskyttelse.

## 15. De avancerede dele

- `coordinator/` – Go coordinator, scheduler og API.
- `worker/` – Rust worker runtime.
- `sdk/python/` – Python SDK og CLI.
- `sdk/typescript/` – TypeScript SDK.
- `simulator/` – C++ scheduler-simulator.
- `tools/chaos_lab/` – chaos/property testing.
- `tools/replay-verifier/` – Java event-replay verifier.
- `storage/postgres/` – PostgreSQL persistence-design.
- `protocol/` – OpenAPI og JSON Schema.
