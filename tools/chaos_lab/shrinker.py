from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .generator import GeneratedScenario, GeneratedWorkflow
from .model import Action, ActionKind, Model, Task
from .runner import ReplayFailure, ReplayReport, ScenarioRunner


FailurePredicate = Callable[[ReplayReport], bool]


@dataclass(frozen=True, slots=True)
class ShrinkPass:
    name: str
    before: int
    after: int
    accepted: bool


@dataclass(frozen=True, slots=True)
class ShrinkResult:
    original: GeneratedScenario
    minimized: GeneratedScenario
    report: ReplayReport
    passes: tuple[ShrinkPass, ...]

    @property
    def removed_actions(self) -> int:
        return len(self.original.actions) - len(self.minimized.actions)


class ScenarioShrinker:
    """Delta-debugger for failing chaos traces.

    The shrinker is deterministic and dependency-aware. It first removes large chunks of
    actions, then simplifies values, then attempts to remove workflow tasks and workers while
    preserving a caller-supplied failure predicate.
    """

    def __init__(
        self,
        *,
        runner: ScenarioRunner | None = None,
        predicate: FailurePredicate | None = None,
        max_rounds: int = 30,
    ) -> None:
        self.runner = runner or ScenarioRunner()
        self.predicate = predicate or (lambda report: not report.ok)
        self.max_rounds = max(1, int(max_rounds))

    def shrink(self, scenario: GeneratedScenario) -> ShrinkResult:
        baseline = self.runner.replay(scenario)
        if not self.predicate(baseline):
            raise ValueError("scenario does not satisfy failure predicate")

        current = scenario
        passes: list[ShrinkPass] = []
        for _round in range(self.max_rounds):
            changed = False
            candidate, trace = self._ddmin_actions(current)
            passes.extend(trace)
            if len(candidate.actions) < len(current.actions):
                current = candidate
                changed = True

            candidate, trace = self._simplify_actions(current)
            passes.extend(trace)
            if candidate.actions != current.actions:
                current = candidate
                changed = True

            candidate, trace = self._remove_workers(current)
            passes.extend(trace)
            if candidate.worker_specs != current.worker_specs:
                current = candidate
                changed = True

            candidate, trace = self._remove_tasks(current)
            passes.extend(trace)
            if candidate.workflow.tasks != current.workflow.tasks:
                current = candidate
                changed = True

            if not changed:
                break

        report = self.runner.replay(current)
        if not self.predicate(report):
            raise RuntimeError("shrinker lost target failure")
        return ShrinkResult(
            original=scenario,
            minimized=current,
            report=report,
            passes=tuple(passes),
        )

    def _ddmin_actions(self, scenario: GeneratedScenario) -> tuple[GeneratedScenario, list[ShrinkPass]]:
        actions = list(scenario.actions)
        passes: list[ShrinkPass] = []
        if len(actions) <= 1:
            return scenario, passes

        granularity = 2
        while len(actions) >= 2:
            chunk_size = max(1, (len(actions) + granularity - 1) // granularity)
            removed = False
            for start in range(0, len(actions), chunk_size):
                candidate_actions = actions[:start] + actions[start + chunk_size :]
                candidate = dataclasses.replace(scenario, actions=tuple(candidate_actions))
                accepted = self._fails(candidate)
                passes.append(
                    ShrinkPass(
                        name=f"remove-actions[{start}:{start + chunk_size}]",
                        before=len(actions),
                        after=len(candidate_actions),
                        accepted=accepted,
                    )
                )
                if accepted:
                    actions = candidate_actions
                    removed = True
                    granularity = max(2, granularity - 1)
                    break
            if removed:
                continue
            if granularity >= len(actions):
                break
            granularity = min(len(actions), granularity * 2)
        return dataclasses.replace(scenario, actions=tuple(actions)), passes

    def _simplify_actions(self, scenario: GeneratedScenario) -> tuple[GeneratedScenario, list[ShrinkPass]]:
        actions = list(scenario.actions)
        passes: list[ShrinkPass] = []
        for index in range(len(actions)):
            original = actions[index]
            for simplified in self._action_variants(original):
                if simplified == original:
                    continue
                candidate_actions = list(actions)
                candidate_actions[index] = simplified
                candidate = dataclasses.replace(scenario, actions=tuple(candidate_actions))
                accepted = self._fails(candidate)
                passes.append(
                    ShrinkPass(
                        name=f"simplify-action[{index}]",
                        before=self._action_complexity(original),
                        after=self._action_complexity(simplified),
                        accepted=accepted,
                    )
                )
                if accepted:
                    actions = candidate_actions
                    original = simplified
        return dataclasses.replace(scenario, actions=tuple(actions)), passes

    def _remove_workers(self, scenario: GeneratedScenario) -> tuple[GeneratedScenario, list[ShrinkPass]]:
        specs = list(scenario.worker_specs)
        passes: list[ShrinkPass] = []
        index = 0
        while index < len(specs):
            worker_id = specs[index][0]
            if self._actions_reference_worker(scenario.actions, worker_id):
                index += 1
                continue
            candidate_specs = specs[:index] + specs[index + 1 :]
            if not candidate_specs:
                index += 1
                continue
            candidate = dataclasses.replace(scenario, worker_specs=tuple(candidate_specs))
            accepted = self._fails(candidate)
            passes.append(
                ShrinkPass(
                    name=f"remove-worker[{worker_id}]",
                    before=len(specs),
                    after=len(candidate_specs),
                    accepted=accepted,
                )
            )
            if accepted:
                specs = candidate_specs
            else:
                index += 1
        return dataclasses.replace(scenario, worker_specs=tuple(specs)), passes

    def _remove_tasks(self, scenario: GeneratedScenario) -> tuple[GeneratedScenario, list[ShrinkPass]]:
        tasks = [task.clone() for task in scenario.workflow.tasks]
        passes: list[ShrinkPass] = []
        index = len(tasks) - 1
        while index >= 0 and len(tasks) > 1:
            target = tasks[index].id
            if self._actions_reference_task(scenario.actions, target):
                index -= 1
                continue
            candidate_tasks = self._without_task(tasks, target)
            try:
                Model.validate_dag(candidate_tasks)
            except Exception:
                index -= 1
                continue
            candidate_workflow = dataclasses.replace(
                scenario.workflow,
                tasks=tuple(task.clone() for task in candidate_tasks),
            )
            candidate = dataclasses.replace(scenario, workflow=candidate_workflow)
            accepted = self._fails(candidate)
            passes.append(
                ShrinkPass(
                    name=f"remove-task[{target}]",
                    before=len(tasks),
                    after=len(candidate_tasks),
                    accepted=accepted,
                )
            )
            if accepted:
                tasks = candidate_tasks
                index = min(index - 1, len(tasks) - 1)
            else:
                index -= 1
        workflow = dataclasses.replace(
            scenario.workflow,
            tasks=tuple(task.clone() for task in tasks),
        )
        return dataclasses.replace(scenario, workflow=workflow), passes

    def _fails(self, scenario: GeneratedScenario) -> bool:
        try:
            return self.predicate(self.runner.replay(scenario))
        except Exception:
            return True

    @staticmethod
    def _action_variants(action: Action) -> Iterable[Action]:
        if action.amount not in {0, 1}:
            yield dataclasses.replace(action, amount=1)
        if action.text:
            yield dataclasses.replace(action, text="")
            if len(action.text) > 1:
                yield dataclasses.replace(action, text=action.text[:1])
        if action.kind == ActionKind.ADVANCE_TIME and action.amount > 1:
            for value in (1, 10, 100, 1_000):
                if value < action.amount:
                    yield dataclasses.replace(action, amount=value)
        if action.kind == ActionKind.LOG and len(action.text) > 2:
            yield dataclasses.replace(action, text="x\n")
        if action.kind == ActionKind.COMPLETE_FAILURE:
            yield dataclasses.replace(action, amount=1, text="x")

    @staticmethod
    def _action_complexity(action: Action) -> int:
        return (
            1
            + len(action.workflow_id)
            + len(action.task_id)
            + len(action.worker_id)
            + len(action.token)
            + min(abs(action.amount), 1_000_000)
            + len(action.text)
        )

    @staticmethod
    def _actions_reference_worker(actions: Sequence[Action], worker_id: str) -> bool:
        return any(action.worker_id == worker_id for action in actions)

    @staticmethod
    def _actions_reference_task(actions: Sequence[Action], task_id: str) -> bool:
        return any(action.task_id == task_id for action in actions)

    @staticmethod
    def _without_task(tasks: Sequence[Task], target: str) -> list[Task]:
        remaining: list[Task] = []
        target_deps: tuple[str, ...] = ()
        for task in tasks:
            if task.id == target:
                target_deps = task.depends_on
                break
        for task in tasks:
            if task.id == target:
                continue
            copy = task.clone()
            if target in copy.depends_on:
                expanded: list[str] = []
                for dependency in copy.depends_on:
                    if dependency == target:
                        expanded.extend(target_deps)
                    else:
                        expanded.append(dependency)
                copy.depends_on = tuple(dict.fromkeys(expanded))
            remaining.append(copy)
        return remaining


def same_failure_signature(expected: ReplayFailure) -> FailurePredicate:
    def predicate(report: ReplayReport) -> bool:
        failure = report.failure
        if failure is None:
            return False
        if expected.invariant_code:
            return failure.invariant_code == expected.invariant_code
        return (
            failure.error_type == expected.error_type
            and _prefix(failure.error) == _prefix(expected.error)
        )
    return predicate


def _prefix(value: str) -> str:
    return value.split(":", 1)[0].strip()


def format_shrink_summary(result: ShrinkResult) -> str:
    accepted = sum(1 for item in result.passes if item.accepted)
    rejected = len(result.passes) - accepted
    failure = result.report.failure
    lines = [
        "HelixGrid chaos shrink result",
        f"actions: {len(result.original.actions)} -> {len(result.minimized.actions)}",
        f"workers: {len(result.original.worker_specs)} -> {len(result.minimized.worker_specs)}",
        f"tasks: {len(result.original.workflow.tasks)} -> {len(result.minimized.workflow.tasks)}",
        f"passes: {len(result.passes)} ({accepted} accepted, {rejected} rejected)",
    ]
    if failure is not None:
        lines.append(
            f"failure: step={failure.index} type={failure.error_type} "
            f"code={failure.invariant_code or '-'} error={failure.error}"
        )
    return "\n".join(lines)
