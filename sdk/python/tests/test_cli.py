import argparse
import unittest
from unittest.mock import patch

from helixgrid.cli import command_submit


class FakeClient:
    def __init__(self, final_state: str) -> None:
        self.final_state = final_state

    def submit_workflow(self, payload, *, idempotency_key=None):
        return {"id": "wf-test", "name": "test", "state": "RUNNING", "runtime": {}}

    def wait(self, workflow_id):
        return {"id": workflow_id, "name": "test", "state": self.final_state, "runtime": {}}


class SubmitCommandTests(unittest.TestCase):
    def args(self, *, wait: bool) -> argparse.Namespace:
        return argparse.Namespace(
            file="workflow.json",
            idempotency_key=None,
            wait=wait,
            json=True,
        )

    @patch("helixgrid.cli.load_workflow_file", return_value={"name": "test", "tasks": []})
    def test_waited_success_returns_zero(self, _load) -> None:
        self.assertEqual(command_submit(FakeClient("SUCCEEDED"), self.args(wait=True)), 0)

    @patch("helixgrid.cli.load_workflow_file", return_value={"name": "test", "tasks": []})
    def test_waited_cancelled_returns_failure(self, _load) -> None:
        self.assertEqual(command_submit(FakeClient("CANCELLED"), self.args(wait=True)), 2)

    @patch("helixgrid.cli.load_workflow_file", return_value={"name": "test", "tasks": []})
    def test_async_submit_returns_zero_while_running(self, _load) -> None:
        self.assertEqual(command_submit(FakeClient("FAILED"), self.args(wait=False)), 0)


if __name__ == "__main__":
    unittest.main()
