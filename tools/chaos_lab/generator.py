from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .model import Action, ActionKind, Model, RetryPolicy, Task, TaskState, WorkflowState


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    min_tasks: int = 3
    max_tasks: int = 24
    max_dependencies: int = 4
    max_workers: int = 6
    max_capacity: int = 4
    label_probability: float = 0.22
    retry_probability: float = 0.35
    failure_probability: float = 0.18
    timeout_probability: float = 0.12
    stale_operation_probability: float = 0.08
    cancel_probability: float = 0.03
    worker_drop_probability: float = 0.04
    log_probability: float = 0.25
    heartbeat_probability: float = 0.15
    renew_probability: float = 0.20
    max_time_step_ms: int = 8_000

    def __post_init__(self) -> None:
        if self.min_tasks < 1:
            raise ValueError("min_tasks must be positive")
        if self.max_tasks < self.min_tasks:
            raise ValueError("max_tasks must be >= min_tasks")
        if self.max_dependencies < 0:
            raise ValueError("max_dependencies may not be negative")
        if self.max_workers < 1:
            raise ValueError("max_workers must be positive")
        if self.max_capacity < 1:
            raise ValueError("max_capacity must be positive")
        for name in (
            "label_probability",
            "retry_probability",
            "failure_probability",
            "timeout_probability",
            "stale_operation_probability",
            "cancel_probability",
            "worker_drop_probability",
            "log_probability",
            "heartbeat_probability",
            "renew_probability",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0,1]")
        if self.max_time_step_ms < 1:
            raise ValueError("max_time_step_ms must be positive")


@dataclass(frozen=True, slots=True)
class GeneratedWorkflow:
    name: str
    tasks: tuple[Task, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def edge_count(self) -> int:
        return sum(len(task.depends_on) for task in self.tasks)

    @property
    def width_hint(self) -> int:
        roots = sum(1 for task in self.tasks if not task.depends_on)
        return max(1, roots)


@dataclass(frozen=True, slots=True)
class GeneratedScenario:
    seed: int
    workflow: GeneratedWorkflow
    worker_specs: tuple[tuple[str, int, Mapping[str, str]], ...]
    actions: tuple[Action, ...]
    notes: tuple[str, ...] = ()


class ScenarioGenerator:
    """Deterministic generator for valid DAGs and adversarial scheduler actions.

    It deliberately generates only valid input workflows. Fault injection happens through
    lifecycle actions: lease expiry, stale completion, duplicate completion, worker loss,
    retries, cancellations, and scheduling pressure.
    """

    LABELS: tuple[tuple[str, str], ...] = (
        ("os", "linux"),
        ("os", "windows"),
        ("arch", "x86_64"),
        ("arch", "arm64"),
        ("tier", "cpu"),
        ("tier", "fast"),
    )

    def __init__(self, seed: int, config: GeneratorConfig | None = None) -> None:
        self.seed = int(seed)
        self.config = config or GeneratorConfig()
        self.rng = random.Random(self.seed)

    def workflow(self, *, name: str | None = None) -> GeneratedWorkflow:
        count = self.rng.randint(self.config.min_tasks, self.config.max_tasks)
        ids = [f"task-{index:02d}" for index in range(count)]
        tasks: list[Task] = []
        for index, task_id in enumerate(ids):
            candidates = ids[:index]
            dependency_count = 0
            if candidates:
                upper = min(self.config.max_dependencies, len(candidates))
                # Keep some roots, but favor connected graphs.
                if self.rng.random() < 0.78:
                    dependency_count = self.rng.randint(1, upper)
            dependencies = tuple(sorted(self.rng.sample(candidates, dependency_count)))
            labels = self._task_labels()
            retry = self._retry_policy()
            timeout_ms = 0
            if self.rng.random() < self.config.timeout_probability:
                timeout_ms = self.rng.randint(1, 120) * 1_000
            tasks.append(
                Task(
                    id=task_id,
                    depends_on=dependencies,
                    labels=labels,
                    retry=retry,
                    timeout_ms=timeout_ms,
                )
            )
        # Guarantee at least one dependency when possible. This makes generated scenarios
        # exercise dependency transitions even when random choices made every task a root.
        if count > 1 and not any(task.depends_on for task in tasks):
            tasks[-1].depends_on = (tasks[0].id,)
        return GeneratedWorkflow(
            name=name or f"chaos-{self.seed}",
            tasks=tuple(task.clone() for task in tasks),
            metadata={
                "generator": "helixgrid-chaos",
                "seed": str(self.seed),
            },
        )

    def workers(self, workflow: GeneratedWorkflow) -> tuple[tuple[str, int, Mapping[str, str]], ...]:
        required: dict[str, set[str]] = {}
        for task in workflow.tasks:
            for key, value in task.labels.items():
                required.setdefault(key, set()).add(value)

        count = self.rng.randint(1, self.config.max_workers)
        specs: list[tuple[str, int, Mapping[str, str]]] = []
        for index in range(count):
            labels: dict[str, str] = {}
            # Random workers get broad-but-not-universal placement capabilities.
            for key, values in sorted(required.items()):
                if values and self.rng.random() < 0.72:
                    labels[key] = self.rng.choice(sorted(values))
            specs.append(
                (
                    f"worker-{index:02d}",
                    self.rng.randint(1, self.config.max_capacity),
                    labels,
                )
            )

        # Placement must never deadlock solely because generation forgot a compatible worker.
        # Add exact-match workers for any task label-set that no current worker can satisfy.
        for task in workflow.tasks:
            if any(Model.labels_match(labels, task.labels) for _, _, labels in specs):
                continue
            specs.append(
                (
                    f"worker-placement-{len(specs):02d}",
                    1,
                    dict(task.labels),
                )
            )
        return tuple(specs)

    def scenario(self, *, steps: int = 250) -> GeneratedScenario:
        workflow = self.workflow()
        workers = self.workers(workflow)
        model = Model()
        wf = model.add_workflow(
            workflow.name,
            workflow.tasks,
            workflow_id=f"workflow-{self.seed}",
            metadata=workflow.metadata,
        )
        for worker_id, capacity, labels in workers:
            model.register_worker(
                worker_id,
                worker_id=worker_id,
                capacity=capacity,
                labels=labels,
            )

        actions: list[Action] = []
        notes: list[str] = []
        for _ in range(max(1, int(steps))):
            action = self.next_action(model)
            if action is None:
                if model.terminal():
                    break
                action = Action(ActionKind.ADVANCE_TIME, amount=self.rng.randint(1, 500))
            actions.append(action)
            try:
                model.apply(action)
            except Exception as exc:  # rejections are part of generated adversarial traces
                notes.append(f"{action.kind.value}: {type(exc).__name__}: {exc}")
            model.check_invariants()
            if model.terminal() and not model.active_lease_tokens():
                break

        return GeneratedScenario(
            seed=self.seed,
            workflow=workflow,
            worker_specs=workers,
            actions=tuple(actions),
            notes=tuple(notes),
        )

    def next_action(self, model: Model) -> Action | None:
        choices: list[tuple[float, Action]] = []
        live_workers = model.worker_ids()
        active = model.active_lease_tokens()
        closed = model.closed_lease_tokens()
        workflows = model.workflow_ids()

        for worker_id in live_workers:
            worker = model.workers[worker_id]
            if not worker.dropped:
                choices.append((5.0, Action(ActionKind.LEASE, worker_id=worker_id)))
                if self.rng.random() < self.config.heartbeat_probability:
                    choices.append((1.5, Action(ActionKind.HEARTBEAT, worker_id=worker_id)))
                if self.rng.random() < self.config.worker_drop_probability:
                    choices.append((0.5, Action(ActionKind.DROP_WORKER, worker_id=worker_id)))

        for token in active:
            lease = model.leases[token]
            task = model.workflows[lease.workflow_id].tasks[lease.task_id]
            if task.state == TaskState.LEASED:
                choices.append((5.0, Action(ActionKind.START, token=token)))
            if task.state in {TaskState.LEASED, TaskState.RUNNING}:
                choices.append((4.5, self._completion_action(token)))
                if self.rng.random() < self.config.renew_probability:
                    choices.append((1.3, Action(ActionKind.RENEW, token=token)))
                if self.rng.random() < self.config.log_probability:
                    choices.append(
                        (
                            1.0,
                            Action(
                                ActionKind.LOG,
                                token=token,
                                text=self._log_text(lease.workflow_id, lease.task_id),
                            ),
                        )
                    )

        if closed and self.rng.random() < self.config.stale_operation_probability:
            token = self.rng.choice(closed)
            kind = self.rng.choice(
                (
                    ActionKind.STALE_COMPLETE,
                    ActionKind.STALE_RENEW,
                    ActionKind.DUPLICATE_COMPLETE,
                )
            )
            choices.append((0.9, Action(kind, token=token)))

        if workflows and self.rng.random() < self.config.cancel_probability:
            workflow_id = self.rng.choice(workflows)
            if not model.workflows[workflow_id].state.terminal:
                choices.append((0.4, Action(ActionKind.CANCEL_WORKFLOW, workflow_id=workflow_id)))

        # Advancing time is always an option. Larger jumps stress lease expiry and retry wakeups.
        choices.append(
            (
                1.8,
                Action(
                    ActionKind.ADVANCE_TIME,
                    amount=self.rng.randint(1, self.config.max_time_step_ms),
                ),
            )
        )
        choices.append((0.3, Action(ActionKind.SWEEP)))

        if not choices:
            return None
        total = sum(weight for weight, _ in choices)
        pick = self.rng.random() * total
        cursor = 0.0
        for weight, action in choices:
            cursor += weight
            if pick <= cursor:
                return action
        return choices[-1][1]

    def _task_labels(self) -> dict[str, str]:
        if self.rng.random() >= self.config.label_probability:
            return {}
        key, value = self.rng.choice(self.LABELS)
        labels = {key: value}
        if self.rng.random() < 0.25:
            other_key, other_value = self.rng.choice(self.LABELS)
            if other_key != key:
                labels[other_key] = other_value
        return labels

    def _retry_policy(self) -> RetryPolicy:
        if self.rng.random() >= self.config.retry_probability:
            return RetryPolicy()
        attempts = self.rng.randint(2, 5)
        base = self.rng.choice((50, 100, 250, 500, 1_000))
        maximum = max(base, self.rng.choice((500, 1_000, 2_000, 5_000, 10_000)))
        return RetryPolicy(attempts, base, maximum)

    def _completion_action(self, token: str) -> Action:
        if self.rng.random() < self.config.failure_probability:
            return Action(
                ActionKind.COMPLETE_FAILURE,
                token=token,
                amount=self.rng.choice((1, 2, 3, 125, 137)),
                text=self.rng.choice(
                    (
                        "simulated process failure",
                        "simulated dependency outage",
                        "simulated worker exception",
                        "simulated non-zero exit",
                    )
                ),
            )
        return Action(ActionKind.COMPLETE_SUCCESS, token=token)

    def _log_text(self, workflow_id: str, task_id: str) -> str:
        messages = (
            "starting phase",
            "checkpoint reached",
            "processing batch",
            "artifact written",
            "dependency response received",
            "validation passed",
        )
        return f"[{workflow_id}/{task_id}] {self.rng.choice(messages)}\n"


def generate_many(
    seeds: Iterable[int],
    *,
    config: GeneratorConfig | None = None,
    steps: int = 250,
) -> list[GeneratedScenario]:
    return [ScenarioGenerator(seed, config=config).scenario(steps=steps) for seed in seeds]


def clone_workflow(workflow: GeneratedWorkflow) -> GeneratedWorkflow:
    return GeneratedWorkflow(
        name=workflow.name,
        tasks=tuple(task.clone() for task in workflow.tasks),
        metadata=dict(workflow.metadata),
    )


def replace_task(workflow: GeneratedWorkflow, task_id: str, **changes) -> GeneratedWorkflow:
    tasks: list[Task] = []
    found = False
    for task in workflow.tasks:
        copy = task.clone()
        if copy.id == task_id:
            found = True
            for key, value in changes.items():
                if not hasattr(copy, key):
                    raise AttributeError(key)
                setattr(copy, key, value)
        tasks.append(copy)
    if not found:
        raise KeyError(task_id)
    Model.validate_dag(tasks)
    return dataclasses.replace(workflow, tasks=tuple(tasks))
