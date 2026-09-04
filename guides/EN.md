# HelixGrid – easy English guide

This guide shows the easiest way to run HelixGrid from scratch.

## What is HelixGrid?

HelixGrid runs dependency-aware workflows across workers. The **coordinator** manages task states, dependencies, retries and leases. **Workers** lease tasks and execute their commands.

## 1. Requirements

Install:

- Git
- Docker with Docker Compose
- Python 3.11+ if you want to use the CLI

## 2. Download

```bash
git clone https://github.com/chingchang2000/app.git
cd app
```

If you already cloned it:

```bash
git pull
```

## 3. Start HelixGrid

```bash
docker compose up --build --scale worker=3
```

This starts one coordinator and three workers.

Open:

```text
http://127.0.0.1:8080/healthz
```

The response should contain `"status":"ok"`.

## 4. Install the Python CLI

Windows:

```powershell
py -m pip install -e .\sdk\python
```

Linux/macOS:

```bash
python3 -m pip install -e ./sdk/python
```

## 5. Run the example workflow

Windows:

```powershell
py -m helixgrid.cli submit examples\workflow.json --wait
```

Linux/macOS:

```bash
python3 -m helixgrid.cli submit examples/workflow.json --wait
```

A successful run ends in `SUCCEEDED`.

## 6. Useful commands

```bash
python3 -m helixgrid.cli list
python3 -m helixgrid.cli get WORKFLOW_ID
python3 -m helixgrid.cli workers
python3 -m helixgrid.cli watch WORKFLOW_ID
python3 -m helixgrid.cli cancel WORKFLOW_ID
docker compose logs -f
docker compose down
```

On Windows you can use `py` instead of `python3`.

## 7. Create your own workflow

Create `my-workflow.json`:

```json
{
  "name": "my-first-workflow",
  "tasks": [
    {
      "id": "hello",
      "command": ["sh", "-lc", "echo Hello from HelixGrid"]
    },
    {
      "id": "done",
      "depends_on": ["hello"],
      "command": ["sh", "-lc", "echo Finished"]
    }
  ]
}
```

Run it:

```bash
python3 -m helixgrid.cli submit my-workflow.json --wait
```

## 8. Task states

- `PENDING` – waiting to become runnable.
- `READY` – available for a worker.
- `LEASED` – assigned to a worker.
- `RUNNING` – currently executing.
- `RETRY_WAIT` – waiting before another attempt.
- `SUCCEEDED` – completed successfully.
- `FAILED` – permanently failed.
- `CANCELLED` – cancelled.

## 9. Tests

```bash
make test
make check
```

GitHub Actions also validates all language components, protocol contracts, the PostgreSQL schema, Docker images and a real end-to-end workflow.

## 10. Storage note

The active coordinator currently uses an **in-memory store**. Restarting the coordinator clears workflow and worker state.

`storage/postgres/schema.sql` contains the durable PostgreSQL design and is schema-tested by CI, but PostgreSQL is not wired into the active coordinator yet.

## 11. Troubleshooting

If a worker is not taking tasks:

```bash
docker compose logs worker
python3 -m helixgrid.cli workers
```

If a workflow fails:

```bash
python3 -m helixgrid.cli get WORKFLOW_ID
docker compose logs worker
```

Check Docker itself:

```bash
docker --version
docker compose version
docker compose config
```

## 12. Security warning

Workers execute commands received from workflows, and the coordinator currently has no authentication layer.

Do **not** expose port 8080 directly to the public internet on a machine you care about unless you add authentication and proper network protection.

## 13. Project map

- `coordinator/` – Go coordinator/API/scheduler.
- `worker/` – Rust worker.
- `sdk/python/` – Python SDK/CLI.
- `sdk/typescript/` – TypeScript SDK.
- `simulator/` – C++ scheduler simulator.
- `tools/chaos_lab/` – chaos/property testing.
- `tools/replay-verifier/` – Java event replay verifier.
- `storage/postgres/` – PostgreSQL design.
- `protocol/` – OpenAPI and JSON Schema.
