# HelixGrid

**A polyglot distributed workflow runtime and systems-engineering lab — not a website, not a mockup, and not a collection of disconnected hello-world files.**

HelixGrid executes dependency-aware workflows across worker nodes and includes the tooling needed to analyze, stress, verify and extend the runtime. Each language has a concrete role:

- **Go** — coordinator API, DAG scheduler, state machines, worker registry, leases, retries, event bus, graph analytics and concurrent load generator.
- **Rust** — asynchronous worker runtime, process supervision, bounded concurrency, lease renewal, timeout/cancellation and stdout/stderr forwarding.
- **Python** — zero-runtime-dependency SDK/CLI plus a custom workflow DSL compiler, lexer, parser, formatter and graph analyzer.
- **TypeScript** — strict typed SDK, SSE client and fluent DAG builder.
- **C++20** — deterministic discrete-event / Monte Carlo scheduler simulator with critical-path priority and utilization/percentile analysis.
- **Java 21** — zero-dependency historical event replay verifier with a strict JSON engine and distributed-invariant checks.
- **PostgreSQL / SQL** — production-shaped durable storage design using row locking, `SKIP LOCKED`, lease recovery, optimistic versions and event notifications.
- **JSON Schema + OpenAPI** — language-neutral wire contracts.
- **Docker / GitHub Actions / Make** — reproducible development, container builds and multi-language CI.

The design intentionally uses real distributed-systems concepts: leases, heartbeats, idempotency keys, dependency DAGs, retry policy, explicit state transitions, event history, graceful shutdown, bounded concurrency, stale-completion rejection and failure recovery.

## Architecture

```text
                        ┌───────────────────────────────┐
                        │ Python CLI / DSL / SDK        │
                        │ TypeScript SDK                │
                        └───────────────┬───────────────┘
                                        │ HTTP + JSON / SSE
                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                           Go Control Plane                               │
│                                                                          │
│ API → validation → workflow store → DAG scheduler → task lease queue    │
│  │                        │               │                 │             │
│  ├──── event history ◄────┴───────────────┘                 │             │
│  ├──── graph/runtime analytics                              │             │
│  └──── live SSE event stream                                │             │
└──────────────────────────────────────────────────────────────┼─────────────┘
                                                               │ leases
                                                               ▼
                                              ┌────────────────────────────┐
                                              │ Rust worker pool           │
                                              │                            │
                                              │ lease → spawn → stream log │
                                              │ renew → timeout → complete │
                                              └────────────────────────────┘

Offline engineering tools:

  C++ scheduler simulator  ← DAG / duration / failure experiments
  Java replay verifier     ← event JSONL / invariant verification
  Go load generator        ← concurrent API stress + latency percentiles
  PostgreSQL schema        ← durable multi-coordinator storage design
```

## Runtime behavior

A workflow is a directed acyclic graph. A task only becomes runnable after every dependency succeeds. A worker does not simply receive a fire-and-forget command: it receives a time-limited lease identifying one specific attempt. Logs, renewal and completion are bound to that lease. If a worker disappears, the lease expires and the coordinator can recover the task without accepting a later stale completion from the dead attempt.

Implemented behavior includes:

- strict DAG validation and deterministic topological ordering;
- cycle, duplicate ID, self-dependency and unknown-dependency rejection;
- workflow/task state machines;
- idempotent workflow creation with `Idempotency-Key`;
- worker registration, capabilities/labels and heartbeat TTL;
- lease ownership, expiration and renewal;
- automatic expired-lease recovery;
- stale lease/completion rejection;
- bounded exponential retry backoff;
- task timeout and cancellation;
- bounded worker concurrency using Tokio semaphores;
- worker placement through task/worker labels;
- stdout/stderr streaming with chunk and total-output limits;
- append-only in-memory event history with replay;
- Server-Sent Events with `Last-Event-ID` support;
- graph analytics: roots, sinks, levels, descendants, critical path, bottlenecks and parallelism;
- runtime analytics: attempts, retries, active/blocked tasks, completion ratio and timing;
- deterministic makespan estimation;
- health/readiness endpoints;
- strict typed Python and TypeScript clients;
- custom `.helix` workflow DSL;
- Monte Carlo scheduler simulation;
- historical event invariant verification;
- API load generation with p50/p90/p95/p99 latency metrics;
- durable PostgreSQL schema design;
- containerized coordinator and worker images;
- polyglot CI including a source-size gate.

## Quick start

### Coordinator

```bash
cd coordinator
go run ./cmd/helixd
```

The coordinator listens on `http://127.0.0.1:8080` by default.

### Rust worker

```bash
cd worker
cargo run --release
```

Start several worker processes to create a local pool.

### Python CLI

```bash
python -m pip install -e ./sdk/python
helix submit examples/workflow.json
helix list
helix watch <workflow-id>
```

### Docker cluster

```bash
docker compose up --build --scale worker=3
```

## Workflow JSON

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

`build-a` and `build-b` can execute concurrently. `finalize` is not eligible for a lease until both succeed.

## Custom workflow DSL

The Python package contains a real lexer/parser/compiler rather than regex substitution. It validates graphs, calculates critical paths, formats source deterministically and compiles to the same JSON contract accepted by the coordinator.

```text
workflow release-pipeline {
  meta team = "platform"

  task prepare {
    run ["sh", "-lc", "echo preparing"]
    timeout 30s
    retry attempts=2, base=250ms, max=2s
  }

  task test-linux {
    run ["sh", "-lc", "echo tests"]
    needs [prepare]
    labels os = "linux"
  }

  task publish {
    run ["sh", "-lc", "echo publish"]
    needs [test-linux]
  }
}
```

The implementation lives in `sdk/python/src/helixgrid/dsl.py`.

## TypeScript SDK

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

## C++ scheduler simulator

`simulator/` contains a C++20 discrete-event Monte Carlo simulator. It models task duration jitter, failures, retries, worker slots and critical-path-aware scheduling.

```bash
cmake -S simulator -B .build/simulator -DCMAKE_BUILD_TYPE=Release
cmake --build .build/simulator
.build/simulator/helix-sim --demo --workers 8 --runs 10000 --seed 1337
```

Machine-readable result:

```bash
.build/simulator/helix-sim --demo --workers 8 --runs 10000 --seed 1337 --json
```

Graphviz DAG:

```bash
.build/simulator/helix-sim --demo --dot > workflow.dot
```

The CI suite verifies deterministic output for identical seeds.

## Go load generator

The coordinator includes a concurrent load generator that creates real fan-out/fan-in workflows while mixing submit/get/list/cancel traffic and measuring throughput, errors, traffic volume and latency percentiles.

```bash
cd coordinator
go run ./cmd/helix-loadgen \
  --url http://127.0.0.1:8080 \
  --clients 32 \
  --duration 30s \
  --tasks 40 \
  --fanout 6
```

## Java replay verifier

`tools/replay-verifier/` reconstructs system state from coordinator event JSONL and checks historical invariants instead of trusting a final snapshot. It detects conditions such as overlapping leases, completions from the wrong worker, invalid task transitions, sequence gaps, terminal-state changes and traces ending with live leases.

It purposely has no external runtime dependencies and includes its own strict RFC-8259-style JSON parser.

```bash
mkdir -p build/replay-verifier
javac --release 21 \
  -d build/replay-verifier \
  tools/replay-verifier/src/main/java/dev/helixgrid/verifier/Json.java \
  tools/replay-verifier/src/main/java/dev/helixgrid/verifier/Main.java

java -cp build/replay-verifier dev.helixgrid.verifier.Main events.jsonl --strict
```

## PostgreSQL durability design

`storage/postgres/schema.sql` defines a production-shaped persistence model for replacing the in-memory coordinator store. It includes:

- workflow/task state enums;
- normalized task dependencies;
- worker heartbeats;
- one active lease per task enforced by a unique constraint;
- append-only events and task log chunks;
- runnable-task indexes;
- transactional task claiming with `FOR UPDATE ... SKIP LOCKED`;
- `pg_notify` for event listeners;
- optimistic workflow versions;
- an idempotent expired-lease recovery function.

The schema is deliberately separate from the initial runtime implementation so the concurrency/state-machine logic remains easy to study.

## API overview

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | liveness |
| `GET` | `/readyz` | readiness |
| `POST` | `/v1/workflows` | submit workflow |
| `GET` | `/v1/workflows` | list workflows |
| `GET` | `/v1/workflows/{id}` | workflow snapshot |
| `POST` | `/v1/workflows/{id}/cancel` | cancel workflow |
| `GET` | `/v1/workflows/{id}/events` | SSE event history + live stream |
| `POST` | `/v1/workers/register` | register worker |
| `GET` | `/v1/workers` | worker registry snapshot |
| `POST` | `/v1/workers/{id}/heartbeat` | worker heartbeat |
| `POST` | `/v1/leases` | acquire runnable task |
| `POST` | `/v1/leases/{token}/start` | acknowledge execution start |
| `POST` | `/v1/leases/{token}/renew` | extend lease |
| `POST` | `/v1/leases/{token}/logs` | append output chunk |
| `POST` | `/v1/leases/{token}/complete` | report attempt result |

Full contract: `protocol/openapi.yaml` and `protocol/workflow.schema.json`.

## Repository layout

```text
.
├── coordinator/
│   ├── cmd/
│   │   ├── helixd/             # Go coordinator daemon
│   │   └── helix-loadgen/      # concurrent API load generator
│   └── internal/
│       ├── api/                 # HTTP + SSE transport
│       └── core/                # DAG/state/lease/analytics engine
├── worker/                      # Tokio-based Rust worker runtime
├── sdk/
│   ├── python/                  # Python SDK, CLI and DSL compiler
│   └── typescript/              # strict TypeScript SDK
├── simulator/                   # C++20 Monte Carlo scheduler simulator
├── tools/
│   └── replay-verifier/         # Java 21 event/invariant verifier
├── storage/
│   └── postgres/schema.sql      # durable storage design
├── protocol/
│   ├── workflow.schema.json
│   └── openapi.yaml
├── examples/
├── scripts/
│   └── code_stats.py            # deterministic source-line gate
├── .github/workflows/ci.yml
├── docker-compose.yml
├── Makefile
└── LICENSE
```

## State machines

Tasks move through constrained transitions rather than arbitrary strings:

```text
PENDING → READY → LEASED → RUNNING → SUCCEEDED
                    │          │
                    │          ├────→ RETRY_WAIT → READY
                    │          ├────→ FAILED
                    │          └────→ CANCELLED
                    └───────────────→ READY   (lease expiry)
```

The coordinator rejects invalid transitions, and the independent Java replay tool can detect impossible historical sequences after the fact.

## Failure model

HelixGrid is designed around these invariants:

1. A lease token identifies one attempt of one task.
2. Completion for unknown or stale leases is rejected.
3. A task is not intentionally leased while another non-expired lease owns it.
4. Worker capacity limits concurrent leases.
5. A workflow only succeeds when every task succeeds.
6. A terminally failed dependency prevents dependent tasks from running.
7. Cancellation is monotonic.
8. Dependency cycles are rejected before acceptance.
9. Expired leases can be recovered without trusting the disappeared worker.
10. Event sequence IDs are monotonically generated by the coordinator event bus.

## Source-size verification

The repository does not rely on a guessed line count. CI runs:

```bash
python scripts/code_stats.py . --minimum 10000 --files
```

Only recognized source-code extensions are counted; Markdown/README/YAML files do not satisfy the source-size gate. Build output, dependencies, generated files, `node_modules`, Rust `target`, virtualenvs and caches are excluded.

## Development

```bash
make check
make test
make build
make fmt
```

Individual test stacks:

```bash
make test-go
make test-rust
make test-python
make test-typescript
make test-cpp
```

## CI

GitHub Actions validates independent stacks rather than only checking syntax:

- source-code line gate;
- Go format normalization, `go vet`, race-detector tests and binary builds;
- Rust format normalization, Clippy with warnings denied, tests and release build;
- Python package installation and tests on 3.11, 3.12 and 3.13;
- TypeScript strict typecheck and declaration build;
- C++ configure/build/CTest/determinism verification;
- Java 21 compilation and CLI smoke test;
- JSON Schema / example JSON / OpenAPI parsing;
- coordinator and worker Docker image builds after Go/Rust succeed.

## Security boundary

This project is an orchestration runtime, **not an execution sandbox**. Worker commands run with the operating-system permissions of the worker process. Do not expose a coordinator that accepts untrusted workflows to the public internet without authentication, authorization and an actual isolation boundary such as containers, namespaces or VMs.

## License

MIT — see [`LICENSE`](LICENSE).
