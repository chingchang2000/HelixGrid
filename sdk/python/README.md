# HelixGrid Python SDK

Python 3.11+ client library and command-line interface for the HelixGrid distributed workflow runtime.

The package intentionally depends only on the Python standard library at runtime. It provides:

- a synchronous coordinator client;
- a fluent DAG/workflow builder;
- idempotent workflow submission;
- workflow polling and terminal-state waiting;
- Server-Sent Events parsing for live workflow events;
- a `helix` CLI for submitting, inspecting, watching and cancelling workflows.

## Install from this repository

```bash
python -m pip install -e ./sdk/python
```

## CLI

```bash
helix --help
helix submit examples/workflow.json
helix list
helix get <workflow-id>
helix watch <workflow-id>
helix wait <workflow-id>
helix cancel <workflow-id>
helix workers
```

Set a different coordinator with either `--url` or `HELIX_URL`.

## Python API

```python
from helixgrid import HelixClient, RetryPolicy, WorkflowBuilder

workflow = (
    WorkflowBuilder("release")
    .metadata(team="platform")
    .task("prepare", ["sh", "-lc", "echo prepare"])
    .task(
        "test",
        ["sh", "-lc", "echo test"],
        retry=RetryPolicy(max_attempts=3, base_delay_ms=250),
    )
    .task("package", ["sh", "-lc", "echo package"])
    .depends_on("test", "prepare")
    .depends_on("package", "test")
    .build()
)

client = HelixClient("http://127.0.0.1:8080")
created = client.submit_workflow(workflow, idempotency_key="release-001")
finished = client.wait(created["id"])
print(finished["state"])
```

The builder performs local graph checks before submission, while the coordinator remains authoritative and validates the workflow again at the protocol boundary.
