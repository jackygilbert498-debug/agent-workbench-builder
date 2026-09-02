from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from agent_workbench.cli import main as cli_main
import agent_workbench.core as core_module
from agent_workbench.core import AgentError, run_agent
from agent_workbench.domain import ReferenceProvider
from agent_workbench.server import create_server


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = {"request_id": "test-001", "content": "urgent meeting request"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_once(self, *, approved: bool, run_id: str):
        return run_agent(
            self.request,
            approved=approved,
            run_id=run_id,
            state_dir=self.root / "state",
            output_dir=self.root / "output",
            receipt_dir=self.root / "receipts",
        )

    def test_denied_by_default_has_no_business_output(self) -> None:
        input_path = self.root / "request.json"
        input_path.write_text(json.dumps(self.request), encoding="utf-8")
        code = cli_main(["--input", str(input_path), "--run-id", "denied", "--work-dir", str(self.root / "work")])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "work/output").exists())

    def test_three_runs_write_once_and_share_outcome(self) -> None:
        results = [self.run_once(approved=True, run_id=f"run-{index}") for index in range(3)]
        self.assertEqual([item["status"] for item in results], ["committed", "replayed", "replayed"])
        self.assertEqual(sum(item["sideEffectWritten"] for item in results), 1)
        self.assertEqual(len({item["outcomeHash"] for item in results}), 1)
        self.assertEqual(len(list((self.root / "output").glob("*.json"))), 1)

    def test_concurrent_same_key_requests_commit_once(self) -> None:
        """Two workers may race, but only one may create the business side effect."""

        barrier = threading.Barrier(2)
        original_read_json = core_module.read_json

        class SynchronizedProvider(ReferenceProvider):
            def build_plan(self, request_id: str, content: str):
                barrier.wait(timeout=5)
                return super().build_plan(request_id, content)

        def slow_read(*args, **kwargs):
            value = original_read_json(*args, **kwargs)
            time.sleep(0.1)
            return value

        results = []
        errors = []

        def worker(index: int) -> None:
            try:
                results.append(
                    run_agent(
                        self.request,
                        approved=True,
                        run_id=f"concurrent-{index}",
                        state_dir=self.root / "state",
                        output_dir=self.root / "output",
                        receipt_dir=self.root / "receipts",
                        provider=SynchronizedProvider(),
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)

        with patch("agent_workbench.core.read_json", side_effect=slow_read):
            threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item["status"] for item in results), ["committed", "replayed"])
        self.assertEqual(sum(item["sideEffectWritten"] for item in results), 1)
        self.assertEqual(len(list((self.root / "output").glob("*.json"))), 1)

    def test_concurrent_distinct_keys_preserve_both_ledger_entries(self) -> None:
        barrier = threading.Barrier(2)

        class SynchronizedProvider(ReferenceProvider):
            def build_plan(self, request_id: str, content: str):
                barrier.wait(timeout=5)
                return super().build_plan(request_id, content)

        results = []

        def worker(index: int) -> None:
            results.append(
                run_agent(
                    {"request_id": f"distinct-{index}", "content": f"request {index}"},
                    approved=True,
                    run_id=f"distinct-{index}",
                    state_dir=self.root / "state",
                    output_dir=self.root / "output",
                    receipt_dir=self.root / "receipts",
                    provider=SynchronizedProvider(),
                )
            )

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([item["status"] for item in results].count("committed"), 2)
        ledger = json.loads((self.root / "state/idempotency-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["entries"]), 2)
        self.assertEqual(len(list((self.root / "output").glob("*.json"))), 2)
        for index in range(2):
            replay = run_agent(
                {"request_id": f"distinct-{index}", "content": f"request {index}"},
                approved=True,
                run_id=f"distinct-replay-{index}",
                state_dir=self.root / "state",
                output_dir=self.root / "output",
                receipt_dir=self.root / "receipts",
            )
            self.assertEqual(replay["status"], "replayed")

    def test_separate_processes_commit_same_key_once(self) -> None:
        start = self.root / "start"
        code = """
import json, pathlib, sys, time
from agent_workbench.core import run_agent
start = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 10
while not start.exists():
    if time.monotonic() >= deadline:
        raise SystemExit('start timeout')
    time.sleep(0.01)
root = pathlib.Path(sys.argv[2])
result = run_agent(
    {'request_id': 'process-001', 'content': 'urgent meeting request'},
    approved=True,
    run_id=sys.argv[3],
    state_dir=root / 'state',
    output_dir=root / 'output',
    receipt_dir=root / 'receipts',
)
print(json.dumps(result))
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(start), str(self.root), f"process-{index}"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(2)
        ]
        start.write_text("go\n", encoding="utf-8")
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sorted(item["status"] for item in results), ["committed", "replayed"])
        self.assertEqual(sum(item["sideEffectWritten"] for item in results), 1)

    def test_separate_processes_preserve_distinct_ledger_keys(self) -> None:
        start = self.root / "distinct-start"
        code = """
import json, pathlib, sys, time
from agent_workbench.core import run_agent
start = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 10
while not start.exists():
    if time.monotonic() >= deadline:
        raise SystemExit('start timeout')
    time.sleep(0.01)
root = pathlib.Path(sys.argv[2])
request_id = sys.argv[3]
result = run_agent(
    {'request_id': request_id, 'content': 'distinct ' + request_id},
    approved=True,
    run_id='run-' + request_id,
    state_dir=root / 'state', output_dir=root / 'output', receipt_dir=root / 'receipts',
)
print(json.dumps(result))
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(start), str(self.root), f"worker-{index}"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(2)
        ]
        start.write_text("go\n", encoding="utf-8")
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "committed")
        ledger = json.loads((self.root / "state/idempotency-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["entries"]), 2)
        self.assertEqual(len(list((self.root / "output").glob("*.json"))), 2)

    def test_linked_output_directory_is_rejected_before_write(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked-output"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory links are unavailable: {exc}")
        with self.assertRaises(AgentError) as raised:
            run_agent(
                self.request,
                approved=True,
                run_id="linked",
                state_dir=self.root / "state",
                output_dir=linked,
                receipt_dir=self.root / "receipts",
            )
        self.assertEqual(raised.exception.code, "UNSAFE_PATH")
        self.assertEqual(list(outside.iterdir()), [])

    def test_denial_receipt_is_auditable(self) -> None:
        result = self.run_once(approved=False, run_id="deny-1")
        self.assertEqual(result["status"], "denied")
        self.assertFalse(result["sideEffectWritten"])
        self.assertTrue((self.root / "receipts/deny-1.json").is_file())

    def test_receipt_is_immutable_across_denied_approved_and_other_tasks(self) -> None:
        denied = self.run_once(approved=False, run_id="immutable")
        receipt_path = self.root / "receipts/immutable.json"
        before = receipt_path.read_bytes()
        self.assertEqual(self.run_once(approved=False, run_id="immutable"), denied)

        with self.assertRaises(AgentError) as approval_conflict:
            self.run_once(approved=True, run_id="immutable")
        self.assertEqual(approval_conflict.exception.code, "RECEIPT_CONFLICT")
        self.assertFalse((self.root / "output/test-001.json").exists())
        self.assertFalse((self.root / "state/idempotency-ledger.json").exists())
        with self.assertRaises(AgentError) as task_conflict:
            run_agent(
                {"request_id": "other-001", "content": "another task"},
                approved=False,
                run_id="immutable",
                state_dir=self.root / "state",
                output_dir=self.root / "output",
                receipt_dir=self.root / "receipts",
            )
        self.assertEqual(task_conflict.exception.code, "RECEIPT_CONFLICT")
        self.assertEqual(receipt_path.read_bytes(), before)

    def test_oversized_ledger_is_rejected_without_rewrite(self) -> None:
        ledger = self.root / "state/idempotency-ledger.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(b" " * (1024 * 1024 + 1))
        before = ledger.read_bytes()

        with self.assertRaises(AgentError) as raised:
            self.run_once(approved=True, run_id="ledger-limit")

        self.assertEqual(raised.exception.code, "LEDGER_UNREADABLE")
        self.assertEqual(ledger.read_bytes(), before)

    def test_invalid_request_has_stable_recovery(self) -> None:
        with self.assertRaises(AgentError) as raised:
            run_agent(
                {"request_id": "bad", "content": ""},
                approved=True,
                run_id="invalid",
                state_dir=self.root / "state",
                output_dir=self.root / "output",
                receipt_dir=self.root / "receipts",
            )
        self.assertEqual(raised.exception.code, "INVALID_REQUEST")
        self.assertTrue(raised.exception.recovery)

    def test_windows_device_names_are_rejected_for_request_and_run_ids(self) -> None:
        with self.assertRaises(AgentError) as request_error:
            run_agent(
                {"request_id": "NUL", "content": "valid content"},
                approved=True,
                run_id="safe-run",
                state_dir=self.root / "state",
                output_dir=self.root / "output",
                receipt_dir=self.root / "receipts",
            )
        self.assertEqual(request_error.exception.code, "INVALID_REQUEST_ID")
        with self.assertRaises(AgentError) as run_error:
            self.run_once(approved=False, run_id="CON")
        self.assertEqual(run_error.exception.code, "INVALID_RUN_ID")

    def test_cli_rejects_oversized_input_before_json_decode(self) -> None:
        input_path = self.root / "oversized.json"
        input_path.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))
        code = cli_main(
            [
                "--input",
                str(input_path),
                "--run-id",
                "oversized",
                "--work-dir",
                str(self.root / "work"),
            ]
        )
        self.assertEqual(code, 3)
        self.assertFalse((self.root / "work").exists())

    def test_ledger_artifact_conflict_never_overwrites(self) -> None:
        self.run_once(approved=True, run_id="first")
        artifact = self.root / "output/test-001.json"
        artifact.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(AgentError) as raised:
            self.run_once(approved=True, run_id="retry")
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "{}\n")

    def test_loopback_health_and_html(self) -> None:
        server = create_server("127.0.0.1", 0, self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(f"{base}/api/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertEqual(health["status"], "ok")
            self.assertIn("read-only status", html)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
