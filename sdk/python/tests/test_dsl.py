import json
import unittest

from helixgrid.dsl import (
    DSLError,
    EXAMPLE,
    compile_source,
    critical_path,
    estimate_parallelism,
    find_cycle,
    format_source,
    graphviz,
    parse,
    statistics,
    validate,
)


class WorkflowDSLTests(unittest.TestCase):
    def test_compiles_example(self) -> None:
        workflow = compile_source(EXAMPLE, filename="example.helix")
        payload = workflow.as_dict()
        self.assertEqual(payload["name"], "release-pipeline")
        self.assertEqual(payload["metadata"]["team"], "platform")
        self.assertEqual([task["id"] for task in payload["tasks"]], [
            "prepare", "test-linux", "test-windows", "publish"
        ])
        prepare = payload["tasks"][0]
        self.assertEqual(prepare["timeout_seconds"], 30)
        self.assertEqual(prepare["retry"]["max_attempts"], 2)
        self.assertEqual(prepare["retry"]["base_delay_ms"], 250)
        self.assertEqual(prepare["retry"]["max_delay_ms"], 2000)

    def test_round_trip_formatter(self) -> None:
        first = parse(EXAMPLE, filename="first.helix")
        formatted = format_source(first)
        second = parse(formatted, filename="second.helix")
        self.assertEqual(compile_source(EXAMPLE).as_dict(), compile_source(formatted).as_dict())
        self.assertEqual(first.name, second.name)

    def test_graphviz_contains_edges(self) -> None:
        dot = graphviz(parse(EXAMPLE))
        self.assertIn('"prepare" -> "test-linux";', dot)
        self.assertIn('"test-linux" -> "publish";', dot)
        self.assertTrue(dot.startswith("digraph helixgrid"))

    def test_statistics(self) -> None:
        stats = statistics(parse(EXAMPLE))
        self.assertEqual(stats["tasks"], 4)
        self.assertEqual(stats["edges"], 4)
        self.assertEqual(stats["roots"], ["prepare"])
        self.assertEqual(stats["sinks"], ["publish"])
        self.assertEqual(stats["parallelism_upper_bound"], 2)
        self.assertEqual(stats["critical_path_tasks"], 3)

    def test_weighted_critical_path(self) -> None:
        node = parse(EXAMPLE)
        path, score = critical_path(node, {
            "prepare": 1,
            "test-linux": 2,
            "test-windows": 10,
            "publish": 1,
        })
        self.assertEqual(path, ["prepare", "test-windows", "publish"])
        self.assertEqual(score, 12)

    def test_cycle_diagnostic(self) -> None:
        source = '''
workflow bad {
  task a {
    run ["true"]
    needs [c]
  }
  task b {
    run ["true"]
    needs [a]
  }
  task c {
    run ["true"]
    needs [b]
  }
}
'''
        node = parse(source, filename="cycle.helix")
        cycle = find_cycle(node)
        self.assertTrue(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        diagnostics = validate(node, source="cycle.helix")
        self.assertTrue(any(item.code == "E200" for item in diagnostics))
        with self.assertRaises(DSLError):
            compile_source(source, filename="cycle.helix")

    def test_unknown_dependency(self) -> None:
        source = '''
workflow bad {
  task a {
    run ["true"]
    needs [missing]
  }
}
'''
        diagnostics = validate(parse(source), source="unknown.helix")
        self.assertTrue(any(item.code == "E104" for item in diagnostics))

    def test_duplicate_task_is_diagnostic(self) -> None:
        source = '''
workflow duplicate {
  task same { run ["true"] }
  task same { run ["true"] }
}
'''
        with self.assertRaises(DSLError):
            parse(source, filename="duplicate.helix")

    def test_requires_command(self) -> None:
        source = '''
workflow missing {
  task task-a {
    timeout 10s
  }
}
'''
        with self.assertRaisesRegex(DSLError, "missing a run command"):
            parse(source, filename="missing.helix")

    def test_timeout_rejects_subsecond_value(self) -> None:
        source = '''
workflow timeout {
  task a {
    run ["true"]
    timeout 250ms
  }
}
'''
        with self.assertRaisesRegex(DSLError, "whole seconds"):
            parse(source, filename="timeout.helix")

    def test_retry_max_must_cover_base(self) -> None:
        source = '''
workflow retry {
  task a {
    run ["true"]
    retry attempts=3, base=2s, max=1s
  }
}
'''
        with self.assertRaisesRegex(DSLError, "smaller"):
            parse(source)

    def test_comments_and_escapes(self) -> None:
        source = r'''
# comment before workflow
workflow escaped {
  meta note = "line\nvalue" # inline comment
  task a {
    run ["python", "-c", "print(\"hello\")"]
    env MESSAGE = "tab\tvalue"
  }
}
'''
        workflow = compile_source(source).as_dict()
        self.assertEqual(workflow["metadata"]["note"], "line\nvalue")
        self.assertEqual(workflow["tasks"][0]["env"]["MESSAGE"], "tab\tvalue")

    def test_parallelism_estimate_for_wide_graph(self) -> None:
        source = '''
workflow wide {
  task root { run ["true"] }
  task a { run ["true"] needs [root] }
  task b { run ["true"] needs [root] }
  task c { run ["true"] needs [root] }
  task join { run ["true"] needs [a, b, c] }
}
'''
        self.assertEqual(estimate_parallelism(parse(source)), 3)


if __name__ == "__main__":
    unittest.main()
