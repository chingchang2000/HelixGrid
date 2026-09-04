from __future__ import annotations

import dataclasses
import json
import re
import shlex
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

from .client import RetryPolicy, Task, WorkflowDefinition


class DSLError(ValueError):
    """Raised when a HelixGrid DSL source file cannot be parsed or validated."""

    def __init__(self, message: str, *, source: str = "<memory>", line: int = 0, column: int = 0) -> None:
        location = source
        if line:
            location += f":{line}"
            if column:
                location += f":{column}"
        super().__init__(f"{location}: {message}")
        self.message = message
        self.source = source
        self.line = line
        self.column = column


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    column: int


@dataclass(slots=True)
class TaskNode:
    id: str
    line: int
    command: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass(slots=True)
class WorkflowNode:
    name: str
    line: int
    metadata: dict[str, str] = field(default_factory=dict)
    tasks: list[TaskNode] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    source: str
    line: int
    column: int = 1

    def format(self) -> str:
        return f"{self.source}:{self.line}:{self.column}: {self.severity}: {self.code}: {self.message}"


_KEYWORDS = {
    "workflow",
    "meta",
    "task",
    "run",
    "needs",
    "env",
    "labels",
    "timeout",
    "retry",
    "attempts",
    "base",
    "max",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")
_INTEGER_RE = re.compile(r"[0-9]+")
_DURATION_RE = re.compile(r"(?P<number>[0-9]+)(?P<unit>ms|s|m|h)")


class Lexer:
    def __init__(self, source: str, *, filename: str = "<memory>") -> None:
        self.source = source
        self.filename = filename
        self.index = 0
        self.line = 1
        self.column = 1

    def tokens(self) -> Iterator[Token]:
        while self.index < len(self.source):
            c = self.source[self.index]
            if c in " \t\r":
                self._advance(c)
                continue
            if c == "\n":
                line, column = self.line, self.column
                self._advance(c)
                yield Token("NEWLINE", "\n", line, column)
                continue
            if c == "#":
                self._skip_comment()
                continue
            if c in "{}[]=,":
                line, column = self.line, self.column
                self._advance(c)
                yield Token(c, c, line, column)
                continue
            if c in "\"'":
                yield self._string(c)
                continue
            match = _DURATION_RE.match(self.source, self.index)
            if match:
                value = match.group(0)
                line, column = self.line, self.column
                for ch in value:
                    self._advance(ch)
                yield Token("DURATION", value, line, column)
                continue
            match = _INTEGER_RE.match(self.source, self.index)
            if match:
                value = match.group(0)
                line, column = self.line, self.column
                for ch in value:
                    self._advance(ch)
                yield Token("INTEGER", value, line, column)
                continue
            match = _IDENT_RE.match(self.source, self.index)
            if match:
                value = match.group(0)
                line, column = self.line, self.column
                for ch in value:
                    self._advance(ch)
                kind = value if value in _KEYWORDS else "IDENT"
                yield Token(kind, value, line, column)
                continue
            raise DSLError(
                f"unexpected character {c!r}",
                source=self.filename,
                line=self.line,
                column=self.column,
            )
        yield Token("EOF", "", self.line, self.column)

    def _advance(self, c: str) -> None:
        self.index += 1
        if c == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1

    def _skip_comment(self) -> None:
        while self.index < len(self.source) and self.source[self.index] != "\n":
            self._advance(self.source[self.index])

    def _string(self, quote: str) -> Token:
        line, column = self.line, self.column
        self._advance(quote)
        out: list[str] = []
        while self.index < len(self.source):
            c = self.source[self.index]
            if c == quote:
                self._advance(c)
                return Token("STRING", "".join(out), line, column)
            if c == "\n":
                raise DSLError(
                    "unterminated string literal",
                    source=self.filename,
                    line=line,
                    column=column,
                )
            if c != "\\":
                out.append(c)
                self._advance(c)
                continue

            self._advance(c)
            if self.index >= len(self.source):
                break
            escaped = self.source[self.index]
            mapping = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "\\": "\\",
                "\"": "\"",
                "'": "'",
            }
            if escaped == "u":
                self._advance(escaped)
                digits = self.source[self.index : self.index + 4]
                if len(digits) != 4 or not all(ch in "0123456789abcdefABCDEF" for ch in digits):
                    raise DSLError(
                        "invalid unicode escape",
                        source=self.filename,
                        line=self.line,
                        column=self.column,
                    )
                out.append(chr(int(digits, 16)))
                for ch in digits:
                    self._advance(ch)
                continue
            if escaped not in mapping:
                raise DSLError(
                    f"unknown escape sequence \\{escaped}",
                    source=self.filename,
                    line=self.line,
                    column=self.column,
                )
            out.append(mapping[escaped])
            self._advance(escaped)

        raise DSLError(
            "unterminated string literal",
            source=self.filename,
            line=line,
            column=column,
        )


class Parser:
    def __init__(self, tokens: Sequence[Token], *, filename: str = "<memory>") -> None:
        self.tokens = tokens
        self.filename = filename
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def parse(self) -> WorkflowNode:
        self._skip_newlines()
        start = self._expect("workflow")
        name = self._scalar("workflow name")
        workflow = WorkflowNode(name=name, line=start.line)
        self._skip_newlines()
        self._expect("{")
        self._skip_newlines()
        task_ids: set[str] = set()
        while self.current.kind != "}":
            if self.current.kind == "EOF":
                self._error("unterminated workflow block")
            if self.current.kind == "meta":
                self._parse_meta(workflow)
            elif self.current.kind == "task":
                task = self._parse_task()
                if task.id in task_ids:
                    raise DSLError(
                        f"duplicate task id {task.id!r}",
                        source=self.filename,
                        line=task.line,
                    )
                task_ids.add(task.id)
                workflow.tasks.append(task)
            else:
                self._error("expected 'meta', 'task', or '}'")
            self._skip_newlines()
        self._expect("}")
        self._skip_newlines()
        self._expect("EOF")
        return workflow

    def _parse_meta(self, workflow: WorkflowNode) -> None:
        token = self._expect("meta")
        key = self._scalar("metadata key")
        self._expect("=")
        value = self._scalar("metadata value")
        if key in workflow.metadata:
            raise DSLError(
                f"duplicate metadata key {key!r}",
                source=self.filename,
                line=token.line,
                column=token.column,
            )
        workflow.metadata[key] = value
        self._line_end()

    def _parse_task(self) -> TaskNode:
        token = self._expect("task")
        task_id = self._scalar("task id")
        task = TaskNode(id=task_id, line=token.line)
        self._skip_newlines()
        self._expect("{")
        self._skip_newlines()
        seen: set[str] = set()
        while self.current.kind != "}":
            if self.current.kind == "EOF":
                self._error(f"unterminated task {task_id!r}")
            key = self.current.kind
            if key in seen and key not in {"env", "labels"}:
                self._error(f"task field {key!r} appears more than once")
            if key == "run":
                seen.add(key)
                self._advance()
                task.command = self._command()
                self._line_end()
            elif key == "needs":
                seen.add(key)
                self._advance()
                task.depends_on = self._list_of_scalars("dependency")
                self._line_end()
            elif key == "env":
                self._advance()
                name = self._scalar("environment variable name")
                self._expect("=")
                value = self._scalar("environment variable value")
                if name in task.env:
                    self._error(f"duplicate environment variable {name!r}")
                task.env[name] = value
                self._line_end()
            elif key == "labels":
                self._advance()
                name = self._scalar("label name")
                self._expect("=")
                value = self._scalar("label value")
                if name in task.labels:
                    self._error(f"duplicate label {name!r}")
                task.labels[name] = value
                self._line_end()
            elif key == "timeout":
                seen.add(key)
                self._advance()
                duration = self._expect("DURATION")
                task.timeout_seconds = duration_to_seconds(duration.value, self.filename, duration.line, duration.column)
                self._line_end()
            elif key == "retry":
                seen.add(key)
                self._advance()
                task.retry = self._retry_policy()
                self._line_end()
            else:
                self._error("unknown task field")
            self._skip_newlines()
        self._expect("}")
        if not task.command:
            raise DSLError(
                f"task {task.id!r} is missing a run command",
                source=self.filename,
                line=task.line,
            )
        return task

    def _retry_policy(self) -> RetryPolicy:
        attempts = 1
        base_ms = 250
        max_ms = 30_000
        consumed: set[str] = set()
        while self.current.kind not in {"NEWLINE", "}", "EOF"}:
            key = self.current.kind
            if key not in {"attempts", "base", "max"}:
                self._error("retry expects attempts/base/max options")
            if key in consumed:
                self._error(f"retry option {key!r} appears twice")
            consumed.add(key)
            self._advance()
            self._expect("=")
            if key == "attempts":
                attempts = int(self._expect("INTEGER").value)
            else:
                duration = self._expect("DURATION")
                ms = duration_to_milliseconds(duration.value, self.filename, duration.line, duration.column)
                if key == "base":
                    base_ms = ms
                else:
                    max_ms = ms
            if self.current.kind == ",":
                self._advance()
        if attempts < 1 or attempts > 100:
            self._error("retry attempts must be between 1 and 100")
        if base_ms < 1:
            self._error("retry base delay must be positive")
        if max_ms < base_ms:
            self._error("retry max delay cannot be smaller than base delay")
        return RetryPolicy(max_attempts=attempts, base_delay_ms=base_ms, max_delay_ms=max_ms)

    def _command(self) -> list[str]:
        if self.current.kind == "[":
            return self._list_of_scalars("command argument")
        value = self._scalar("command")
        try:
            command = shlex.split(value, posix=True)
        except ValueError as exc:
            self._error(f"invalid shell-style command: {exc}")
        if not command:
            self._error("run command may not be empty")
        return command

    def _list_of_scalars(self, description: str) -> list[str]:
        self._expect("[")
        values: list[str] = []
        if self.current.kind == "]":
            self._advance()
            return values
        while True:
            values.append(self._scalar(description))
            if self.current.kind == "]":
                self._advance()
                return values
            self._expect(",")

    def _scalar(self, description: str) -> str:
        token = self.current
        if token.kind in {"STRING", "IDENT", "INTEGER", "DURATION"} or token.kind in _KEYWORDS:
            self._advance()
            return token.value
        self._error(f"expected {description}")
        raise AssertionError("unreachable")

    def _line_end(self) -> None:
        if self.current.kind == "NEWLINE":
            self._skip_newlines()
            return
        if self.current.kind in {"}", "EOF"}:
            return
        # Compact task blocks may place multiple fields on one physical line.
        # The parser still rejects arbitrary trailing tokens and duplicate fields.
        if self.current.kind in {"run", "needs", "env", "labels", "timeout", "retry"}:
            return
        self._error("expected end of line")

    def _skip_newlines(self) -> None:
        while self.current.kind == "NEWLINE":
            self._advance()

    def _expect(self, kind: str) -> Token:
        token = self.current
        if token.kind != kind:
            self._error(f"expected {kind!r}, got {token.kind!r}")
        self._advance()
        return token

    def _advance(self) -> None:
        if self.index < len(self.tokens) - 1:
            self.index += 1

    def _error(self, message: str) -> None:
        token = self.current
        raise DSLError(
            message,
            source=self.filename,
            line=token.line,
            column=token.column,
        )


def duration_to_milliseconds(value: str, source: str, line: int, column: int) -> int:
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise DSLError("invalid duration", source=source, line=line, column=column)
    number = int(match.group("number"))
    unit = match.group("unit")
    multiplier = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000}[unit]
    result = number * multiplier
    if result > 86_400_000:
        raise DSLError("duration exceeds 24 hours", source=source, line=line, column=column)
    return result


def duration_to_seconds(value: str, source: str, line: int, column: int) -> int:
    milliseconds = duration_to_milliseconds(value, source, line, column)
    if milliseconds % 1_000:
        raise DSLError(
            "task timeout must resolve to whole seconds",
            source=source,
            line=line,
            column=column,
        )
    return milliseconds // 1_000


def parse(source: str, *, filename: str = "<memory>") -> WorkflowNode:
    tokens = list(Lexer(source, filename=filename).tokens())
    return Parser(tokens, filename=filename).parse()


def validate(node: WorkflowNode, *, source: str = "<memory>") -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not node.name.strip():
        diagnostics.append(Diagnostic("error", "E001", "workflow name may not be empty", source, node.line))
    if not node.tasks:
        diagnostics.append(Diagnostic("error", "E002", "workflow has no tasks", source, node.line))

    by_id: dict[str, TaskNode] = {}
    for task in node.tasks:
        if task.id in by_id:
            diagnostics.append(
                Diagnostic("error", "E100", f"duplicate task id {task.id!r}", source, task.line)
            )
        else:
            by_id[task.id] = task
        if not task.command:
            diagnostics.append(
                Diagnostic("error", "E101", f"task {task.id!r} has no command", source, task.line)
            )
        if task.id in task.depends_on:
            diagnostics.append(
                Diagnostic("error", "E102", f"task {task.id!r} depends on itself", source, task.line)
            )
        if len(task.depends_on) != len(set(task.depends_on)):
            diagnostics.append(
                Diagnostic("error", "E103", f"task {task.id!r} contains duplicate dependencies", source, task.line)
            )
        for dependency in task.depends_on:
            if dependency not in by_id and dependency not in {candidate.id for candidate in node.tasks}:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "E104",
                        f"task {task.id!r} references unknown dependency {dependency!r}",
                        source,
                        task.line,
                    )
                )
        if task.timeout_seconds > 86_400:
            diagnostics.append(
                Diagnostic("error", "E105", f"task {task.id!r} timeout exceeds 24 hours", source, task.line)
            )
        if len(task.command) > 256:
            diagnostics.append(
                Diagnostic("warning", "W100", f"task {task.id!r} has an unusually long argv", source, task.line)
            )
        if task.retry.max_attempts > 10:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "W101",
                    f"task {task.id!r} retries {task.retry.max_attempts} times; this may hide persistent failures",
                    source,
                    task.line,
                )
            )

    cycle = find_cycle(node)
    if cycle:
        diagnostics.append(
            Diagnostic(
                "error",
                "E200",
                "dependency cycle: " + " -> ".join(cycle),
                source,
                by_id.get(cycle[0], TaskNode(cycle[0], node.line)).line,
            )
        )

    roots = [task.id for task in node.tasks if not task.depends_on]
    if len(roots) > 32:
        diagnostics.append(
            Diagnostic("warning", "W200", f"workflow has {len(roots)} independent root tasks", source, node.line)
        )

    reverse = reverse_edges(node)
    sinks = [task.id for task in node.tasks if not reverse.get(task.id)]
    if len(sinks) > 16:
        diagnostics.append(
            Diagnostic(
                "warning",
                "W201",
                f"workflow has {len(sinks)} terminal branches and no common fan-in",
                source,
                node.line,
            )
        )
    return diagnostics


def reverse_edges(node: WorkflowNode) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for task in node.tasks:
        children.setdefault(task.id, [])
        for dependency in task.depends_on:
            children[dependency].append(task.id)
    for values in children.values():
        values.sort()
    return dict(children)


def topological_order(node: WorkflowNode) -> list[str]:
    by_id = {task.id: task for task in node.tasks}
    indegree = {task.id: 0 for task in node.tasks}
    children = reverse_edges(node)
    for task in node.tasks:
        for dependency in task.depends_on:
            if dependency in by_id:
                indegree[task.id] += 1
    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in children.get(current, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(node.tasks):
        cycle = find_cycle(node)
        raise DSLError("dependency cycle: " + " -> ".join(cycle or ["unknown"]))
    return order


def find_cycle(node: WorkflowNode) -> list[str]:
    by_id = {task.id: task for task in node.tasks}
    state: dict[str, int] = {}
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(task_id: str) -> list[str] | None:
        current = state.get(task_id, 0)
        if current == 2:
            return None
        if current == 1:
            start = positions[task_id]
            return stack[start:] + [task_id]
        state[task_id] = 1
        positions[task_id] = len(stack)
        stack.append(task_id)
        task = by_id.get(task_id)
        if task is not None:
            for dependency in task.depends_on:
                if dependency not in by_id:
                    continue
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        positions.pop(task_id, None)
        state[task_id] = 2
        return None

    for task_id in sorted(by_id):
        cycle = visit(task_id)
        if cycle:
            return cycle
    return []


def critical_path(node: WorkflowNode, weights: Mapping[str, int] | None = None) -> tuple[list[str], int]:
    """Return a deterministic longest dependency path.

    The optional weight mapping can represent estimated task durations. Missing tasks use a
    weight of one, which makes the result the longest path by task count.
    """

    weights = weights or {}
    by_id = {task.id: task for task in node.tasks}
    order = topological_order(node)
    score: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    for task_id in order:
        task = by_id[task_id]
        best_parent: str | None = None
        best_score = 0
        for dependency in sorted(task.depends_on):
            candidate = score.get(dependency, 0)
            if candidate > best_score or (candidate == best_score and (best_parent is None or dependency < best_parent)):
                best_parent = dependency
                best_score = candidate
        score[task_id] = best_score + max(int(weights.get(task_id, 1)), 0)
        parent[task_id] = best_parent
    if not score:
        return [], 0
    end = min(score, key=lambda task_id: (-score[task_id], task_id))
    path: list[str] = []
    current: str | None = end
    while current is not None:
        path.append(current)
        current = parent[current]
    path.reverse()
    return path, score[end]


def compile_node(node: WorkflowNode, *, source: str = "<memory>") -> WorkflowDefinition:
    diagnostics = validate(node, source=source)
    errors = [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]
    if errors:
        raise DSLError("; ".join(diagnostic.message for diagnostic in errors), source=source)
    by_id = {task.id: task for task in node.tasks}
    tasks: list[Task] = []
    for task_id in topological_order(node):
        source_task = by_id[task_id]
        tasks.append(
            Task(
                id=source_task.id,
                command=list(source_task.command),
                depends_on=list(source_task.depends_on),
                env=dict(source_task.env),
                timeout_seconds=source_task.timeout_seconds,
                retry=dataclasses.replace(source_task.retry),
                labels=dict(source_task.labels),
            )
        )
    return WorkflowDefinition(name=node.name, tasks=tasks, metadata=dict(node.metadata))


def compile_source(source: str, *, filename: str = "<memory>") -> WorkflowDefinition:
    node = parse(source, filename=filename)
    return compile_node(node, source=filename)


def compile_file(path: str | Path) -> WorkflowDefinition:
    file = Path(path)
    return compile_source(file.read_text(encoding="utf-8"), filename=str(file))


def format_source(node: WorkflowNode) -> str:
    lines = [f"workflow {quote_if_needed(node.name)} {{"]
    for key in sorted(node.metadata):
        lines.append(f"  meta {quote_if_needed(key)} = {json.dumps(node.metadata[key], ensure_ascii=False)}")
    if node.metadata and node.tasks:
        lines.append("")
    for index, task in enumerate(node.tasks):
        lines.append(f"  task {quote_if_needed(task.id)} {{")
        lines.append("    run [" + ", ".join(json.dumps(arg, ensure_ascii=False) for arg in task.command) + "]")
        if task.depends_on:
            lines.append("    needs [" + ", ".join(quote_if_needed(dep) for dep in task.depends_on) + "]")
        for key in sorted(task.env):
            lines.append(f"    env {quote_if_needed(key)} = {json.dumps(task.env[key], ensure_ascii=False)}")
        for key in sorted(task.labels):
            lines.append(f"    labels {quote_if_needed(key)} = {json.dumps(task.labels[key], ensure_ascii=False)}")
        if task.timeout_seconds:
            lines.append(f"    timeout {format_duration(task.timeout_seconds * 1000)}")
        retry = task.retry
        if retry != RetryPolicy():
            lines.append(
                "    retry "
                f"attempts={retry.max_attempts}, "
                f"base={format_duration(retry.base_delay_ms)}, "
                f"max={format_duration(retry.max_delay_ms)}"
            )
        lines.append("  }")
        if index != len(node.tasks) - 1:
            lines.append("")
    lines.append("}")
    return "\n".join(lines) + "\n"


def quote_if_needed(value: str) -> str:
    if _IDENT_RE.fullmatch(value) and value not in _KEYWORDS:
        return value
    return json.dumps(value, ensure_ascii=False)


def format_duration(milliseconds: int) -> str:
    if milliseconds % 3_600_000 == 0:
        return f"{milliseconds // 3_600_000}h"
    if milliseconds % 60_000 == 0:
        return f"{milliseconds // 60_000}m"
    if milliseconds % 1_000 == 0:
        return f"{milliseconds // 1_000}s"
    return f"{milliseconds}ms"


def graphviz(node: WorkflowNode) -> str:
    diagnostics = validate(node)
    if any(item.severity == "error" for item in diagnostics):
        raise DSLError("cannot render invalid workflow graph")
    lines = [
        "digraph helixgrid {",
        "  rankdir=LR;",
        '  graph [fontname="Inter"];',
        '  node [shape=box, style=rounded, fontname="Inter"];',
    ]
    roots = {task.id for task in node.tasks if not task.depends_on}
    sinks = {task.id for task in node.tasks} - set(reverse_edges(node).keys())
    for task in sorted(node.tasks, key=lambda item: item.id):
        attributes: list[str] = [f'label={json.dumps(task.id)}']
        if task.id in roots:
            attributes.append('penwidth="2"')
        lines.append(f"  {json.dumps(task.id)} [{', '.join(attributes)}];")
    for task in sorted(node.tasks, key=lambda item: item.id):
        for dependency in sorted(task.depends_on):
            lines.append(f"  {json.dumps(dependency)} -> {json.dumps(task.id)};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def statistics(node: WorkflowNode) -> dict[str, Any]:
    children = reverse_edges(node)
    order = topological_order(node)
    roots = [task.id for task in node.tasks if not task.depends_on]
    sinks = [task.id for task in node.tasks if not children.get(task.id)]
    edges = sum(len(task.depends_on) for task in node.tasks)
    path, path_weight = critical_path(node)
    indegrees = [len(task.depends_on) for task in node.tasks]
    outdegrees = [len(children.get(task.id, [])) for task in node.tasks]
    return {
        "name": node.name,
        "tasks": len(node.tasks),
        "edges": edges,
        "roots": sorted(roots),
        "sinks": sorted(sinks),
        "max_indegree": max(indegrees, default=0),
        "max_outdegree": max(outdegrees, default=0),
        "topological_order": order,
        "critical_path": path,
        "critical_path_tasks": path_weight,
        "parallelism_upper_bound": estimate_parallelism(node),
    }


def estimate_parallelism(node: WorkflowNode) -> int:
    """Estimate maximum runnable width using deterministic Kahn levels."""

    by_id = {task.id: task for task in node.tasks}
    indegree = {task.id: len([dep for dep in task.depends_on if dep in by_id]) for task in node.tasks}
    children = reverse_edges(node)
    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    maximum = len(ready)
    while ready:
        wave = ready
        ready = []
        for current in wave:
            for child in children.get(current, []):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        ready.sort()
        maximum = max(maximum, len(ready))
    return maximum


EXAMPLE = '''\
workflow release-pipeline {
  meta team = "platform"
  meta environment = "dev"

  task prepare {
    run ["sh", "-lc", "echo preparing"]
    timeout 30s
    retry attempts=2, base=250ms, max=2s
  }

  task test-linux {
    run ["sh", "-lc", "echo linux tests"]
    needs [prepare]
    labels os = "linux"
  }

  task test-windows {
    run ["powershell", "-NoProfile", "-Command", "Write-Host tests"]
    needs [prepare]
    labels os = "windows"
  }

  task publish {
    run ["sh", "-lc", "echo publish"]
    needs [test-linux, test-windows]
  }
}
'''
