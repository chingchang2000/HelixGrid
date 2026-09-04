from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable, Mapping

from .client import APIError, HelixClient, HelixError, load_workflow_file, summarize_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix",
        description="Command-line client for the HelixGrid distributed workflow runtime.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("HELIX_URL", "http://127.0.0.1:8080"),
        help="coordinator URL (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="submit a workflow JSON file")
    submit.add_argument("file")
    submit.add_argument("--idempotency-key")
    submit.add_argument("--wait", action="store_true")

    sub.add_parser("list", help="list workflows")

    get = sub.add_parser("get", help="show one workflow")
    get.add_argument("workflow_id")

    watch = sub.add_parser("watch", help="stream workflow events")
    watch.add_argument("workflow_id")
    watch.add_argument("--after", type=int)

    wait = sub.add_parser("wait", help="wait until a workflow reaches a terminal state")
    wait.add_argument("workflow_id")
    wait.add_argument("--timeout", type=float)

    cancel = sub.add_parser("cancel", help="cancel a workflow")
    cancel.add_argument("workflow_id")

    sub.add_parser("workers", help="list registered workers")
    return parser


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def print_workflow(workflow: Mapping[str, Any], *, detailed: bool = False) -> None:
    print(summarize_workflow(workflow))
    if not detailed:
        return
    print()
    runtime = workflow.get("runtime") or {}
    tasks = workflow.get("tasks") or {}
    order = workflow.get("order") or sorted(runtime)
    for task_id in order:
        state = runtime.get(task_id, {}).get("state", "?")
        attempt = runtime.get(task_id, {}).get("attempt", 0)
        command = tasks.get(task_id, {}).get("command", [])
        print(f"  {task_id:24} {state:12} attempt={attempt:<3} {' '.join(map(str, command))}")


def command_submit(client: HelixClient, args: argparse.Namespace) -> int:
    payload = load_workflow_file(args.file)
    workflow = client.submit_workflow(payload, idempotency_key=args.idempotency_key)
    if args.wait:
        workflow = client.wait(workflow["id"])
    if args.json:
        print_json(workflow)
    else:
        print_workflow(workflow, detailed=True)
    if args.wait:
        return 0 if workflow.get("state") == "SUCCEEDED" else 2
    return 0


def command_list(client: HelixClient, args: argparse.Namespace) -> int:
    workflows = client.list_workflows()
    if args.json:
        print_json(workflows)
    elif not workflows:
        print("No workflows.")
    else:
        for workflow in workflows:
            print_workflow(workflow)
    return 0


def command_get(client: HelixClient, args: argparse.Namespace) -> int:
    workflow = client.get_workflow(args.workflow_id)
    if args.json:
        print_json(workflow)
    else:
        print_workflow(workflow, detailed=True)
    return 0


def command_watch(client: HelixClient, args: argparse.Namespace) -> int:
    for event in client.iter_events(args.workflow_id, last_event_id=args.after):
        if args.json:
            print(json.dumps(event, separators=(",", ":"), ensure_ascii=False), flush=True)
            continue
        data = event.get("data") or {}
        if isinstance(data, Mapping):
            event_type = data.get("type", event.get("event", "event"))
            task_id = data.get("task_id", "")
            details = data.get("data") or {}
            suffix = f" {json.dumps(details, ensure_ascii=False)}" if details else ""
            print(f"[{event.get('id', '?'):>5}] {event_type:24} {task_id}{suffix}", flush=True)
        else:
            print(event, flush=True)
    return 0


def command_wait(client: HelixClient, args: argparse.Namespace) -> int:
    workflow = client.wait(args.workflow_id, timeout=args.timeout)
    if args.json:
        print_json(workflow)
    else:
        print_workflow(workflow, detailed=True)
    return 0 if workflow.get("state") == "SUCCEEDED" else 2


def command_cancel(client: HelixClient, args: argparse.Namespace) -> int:
    workflow = client.cancel_workflow(args.workflow_id)
    if args.json:
        print_json(workflow)
    else:
        print_workflow(workflow, detailed=True)
    return 0


def command_workers(client: HelixClient, args: argparse.Namespace) -> int:
    workers = client.list_workers()
    if args.json:
        print_json(workers)
        return 0
    if not workers:
        print("No workers registered.")
        return 0
    for worker in workers:
        labels = ",".join(f"{k}={v}" for k, v in sorted((worker.get("labels") or {}).items()))
        print(
            f"{worker.get('id', '?')}  {worker.get('name', '?'):20} "
            f"slots={worker.get('active_leases', 0)}/{worker.get('capacity', '?')}  {labels}"
        )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    client = HelixClient(args.url)
    handlers = {
        "submit": command_submit,
        "list": command_list,
        "get": command_get,
        "watch": command_watch,
        "wait": command_wait,
        "cancel": command_cancel,
        "workers": command_workers,
    }
    try:
        return handlers[args.command](client, args)
    except APIError as exc:
        print(f"API error: {exc.message}", file=sys.stderr)
        return 3
    except (HelixError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
