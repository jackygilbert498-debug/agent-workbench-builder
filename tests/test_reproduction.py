from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import zipfile

from verify_reproduction import ReproductionError, _assert_no_path_leak, _require_verified_dsh, _run, _safe_member, reproduce


class ReproductionTests(unittest.TestCase):
    def test_path_leak_scan_rejects_utf16_and_unlisted_known_machine_paths(self) -> None:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-8"):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "receipt.txt").write_bytes("C:/Users/review/private.txt".encode(encoding))
                with self.assertRaisesRegex(ReproductionError, "serialized path leak"):
                    _assert_no_path_leak(root, [])

    def test_reproduction_rejects_fork_or_dirty_dsh_provenance(self) -> None:
        cases = [
            {"originIdentity": "github.com/example/fork", "head": "1" * 40, "dirty": False},
            {"originIdentity": "github.com/deepseek-ai/deepseek-harness", "head": "1" * 40, "dirty": True},
        ]
        for git in cases:
            with self.subTest(git=git), patch(
                "verify_reproduction.diagnose",
                return_value=({"status": "PARTIAL", "git": git}, 2),
            ), self.assertRaises(ReproductionError):
                _require_verified_dsh(Path("C:/fake-dsh"))

    def test_extraction_rejects_windows_drive_and_device_members(self) -> None:
        for value in ("C:/escape.txt", "NUL", "folder/COM1.txt", "//server/share"):
            with self.subTest(value=value):
                self.assertFalse(_safe_member(zipfile.ZipInfo(value)))

    def test_unicode_space_clean_room_reproduction(self) -> None:
        report = reproduce(runtime="standalone", product_kind="focused-agent")
        self.assertEqual(report["productKind"], "focused-agent")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["starterEvaluationStatus"], "PARTIAL")
        self.assertTrue(report["domainAdaptationApplied"])
        self.assertTrue(report["domainAdaptedProjectGraduated"])
        self.assertTrue(report["handoffExtractedAndGraduated"])
        self.assertTrue(report["resultDigestStable"])
        self.assertEqual(report["builder"]["version"], "4.0.1")
        self.assertEqual(report["builder"]["releaseTag"], "v4.0.1")
        self.assertRegex(report["builder"]["sourceTreeSha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["builder"]["protectedHarnessSha256"], r"^[0-9a-f]{64}$")
        self.assertIn("generatedAt", report)
        self.assertGreater(report["extractedSerializedFilesChecked"], 0)

    def test_child_json_is_utf8_without_parent_utf8_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "receipt.py"
            script.write_text(
                "import json\nprint(json.dumps({'schema':'test/v1','status':'通过'}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            previous_utf8 = os.environ.pop("PYTHONUTF8", None)
            previous_io = os.environ.pop("PYTHONIOENCODING", None)
            try:
                receipt = _run([sys.executable, str(script)], root)
            finally:
                if previous_utf8 is not None:
                    os.environ["PYTHONUTF8"] = previous_utf8
                if previous_io is not None:
                    os.environ["PYTHONIOENCODING"] = previous_io
            self.assertEqual(receipt["status"], "通过")

    def test_failed_child_receipt_redacts_workspace_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "fail.py"
            script.write_text(
                "import os,sys\nsys.stderr.write(os.getcwd() + ' token=ghp_123456789012345678901234567890\\n')\nraise SystemExit(7)\n",
                encoding="utf-8",
            )
            with self.assertRaises(ReproductionError) as caught:
                _run([sys.executable, str(script)], root)
            message = str(caught.exception)
            self.assertIn("exit 7", message)
            self.assertIn("<WORKDIR>", message)
            self.assertNotIn(str(root), message)
            self.assertNotIn("ghp_123456789012345678901234567890", message)


if __name__ == "__main__":
    unittest.main()
