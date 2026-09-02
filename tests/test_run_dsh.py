from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


LAUNCHER_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets/dsh-product-template/tools/run_dsh.py"
)
SPEC = importlib.util.spec_from_file_location("builder_run_dsh", LAUNCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


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


class DshLauncherTests(unittest.TestCase):
    def test_stage_root_rejects_link_or_junction_before_resolving(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            actual = root / "actual-stage"
            actual.mkdir()
            alias = root / "stage-alias"
            try:
                make_directory_link(actual, alias)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(LAUNCHER.DshRuntimeError, "link or junction"):
                    LAUNCHER.resolve_stage_base(alias / "child")
            finally:
                remove_directory_link(alias)

    def test_managed_stage_is_stable_and_keeps_state_in_original_project(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "中文 project with spaces"
            (project / "src").mkdir(parents=True)
            (project / "src/main.mjs").write_text("export default 1\n", encoding="utf-8")
            (project / "agent_project.json").write_text("{}\n", encoding="utf-8")
            (project / "work").mkdir()
            (project / "work/result.json").write_text("persistent\n", encoding="utf-8")
            stage_base = root / "safe-stage"

            first, first_receipt = LAUNCHER.prepare_managed_product(project, stage_base)
            second, second_receipt = LAUNCHER.prepare_managed_product(project, stage_base)
            self.assertEqual(first, second)
            self.assertFalse((first / "work").exists())
            self.assertTrue((project / "work/result.json").is_file())
            self.assertFalse(first_receipt["reused"])
            self.assertTrue(second_receipt["reused"])

            (project / "src/main.mjs").write_text("export default 2\n", encoding="utf-8")
            third, third_receipt = LAUNCHER.prepare_managed_product(project, stage_base)
            self.assertEqual(third, first)
            self.assertNotEqual(first_receipt["sourceTreeSha256"], third_receipt["sourceTreeSha256"])
            self.assertEqual(list(third.parent.glob("next-*")), [])
            self.assertEqual(list(third.parent.glob("previous-*")), [])

    def test_managed_parent_cannot_be_redirected_by_directory_link(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "project"
            (project / "src").mkdir(parents=True)
            (project / "src/main.mjs").write_text("export default 1\n", encoding="utf-8")
            (project / "agent_project.json").write_text("{}\n", encoding="utf-8")
            stage_base = root / "safe-stage"
            stage_base.mkdir()
            outside = root / "outside"
            outside.mkdir()
            alias = stage_base / "agent-workbench-stages"
            try:
                make_directory_link(outside, alias)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(LAUNCHER.DshRuntimeError, "link or junction"):
                    LAUNCHER.prepare_managed_product(project, stage_base)
            finally:
                remove_directory_link(alias)

    def test_runtime_lock_rejects_existing_file_link(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.lock"
            target.write_bytes(b"safe")
            alias = root / "runtime.lock"
            try:
                os.symlink(target, alias)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(LAUNCHER.DshRuntimeError, "link or junction"):
                    with LAUNCHER.exclusive_runtime_lock(alias):
                        self.fail("linked lock must not be acquired")
            finally:
                alias.unlink()

    def test_runtime_home_rejects_linked_runtime_or_dsh_home(self) -> None:
        for linked_component in (".runtime", "dsh-home"):
            with self.subTest(linked_component=linked_component), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                project = root / "project"
                project.mkdir()
                outside = root / "outside"
                outside.mkdir()
                if linked_component == "dsh-home":
                    (project / ".runtime").mkdir()
                    alias = project / ".runtime" / "dsh-home"
                else:
                    alias = project / ".runtime"
                try:
                    make_directory_link(outside, alias)
                except OSError as exc:
                    self.skipTest(f"directory links are unavailable: {exc}")
                try:
                    with self.assertRaisesRegex(LAUNCHER.DshRuntimeError, "link or junction"):
                        LAUNCHER._prepare_runtime_home(project)
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    remove_directory_link(alias)


if __name__ == "__main__":
    unittest.main()
