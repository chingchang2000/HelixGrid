from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence


class HelixError(RuntimeError):
    """Base exception raised by the HelixGrid SDK."""


class APIError(HelixError):
    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"HelixGrid API returned HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: int = 250
    max_delay_ms: int = 30_000

    def as_dict(self) -> Dict[str, int]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay_ms": self.base_delay_ms,
            "max_delay_ms": self.max_delay_ms,
        }


@dataclass(slots=True)
class Task:
    id: str
    command: Sequence[str]
    depends_on: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    labels: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "command": list(self.command),
            "retry": self.retry.as_dict(),
        }
        if self.depends_on:
            payload["depends_on"] = list(self.depends_on)
        if self.env:
            payload["env"] = dict(self.env)
        if self.timeout_seconds:
            payload["timeout_seconds"] = self.timeout_seconds
        if self.labels:
            payload["labels"] = dict(self.labels)
        return payload


@dataclass(slots=True)
class WorkflowDefinition:
    name: str
    tasks: List[Task]
    metadata: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "tasks": [task.as_dict() for task in self.tasks],
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


class WorkflowBuilder:
    """Fluent workflow builder with local dependency validation."""

    def __init__(self, name: str) -> None:
        if not name.strip():
            raise ValueError("workflow name may not be empty")
        self._name = name
        self._metadata: Dict[str, str] = {}
        self._tasks: MutableMapping[str, Task] = {}

    def metadata(self, **values: str) -> "WorkflowBuilder":
        self._metadata.update(values)
        return self

    def task(
        self,
        task_id: str,
        command: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout_seconds: int = 0,
        retry: Optional[RetryPolicy] = None,
        labels: Optional[Mapping[str, str]] = None,
    ) -> "WorkflowBuilder":
        if task_id in self._tasks:
            raise ValueError(f"duplicate task id: {task_id}")
        if not task_id.strip():
            raise ValueError("task id may not be empty")
        if not command:
            raise ValueError("command may not be empty")
        self._tasks[task_id] = Task(
            id=task_id,
            command=list(command),
            env=dict(env or {}),
            timeout_seconds=timeout_seconds,
            retry=retry or RetryPolicy(),
            labels=dict(labels or {}),
        )
        return self

    def depends_on(self, task_id: str, *dependencies: str) -> "WorkflowBuilder":
        if task_id not in self._tasks:
            raise KeyError(f"unknown task: {task_id}")
        for dependency in dependencies:
            if dependency not in self._tasks:
                raise KeyError(f"unknown dependency: {dependency}")
            if dependency == task_id:
                raise ValueError("task cannot depend on itself")
            if dependency not in self._tasks[task_id].depends_on:
                self._tasks[task_id].depends_on.append(dependency)
        return self

    def build(self) -> WorkflowDefinition:
        if not self._tasks:
            raise ValueError("workflow must contain at least one task")
        self._validate_acyclic()
        return WorkflowDefinition(
            name=self._name,
            tasks=[self._tasks[task_id] for task_id in self._topological_order()],
            metadata=dict(self._metadata),
        )

    def _topological_order(self) -> List[str]:
        indegree = {task_id: 0 for task_id in self._tasks}
        children: Dict[str, List[str]] = {task_id: [] for task_id in self._tasks}
        for task in self._tasks.values():
            for dependency in task.depends_on:
                indegree[task.id] += 1
                children[dependency].append(task.id)

        ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
        order: List[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort()
        return order

    def _validate_acyclic(self) -> None:
        order = self._topological_order()
        if len(order) != len(self._tasks):
            raise ValueError("workflow graph contains a dependency cycle")


class HelixClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        timeout: float = 15.0,
        user_agent: str = "helixgrid-python/0.1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent

    def submit_workflow(
        self,
        workflow: WorkflowDefinition | Mapping[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = workflow.as_dict() if isinstance(workflow, WorkflowDefinition) else dict(workflow)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/v1/workflows", body=body, headers=headers)["data"]

    def list_workflows(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/workflows")["data"]

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/workflows/{self._quote(workflow_id)}")["data"]

    def cancel_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/v1/workflows/{self._quote(workflow_id)}/cancel")["data"]

    def list_workers(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/workers")["data"]

    def wait(
        self,
        workflow_id: str,
        *,
        poll_interval: float = 0.5,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED"}
        while True:
            workflow = self.get_workflow(workflow_id)
            if workflow.get("state") in terminal:
                return workflow
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError(f"workflow {workflow_id} did not finish within {timeout}s")
            time.sleep(poll_interval)

    def iter_events(
        self,
        workflow_id: str,
        *,
        last_event_id: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        path = f"/v1/workflows/{self._quote(workflow_id)}/events"
        headers = self._headers(None)
        headers["Accept"] = "text/event-stream"
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        request = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
            raise AssertionError("unreachable")

        event: Dict[str, Any] = {}
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if event:
                        if "data" in event:
                            try:
                                event["data"] = json.loads(event["data"])
                            except json.JSONDecodeError:
                                pass
                        yield event
                        event = {}
                    continue
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator:
                    continue
                value = value.lstrip(" ")
                if field == "id":
                    try:
                        event["id"] = int(value)
                    except ValueError:
                        event["id"] = value
                elif field == "event":
                    event["event"] = value
                elif field == "data":
                    current = event.get("data")
                    event["data"] = f"{current}\n{value}" if current is not None else value

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        raw: Optional[bytes] = None
        final_headers = self._headers(headers)
        if body is not None:
            raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
            final_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path,
            data=raw,
            headers=final_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
                if not data:
                    return {}
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
            raise AssertionError("unreachable")
        except urllib.error.URLError as exc:
            raise HelixError(f"unable to reach HelixGrid coordinator: {exc.reason}") from exc

    def _headers(self, extra: Optional[Mapping[str, str]]) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _quote(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    @staticmethod
    def _raise_http_error(exc: urllib.error.HTTPError) -> None:
        body = exc.read().decode("utf-8", errors="replace")
        message = body
        try:
            decoded = json.loads(body)
            if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
                message = decoded["error"]
        except json.JSONDecodeError:
            pass
        raise APIError(exc.code, message, body) from exc


def load_workflow_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("workflow file must contain a JSON object")
    return payload


def summarize_workflow(workflow: Mapping[str, Any]) -> str:
    runtime = workflow.get("runtime") or {}
    counts: Dict[str, int] = {}
    if isinstance(runtime, Mapping):
        for value in runtime.values():
            if isinstance(value, Mapping):
                state = str(value.get("state", "UNKNOWN"))
                counts[state] = counts.get(state, 0) + 1
    task_summary = ", ".join(f"{key.lower()}={counts[key]}" for key in sorted(counts))
    return f"{workflow.get('id', '?')}  {workflow.get('state', '?'):10}  {workflow.get('name', '?')}  {task_summary}"
