from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from tools.chaos_lab.generator import (
    GeneratorConfig,
    GeneratedScenario,
    ScenarioGenerator,
    clone_workflow,
    replace_task,
)
from tools.chaos_lab.model import (
    Action,
    ActionKind,
    Model,
    ModelError,
    RetryPolicy,
    Task,
    TaskState,
    WorkflowState,
)
from tools.chaos_lab.runner import (
    ScenarioRunner,
    load_scenario,
    run_campaign,
    save_scenario,
    scenario_from_dict,
    scenario_to_dict,
)
from tools.chaos_lab.shrinker import ScenarioShrinker


class GeneratorTests(unittest.TestCase):
    def test_same_seed_generates_same_scenario(self) -> None:
        first = scenario_to_dict(ScenarioGenerator(123).scenario(steps=80))
        second = scenario_to_dict(ScenarioGenerator(123).scenario(steps=80))
        self.assertEqual(first, second)

    def test_generated_workflow_is_valid_dag(self) -> None:
        for seed in range(1, 30):
            workflow = ScenarioGenerator(seed).workflow()
            order = Model.validate_dag(workflow.tasks)
            self.assertEqual(len(order), len(workflow.tasks))
            self.assertEqual(set(order), {task.id for task in workflow.tasks})

    def test_workers_cover_every_task_placement(self) -> None:
        for seed in range(1, 20):
            generator = ScenarioGenerator(seed)
            workflow = generator.workflow()
            workers = generator.workers(workflow)
            for task in workflow.tasks:
                self.assertTrue(
                    any(Model.labels_match(labels, task.labels) for _, _, labels in workers),
                    task.id,
                )

    def test_generator_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            GeneratorConfig(min_tasks=0)
        with self.assertRaises(ValueError):
            GeneratorConfig(min_tasks=5, max_tasks=4)
        with self.assertRaises(ValueError):
            GeneratorConfig(failure_probability=2.0)

    def test_replace_task_preserves_validity(self) -> None:
        workflow = ScenarioGenerator(7).workflow()
        task = workflow.tasks[0]
        changed = replace_task(workflow, task.id, timeout_ms=1234)
        self.assertEqual(changed.tasks[0].timeout_ms, 1234)
        Model.validate_dag(changed.tasks)

    def test_clone_workflow_is_independent(self) -> None:
        workflow = ScenarioGenerator(9).workflow()
        clone = clone_workflow(workflow)
        self.assertIsNot(workflow.tasks[0], clone.tasks[0])


class RunnerTests(unittest.TestCase):
    def test_replay_generated_scenarios(self) -> None:
        runner = ScenarioRunner()
        for seed in range(1, 15):
            scenario = ScenarioGenerator(seed).scenario(steps=120)
            report = runner.replay(scenario)
            self.assertTrue(report.ok, report.failure)
            self.assertTrue(report.final_digest)
            self.assertGreater(len(report.records), 0)

    def test_replay_is_deterministic(self) -> None:
        scenario = ScenarioGenerator(55).scenario(steps=150)
        same, first, second = ScenarioRunner().determinism_check(scenario)
        self.assertTrue(same)
        self.assertEqual(first.final_digest, second.final_digest)
        self.assertEqual(first.event_jsonl, second.event_jsonl)

    def test_round_trip_scenario_json(self) -> None:
        scenario = ScenarioGenerator(88).scenario(steps=40)
        encoded = scenario_to_dict(scenario)
        decoded = scenario_from_dict(json.loads(json.dumps(encoded)))
        self.assertEqual(scenario_to_dict(decoded), encoded)

    def test_save_and_load(self) -> None:
        scenario = ScenarioGenerator(91).scenario(steps=50)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            save_scenario(path, scenario)
            loaded = load_scenario(path)
        self.assertEqual(scenario_to_dict(loaded), scenario_to_dict(scenario))

    def test_campaign(self) -> None:
        report = run_campaign(first_seed=10, count=8, steps=80)
        self.assertTrue(report.ok)
        self.assertEqual(report.count, 8)
        self.assertGreater(sum(len(item.records) for item in report.reports), 0)

    def test_strict_rejections_can_fail_trace(self) -> None:
        scenario = ScenarioGenerator(1).scenario(steps=1)
        worker = scenario.worker_specs[0][0]
        bad = dataclasses.replace(
            scenario,
            actions=(Action(ActionKind.START, token="does-not-exist"),),
        )
        report = ScenarioRunner(strict_rejections=True).replay(bad)
        self.assertFalse(report.ok)
        self.assertEqual(report.failure.index, 0)


class ModelBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = Model(lease_duration_ms=100, worker_ttl_ms=1_000)
        self.workflow = self.model.add_workflow(
            "test",
            [
                Task("a", retry=RetryPolicy(2, 10, 20)),
                Task("b", depends_on=("a",)),
            ],
            workflow_id="wf",
        )
        self.worker = self.model.register_worker("worker", worker_id="worker", capacity=2)

    def test_success_unlocks_dependency(self) -> None:
        lease = self.model.lease_next("worker")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.task_id, "a")
        self.model.start(lease.token)
        self.model.complete_success(lease.token)
        self.assertEqual(self.model.workflows["wf"].tasks["a"].state, TaskState.SUCCEEDED)
        self.assertEqual(self.model.workflows["wf"].tasks["b"].state, TaskState.READY)
        self.model.check_invariants()

    def test_failure_enters_retry_wait(self) -> None:
        lease = self.model.lease_next("worker")
        self.model.start(lease.token)
        self.model.complete_failure(lease.token)
        task = self.model.workflows["wf"].tasks["a"]
        self.assertEqual(task.state, TaskState.RETRY_WAIT)
        self.assertIsNotNone(task.next_retry_ms)
        self.model.advance(10)
        self.assertEqual(task.state, TaskState.READY)
        self.model.check_invariants()

    def test_expired_lease_returns_task_to_ready(self) -> None:
        lease = self.model.lease_next("worker")
        self.model.start(lease.token)
        self.model.advance(101)
        self.assertEqual(self.model.workflows["wf"].tasks["a"].state, TaskState.READY)
        self.assertIn(lease.token, self.model.closed_leases)
        self.model.check_invariants()

    def test_stale_completion_rejected(self) -> None:
        lease = self.model.lease_next("worker")
        self.model.complete_success(lease.token)
        with self.assertRaises(ModelError):
            self.model.stale_complete(lease.token)

    def test_cancellation_closes_live_lease(self) -> None:
        lease = self.model.lease_next("worker")
        self.model.start(lease.token)
        self.model.cancel_workflow("wf")
        self.assertNotIn(lease.token, self.model.leases)
        self.assertEqual(self.model.workflows["wf"].state, WorkflowState.CANCELLED)
        self.model.check_invariants()

    def test_worker_capacity(self) -> None:
        model = Model()
        model.add_workflow("wide", [Task("a"), Task("b")], workflow_id="wide")
        model.register_worker("single", worker_id="single", capacity=1)
        first = model.lease_next("single")
        self.assertIsNotNone(first)
        self.assertIsNone(model.lease_next("single"))
        model.complete_success(first.token)
        self.assertIsNotNone(model.lease_next("single"))

    def test_label_placement(self) -> None:
        model = Model()
        model.add_workflow(
            "labels",
            [Task("gpu", labels={"tier": "fast"})],
            workflow_id="labels",
        )
        model.register_worker("slow", worker_id="slow", labels={"tier": "cpu"})
        model.register_worker("fast", worker_id="fast", labels={"tier": "fast"})
        self.assertIsNone(model.lease_next("slow"))
        self.assertIsNotNone(model.lease_next("fast"))


class ShrinkerTests(unittest.TestCase):
    def test_ddmin_removes_irrelevant_actions(self) -> None:
        base = ScenarioGenerator(3).scenario(steps=1)
        scenario = dataclasses.replace(
            base,
            actions=(
                Action(ActionKind.ADVANCE_TIME, amount=1),
                Action(ActionKind.SWEEP),
                Action(ActionKind.START, token="missing"),
                Action(ActionKind.ADVANCE_TIME, amount=100),
            ),
        )
        runner = ScenarioRunner(strict_rejections=True)
        self.assertFalse(runner.replay(scenario).ok)
        result = ScenarioShrinker(runner=runner).shrink(scenario)
        self.assertFalse(result.report.ok)
        self.assertLess(len(result.minimized.actions), len(scenario.actions))

    def test_simplifies_failure_action(self) -> None:
        base = ScenarioGenerator(4).scenario(steps=1)
        scenario = dataclasses.replace(
            base,
            actions=(Action(ActionKind.START, token="a-very-long-unknown-token", amount=999),),
        )
        runner = ScenarioRunner(strict_rejections=True)
        result = ScenarioShrinker(runner=runner).shrink(scenario)
        self.assertFalse(result.report.ok)


if __name__ == "__main__":
    unittest.main()
