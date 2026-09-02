from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_tests import SuiteContractError, validate_suite


class TestRunnerContractTests(unittest.TestCase):
    def test_runner_rejects_missing_modules(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(SuiteContractError, "module mismatch"):
                validate_suite(Path(raw), unittest.TestSuite())

    def test_runner_rejects_wrong_discovered_count(self) -> None:
        skill_root = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(SuiteContractError, "count mismatch"):
            validate_suite(skill_root / "tests", unittest.TestSuite())


if __name__ == "__main__":
    unittest.main()
