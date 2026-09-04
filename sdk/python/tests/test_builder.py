import unittest

from helixgrid import RetryPolicy, WorkflowBuilder


class WorkflowBuilderTests(unittest.TestCase):
    def test_builds_topological_workflow(self) -> None:
        workflow = (
            WorkflowBuilder("release")
            .metadata(team="platform")
            .task("prepare", ["echo", "prepare"])
            .task("lint", ["echo", "lint"])
            .task("test", ["echo", "test"], retry=RetryPolicy(max_attempts=3))
            .task("package", ["echo", "package"])
            .depends_on("lint", "prepare")
            .depends_on("test", "prepare")
            .depends_on("package", "lint", "test")
            .build()
        )
        payload = workflow.as_dict()
        self.assertEqual(payload["name"], "release")
        self.assertEqual(payload["metadata"], {"team": "platform"})
        ids = [task["id"] for task in payload["tasks"]]
        self.assertLess(ids.index("prepare"), ids.index("lint"))
        self.assertLess(ids.index("prepare"), ids.index("test"))
        self.assertLess(ids.index("lint"), ids.index("package"))
        self.assertLess(ids.index("test"), ids.index("package"))

    def test_rejects_duplicate_task(self) -> None:
        builder = WorkflowBuilder("x").task("a", ["true"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            builder.task("a", ["true"])

    def test_rejects_unknown_dependency(self) -> None:
        builder = WorkflowBuilder("x").task("a", ["true"])
        with self.assertRaises(KeyError):
            builder.depends_on("a", "missing")

    def test_rejects_self_dependency(self) -> None:
        builder = WorkflowBuilder("x").task("a", ["true"])
        with self.assertRaisesRegex(ValueError, "itself"):
            builder.depends_on("a", "a")

    def test_rejects_cycle(self) -> None:
        builder = (
            WorkflowBuilder("cycle")
            .task("a", ["true"])
            .task("b", ["true"])
            .task("c", ["true"])
            .depends_on("b", "a")
            .depends_on("c", "b")
            .depends_on("a", "c")
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            builder.build()

    def test_retry_serialization(self) -> None:
        workflow = (
            WorkflowBuilder("retry")
            .task(
                "unstable",
                ["false"],
                retry=RetryPolicy(max_attempts=5, base_delay_ms=100, max_delay_ms=2000),
            )
            .build()
            .as_dict()
        )
        self.assertEqual(
            workflow["tasks"][0]["retry"],
            {"max_attempts": 5, "base_delay_ms": 100, "max_delay_ms": 2000},
        )


if __name__ == "__main__":
    unittest.main()


    def test_rejects_protocol_invalid_task_fields(self) -> None:
        builder = WorkflowBuilder("x")
        with self.assertRaisesRegex(ValueError, "invalid task id"):
            builder.task("bad id", ["true"])
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            builder.task("timeout", ["true"], timeout_seconds=100_000)
        with self.assertRaisesRegex(ValueError, "max_attempts"):
            builder.task("retry", ["true"], retry=RetryPolicy(max_attempts=101))
