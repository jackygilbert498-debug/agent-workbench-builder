#!/usr/bin/env python3
"""Run the generated product's dependency-free Node test suite."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsh_runtime import DshRuntimeError, find_compatible_node, run_bounded_subprocess  # noqa: E402


def main() -> int:
    tests = sorted((PROJECT_ROOT / "tests").glob("*.test.mjs"))
    try:
        node, node_version = find_compatible_node()
        if not tests:
            raise DshRuntimeError("no Node tests were found")
        completed = run_bounded_subprocess(
            [str(node), "--test", *[str(path) for path in tests]],
            cwd=PROJECT_ROOT,
            environment=None,
            timeout=90,
        )
        if completed["outputLimited"]:
            raise DshRuntimeError("Node test output exceeded the safety limit")
        if completed["timedOut"]:
            raise DshRuntimeError("Node test suite timed out")
        passed = completed["returnCode"] == 0
        report = {
            "schema": "agent-workbench-test/v3",
            "status": "PASS" if passed else "FAIL",
            "nodeVersion": node_version,
            "testFiles": len(tests),
            "proof": "node-test-live-exit-code",
        }
        code = 0 if passed else 3
    except (OSError, subprocess.TimeoutExpired, DshRuntimeError) as exc:
        report = {
            "schema": "agent-workbench-test/v3",
            "status": "FAIL",
            "error": {"code": "TEST_FAILED", "message": str(exc).replace(str(PROJECT_ROOT), "<PROJECT_ROOT>")},
        }
        code = 3
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
