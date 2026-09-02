from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dsh_doctor import (
    OFFICIAL_DSH_REPOSITORY_IDENTITY,
    TESTED_DSH_COMMIT,
    TESTED_DSH_TAG,
    _git_provenance,
    _provenance_limitations,
    _provenance_verified,
    _repository_identity,
    diagnose,
)


class DshDoctorTests(unittest.TestCase):
    def test_unverified_checkout_is_never_executed_by_live_doctor(self) -> None:
        unverified = {
            "isRepository": True,
            "head": "0" * 40,
            "dirty": False,
            "originIdentity": "github.com/example/fork",
            "tagCommit": None,
            "tagsAtHead": [],
            "inspectionError": None,
        }
        with tempfile.TemporaryDirectory() as raw, patch(
            "dsh_doctor._git_provenance", return_value=unverified
        ), patch("dsh_doctor.inspect_external_dsh") as runtime_probe:
            report, code = diagnose(Path(raw), live=True)
        runtime_probe.assert_not_called()
        self.assertEqual((report["status"], code), ("PARTIAL", 2))
        self.assertEqual(report["observed"]["runtimeProbe"], "skipped-unverified-provenance")

    def test_git_provenance_disables_repository_fsmonitor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Builder Test"], check=True)
            (root / "marker.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            sentinel = root.parent / "fsmonitor-ran.txt"
            if os.name == "nt":
                monitor = root.parent / "fsmonitor.cmd"
                monitor.write_text(
                    f"@echo off\r\necho ran>\"{sentinel}\"\r\nexit /b 0\r\n",
                    encoding="utf-8",
                )
            else:
                monitor = root.parent / "fsmonitor.sh"
                monitor.write_text(
                    f"#!/bin/sh\nprintf ran > '{sentinel}'\nexit 0\n",
                    encoding="utf-8",
                )
                monitor.chmod(0o755)
            subprocess.run(["git", "-C", str(root), "config", "core.fsmonitor", str(monitor)], check=True)
            provenance = _git_provenance(root)
            self.assertFalse(sentinel.exists())
            self.assertIsNone(provenance["inspectionError"])

    def test_status_inspection_failure_can_never_be_clean(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch(
            "dsh_doctor._git_bytes",
            side_effect=[b"true\n", RuntimeError("status failed")],
        ):
            provenance = _git_provenance(Path(raw))
        self.assertIsNone(provenance["dirty"])
        self.assertIsNotNone(provenance["inspectionError"])
        self.assertFalse(_provenance_verified(provenance))

    def test_git_provenance_records_head_and_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Builder Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True)
            marker = root / "marker.txt"
            marker.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            clean = _git_provenance(root)
            self.assertTrue(clean["isRepository"])
            self.assertFalse(clean["dirty"])
            self.assertTrue(clean["topLevelMatches"])
            self.assertTrue(clean["trackedTreeVerified"])
            self.assertEqual(clean["trackedFilesChecked"], 1)
            self.assertRegex(clean["head"], r"^[a-f0-9]{40}$")
            marker.write_text("two\n", encoding="utf-8")
            dirty = _git_provenance(root)
            self.assertTrue(dirty["dirty"])
            self.assertEqual(dirty["trackedChanged"], 1)
            self.assertEqual(dirty["untracked"], 0)
            self.assertRegex(dirty["statusSha256"], r"^[a-f0-9]{64}$")
            self.assertFalse(_provenance_verified(clean))
            self.assertTrue(_provenance_limitations(clean))
            self.assertIn("dirty", _provenance_limitations(dirty)[0].lower())

    def test_only_exact_clean_official_rc8_provenance_is_verified(self) -> None:
        verified = {
            "isRepository": True,
            "head": TESTED_DSH_COMMIT,
            "dirty": False,
            "topLevelMatches": True,
            "trackedTreeVerified": True,
            "unsafeIndexFlags": [],
            "originIdentity": OFFICIAL_DSH_REPOSITORY_IDENTITY,
            "tagCommit": TESTED_DSH_COMMIT,
            "tagsAtHead": [TESTED_DSH_TAG],
        }
        self.assertTrue(_provenance_verified(verified))
        self.assertEqual(_provenance_limitations(verified), [])
        for key, value in (
            ("head", "0" * 40),
            ("dirty", True),
            ("topLevelMatches", False),
            ("trackedTreeVerified", False),
            ("unsafeIndexFlags", [{"path": "marker", "flag": "S"}]),
            ("originIdentity", "github.com/example/fork"),
            ("tagCommit", "0" * 40),
            ("tagsAtHead", []),
        ):
            candidate = dict(verified, **{key: value})
            with self.subTest(key=key):
                self.assertFalse(_provenance_verified(candidate))
                self.assertTrue(_provenance_limitations(candidate))

    def test_repository_identity_rejects_arbitrary_schemes_and_paths(self) -> None:
        expected = "github.com/deepseek-ai/deepseek-harness"
        self.assertEqual(
            _repository_identity("https://github.com/deepseek-ai/deepseek-harness.git"),
            expected,
        )
        self.assertEqual(
            _repository_identity("ssh://git@github.com/deepseek-ai/deepseek-harness.git"),
            expected,
        )
        self.assertEqual(
            _repository_identity("git@github.com:deepseek-ai/deepseek-harness.git"),
            expected,
        )
        for value in (
            "file://github.com/deepseek-ai/deepseek-harness",
            "git://github.com/deepseek-ai/deepseek-harness",
            "github.com/deepseek-ai/deepseek-harness",
            "C:/github.com/deepseek-ai/deepseek-harness",
        ):
            with self.subTest(value=value):
                self.assertIsNone(_repository_identity(value))

    def test_subdirectory_is_not_accepted_as_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Builder Test"], check=True)
            (root / "marker.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            nested = root / "nested"
            nested.mkdir()

            provenance = _git_provenance(nested)

            self.assertFalse(provenance["topLevelMatches"])
            self.assertFalse(provenance["trackedTreeVerified"])
            self.assertFalse(_provenance_verified(provenance))

    def test_skip_worktree_cannot_hide_modified_tracked_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Builder Test"], check=True)
            marker = root / "marker.txt"
            marker.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "marker.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "update-index", "--skip-worktree", "marker.txt"],
                check=True,
            )
            marker.write_text("tampered\n", encoding="utf-8")

            provenance = _git_provenance(root)

            self.assertFalse(provenance["dirty"])
            self.assertFalse(provenance["trackedTreeVerified"])
            self.assertEqual(provenance["unsafeIndexFlags"], [{"path": "marker.txt", "flag": "S"}])
            self.assertFalse(_provenance_verified(provenance))


if __name__ == "__main__":
    unittest.main()
