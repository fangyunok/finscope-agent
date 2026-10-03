from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from finscope.evaluation import evaluate_cases
from finscope.resources import data_path


class SyntheticSequenceEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_independent_labelled_sequences_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            report = await evaluate_cases(output)
            failures = [case for case in report["cases"] if not case["passed"]]
            self.assertTrue(report["passed"], failures)
            self.assertEqual(report["cases_total"], 20)
            self.assertEqual(report["cases_passed"], 20)
            self.assertFalse(report["model_used"])
            self.assertEqual(report["model_calls"], 0)
            self.assertGreater(report["tool_calls"], 20)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_labels_are_unique_and_bundled_copy_matches(self):
        source = data_path("evaluation_cases.json")
        fixture = json.loads(source.read_text(encoding="utf-8"))
        self.assertTrue(fixture["synthetic"])
        ids = [case["case_id"] for case in fixture["cases"]]
        self.assertEqual(len(set(ids)), 20)
        package_copy = Path(__file__).resolve().parents[1] / "src" / "finscope" / "data" / source.name
        self.assertEqual(source.read_bytes(), package_copy.read_bytes())


if __name__ == "__main__":
    unittest.main()
