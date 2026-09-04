from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scaffold_project import ScaffoldError, _load_blueprint, scaffold
from evaluate_project import _validate_contract, _verify_immutable_harness


def build_focused(root: Path, name: str = "单场景 项目", *, runtime: str = "dsh") -> Path:
    destination = root / name
    scaffold(
        destination,
        product_kind="focused-agent",
        slug="request-triage-agent",
        title="请求分诊 Agent",
        scenario="把本地请求分诊为待办",
        primary_user="项目负责人",
        trigger="收到新的请求文件",
        input_description="包含 task_id、scenario_id 和 content 的 JSON",
        observable_output="经批准后生成的任务 JSON",
        dangerous_write="在输出目录创建任务文件",
        runtime=runtime,
    )
    return destination


def blueprint() -> dict[str, object]:
    return json.loads(
        (SKILL_ROOT / "assets/workbench-blueprint.example.json").read_text(encoding="utf-8")
    )


def build_workbench(root: Path, name: str = "通用 工作台") -> Path:
    destination = root / name
    scaffold(
        destination,
        product_kind="workbench",
        blueprint=blueprint(),
    )
    return destination


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


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


class ScaffoldTests(unittest.TestCase):
    def test_git_clone_preserves_protected_harness_with_autocrlf_enabled(self) -> None:
        """A Windows Git checkout must not invalidate the release's byte contract."""

        for runtime in ("standalone", "dsh"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                project = build_focused(root, "original", runtime=runtime)
                clone = root / "receiver"
                git = [
                    "git", "-c", "core.autocrlf=true",
                    "-c", f"core.hooksPath={root / 'no-hooks'}",
                    "-c", "user.name=Builder Test",
                    "-c", "user.email=builder-test@example.invalid",
                    "-c", "commit.gpgSign=false",
                ]
                for args in (
                    ["init", "--quiet"],
                    ["add", "--all"],
                    ["commit", "--quiet", "-m", "test fixture"],
                    ["clone", "--quiet", "--", str(project), str(clone)],
                ):
                    result = subprocess.run(
                        [*git, *args], cwd=project, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=60, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                passed, details = _verify_immutable_harness(clone, _validate_contract(clone))
                self.assertTrue(passed, details)

    def test_blueprint_reader_rejects_oversized_and_non_regular_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ScaffoldError, "exceeds"):
                _load_blueprint(oversized)
            with self.assertRaisesRegex(ScaffoldError, "regular file"):
                _load_blueprint(root)

    def test_scaffold_rejects_destination_under_linked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.mkdir()
            linked_parent = root / "linked-parent"
            try:
                make_directory_link(outside, linked_parent)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(ScaffoldError, "link or junction"):
                    build_focused(linked_parent, "redirected-project")
                self.assertFalse((outside / "redirected-project").exists())
            finally:
                remove_directory_link(linked_parent)

    def test_scaffold_rejects_dangling_destination_link(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "dangling-project"
            outside = root / "missing-target"
            try:
                os.symlink(outside, destination, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            with self.assertRaisesRegex(ScaffoldError, "already exists|link or junction"):
                build_focused(root, destination.name)
            self.assertFalse(outside.exists())

    def test_acceptance_rejects_contract_path_traversal_before_external_runtime(self) -> None:
        for runtime in ("standalone", "dsh"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                project = build_focused(root, runtime=runtime)
                contract_path = project / "agent_project.json"
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                contract["development"]["domainEvidence"]["report"] = "../outside-domain.json"
                contract_path.write_text(
                    json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                outside = root / "outside-domain.json"
                command = [
                    sys.executable,
                    "tools/acceptance.py",
                    "--output",
                    "evidence/path-safety.json",
                ]
                if runtime == "dsh":
                    command.extend(["--dsh-root", str(root / "untrusted-dsh")])
                completed = subprocess.run(
                    command,
                    cwd=project,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(completed.returncode, 3)
                self.assertFalse(outside.exists())
                self.assertIn(
                    "must stay inside the project",
                    completed.stdout.decode("utf-8", errors="replace"),
                )

    def test_acceptance_rejects_required_file_through_directory_link(self) -> None:
        for runtime in ("standalone", "dsh"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                project = build_focused(root, runtime=runtime)
                outside = root / "outside"
                outside.mkdir()
                (outside / "secret.txt").write_text("do not read\n", encoding="utf-8")
                link = project / "linked-source"
                try:
                    make_directory_link(outside, link)
                except OSError as exc:
                    self.skipTest(f"directory links are unavailable: {exc}")
                try:
                    contract_path = project / "agent_project.json"
                    contract = json.loads(contract_path.read_text(encoding="utf-8"))
                    contract["requiredFiles"].append("linked-source/secret.txt")
                    contract_path.write_text(
                        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    command = [
                        sys.executable,
                        "tools/acceptance.py",
                        "--output",
                        "evidence/link-safety.json",
                    ]
                    if runtime == "dsh":
                        command.extend(["--dsh-root", str(root / "untrusted-dsh")])
                    completed = subprocess.run(
                        command,
                        cwd=project,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 3)
                    self.assertIn(
                        "link or junction",
                        completed.stdout.decode("utf-8", errors="replace"),
                    )
                finally:
                    remove_directory_link(link)

    def test_focused_scaffold_is_deterministic_and_portable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = build_focused(root, "项目 一")
            second = build_focused(root, "项目 二")
            self.assertEqual(tree_digest(first), tree_digest(second))
            text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in first.rglob("*")
                if path.is_file() and path.suffix not in {".zip", ".pyc", ".pyo"}
            )
            self.assertNotIn("__PROJECT_", text)
            self.assertNotIn(str(SKILL_ROOT), text)
            # Generated instructions must remain true after domain adaptation;
            # the project contract, not inherited prose, owns current state.
            agents = (first / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("agent_project.json#development.stage", agents)
            self.assertNotIn("当前生成状态是 `starter`", agents)
            readme = (first / "README.md").read_text(encoding="utf-8")
            self.assertIn("agent_project.json#development.stage", readme)
            self.assertNotIn("当前 `development.stage=starter`", readme)
            contract = json.loads((first / "agent_project.json").read_text(encoding="utf-8"))
            provenance = json.loads(
                (first / "builder-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["schema"], "agent-workbench-project/v4")
            self.assertEqual(contract["project"]["kind"], "focused-agent")
            self.assertEqual(len(contract["capabilities"]), 1)
            self.assertEqual(len(contract["acceptanceScenarios"]), 1)
            self.assertFalse(contract["runtime"]["bundled"])
            self.assertEqual(contract["development"]["stage"], "starter")
            self.assertEqual(
                contract["development"]["domainEvidence"]["fixtures"],
                "fixtures/domain-cases.json",
            )
            self.assertEqual(provenance["schema"], "agent-workbench-builder-provenance/v3")
            self.assertEqual(provenance["builderVersion"], "4.0.3")
            self.assertEqual(provenance["builderReleaseTag"], "v4.0.3")
            self.assertFalse(provenance["builderBundled"])
            self.assertEqual(
                provenance["builderPublicUrl"],
                "https://github.com/jackygilbert498-debug/agent-workbench-builder",
            )
            self.assertEqual(provenance["starterStage"], "starter")
            self.assertEqual(
                set(provenance["starterFileSha256"]),
                set(contract["development"]["criticalFiles"]),
            )
            self.assertTrue(
                all(len(value) == 64 for value in provenance["starterFileSha256"].values())
            )
            self.assertTrue((first / "fixtures/domain-cases.json").is_file())
            self.assertFalse(any(path.name == "DSH" and path.is_dir() for path in first.rglob("*")))

    def test_generated_rollback_preserves_state_until_explicit_owner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for runtime in ("standalone", "dsh"):
                project = build_focused(Path(raw), f"rollback-{runtime}", runtime=runtime)
                contract = json.loads((project / "agent_project.json").read_text(encoding="utf-8"))
                rollback = contract["rollback"].casefold()
                self.assertNotRegex(rollback, r"\b(?:delete|remove)\b|删除")
                self.assertIn("backup", rollback)
                self.assertIn("hash", rollback)
                self.assertIn("explicitly confirms", rollback)
                readme = (project / "README.md").read_text(encoding="utf-8")
                rollback_lines = "\n".join(
                    line for line in readme.splitlines() if "回退" in line or "rollback" in line.casefold()
                )
                self.assertNotRegex(rollback_lines.casefold(), r"\b(?:delete|remove)\b|删除")
                self.assertIn("备份", rollback_lines)

    def test_workbench_scaffold_preserves_multiple_capabilities_and_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = build_workbench(Path(raw))
            contract = json.loads((project / "agent_project.json").read_text(encoding="utf-8"))
            provenance = json.loads(
                (project / "builder-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["project"]["kind"], "workbench")
            self.assertEqual(len(contract["capabilities"]), 3)
            self.assertEqual(len(contract["acceptanceScenarios"]), 3)
            self.assertEqual(provenance["productKind"], "workbench")
            self.assertTrue((project / "src/capabilities.mjs").is_file())
            fixtures = json.loads(
                (project / "fixtures/domain-cases.json").read_text(encoding="utf-8")
            )
            positive = [case for case in fixtures["cases"] if case["kind"] == "positive"]
            boundary = [case for case in fixtures["cases"] if case["kind"] == "boundary"]
            self.assertGreaterEqual(len(positive), 3)
            self.assertGreaterEqual(len(boundary), 1)
            self.assertEqual(
                {case["scenarioId"] for case in positive},
                {item["id"] for item in contract["acceptanceScenarios"]},
            )
            self.assertEqual(
                {case["capabilityId"] for case in positive},
                {item["id"] for item in contract["capabilities"]},
            )

    def test_documented_workbench_blueprint_is_runnable(self) -> None:
        text = (SKILL_ROOT / "references/workbench-blueprint.md").read_text(
            encoding="utf-8"
        )
        fenced_json = text.split("```json", 1)[1].split("```", 1)[0]
        documented_blueprint = json.loads(fenced_json)
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "documented-workbench"
            scaffold(
                project,
                product_kind="workbench",
                blueprint=documented_blueprint,
            )
            contract = json.loads(
                (project / "agent_project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["project"]["kind"], "workbench")
            self.assertEqual(len(contract["capabilities"]), 2)
            self.assertEqual(len(contract["acceptanceScenarios"]), 3)

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "existing"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(ScaffoldError):
                build_focused(Path(raw), "existing")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_invalid_slug_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ScaffoldError):
                scaffold(
                    Path(raw) / "project",
                    product_kind="focused-agent",
                    slug="Bad Slug",
                    title="Title",
                    scenario="Scenario",
                    primary_user="User",
                    trigger="Trigger",
                    input_description="Input",
                    observable_output="Output",
                    dangerous_write="Write",
                )

    def test_workbench_rejects_single_scenario_blueprint(self) -> None:
        value = blueprint()
        value["scenarios"] = value["scenarios"][:1]
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ScaffoldError, "3-20"):
                scaffold(
                    Path(raw) / "project",
                    product_kind="workbench",
                    blueprint=value,
                )

    def test_generated_projects_run_their_unit_tests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for project in (build_focused(Path(raw)), build_workbench(Path(raw))):
                completed = subprocess.run(
                    [sys.executable, "tools/test_project.py"],
                    cwd=project,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode(errors="replace"),
                )

    def test_dsh_project_tests_preserve_existing_business_work_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = build_focused(Path(raw), "used-project", runtime="dsh")
            business_file = project / "work" / "outputs" / "existing.json"
            business_file.parent.mkdir(parents=True)
            original = b'{"owner":"user","status":"committed"}\n'
            business_file.write_bytes(original)

            completed = subprocess.run(
                [sys.executable, "tools/test_project.py"],
                cwd=project,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode(errors="replace"),
            )
            self.assertEqual(business_file.read_bytes(), original)

    def test_standalone_supports_focused_agent_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = build_focused(Path(raw), "standalone", runtime="standalone")
            self.assertTrue((destination / "agent_workbench/core.py").is_file())
            self.assertFalse((destination / "cordis.patch.yml").exists())
            with self.assertRaisesRegex(ScaffoldError, "focused-agent only"):
                scaffold(
                    Path(raw) / "unsupported",
                    product_kind="workbench",
                    blueprint=blueprint(),
                    runtime="standalone",
                )


if __name__ == "__main__":
    unittest.main()
