from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .generator import GeneratedScenario, GeneratorConfig, ScenarioGenerator
from .model import Action, ActionKind, InvariantViolation, Model, ModelError, Task


@dataclass(frozen=True, slots=True)
class StepRecord:
    index: int
    action: Action
    accepted: bool
    error_type: str = ""
    error: str = ""
    digest: str = ""
    event_count: int = 0
    active_leases: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action.as_dict(),
            "accepted": self.accepted,
            "error_type": self.error_type,
            "error": self.error,
            "digest": self.digest,
            "event_count": self.event_count,
            "active_leases": self.active_leases,
        }


@dataclass(frozen=True, slots=True)
class ReplayFailure:
    index: int
    action: Action | None
    error_type: str
    error: str
    invariant_code: str = ""
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": None if self.action is None else self.action.as_dict(),
            "error_type": self.error_type,
            "error": self.error,
            "invariant_code": self.invariant_code,
            "snapshot": dict(self.snapshot),
        }


@dataclass(frozen=True, slots=True)
class ReplayReport:
    seed: int
    records: tuple[StepRecord, ...]
    final_digest: str
    final_snapshot: Mapping[str, Any]
    event_jsonl: str
    failure: ReplayFailure | None
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def accepted(self) -> int:
        return sum(1 for record in self.records if record.accepted)

    @property
    def rejected(self) -> int:
        return len(self.records) - self.accepted

    def rejection_types(self) -> dict[str, int]:
        counts = Counter(record.error_type for record in self.records if not record.accepted)
        return dict(sorted(counts.items()))

    def as_dict(self, *, include_records: bool = True, include_events: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "seed": self.seed,
            "ok": self.ok,
            "steps": len(self.records),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejection_types": self.rejection_types(),
            "final_digest": self.final_digest,
            "final_snapshot": dict(self.final_snapshot),
            "failure": None if self.failure is None else self.failure.as_dict(),
            "elapsed_seconds": self.elapsed_seconds,
        }
        if include_records:
            value["records"] = [record.as_dict() for record in self.records]
        if include_events:
            value["event_jsonl"] = self.event_jsonl
        return value


@dataclass(frozen=True, slots=True)
class CampaignReport:
    first_seed: int
    count: int
    reports: tuple[ReplayReport, ...]
    elapsed_seconds: float

    @property
    def failures(self) -> tuple[ReplayReport, ...]:
        return tuple(report for report in self.reports if not report.ok)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        total_steps = sum(len(report.records) for report in self.reports)
        accepted = sum(report.accepted for report in self.reports)
        rejected = sum(report.rejected for report in self.reports)
        return {
            "first_seed": self.first_seed,
            "count": self.count,
            "ok": self.ok,
            "failures": [report.as_dict(include_records=False) for report in self.failures],
            "total_steps": total_steps,
            "accepted": accepted,
            "rejected": rejected,
            "elapsed_seconds": self.elapsed_seconds,
            "scenarios_per_second": self.count / self.elapsed_seconds if self.elapsed_seconds else 0.0,
            "steps_per_second": total_steps / self.elapsed_seconds if self.elapsed_seconds else 0.0,
        }


class ScenarioRunner:
    def __init__(self, *, strict_rejections: bool = False) -> None:
        self.strict_rejections = bool(strict_rejections)

    def bootstrap(self, scenario: GeneratedScenario) -> Model:
        model = Model()
        model.add_workflow(
            scenario.workflow.name,
            scenario.workflow.tasks,
            workflow_id=f"workflow-{scenario.seed}",
            metadata=scenario.workflow.metadata,
        )
        for worker_id, capacity, labels in scenario.worker_specs:
            model.register_worker(
                worker_id,
                worker_id=worker_id,
                capacity=capacity,
                labels=labels,
            )
        model.check_invariants()
        return model

    def replay(
        self,
        scenario: GeneratedScenario,
        *,
        actions: Sequence[Action] | None = None,
        stop_on_failure: bool = True,
    ) -> ReplayReport:
        started = time.perf_counter()
        records: list[StepRecord] = []
        failure: ReplayFailure | None = None
        try:
            model = self.bootstrap(scenario)
        except Exception as exc:
            snapshot: Mapping[str, Any] = {}
            if isinstance(exc, InvariantViolation):
                snapshot = exc.snapshot
            failure = ReplayFailure(
                index=-1,
                action=None,
                error_type=type(exc).__name__,
                error=str(exc),
                invariant_code=getattr(exc, "code", ""),
                snapshot=snapshot,
            )
            return ReplayReport(
                seed=scenario.seed,
                records=(),
                final_digest="",
                final_snapshot={},
                event_jsonl="",
                failure=failure,
                elapsed_seconds=time.perf_counter() - started,
            )

        sequence = scenario.actions if actions is None else tuple(actions)
        for index, action in enumerate(sequence):
            accepted = True
            error_type = ""
            error = ""
            try:
                model.apply(action)
            except ModelError as exc:
                accepted = False
                error_type = type(exc).__name__
                error = str(exc)
                if self.strict_rejections:
                    failure = ReplayFailure(
                        index=index,
                        action=action,
                        error_type=error_type,
                        error=error,
                        snapshot=model.snapshot(),
                    )
            except Exception as exc:
                accepted = False
                error_type = type(exc).__name__
                error = str(exc)
                failure = ReplayFailure(
                    index=index,
                    action=action,
                    error_type=error_type,
                    error=error,
                    snapshot=model.snapshot(),
                )

            if failure is None:
                try:
                    model.check_invariants()
                except InvariantViolation as exc:
                    failure = ReplayFailure(
                        index=index,
                        action=action,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        invariant_code=exc.code,
                        snapshot=exc.snapshot,
                    )
                except Exception as exc:
                    failure = ReplayFailure(
                        index=index,
                        action=action,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        snapshot=model.snapshot(),
                    )

            records.append(
                StepRecord(
                    index=index,
                    action=action,
                    accepted=accepted,
                    error_type=error_type,
                    error=error,
                    digest=model.digest(),
                    event_count=len(model.events),
                    active_leases=len(model.leases),
                )
            )
            if failure is not None and stop_on_failure:
                break

        elapsed = time.perf_counter() - started
        return ReplayReport(
            seed=scenario.seed,
            records=tuple(records),
            final_digest=model.digest(),
            final_snapshot=model.snapshot(),
            event_jsonl=model.event_jsonl(),
            failure=failure,
            elapsed_seconds=elapsed,
        )

    def determinism_check(self, scenario: GeneratedScenario) -> tuple[bool, ReplayReport, ReplayReport]:
        first = self.replay(scenario)
        second = self.replay(scenario)
        same = (
            first.final_digest == second.final_digest
            and first.event_jsonl == second.event_jsonl
            and [record.as_dict() for record in first.records]
            == [record.as_dict() for record in second.records]
        )
        return same, first, second


def run_campaign(
    *,
    first_seed: int = 1,
    count: int = 100,
    steps: int = 250,
    config: GeneratorConfig | None = None,
    strict_rejections: bool = False,
    stop_on_first_failure: bool = True,
) -> CampaignReport:
    if count < 1:
        raise ValueError("count must be positive")
    runner = ScenarioRunner(strict_rejections=strict_rejections)
    started = time.perf_counter()
    reports: list[ReplayReport] = []
    for offset in range(count):
        seed = first_seed + offset
        scenario = ScenarioGenerator(seed, config=config).scenario(steps=steps)
        report = runner.replay(scenario)
        reports.append(report)
        if not report.ok and stop_on_first_failure:
            break
    return CampaignReport(
        first_seed=first_seed,
        count=len(reports),
        reports=tuple(reports),
        elapsed_seconds=time.perf_counter() - started,
    )


def action_from_dict(value: Mapping[str, Any]) -> Action:
    return Action(
        kind=ActionKind(str(value["kind"])),
        workflow_id=str(value.get("workflow_id", "")),
        task_id=str(value.get("task_id", "")),
        worker_id=str(value.get("worker_id", "")),
        token=str(value.get("token", "")),
        amount=int(value.get("amount", 0)),
        text=str(value.get("text", "")),
    )


def scenario_to_dict(scenario: GeneratedScenario) -> dict[str, Any]:
    return {
        "seed": scenario.seed,
        "workflow": {
            "name": scenario.workflow.name,
            "metadata": dict(scenario.workflow.metadata),
            "tasks": [
                {
                    "id": task.id,
                    "depends_on": list(task.depends_on),
                    "labels": dict(task.labels),
                    "retry": dataclasses.asdict(task.retry),
                    "timeout_ms": task.timeout_ms,
                }
                for task in scenario.workflow.tasks
            ],
        },
        "workers": [
            {
                "id": worker_id,
                "capacity": capacity,
                "labels": dict(labels),
            }
            for worker_id, capacity, labels in scenario.worker_specs
        ],
        "actions": [action.as_dict() for action in scenario.actions],
        "notes": list(scenario.notes),
    }


def scenario_from_dict(value: Mapping[str, Any]) -> GeneratedScenario:
    from .generator import GeneratedWorkflow
    from .model import RetryPolicy

    workflow_value = value["workflow"]
    tasks = []
    for task_value in workflow_value["tasks"]:
        retry_value = task_value.get("retry", {})
        tasks.append(
            Task(
                id=str(task_value["id"]),
                depends_on=tuple(map(str, task_value.get("depends_on", ()))),
                labels={str(k): str(v) for k, v in task_value.get("labels", {}).items()},
                retry=RetryPolicy(
                    int(retry_value.get("max_attempts", 1)),
                    int(retry_value.get("base_delay_ms", 250)),
                    int(retry_value.get("max_delay_ms", 30_000)),
                ),
                timeout_ms=int(task_value.get("timeout_ms", 0)),
            )
        )
    workflow = GeneratedWorkflow(
        name=str(workflow_value["name"]),
        tasks=tuple(tasks),
        metadata={str(k): str(v) for k, v in workflow_value.get("metadata", {}).items()},
    )
    workers = tuple(
        (
            str(worker["id"]),
            int(worker.get("capacity", 1)),
            {str(k): str(v) for k, v in worker.get("labels", {}).items()},
        )
        for worker in value.get("workers", ())
    )
    actions = tuple(action_from_dict(action) for action in value.get("actions", ()))
    return GeneratedScenario(
        seed=int(value.get("seed", 0)),
        workflow=workflow,
        worker_specs=workers,
        actions=actions,
        notes=tuple(map(str, value.get("notes", ()))),
    )


def save_scenario(path: str | pathlib.Path, scenario: GeneratedScenario) -> None:
    pathlib.Path(path).write_text(
        json.dumps(scenario_to_dict(scenario), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_scenario(path: str | pathlib.Path) -> GeneratedScenario:
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("scenario root must be a JSON object")
    return scenario_from_dict(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.chaos_lab.runner",
        description="Deterministic scheduler/lease chaos laboratory for HelixGrid",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate one replayable scenario")
    generate.add_argument("--seed", type=int, default=1)
    generate.add_argument("--steps", type=int, default=250)
    generate.add_argument("--output", required=True)

    replay = sub.add_parser("replay", help="replay a saved scenario")
    replay.add_argument("scenario")
    replay.add_argument("--json", action="store_true")
    replay.add_argument("--strict-rejections", action="store_true")

    campaign = sub.add_parser("campaign", help="run many deterministic generated scenarios")
    campaign.add_argument("--seed", type=int, default=1)
    campaign.add_argument("--count", type=int, default=100)
    campaign.add_argument("--steps", type=int, default=250)
    campaign.add_argument("--json", action="store_true")
    campaign.add_argument("--strict-rejections", action="store_true")
    campaign.add_argument("--keep-going", action="store_true")

    determinism = sub.add_parser("determinism", help="verify exact replay determinism")
    determinism.add_argument("scenario")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate":
        scenario = ScenarioGenerator(args.seed).scenario(steps=args.steps)
        save_scenario(args.output, scenario)
        print(f"wrote seed={scenario.seed} steps={len(scenario.actions)} to {args.output}")
        return 0

    if args.command == "replay":
        scenario = load_scenario(args.scenario)
        report = ScenarioRunner(strict_rejections=args.strict_rejections).replay(scenario)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            print(
                f"seed={report.seed} ok={report.ok} steps={len(report.records)} "
                f"accepted={report.accepted} rejected={report.rejected} "
                f"digest={report.final_digest}"
            )
            if report.failure:
                print(
                    f"failure at step {report.failure.index}: "
                    f"{report.failure.error_type}: {report.failure.error}",
                    file=sys.stderr,
                )
        return 0 if report.ok else 1

    if args.command == "campaign":
        report = run_campaign(
            first_seed=args.seed,
            count=args.count,
            steps=args.steps,
            strict_rejections=args.strict_rejections,
            stop_on_first_failure=not args.keep_going,
        )
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            data = report.as_dict()
            print(
                f"scenarios={data['count']} ok={data['ok']} "
                f"steps={data['total_steps']} accepted={data['accepted']} "
                f"rejected={data['rejected']} rate={data['steps_per_second']:.0f} steps/s"
            )
            for failure in report.failures:
                assert failure.failure is not None
                print(
                    f"seed {failure.seed} failed at step {failure.failure.index}: "
                    f"{failure.failure.error}",
                    file=sys.stderr,
                )
        return 0 if report.ok else 1

    if args.command == "determinism":
        scenario = load_scenario(args.scenario)
        same, first, second = ScenarioRunner().determinism_check(scenario)
        print(
            f"deterministic={same} first={first.final_digest} second={second.final_digest} "
            f"steps={len(first.records)}"
        )
        return 0 if same else 1

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
