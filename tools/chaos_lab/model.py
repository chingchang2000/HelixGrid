from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence


class WorkflowState(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class TaskState(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class ActionKind(str, enum.Enum):
    REGISTER_WORKER = "register_worker"
    HEARTBEAT = "heartbeat"
    LEASE = "lease"
    START = "start"
    RENEW = "renew"
    LOG = "log"
    COMPLETE_SUCCESS = "complete_success"
    COMPLETE_FAILURE = "complete_failure"
    ADVANCE_TIME = "advance_time"
    SWEEP = "sweep"
    CANCEL_WORKFLOW = "cancel_workflow"
    DROP_WORKER = "drop_worker"
    STALE_COMPLETE = "stale_complete"
    STALE_RENEW = "stale_renew"
    DUPLICATE_COMPLETE = "duplicate_complete"


class ModelError(RuntimeError):
    """Raised when an operation is rejected by the reference model."""


class InvariantViolation(AssertionError):
    def __init__(self, code: str, message: str, *, snapshot: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.snapshot = dict(snapshot or {})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: int = 250
    max_delay_ms: int = 30_000

    def normalized(self) -> "RetryPolicy":
        attempts = min(100, max(1, int(self.max_attempts)))
        base = max(1, int(self.base_delay_ms))
        maximum = max(base, int(self.max_delay_ms))
        return RetryPolicy(attempts, base, maximum)

    def delay_for_attempt(self, attempt: int) -> int:
        policy = self.normalized()
        exponent = max(0, int(attempt) - 1)
        delay = policy.base_delay_ms
        for _ in range(exponent):
            delay = min(policy.max_delay_ms, delay * 2)
            if delay == policy.max_delay_ms:
                break
        return delay


@dataclass(slots=True)
class Task:
    id: str
    depends_on: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_ms: int = 0
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    lease_token: str = ""
    lease_owner: str = ""
    lease_until_ms: int | None = None
    next_retry_ms: int | None = None
    started_ms: int | None = None
    finished_ms: int | None = None
    exit_code: int | None = None
    error: str = ""
    output_bytes: int = 0
    history: list[TaskState] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.depends_on = tuple(self.depends_on)
        self.labels = dict(self.labels)
        self.retry = self.retry.normalized()
        if not self.id:
            raise ValueError("task id may not be empty")
        if self.timeout_ms < 0:
            raise ValueError("task timeout may not be negative")

    def clone(self) -> "Task":
        copy = dataclasses.replace(self)
        copy.labels = dict(self.labels)
        copy.history = list(self.history)
        return copy


@dataclass(slots=True)
class Workflow:
    id: str
    name: str
    tasks: dict[str, Task]
    order: tuple[str, ...]
    created_ms: int
    state: WorkflowState = WorkflowState.PENDING
    finished_ms: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def clone(self) -> "Workflow":
        return Workflow(
            id=self.id,
            name=self.name,
            tasks={task_id: task.clone() for task_id, task in self.tasks.items()},
            order=tuple(self.order),
            created_ms=self.created_ms,
            state=self.state,
            finished_ms=self.finished_ms,
            metadata=dict(self.metadata),
        )


@dataclass(slots=True)
class Worker:
    id: str
    name: str
    capacity: int = 1
    labels: dict[str, str] = field(default_factory=dict)
    registered_ms: int = 0
    heartbeat_ms: int = 0
    active_tokens: set[str] = field(default_factory=set)
    dropped: bool = False

    def clone(self) -> "Worker":
        copy = dataclasses.replace(self)
        copy.labels = dict(self.labels)
        copy.active_tokens = set(self.active_tokens)
        return copy


@dataclass(slots=True)
class Lease:
    token: str
    workflow_id: str
    task_id: str
    worker_id: str
    attempt: int
    acquired_ms: int
    expires_ms: int
    started: bool = False
    closed: bool = False

    def clone(self) -> "Lease":
        return dataclasses.replace(self)


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    type: str
    at_ms: int
    workflow_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    data: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.sequence,
            "type": self.type,
            "at_ms": self.at_ms,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    workflow_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    token: str = ""
    amount: int = 0
    text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "token": self.token,
            "amount": self.amount,
            "text": self.text,
        }


_ALLOWED_TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.READY, TaskState.CANCELLED},
    TaskState.READY: {TaskState.LEASED, TaskState.CANCELLED},
    TaskState.LEASED: {TaskState.RUNNING, TaskState.READY, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.RETRY_WAIT,
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.READY,
        TaskState.CANCELLED,
    },
    TaskState.RETRY_WAIT: {TaskState.READY, TaskState.CANCELLED},
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


class Model:
    """Independent, deterministic reference implementation of HelixGrid semantics.

    The model intentionally omits networking and process execution. Its job is to make
    scheduling and lease invariants explicit enough that generated action sequences can be
    checked against both this implementation and the real coordinator API.
    """

    def __init__(
        self,
        *,
        lease_duration_ms: int = 20_000,
        worker_ttl_ms: int = 45_000,
        output_limit_bytes: int = 32 * 1024 * 1024,
        log_chunk_limit_bytes: int = 64 * 1024,
    ) -> None:
        if lease_duration_ms <= 0:
            raise ValueError("lease_duration_ms must be positive")
        if worker_ttl_ms <= 0:
            raise ValueError("worker_ttl_ms must be positive")
        self.now_ms = 0
        self.lease_duration_ms = int(lease_duration_ms)
        self.worker_ttl_ms = int(worker_ttl_ms)
        self.output_limit_bytes = int(output_limit_bytes)
        self.log_chunk_limit_bytes = int(log_chunk_limit_bytes)
        self.workflows: dict[str, Workflow] = {}
        self.workers: dict[str, Worker] = {}
        self.leases: dict[str, Lease] = {}
        self.closed_leases: dict[str, Lease] = {}
        self.events: list[Event] = []
        self.rejections: Counter[str] = Counter()
        self._sequence = 0
        self._id_counter = 0

    def next_id(self, prefix: str) -> str:
        self._id_counter += 1
        digest = hashlib.blake2s(
            f"{prefix}:{self._id_counter}:{self.now_ms}".encode("utf-8"),
            digest_size=6,
        ).hexdigest()
        return f"{prefix}_{digest}"

    def add_workflow(
        self,
        name: str,
        tasks: Sequence[Task],
        *,
        workflow_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Workflow:
        workflow_id = workflow_id or self.next_id("wf")
        if workflow_id in self.workflows:
            raise ModelError("workflow already exists")
        order = self.validate_dag(tasks)
        task_map = {task.id: task.clone() for task in tasks}
        for task in task_map.values():
            task.state = TaskState.PENDING
            task.attempt = 0
            task.lease_token = ""
            task.lease_owner = ""
            task.lease_until_ms = None
            task.next_retry_ms = None
            task.started_ms = None
            task.finished_ms = None
            task.exit_code = None
            task.error = ""
            task.output_bytes = 0
            task.history.clear()
        workflow = Workflow(
            id=workflow_id,
            name=name,
            tasks=task_map,
            order=tuple(order),
            created_ms=self.now_ms,
            metadata=dict(metadata or {}),
        )
        self.workflows[workflow.id] = workflow
        self.emit("workflow.created", workflow_id=workflow.id, data={"name": name})
        self.recompute(workflow.id)
        return workflow.clone()

    @staticmethod
    def validate_dag(tasks: Sequence[Task]) -> list[str]:
        if not tasks:
            raise ModelError("workflow must contain at least one task")
        by_id: dict[str, Task] = {}
        for task in tasks:
            if task.id in by_id:
                raise ModelError(f"duplicate task id: {task.id}")
            by_id[task.id] = task
        indegree = {task.id: 0 for task in tasks}
        children: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            seen: set[str] = set()
            for dependency in task.depends_on:
                if dependency == task.id:
                    raise ModelError(f"task {task.id} depends on itself")
                if dependency in seen:
                    raise ModelError(f"task {task.id} repeats dependency {dependency}")
                seen.add(dependency)
                if dependency not in by_id:
                    raise ModelError(f"task {task.id} references unknown dependency {dependency}")
                indegree[task.id] += 1
                children[dependency].append(task.id)
        ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort()
        if len(order) != len(tasks):
            raise ModelError("workflow graph contains a dependency cycle")
        return order

    def register_worker(
        self,
        name: str,
        *,
        capacity: int = 1,
        labels: Mapping[str, str] | None = None,
        worker_id: str | None = None,
    ) -> Worker:
        if capacity < 1 or capacity > 256:
            raise ModelError("worker capacity must be 1..256")
        worker_id = worker_id or self.next_id("worker")
        if worker_id in self.workers and not self.workers[worker_id].dropped:
            raise ModelError("worker already exists")
        worker = Worker(
            id=worker_id,
            name=name,
            capacity=capacity,
            labels=dict(labels or {}),
            registered_ms=self.now_ms,
            heartbeat_ms=self.now_ms,
        )
        self.workers[worker.id] = worker
        self.emit("worker.registered", worker_id=worker.id, data={"name": worker.name})
        return worker.clone()

    def heartbeat(self, worker_id: str) -> Worker:
        worker = self.require_worker(worker_id)
        if worker.dropped:
            return self.reject("worker_dropped", "worker has been dropped")
        worker.heartbeat_ms = self.now_ms
        self.emit("worker.heartbeat", worker_id=worker.id)
        return worker.clone()

    def drop_worker(self, worker_id: str) -> None:
        worker = self.require_worker(worker_id)
        worker.dropped = True

    def lease_next(self, worker_id: str) -> Lease | None:
        self.sweep()
        worker = self.require_worker(worker_id)
        if worker.dropped:
            return self.reject("worker_dropped", "worker has been dropped")
        if self.now_ms - worker.heartbeat_ms > self.worker_ttl_ms:
            return self.reject("worker_expired", "worker heartbeat expired")
        if len(worker.active_tokens) >= worker.capacity:
            return None

        candidates: list[tuple[int, str, str]] = []
        for workflow in self.workflows.values():
            if workflow.state not in {WorkflowState.PENDING, WorkflowState.RUNNING}:
                continue
            self.recompute(workflow.id)
            for index, task_id in enumerate(workflow.order):
                task = workflow.tasks[task_id]
                if task.state != TaskState.READY:
                    continue
                if not self.labels_match(worker.labels, task.labels):
                    continue
                candidates.append((workflow.created_ms, workflow.id, f"{index:08d}:{task_id}"))
        if not candidates:
            return None
        candidates.sort()
        _, workflow_id, encoded = candidates[0]
        task_id = encoded.split(":", 1)[1]
        workflow = self.workflows[workflow_id]
        task = workflow.tasks[task_id]

        token = self.next_id("lease")
        task.attempt += 1
        self.transition_task(workflow, task, TaskState.LEASED)
        task.lease_token = token
        task.lease_owner = worker.id
        task.lease_until_ms = self.now_ms + self.lease_duration_ms
        task.next_retry_ms = None
        lease = Lease(
            token=token,
            workflow_id=workflow.id,
            task_id=task.id,
            worker_id=worker.id,
            attempt=task.attempt,
            acquired_ms=self.now_ms,
            expires_ms=task.lease_until_ms,
        )
        self.leases[token] = lease
        worker.active_tokens.add(token)
        self.emit(
            "task.leased",
            workflow_id=workflow.id,
            task_id=task.id,
            worker_id=worker.id,
            data={"attempt": str(task.attempt), "token": token},
        )
        return lease.clone()

    def start(self, token: str) -> Lease:
        lease = self.require_active_lease(token)
        self.ensure_live_lease(lease)
        workflow, task, _worker = self.resolve_lease(lease)
        if task.lease_token != token:
            return self.reject("stale_lease", "task no longer owns lease")
        if task.state == TaskState.LEASED:
            self.transition_task(workflow, task, TaskState.RUNNING)
            task.started_ms = self.now_ms
            lease.started = True
            self.emit(
                "task.started",
                workflow_id=workflow.id,
                task_id=task.id,
                worker_id=lease.worker_id,
            )
        elif task.state != TaskState.RUNNING:
            return self.reject("invalid_start_state", f"cannot start task in {task.state}")
        return lease.clone()

    def renew(self, token: str) -> Lease:
        lease = self.require_active_lease(token)
        self.ensure_live_lease(lease)
        workflow, task, _worker = self.resolve_lease(lease)
        if task.lease_token != token:
            return self.reject("stale_lease", "task no longer owns lease")
        lease.expires_ms = self.now_ms + self.lease_duration_ms
        task.lease_until_ms = lease.expires_ms
        return lease.clone()

    def append_log(self, token: str, text: str, *, stream: str = "stdout") -> None:
        lease = self.require_active_lease(token)
        self.ensure_live_lease(lease)
        workflow, task, _worker = self.resolve_lease(lease)
        if stream not in {"stdout", "stderr"}:
            return self.reject("invalid_stream", "stream must be stdout or stderr")
        size = len(text.encode("utf-8"))
        if size > self.log_chunk_limit_bytes:
            return self.reject("log_chunk_too_large", "log chunk exceeds limit")
        if task.output_bytes + size > self.output_limit_bytes:
            return self.reject("task_output_limit", "task output exceeds limit")
        task.output_bytes += size
        self.emit(
            "task.log",
            workflow_id=workflow.id,
            task_id=task.id,
            worker_id=lease.worker_id,
            data={"stream": stream, "text": text},
        )

    def complete_success(self, token: str) -> Workflow:
        return self.complete(token, exit_code=0, error="")

    def complete_failure(self, token: str, *, exit_code: int = 1, error: str = "failure") -> Workflow:
        if exit_code == 0 and not error:
            exit_code = 1
        return self.complete(token, exit_code=exit_code, error=error)

    def complete(self, token: str, *, exit_code: int, error: str) -> Workflow:
        lease = self.require_active_lease(token)
        self.ensure_live_lease(lease)
        workflow, task, worker = self.resolve_lease(lease)
        if task.lease_token != token:
            return self.reject("stale_lease", "task no longer owns lease")
        if task.state == TaskState.LEASED:
            self.transition_task(workflow, task, TaskState.RUNNING)
            task.started_ms = self.now_ms
            lease.started = True
        if task.state != TaskState.RUNNING:
            return self.reject("invalid_complete_state", f"cannot complete task in {task.state}")

        task.finished_ms = self.now_ms
        task.exit_code = int(exit_code)
        task.error = error
        self.close_lease(lease)
        if exit_code == 0 and not error:
            self.transition_task(workflow, task, TaskState.SUCCEEDED)
            self.emit(
                "task.succeeded",
                workflow_id=workflow.id,
                task_id=task.id,
                worker_id=worker.id,
            )
        else:
            policy = task.retry.normalized()
            if task.attempt < policy.max_attempts:
                self.transition_task(workflow, task, TaskState.RETRY_WAIT)
                task.next_retry_ms = self.now_ms + policy.delay_for_attempt(task.attempt)
                self.emit(
                    "task.retry",
                    workflow_id=workflow.id,
                    task_id=task.id,
                    worker_id=worker.id,
                    data={"next_retry_ms": str(task.next_retry_ms)},
                )
            else:
                self.transition_task(workflow, task, TaskState.FAILED)
                self.emit(
                    "task.failed",
                    workflow_id=workflow.id,
                    task_id=task.id,
                    worker_id=worker.id,
                    data={"exit_code": str(exit_code), "error": error},
                )
        self.recompute(workflow.id)
        return workflow.clone()

    def stale_complete(self, token: str) -> None:
        if token in self.leases:
            return self.reject("not_stale", "lease is still active")
        if token not in self.closed_leases:
            return self.reject("unknown_lease", "lease token does not exist")
        return self.reject("stale_lease", "completion for closed lease rejected")

    def stale_renew(self, token: str) -> None:
        if token in self.leases:
            return self.reject("not_stale", "lease is still active")
        if token not in self.closed_leases:
            return self.reject("unknown_lease", "lease token does not exist")
        return self.reject("stale_lease", "renewal for closed lease rejected")

    def duplicate_complete(self, token: str) -> None:
        if token in self.leases:
            return self.reject("not_completed", "lease is still active")
        if token not in self.closed_leases:
            return self.reject("unknown_lease", "lease token does not exist")
        return self.reject("duplicate_completion", "completion already applied")

    def cancel_workflow(self, workflow_id: str) -> Workflow:
        workflow = self.require_workflow(workflow_id)
        if workflow.state.terminal:
            return workflow.clone()
        workflow.state = WorkflowState.CANCELLED
        workflow.finished_ms = self.now_ms
        for task_id in workflow.order:
            task = workflow.tasks[task_id]
            if task.state.terminal:
                continue
            if task.lease_token:
                lease = self.leases.get(task.lease_token)
                if lease is not None:
                    self.close_lease(lease)
            task.next_retry_ms = None
            task.lease_token = ""
            task.lease_owner = ""
            task.lease_until_ms = None
            task.finished_ms = self.now_ms
            self.transition_task(workflow, task, TaskState.CANCELLED)
        self.emit("workflow.cancelled", workflow_id=workflow.id)
        return workflow.clone()

    def advance(self, milliseconds: int, *, sweep: bool = True) -> None:
        if milliseconds < 0:
            raise ModelError("time cannot move backwards")
        self.now_ms += int(milliseconds)
        if sweep:
            self.sweep()

    def sweep(self) -> None:
        expired = sorted(
            (
                lease
                for lease in self.leases.values()
                if lease.expires_ms <= self.now_ms
            ),
            key=lambda lease: (lease.expires_ms, lease.token),
        )
        for lease in expired:
            self.expire_lease(lease.token)
        for workflow_id in sorted(self.workflows):
            self.recompute(workflow_id)

    def expire_lease(self, token: str) -> None:
        lease = self.leases.get(token)
        if lease is None:
            return
        if lease.expires_ms > self.now_ms:
            raise ModelError("cannot expire live lease")
        workflow, task, worker = self.resolve_lease(lease)
        self.close_lease(lease)
        if task.state in {TaskState.LEASED, TaskState.RUNNING} and not workflow.state.terminal:
            task.started_ms = None
            task.finished_ms = None
            task.exit_code = None
            task.error = ""
            self.transition_task(workflow, task, TaskState.READY)
            self.emit(
                "lease.expired",
                workflow_id=workflow.id,
                task_id=task.id,
                worker_id=worker.id,
                data={"token": token},
            )

    def recompute(self, workflow_id: str) -> None:
        workflow = self.require_workflow(workflow_id)
        if workflow.state.terminal:
            return
        before = workflow.state
        for task_id in workflow.order:
            task = workflow.tasks[task_id]
            if task.state == TaskState.PENDING:
                dependencies = [workflow.tasks[dep] for dep in task.depends_on]
                impossible = any(dep.state in {TaskState.FAILED, TaskState.CANCELLED} for dep in dependencies)
                ready = all(dep.state == TaskState.SUCCEEDED for dep in dependencies)
                if impossible:
                    task.finished_ms = self.now_ms
                    self.transition_task(workflow, task, TaskState.CANCELLED)
                elif ready:
                    self.transition_task(workflow, task, TaskState.READY)
                    self.emit("task.ready", workflow_id=workflow.id, task_id=task.id)
            elif task.state == TaskState.RETRY_WAIT:
                if task.next_retry_ms is None or task.next_retry_ms <= self.now_ms:
                    task.next_retry_ms = None
                    task.finished_ms = None
                    self.transition_task(workflow, task, TaskState.READY)
                    self.emit("task.ready", workflow_id=workflow.id, task_id=task.id)

        states = [task.state for task in workflow.tasks.values()]
        if all(state == TaskState.SUCCEEDED for state in states):
            workflow.state = WorkflowState.SUCCEEDED
            workflow.finished_ms = self.now_ms
        elif any(state == TaskState.FAILED for state in states) and not any(
            state in {TaskState.READY, TaskState.LEASED, TaskState.RUNNING, TaskState.RETRY_WAIT}
            for state in states
        ):
            workflow.state = WorkflowState.FAILED
            workflow.finished_ms = self.now_ms
        elif workflow.state == WorkflowState.PENDING:
            workflow.state = WorkflowState.RUNNING

        if workflow.state != before:
            if workflow.state == WorkflowState.RUNNING:
                self.emit("workflow.started", workflow_id=workflow.id)
            elif workflow.state == WorkflowState.SUCCEEDED:
                self.emit("workflow.succeeded", workflow_id=workflow.id)
            elif workflow.state == WorkflowState.FAILED:
                self.emit("workflow.failed", workflow_id=workflow.id)

    def transition_task(self, workflow: Workflow, task: Task, next_state: TaskState) -> None:
        if next_state == task.state:
            return
        if next_state not in _ALLOWED_TASK_TRANSITIONS[task.state]:
            raise ModelError(f"invalid transition {task.state} -> {next_state} for {workflow.id}/{task.id}")
        task.state = next_state
        task.history.append(next_state)

    @staticmethod
    def labels_match(worker_labels: Mapping[str, str], task_labels: Mapping[str, str]) -> bool:
        return all(worker_labels.get(key) == value for key, value in task_labels.items())

    def ensure_live_lease(self, lease: Lease) -> None:
        if lease.expires_ms <= self.now_ms:
            self.expire_lease(lease.token)
            return self.reject("lease_expired", "lease has expired")

    def close_lease(self, lease: Lease) -> None:
        if lease.closed:
            return
        workflow, task, worker = self.resolve_lease(lease)
        self.leases.pop(lease.token, None)
        worker.active_tokens.discard(lease.token)
        if task.lease_token == lease.token:
            task.lease_token = ""
            task.lease_owner = ""
            task.lease_until_ms = None
        lease.closed = True
        self.closed_leases[lease.token] = lease.clone()

    def resolve_lease(self, lease: Lease) -> tuple[Workflow, Task, Worker]:
        workflow = self.require_workflow(lease.workflow_id)
        try:
            task = workflow.tasks[lease.task_id]
        except KeyError as exc:
            raise ModelError("lease references missing task") from exc
        worker = self.require_worker(lease.worker_id)
        return workflow, task, worker

    def require_active_lease(self, token: str) -> Lease:
        try:
            return self.leases[token]
        except KeyError:
            if token in self.closed_leases:
                return self.reject("stale_lease", "lease is already closed")
            return self.reject("unknown_lease", "unknown lease token")

    def require_worker(self, worker_id: str) -> Worker:
        try:
            return self.workers[worker_id]
        except KeyError as exc:
            raise ModelError(f"unknown worker: {worker_id}") from exc

    def require_workflow(self, workflow_id: str) -> Workflow:
        try:
            return self.workflows[workflow_id]
        except KeyError as exc:
            raise ModelError(f"unknown workflow: {workflow_id}") from exc

    def emit(
        self,
        event_type: str,
        *,
        workflow_id: str = "",
        task_id: str = "",
        worker_id: str = "",
        data: Mapping[str, str] | None = None,
    ) -> Event:
        self._sequence += 1
        event = Event(
            sequence=self._sequence,
            type=event_type,
            at_ms=self.now_ms,
            workflow_id=workflow_id,
            task_id=task_id,
            worker_id=worker_id,
            data=dict(data or {}),
        )
        self.events.append(event)
        return event

    def reject(self, code: str, message: str):
        self.rejections[code] += 1
        raise ModelError(f"{code}: {message}")

    def apply(self, action: Action) -> Any:
        if action.kind == ActionKind.REGISTER_WORKER:
            return self.register_worker(
                action.text or action.worker_id or "worker",
                capacity=max(1, action.amount or 1),
                worker_id=action.worker_id or None,
            )
        if action.kind == ActionKind.HEARTBEAT:
            return self.heartbeat(action.worker_id)
        if action.kind == ActionKind.LEASE:
            return self.lease_next(action.worker_id)
        if action.kind == ActionKind.START:
            return self.start(action.token)
        if action.kind == ActionKind.RENEW:
            return self.renew(action.token)
        if action.kind == ActionKind.LOG:
            return self.append_log(action.token, action.text or "log\n")
        if action.kind == ActionKind.COMPLETE_SUCCESS:
            return self.complete_success(action.token)
        if action.kind == ActionKind.COMPLETE_FAILURE:
            return self.complete_failure(action.token, exit_code=max(1, action.amount or 1), error=action.text or "failure")
        if action.kind == ActionKind.ADVANCE_TIME:
            return self.advance(max(0, action.amount))
        if action.kind == ActionKind.SWEEP:
            return self.sweep()
        if action.kind == ActionKind.CANCEL_WORKFLOW:
            return self.cancel_workflow(action.workflow_id)
        if action.kind == ActionKind.DROP_WORKER:
            return self.drop_worker(action.worker_id)
        if action.kind == ActionKind.STALE_COMPLETE:
            return self.stale_complete(action.token)
        if action.kind == ActionKind.STALE_RENEW:
            return self.stale_renew(action.token)
        if action.kind == ActionKind.DUPLICATE_COMPLETE:
            return self.duplicate_complete(action.token)
        raise ModelError(f"unhandled action kind: {action.kind}")

    def check_invariants(self) -> None:
        self._check_event_sequence()
        self._check_worker_capacity()
        self._check_lease_bijection()
        self._check_task_lease_fields()
        self._check_dependency_safety()
        self._check_terminal_workflows()
        self._check_attempts()
        self._check_retry_times()
        self._check_timestamps()
        self._check_output_limits()

    def _violate(self, code: str, message: str) -> None:
        raise InvariantViolation(code, message, snapshot=self.snapshot())

    def _check_event_sequence(self) -> None:
        for index, event in enumerate(self.events, start=1):
            if event.sequence != index:
                self._violate("EVENT_SEQUENCE", f"event index {index} has sequence {event.sequence}")
            if index > 1 and event.at_ms < self.events[index - 2].at_ms:
                self._violate("EVENT_TIME", "event time moved backwards")

    def _check_worker_capacity(self) -> None:
        for worker in self.workers.values():
            if len(worker.active_tokens) > worker.capacity:
                self._violate(
                    "WORKER_CAPACITY",
                    f"worker {worker.id} has {len(worker.active_tokens)} leases but capacity {worker.capacity}",
                )
            for token in worker.active_tokens:
                lease = self.leases.get(token)
                if lease is None:
                    self._violate("WORKER_GHOST_LEASE", f"worker {worker.id} tracks missing lease {token}")
                if lease.worker_id != worker.id:
                    self._violate("WORKER_LEASE_OWNER", f"worker {worker.id} tracks lease owned by {lease.worker_id}")

    def _check_lease_bijection(self) -> None:
        task_keys: set[tuple[str, str]] = set()
        for token, lease in self.leases.items():
            if token != lease.token:
                self._violate("LEASE_KEY", f"lease key {token} differs from payload token {lease.token}")
            key = (lease.workflow_id, lease.task_id)
            if key in task_keys:
                self._violate("OVERLAPPING_LEASE", f"task {key} has more than one lease")
            task_keys.add(key)
            if lease.closed:
                self._violate("ACTIVE_CLOSED_LEASE", f"active lease {token} is marked closed")
            if lease.expires_ms <= lease.acquired_ms:
                self._violate("LEASE_DURATION", f"lease {token} has non-positive duration")
            worker = self.workers.get(lease.worker_id)
            if worker is None or token not in worker.active_tokens:
                self._violate("LEASE_WORKER_INDEX", f"lease {token} missing from worker index")

    def _check_task_lease_fields(self) -> None:
        for workflow in self.workflows.values():
            for task in workflow.tasks.values():
                if task.state in {TaskState.LEASED, TaskState.RUNNING}:
                    if not task.lease_token:
                        self._violate("ACTIVE_TASK_NO_LEASE", f"{workflow.id}/{task.id} lacks token")
                    lease = self.leases.get(task.lease_token)
                    if lease is None:
                        self._violate("ACTIVE_TASK_UNKNOWN_LEASE", f"{workflow.id}/{task.id} references missing lease")
                    if lease.workflow_id != workflow.id or lease.task_id != task.id:
                        self._violate("TASK_LEASE_TARGET", f"{workflow.id}/{task.id} lease points elsewhere")
                    if task.lease_owner != lease.worker_id:
                        self._violate("TASK_LEASE_OWNER", f"{workflow.id}/{task.id} owner mismatch")
                    if task.lease_until_ms != lease.expires_ms:
                        self._violate("TASK_LEASE_EXPIRY", f"{workflow.id}/{task.id} expiry mismatch")
                else:
                    if task.lease_token or task.lease_owner or task.lease_until_ms is not None:
                        self._violate("INACTIVE_TASK_HAS_LEASE", f"{workflow.id}/{task.id} retains lease fields")

    def _check_dependency_safety(self) -> None:
        active_states = {TaskState.READY, TaskState.LEASED, TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.RETRY_WAIT}
        for workflow in self.workflows.values():
            for task in workflow.tasks.values():
                if task.state not in active_states:
                    continue
                for dependency_id in task.depends_on:
                    dependency = workflow.tasks[dependency_id]
                    if task.state in {TaskState.READY, TaskState.LEASED, TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.RETRY_WAIT} and dependency.state != TaskState.SUCCEEDED:
                        self._violate(
                            "DEPENDENCY_SAFETY",
                            f"{workflow.id}/{task.id} is {task.state} while dependency {dependency_id} is {dependency.state}",
                        )

    def _check_terminal_workflows(self) -> None:
        for workflow in self.workflows.values():
            states = [task.state for task in workflow.tasks.values()]
            if workflow.state == WorkflowState.SUCCEEDED and not all(state == TaskState.SUCCEEDED for state in states):
                self._violate("WORKFLOW_FALSE_SUCCESS", f"workflow {workflow.id} succeeded with incomplete tasks")
            if workflow.state == WorkflowState.CANCELLED:
                if any(not state.terminal for state in states):
                    self._violate("WORKFLOW_CANCEL_ACTIVE", f"cancelled workflow {workflow.id} has active task")
                if any(lease.workflow_id == workflow.id for lease in self.leases.values()):
                    self._violate("WORKFLOW_CANCEL_LEASE", f"cancelled workflow {workflow.id} has active lease")
            if workflow.state.terminal and workflow.finished_ms is None:
                self._violate("WORKFLOW_TERMINAL_TIME", f"terminal workflow {workflow.id} lacks finished timestamp")
            if not workflow.state.terminal and workflow.finished_ms is not None:
                self._violate("WORKFLOW_ACTIVE_FINISHED", f"active workflow {workflow.id} has finished timestamp")

    def _check_attempts(self) -> None:
        for workflow in self.workflows.values():
            for task in workflow.tasks.values():
                if task.attempt < 0:
                    self._violate("NEGATIVE_ATTEMPT", f"{workflow.id}/{task.id} has negative attempt")
                if task.attempt > task.retry.max_attempts:
                    self._violate("ATTEMPT_BUDGET", f"{workflow.id}/{task.id} exceeded retry budget")
                if task.state == TaskState.PENDING and task.attempt != 0:
                    self._violate("PENDING_ATTEMPT", f"pending task {workflow.id}/{task.id} already attempted")
                if task.state in {TaskState.LEASED, TaskState.RUNNING} and task.attempt < 1:
                    self._violate("ACTIVE_ATTEMPT", f"active task {workflow.id}/{task.id} has no attempt")

    def _check_retry_times(self) -> None:
        for workflow in self.workflows.values():
            for task in workflow.tasks.values():
                if task.state == TaskState.RETRY_WAIT and task.next_retry_ms is None:
                    self._violate("RETRY_TIME", f"{workflow.id}/{task.id} waits for retry without deadline")
                if task.state != TaskState.RETRY_WAIT and task.next_retry_ms is not None:
                    self._violate("STALE_RETRY_TIME", f"{workflow.id}/{task.id} retains retry deadline in {task.state}")

    def _check_timestamps(self) -> None:
        for workflow in self.workflows.values():
            if workflow.created_ms > self.now_ms:
                self._violate("WORKFLOW_FUTURE_CREATED", f"workflow {workflow.id} created in future")
            if workflow.finished_ms is not None and workflow.finished_ms < workflow.created_ms:
                self._violate("WORKFLOW_TIME_ORDER", f"workflow {workflow.id} finished before creation")
            for task in workflow.tasks.values():
                if task.started_ms is not None and task.started_ms > self.now_ms:
                    self._violate("TASK_FUTURE_START", f"{workflow.id}/{task.id} starts in future")
                if task.finished_ms is not None and task.finished_ms > self.now_ms:
                    self._violate("TASK_FUTURE_FINISH", f"{workflow.id}/{task.id} finishes in future")
                if task.started_ms is not None and task.finished_ms is not None and task.finished_ms < task.started_ms:
                    self._violate("TASK_TIME_ORDER", f"{workflow.id}/{task.id} finished before start")

    def _check_output_limits(self) -> None:
        for workflow in self.workflows.values():
            for task in workflow.tasks.values():
                if task.output_bytes < 0:
                    self._violate("NEGATIVE_OUTPUT", f"{workflow.id}/{task.id} has negative output")
                if task.output_bytes > self.output_limit_bytes:
                    self._violate("OUTPUT_LIMIT", f"{workflow.id}/{task.id} exceeds output limit")

    def snapshot(self) -> dict[str, Any]:
        return {
            "now_ms": self.now_ms,
            "workflow_count": len(self.workflows),
            "worker_count": len(self.workers),
            "lease_count": len(self.leases),
            "closed_lease_count": len(self.closed_leases),
            "event_count": len(self.events),
            "rejections": dict(self.rejections),
            "workflows": {
                workflow_id: {
                    "state": workflow.state.value,
                    "tasks": {
                        task_id: {
                            "state": task.state.value,
                            "attempt": task.attempt,
                            "lease_token": task.lease_token,
                            "lease_owner": task.lease_owner,
                            "lease_until_ms": task.lease_until_ms,
                            "next_retry_ms": task.next_retry_ms,
                            "output_bytes": task.output_bytes,
                        }
                        for task_id, task in workflow.tasks.items()
                    },
                }
                for workflow_id, workflow in self.workflows.items()
            },
            "workers": {
                worker_id: {
                    "capacity": worker.capacity,
                    "heartbeat_ms": worker.heartbeat_ms,
                    "active_tokens": sorted(worker.active_tokens),
                    "dropped": worker.dropped,
                }
                for worker_id, worker in self.workers.items()
            },
            "leases": {
                token: {
                    "workflow_id": lease.workflow_id,
                    "task_id": lease.task_id,
                    "worker_id": lease.worker_id,
                    "attempt": lease.attempt,
                    "expires_ms": lease.expires_ms,
                    "started": lease.started,
                }
                for token, lease in self.leases.items()
            },
        }

    def digest(self) -> str:
        payload = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def event_jsonl(self) -> str:
        return "".join(json.dumps(event.as_dict(), sort_keys=True) + "\n" for event in self.events)

    def terminal(self) -> bool:
        return bool(self.workflows) and all(workflow.state.terminal for workflow in self.workflows.values())

    def runnable_tasks(self) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for workflow in self.workflows.values():
            for task_id in workflow.order:
                if workflow.tasks[task_id].state == TaskState.READY:
                    result.append((workflow.id, task_id))
        return sorted(result)

    def active_lease_tokens(self) -> list[str]:
        return sorted(self.leases)

    def closed_lease_tokens(self) -> list[str]:
        return sorted(self.closed_leases)

    def worker_ids(self, *, include_dropped: bool = False) -> list[str]:
        return sorted(
            worker.id
            for worker in self.workers.values()
            if include_dropped or not worker.dropped
        )

    def workflow_ids(self) -> list[str]:
        return sorted(self.workflows)

    def state_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for workflow in self.workflows.values():
            counts[f"workflow.{workflow.state.value}"] += 1
            for task in workflow.tasks.values():
                counts[f"task.{task.state.value}"] += 1
        return dict(sorted(counts.items()))
