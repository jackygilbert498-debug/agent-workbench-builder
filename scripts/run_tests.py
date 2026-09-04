#!/usr/bin/env python3
"""Run the complete, audited Builder test suite from any working directory."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = SKILL_ROOT / "tests"
EXPECTED_TEST_MODULES = frozenset(
    {
        "test_dsh_doctor.py",
        "test_dsh_runtime.py",
        "test_evaluate_project.py",
        "test_handoff_safety.py",
        "test_reproduction.py",
        "test_run_dsh.py",
        "test_run_tests.py",
        "test_scaffold_project.py",
    }
)
EXPECTED_TEST_COUNT = 84


class SuiteContractError(RuntimeError):
    """The shipped test suite is missing, expanded, or undiscoverable."""


def validate_suite(tests_root: Path, suite: unittest.TestSuite) -> None:
    """Fail closed when discovery no longer matches the audited suite contract."""

    actual_modules = frozenset(path.name for path in tests_root.glob("test_*.py") if path.is_file())
    if actual_modules != EXPECTED_TEST_MODULES:
        missing = sorted(EXPECTED_TEST_MODULES - actual_modules)
        extra = sorted(actual_modules - EXPECTED_TEST_MODULES)
        raise SuiteContractError(f"test module mismatch; missing={missing}, extra={extra}")
    observed_count = suite.countTestCases()
    if observed_count != EXPECTED_TEST_COUNT:
        raise SuiteContractError(
            f"test count mismatch; expected={EXPECTED_TEST_COUNT}, observed={observed_count}"
        )


def main() -> int:
    """Discover the exact shipped suite, validate its shape, and execute it."""

    scripts_root = str(SKILL_ROOT / "scripts")
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS_ROOT),
        pattern="test_*.py",
    )
    try:
        validate_suite(TESTS_ROOT, suite)
    except SuiteContractError as exc:
        print(f"SUITE-CONTRACT-FAIL: {exc}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
