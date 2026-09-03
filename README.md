# HelixGrid

**A polyglot distributed workflow runtime built to show real systems programming — not a demo website.**

HelixGrid is a small but serious orchestration platform for executing dependency-aware jobs across worker nodes. It is intentionally built as a monorepo with multiple languages where each language has a concrete role:

- **Go** — control-plane API, scheduler, worker registry, event log and DAG orchestration.
- **Rust** — worker runtime, process supervision, leasing, cancellation, retry/backoff and log forwarding.
- **Python** — ergonomic CLI + SDK for submitting and watching workflows.
- **TypeScript** — strongly typed SDK and fluent DAG builder.
- **JSON Schema** — shared wire-level workflow contract.
- **Docker / GitHub Actions / Make** — reproducible development and CI.

The project is designed around the same ideas used in real distributed systems: leases, heartbeats, idempotency keys, dependency graphs, retry policies, state transitions, event sourcing, graceful shutdown, bounded concurrency and typed protocol boundaries.

## Architecture

```text
                         ┌──────────────────────────────┐
                         │       Python CLI / SDK       │
                         │       TypeScript SDK         │
                         └──────────────┬───────────────┘
                                        │ HTTP / JSON
                                        ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Go Control Plane                             │
│                                                                    │
│  API ──► Validation ──► DAG Store ──► Scheduler ──► Lease Queue   │
│   │                         │              │              │         │
│   │                         │              │              │         │
│   └────► Event Log ◄────────┴──────────────┘              │         │
│               │                                           │         │
│               └──────────── SSE event stream              │         │
└────────────────────────────────────────────────────────────┼─────────┘
                                                             │
                                              long-poll lease │
                                                             ▼
                                           ┌────────────────────────┐
                                           │      Rust Workers      │
                                           │                        │
                                           │ lease → execute → ack  │
                                           │ logs → heartbeat       │
                                           │ timeout / cancellation │
                                           └────────────────────────┘
```

## What it can do

A workflow is a directed acyclic graph of tasks. A task becomes runnable only after all of its dependencies succeed. Workers lease runnable tasks, execute them, stream output and report a terminal result. The coordinator can retry failed tasks according to policy without double-running a valid lease.

Core behavior includes:

- strict DAG cycle detection;
- deterministic topological ordering;
- workflow and task state machines;
- idempotent workflow submission;
- task lease tokens with expiration;
- worker registration and heartbeat tracking;
- automatic recovery of expired leases;
- retry policies with bounded exponential backoff;
- task cancellation;
- per-task timeout support;
- bounded worker concurrency;
- stdout/stderr capture with output-size limits;
- append-only event history;
- Server-Sent Events for live workflow updates;
- health and readiness endpoints;
- typed Python and TypeScript clients;
- JSON Schema validation at the protocol boundary;
- Docker Compose local cluster;
- CI for Go, Rust, Python and TypeScript.

## Quick start

### 1. Start the coordinator

```bash
cd coordinator
go run ./cmd/helixd
```

Default address: `http://127.0.0.1:8080`.

### 2. Start a worker

```bash
cd worker
cargo run --release
```

Start multiple terminals to simulate a worker pool.

### 3. Install the Python CLI

```bash
cd sdk/python
python -m pip install -e .
```

Submit the example workflow:

```bash
helix submit ../../examples/workflow.json
helix list
helix watch <workflow-id>
```

### 4. Use the TypeScript SDK

```bash
cd sdk/typescript
npm install
npm run build
```

```ts
import { HelixClient, WorkflowBuilder } from "@helixgrid/sdk";

const client = new HelixClient({ baseUrl: "http://127.0.0.1:8080" });

const workflow = new WorkflowBuilder("release-pipeline")
  .task("lint", { command: ["sh", "-lc", "echo linting"] })
  .task("test", { command: ["sh", "-lc", "echo testing"] })
  .task("package", { command: ["sh", "-lc", "echo packaging"] })
  .dependsOn("test", "lint")
  .dependsOn("package", "test")
  .build();

const created = await client.submitWorkflow(workflow);
console.log(created.id);
```

## Workflow example

```json
{
  "name": "artifact-pipeline",
  "metadata": {
    "team": "platform",
    "environment": "dev"
  },
  "tasks": [
    {
      "id": "prepare",
      "command": ["sh", "-lc", "echo preparing && sleep 1"],
      "timeout_seconds": 30,
      "retry": { "max_attempts": 2, "base_delay_ms": 250 }
    },
    {
      "id": "build-a",
      "depends_on": ["prepare"],
      "command": ["sh", "-lc", "echo building A && sleep 1"]
    },
    {
      "id": "build-b",
      "depends_on": ["prepare"],
      "command": ["sh", "-lc", "echo building B && sleep 1"]
    },
    {
      "id": "finalize",
      "depends_on": ["build-a", "build-b"],
      "command": ["sh", "-lc", "echo done"]
    }
  ]
}
```

The two build tasks can run in parallel. `finalize` is not eligible for a lease until both complete successfully.

## API overview

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | liveness |
| `GET` | `/readyz` | readiness |
| `POST` | `/v1/workflows` | submit workflow |
| `GET` | `/v1/workflows` | list workflows |
| `GET` | `/v1/workflows/{id}` | workflow snapshot |
| `POST` | `/v1/workflows/{id}/cancel` | cancel workflow |
| `GET` | `/v1/workflows/{id}/events` | SSE stream |
| `POST` | `/v1/workers/register` | worker registration |
| `POST` | `/v1/workers/{id}/heartbeat` | worker heartbeat |
| `POST` | `/v1/leases` | lease next task |
| `POST` | `/v1/leases/{token}/logs` | append task logs |
| `POST` | `/v1/leases/{token}/complete` | report completion |

## Repository layout

```text
.
├── coordinator/            # Go control plane
│   ├── cmd/helixd/
│   └── internal/
│       ├── api/
│       ├── core/
│       └── protocol/
├── worker/                 # Rust worker runtime
│   └── src/
├── sdk/
│   ├── python/             # Python SDK + CLI
│   └── typescript/         # TypeScript SDK
├── protocol/               # shared JSON Schema
├── examples/               # runnable workflow definitions
├── docs/                   # design and protocol docs
├── scripts/                # development helpers
├── .github/workflows/      # polyglot CI
├── docker-compose.yml
└── Makefile
```

## Design principles

### Explicit state machines

Tasks move through a constrained transition graph rather than arbitrary strings:

```text
PENDING → READY → LEASED → RUNNING → SUCCEEDED
                    │          │
                    │          ├────────→ FAILED → RETRY_WAIT → READY
                    │          └────────→ CANCELLED
                    └───────────────────→ READY   (lease expiry)
```

Invalid transitions are rejected in the coordinator.

### Leases instead of fire-and-forget jobs

The coordinator does not simply hand a task to a worker and hope for the best. It creates a time-limited lease token. Every heartbeat, log append and completion request is tied to that token. If the worker disappears, the lease expires and the task is made runnable again when policy allows it.

### Idempotency

Workflow submission accepts `Idempotency-Key`. Re-sending the same logical submission with the same key returns the original workflow instead of creating duplicates.

### Event history

Every meaningful state change creates an event. The in-memory event store is intentionally simple, but the API boundary is designed so it can later be replaced by Postgres, NATS, Kafka or another durable backend.

### Polyglot for a reason

This repository does not mix languages just to increase a language counter. Go is used for concurrency-heavy server orchestration; Rust is used for process supervision and resource-safe worker execution; Python is used for automation ergonomics; TypeScript provides a typed JS ecosystem client.

## Development

Run all local checks that are available on your machine:

```bash
make check
```

Individual stacks:

```bash
make test-go
make test-rust
make test-python
make test-typescript
```

Format:

```bash
make fmt
```

Run coordinator + workers with Docker:

```bash
docker compose up --build --scale worker=3
```

## Failure model

HelixGrid makes a few explicit guarantees:

1. A lease token identifies exactly one attempt of one task.
2. Completion for stale or unknown leases is rejected.
3. A task is never intentionally leased while another non-expired lease owns it.
4. A workflow reaches `SUCCEEDED` only when every task succeeds.
5. A workflow reaches `FAILED` when a terminally failed task makes success impossible.
6. Cancellation is monotonic: cancelled workflows never become active again.
7. Dependency cycles are rejected before a workflow is accepted.

This is not a security sandbox. Worker commands run with the operating-system permissions of the worker process. Do not expose a coordinator that accepts untrusted workflows to the public internet without adding authentication, authorization and a real isolation boundary (containers, VMs, namespaces, etc.).

## Roadmap ideas

- SQLite/Postgres durable storage adapter
- authentication and scoped API tokens
- artifact upload/download protocol
- worker capabilities and task placement constraints
- CPU/memory quotas
- OpenTelemetry traces
- Prometheus metrics endpoint
- WebSocket bidirectional logs
- Kubernetes worker deployment
- pluggable queue backend

## License

MIT — see [`LICENSE`](LICENSE).
