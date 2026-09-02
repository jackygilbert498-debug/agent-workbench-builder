from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


RUNTIME_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets/dsh-product-template/tools/dsh_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("builder_dsh_runtime", RUNTIME_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


def make_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


def remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = BytesIO()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = -9


class DshRuntimeUnitTests(unittest.TestCase):
    def test_bounded_subprocess_stops_noisy_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = RUNTIME.run_bounded_subprocess(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
                cwd=Path(raw),
                timeout=10,
                output_limit=4096,
            )
        self.assertTrue(result["outputLimited"])
        self.assertFalse(result["timedOut"])
        self.assertLessEqual(len(result["stdout"]), 2400)

    def test_stage_bounds_member_count_and_total_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "product"
            project.mkdir()
            (project / "one.txt").write_text("one", encoding="utf-8")
            (project / "two.txt").write_text("two", encoding="utf-8")
            with patch.object(RUNTIME, "MAX_STAGE_MEMBERS", 1):
                with self.assertRaisesRegex(RUNTIME.DshRuntimeError, "member count"):
                    RUNTIME.stage_product_bundle(project, root / "member-stage")
            with patch.object(RUNTIME, "MAX_STAGE_TOTAL_BYTES", 5):
                with self.assertRaisesRegex(RUNTIME.DshRuntimeError, "total bytes"):
                    RUNTIME.stage_product_bundle(project, root / "byte-stage")

    def test_stage_rejects_linked_or_junction_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "product"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "package.json").write_text("{}\n", encoding="utf-8")
            (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
            linked = project / "linked-source"
            try:
                make_directory_link(outside, linked)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(RUNTIME.DshRuntimeError, "link or junction"):
                    RUNTIME.stage_product_bundle(project, root / "stage")
            finally:
                remove_directory_link(linked)

    def test_large_runtime_output_uses_seekable_log_without_pipe_backpressure(self) -> None:
        with tempfile.TemporaryFile() as runtime_log:
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000 + 'TAIL')"],
                stdout=runtime_log,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(process.wait(timeout=10), 0)
            tail = RUNTIME._bounded_log_tail(runtime_log, 64)
            self.assertEqual(len(tail), 64)
            self.assertTrue(tail.endswith("TAIL"))

    def test_unsafe_windows_product_path_is_staged_without_generated_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "中文 product path"
            (project / "src").mkdir(parents=True)
            (project / "src/main.mjs").write_text("export default 1\n", encoding="utf-8")
            (project / "package.json").write_text("{}\n", encoding="utf-8")
            for excluded in (
                "evidence",
                "dist",
                "work",
                ".runtime",
                "_handoff",
                "node_modules",
            ):
                directory = project / excluded
                directory.mkdir()
                (directory / "ignored.txt").write_text("ignored", encoding="utf-8")
            stage_root = root / "safe-stage"
            staged, receipt = RUNTIME.stage_product_bundle(project, stage_root)
            self.assertEqual(staged, stage_root / "product")
            self.assertTrue((staged / "src/main.mjs").is_file())
            self.assertFalse((staged / "evidence").exists())
            self.assertFalse((staged / "_handoff").exists())
            self.assertEqual(receipt["sourceTreeSha256"], receipt["stagedTreeSha256"])
            self.assertGreaterEqual(receipt["files"], 2)
            direct = RUNTIME.portable_staging_evidence(project)
            staged_evidence = RUNTIME.portable_staging_evidence(project, receipt)
            self.assertEqual(direct, staged_evidence)
            self.assertNotIn("used", direct)
            self.assertNotIn("reason", direct)
            self.assertEqual(direct["status"], "PASS")

    def test_controlled_stop_uses_stdin_sentinel_before_process_signals(self) -> None:
        process = FakeProcess()
        receipt = RUNTIME.stop_runtime_process(process, graceful_timeout=0.1)
        self.assertEqual(
            process.stdin.getvalue(),
            b"__AGENT_WORKBENCH_STOP__\n",
        )
        self.assertEqual(receipt["method"], "stdin-sentinel")
        self.assertTrue(receipt["clean"])
        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)


if __name__ == "__main__":
    unittest.main()
