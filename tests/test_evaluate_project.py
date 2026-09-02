from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evaluate_project as evaluator
from evaluate_project import (
    EvaluationError,
    TESTED_DSH_COMMIT,
    _iter_scan_files,
    _run_command,
    _safe_relative,
    evaluate,
)
from scaffold_project import scaffold


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


def make_project(root: Path) -> Path:
    project = root / "不同 Agent 项目"
    scaffold(
        project,
        product_kind="focused-agent",
        slug="request-triage-agent",
        title="请求分诊 Agent",
        scenario="把本地请求分诊为待办",
        primary_user="项目负责人",
        trigger="收到新的请求文件",
        input_description="包含 request_id 和 content 的 JSON",
        observable_output="经批准后生成的任务 JSON",
        dangerous_write="在输出目录创建任务文件",
        runtime="standalone",
    )
    return project


def adapt_standalone_project(project: Path) -> None:
    """Turn the generated starter into a small but behaviorally distinct domain."""

    contract_path = project / "agent_project.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["development"]["stage"] = "domain-adapted"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    domain_path = project / "agent_workbench/domain.py"
    domain_text = domain_path.read_text(encoding="utf-8")
    domain_text = domain_text.replace(
        '("发票", "预算", "invoice", "budget")',
        '("发票", "预算", "报销", "invoice", "budget", "expense")',
    ).replace(
        'category = "finance"',
        'category = "expense-review"',
    )
    domain_path.write_text(domain_text, encoding="utf-8")

    fixture_path = project / "fixtures/domain-cases.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema": "agent-workbench-domain-fixtures/v1",
                "stage": "domain-adapted",
                "cases": [
                    {
                        "id": "expense-urgent",
                        "kind": "positive",
                        "scenarioId": "primary-task",
                        "capabilityId": "core-task",
                        "input": {
                            "request_id": "expense-001",
                            "content": "今天需要审核这笔报销",
                        },
                        "expected": {"category": "expense-review", "priority": "high"},
                    },
                    {
                        "id": "general-normal",
                        "kind": "positive",
                        "scenarioId": "primary-task",
                        "capabilityId": "core-task",
                        "input": {
                            "request_id": "general-001",
                            "content": "整理下周资料",
                        },
                        "expected": {"category": "general", "priority": "normal"},
                    },
                    {
                        "id": "empty-content",
                        "kind": "boundary",
                        "scenarioId": "primary-task",
                        "capabilityId": "core-task",
                        "input": {
                            "request_id": "invalid-001",
                            "content": "",
                        },
                        "expectedError": "INVALID_REQUEST",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    domain_test = project / "tests/test_workbench.py"
    domain_test.write_text(
        domain_test.read_text(encoding="utf-8")
        + "\n# Domain fixture: expense-review is intentionally project-specific.\n",
        encoding="utf-8",
    )


def replace_handoff_manifest(project: Path, payload: object) -> None:
    """Replace only the manifest while keeping receipt and sidecar self-consistent."""

    receipt_path = project / "evidence/handoff.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archive_path = project / receipt["archive"]
    sidecar_path = project / receipt["sidecar"]
    replacement = archive_path.with_name(f".{archive_path.name}.test-rewrite")
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(replacement, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "_handoff/manifest.json":
                data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                )
            target.writestr(info, data)
    os.replace(replacement, archive_path)
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    receipt["sha256"] = archive_hash
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sidecar_path.write_text(
        f"{archive_hash}  {archive_path.name}\n", encoding="utf-8", newline="\n"
    )


def remove_handoff_member(project: Path, relative: str) -> None:
    """Create a self-consistent but incomplete handoff for negative testing."""

    receipt_path = project / "evidence/handoff.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archive_path = project / receipt["archive"]
    sidecar_path = project / receipt["sidecar"]
    replacement = archive_path.with_name(f".{archive_path.name}.test-incomplete")
    with zipfile.ZipFile(archive_path) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    manifest = json.loads(
        next(data for info, data in members if info.filename == "_handoff/manifest.json")
    )
    manifest["files"] = [item for item in manifest["files"] if item["path"] != relative]
    with zipfile.ZipFile(replacement, "w") as target:
        for info, data in members:
            if info.filename == relative:
                continue
            if info.filename == "_handoff/manifest.json":
                data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                )
            target.writestr(info, data)
    os.replace(replacement, archive_path)
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    receipt["sha256"] = archive_hash
    receipt["manifestEntries"] = len(manifest["files"])
    receipt["archiveBytes"] = archive_path.stat().st_size
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sidecar_path.write_text(
        f"{archive_hash}  {archive_path.name}\n", encoding="utf-8", newline="\n"
    )


def add_handoff_member(project: Path, relative: str, payload: bytes) -> None:
    """Add a hidden payload while making every archive hash self-consistent."""

    receipt_path = project / "evidence/handoff.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archive_path = project / receipt["archive"]
    sidecar_path = project / receipt["sidecar"]
    with zipfile.ZipFile(archive_path) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    manifest = json.loads(next(data for info, data in members if info.filename == "_handoff/manifest.json"))
    manifest["files"].append(
        {"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )
    replacement = archive_path.with_suffix(".test-replacement")
    with zipfile.ZipFile(replacement, "w") as target:
        for info, data in members:
            if info.filename == "_handoff/manifest.json":
                data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            target.writestr(info, data)
        target.writestr(relative, payload)
    os.replace(replacement, archive_path)
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    receipt.update({"sha256": archive_hash, "manifestEntries": len(manifest["files"]), "archiveBytes": archive_path.stat().st_size})
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    sidecar_path.write_text(f"{archive_hash}  {archive_path.name}\n", encoding="utf-8", newline="\n")


class EvaluateTests(unittest.TestCase):
    def test_handoff_rejects_excluded_members_even_with_consistent_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            report, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((report["status"], code), ("PASS", 0))
            receipt_path = project / "evidence/handoff.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            original = {
                path: path.read_bytes()
                for path in (receipt_path, project / receipt["archive"], project / receipt["sidecar"])
            }
            for relative in (
                ".runtime/cache.txt", "dist/leftover.txt", "work/output.json",
                "_handoff/extra.json", "nested/node_modules/residual.txt",
                "nested/__pycache__/residual.txt", "evidence/graduation.json",
                "module.pyo", ".DS_Store", ".git/config",
            ):
                with self.subTest(relative=relative):
                    for path, content in original.items():
                        path.write_bytes(content)
                    add_handoff_member(project, relative, b"harmless cached data")
                    passed, details = evaluator._verify_handoff(project, evaluator._validate_contract(project))
                    self.assertFalse(passed, details)
                    self.assertIn("excluded from handoff packaging", details["error"])

    def test_handoff_evaluator_independently_rejects_archive_only_leaks(self) -> None:
        cases = (
            ("evidence/private.txt", b"-----BEGIN PRIVATE KEY-----\nfixture-only\n"),
            ("evidence/utf16.txt", "C:\\Users\\example\\private.txt".encode("utf-16")),
            (".env.local", b"placeholder"),
        )
        for relative, payload in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                project = make_project(Path(raw))
                adapt_standalone_project(project)
                report, code = evaluate(project, run_commands=True, timeout=60)
                self.assertEqual((report["status"], code), ("PASS", 0))
                add_handoff_member(project, relative, payload)
                contract = evaluator._validate_contract(project)

                passed, details = evaluator._verify_handoff(project, contract)

                self.assertFalse(passed)
                self.assertIn("handoff content scan", details["error"])

    def test_contract_rejects_delivery_paths_excluded_by_packager(self) -> None:
        for runtime in ("standalone", "dsh"):
            with tempfile.TemporaryDirectory() as raw:
                project = Path(raw) / runtime
                scaffold(
                    project,
                    product_kind="focused-agent",
                    slug="request-triage-agent",
                    title="请求分诊 Agent",
                    scenario="把本地请求分诊为待办",
                    primary_user="项目负责人",
                    trigger="收到新的请求文件",
                    input_description="包含 request_id 和 content 的 JSON",
                    observable_output="经批准后生成的任务 JSON",
                    dangerous_write="在输出目录创建任务文件",
                    runtime=runtime,
                )
                hidden = project / "work/state.json"
                hidden.parent.mkdir(parents=True)
                hidden.write_text("{}\n", encoding="utf-8")
                contract_path = project / "agent_project.json"
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                contract["architecture"]["state"] = "work/state.json"
                contract["requiredFiles"].append("work/state.json")
                contract_path.write_text(json.dumps(contract), encoding="utf-8")

                with self.subTest(runtime=runtime), self.assertRaisesRegex(
                    EvaluationError, "excluded from handoff"
                ):
                    evaluate(project, run_commands=False, timeout=60)

    def test_self_consistent_handoff_cannot_omit_required_member(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            live, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((live["status"], code), ("PASS", 0))
            remove_handoff_member(project, "README.md")

            contract = evaluator._validate_contract(project)
            handoff_ok, details = evaluator._verify_handoff(project, contract)

            self.assertFalse(handoff_ok)
            self.assertIn("required delivery members", details["error"])

    def test_malformed_domain_fixture_cases_never_escape_as_type_error(self) -> None:
        for cases in (None, 7, "not-a-list", [None]):
            with tempfile.TemporaryDirectory() as raw:
                project = make_project(Path(raw))
                adapt_standalone_project(project)
                fixture_path = project / "fixtures/domain-cases.json"
                fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
                fixtures["cases"] = cases
                fixture_path.write_text(json.dumps(fixtures), encoding="utf-8")

                with self.subTest(cases=cases):
                    report, exit_code = evaluate(project, run_commands=False, timeout=60)
                    self.assertNotEqual(report["status"], "PASS")
                    self.assertNotEqual(exit_code, 0)
                    self.assertIn(
                        "domain-evidence-invalid",
                        report["evidenceSummary"]["domainAdaptation"]["reasonCodes"],
                    )

    def test_cleanliness_scans_packaged_evidence_and_windows_path_variants(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            leak = project / "evidence/leak.txt"
            leak.write_text(
                "API_KEY=fixture-secret-value-1234567890\n"
                "C:/Users/example/private/file.txt\n"
                '{"path":"C:\\\\Users\\\\example\\\\private"}\n'
                "D:/build/example/private/file.txt\n"
                "\\\\server\\share\\private\\file.txt\n"
                "/opt/build/example/private/file.txt\n",
                encoding="utf-8",
            )

            result = evaluator._scan_cleanliness(project)

            self.assertFalse(result["passed"])
            self.assertEqual(result["filesScanned"], len(list(_iter_scan_files(project))))
            self.assertIn("secret-like-value", {item["kind"] for item in result["violations"]})
            self.assertGreaterEqual(
                sum(item["kind"] == "machine-absolute-path" for item in result["violations"]),
                5,
            )

    def test_cleanliness_scans_utf16_and_sensitive_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            (project / "evidence/utf16.txt").write_bytes(
                "C:\\Users\\alice\\private\\file.txt\n".encode("utf-16")
            )
            (project / ".env.local").write_text("placeholder\n", encoding="utf-8")

            result = evaluator._scan_cleanliness(project)

            kinds = {item["kind"] for item in result["violations"]}
            self.assertFalse(result["passed"])
            self.assertIn("machine-absolute-path", kinds)
            self.assertIn("sensitive-filename", kinds)

    def test_cleanliness_does_not_skip_nested_generated_directory_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            expected_paths = set()
            for name in ("work", "dist", ".runtime", "_handoff"):
                leak = project / "src" / name / "leak.txt"
                leak.parent.mkdir(parents=True, exist_ok=True)
                leak.write_text("D:/build/private/file.txt\n", encoding="utf-8")
                expected_paths.add(leak.relative_to(project).as_posix())

            result = evaluator._scan_cleanliness(project)

            observed_paths = {item["path"] for item in result["violations"]}
            self.assertTrue(expected_paths <= observed_paths)

    def test_non_string_critical_file_fails_as_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            contract_path = project / "agent_project.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["development"]["criticalFiles"] = [{}]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")

            with self.assertRaisesRegex(EvaluationError, "criticalFiles"):
                evaluate(project, run_commands=False, timeout=60)

    def test_malformed_handoff_manifest_fails_closed_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            live, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((live["status"], code), ("PASS", 0))
            receipt = json.loads(
                (project / "evidence/handoff.json").read_text(encoding="utf-8")
            )
            archive_path = project / receipt["archive"]
            with zipfile.ZipFile(archive_path) as bundle:
                valid_manifest = json.loads(bundle.read("_handoff/manifest.json"))

            malformed_payloads = (
                [],
                {**valid_manifest, "files": [*valid_manifest["files"], None]},
                {
                    **valid_manifest,
                    "files": [
                        {**valid_manifest["files"][0], "size": "1"},
                        *valid_manifest["files"][1:],
                    ],
                },
            )
            for payload in malformed_payloads:
                with self.subTest(payload=payload):
                    replace_handoff_manifest(project, payload)
                    contract = evaluator._validate_contract(project)
                    handoff_ok, details = evaluator._verify_handoff(project, contract)
                    self.assertFalse(handoff_ok)
                    self.assertIn("error", details)

    def test_large_command_output_is_bounded_and_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            evaluator, "MAX_COMMAND_OUTPUT_BYTES", 4096
        ):
            result = _run_command(
                Path(raw),
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
                "fixture",
                30,
            )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["errorCode"], "COMMAND_OUTPUT_LIMIT")
        self.assertLessEqual(len(result.get("errorOutput", "")), 2600)

    def test_command_timeout_terminates_descendant_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "orphan-marker.txt"
            child = (
                "import pathlib,time; time.sleep(2); "
                f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
            )
            result = _run_command(
                root,
                [sys.executable, "-c", parent],
                "tree-timeout",
                1,
            )
            self.assertEqual(result["errorCode"], "COMMAND_TIMEOUT")
            time.sleep(2.5)
            self.assertFalse(marker.exists())

    def test_no_run_rejects_oversized_contract_before_json_decode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            contract_path = project / "agent_project.json"
            contract_path.write_bytes(contract_path.read_bytes() + b" " * 4096)
            with patch.object(evaluator, "MAX_CONTRACT_JSON_BYTES", 1024):
                with self.assertRaisesRegex(EvaluationError, "exceeds"):
                    evaluate(project, run_commands=False, timeout=60)

    def test_no_run_rejects_linked_evidence_directory_before_consuming_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            project = make_project(parent)
            evidence = project / "evidence"
            outside = parent / "outside-evidence"
            outside.mkdir()
            for child in evidence.iterdir():
                child.unlink()
            evidence.rmdir()
            try:
                make_directory_link(outside, evidence)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(EvaluationError, "link or junction|linked delivery"):
                    evaluate(project, run_commands=False, timeout=60)
            finally:
                if evidence.is_symlink():
                    evidence.unlink()
                else:
                    os.rmdir(evidence)

    def test_static_scan_rejects_linked_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            project = make_project(parent)
            outside = parent / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("outside", encoding="utf-8")
            try:
                make_directory_link(outside, project / "linked-source")
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            with self.assertRaisesRegex(EvaluationError, "linked delivery source"):
                list(_iter_scan_files(project))

    def test_live_dsh_evaluation_rejects_fork_and_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "dsh-project"
            scaffold(
                project,
                product_kind="focused-agent",
                slug="request-triage-agent",
                title="请求分诊 Agent",
                scenario="把本地请求分诊为待办",
                primary_user="项目负责人",
                trigger="收到新的请求文件",
                input_description="包含 request_id 和 content 的 JSON",
                observable_output="经批准后生成的任务 JSON",
                dangerous_write="在输出目录创建任务文件",
                runtime="dsh",
            )
            fake_dsh = root / "dsh"
            fake_dsh.mkdir()
            for observed in (
                {"originIdentity": "github.com/example/fork", "dirty": False, "head": "0" * 40},
                {"originIdentity": "github.com/deepseek-ai/deepseek-harness", "dirty": True, "head": TESTED_DSH_COMMIT},
            ):
                with self.subTest(observed=observed), patch(
                    "evaluate_project._git_provenance", return_value=observed
                ), self.assertRaisesRegex(EvaluationError, "exact clean official"):
                    evaluate(project, run_commands=True, timeout=60, dsh_root=fake_dsh)

    def test_windows_drive_and_device_paths_are_rejected(self) -> None:
        for value in (
            "C:/escape.txt",
            "NUL.txt",
            "folder/COM1.log",
            "folder/COM¹.log",
            "//server/share",
            "bad<name>.txt",
            'bad"name.txt',
            "bad|name.txt",
            "bad?name.txt",
            "bad*name.txt",
            "control\x01.txt",
        ):
            with self.subTest(value=value), self.assertRaises(EvaluationError):
                _safe_relative(value, label="tampered ZIP member")

    def test_workbench_contract_requires_multiple_representative_scenarios(self) -> None:
        blueprint = json.loads(
            (SCRIPTS.parent / "assets/workbench-blueprint.example.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / "workbench"
            scaffold(
                project,
                product_kind="workbench",
                blueprint=blueprint,
            )
            contract_path = project / "agent_project.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["acceptanceScenarios"] = contract["acceptanceScenarios"][:2]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "representative scenario coverage"):
                evaluate(project, run_commands=False, timeout=60)

    def test_live_evaluation_passes_and_no_run_stays_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            starter, starter_code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((starter["status"], starter_code), ("PARTIAL", 2))
            self.assertEqual(starter["project"]["developmentStage"], "starter")
            self.assertEqual(
                next(gate for gate in starter["hardGates"] if gate["id"] == "domain-adaptation")["status"],
                "partial",
            )

            adapt_standalone_project(project)
            live, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((live["status"], code), ("PASS", 0))
            self.assertEqual(live["score"]["earned"], 20)
            self.assertTrue(all(gate["status"] == "pass" for gate in live["hardGates"]))
            identity_gate = next(
                gate for gate in live["hardGates"] if gate["id"] == "non-xiaoshe-identity"
            )
            self.assertEqual(identity_gate["title"], "非小蛇品牌身份声明")
            (project / "evidence/graduation.json").write_text(
                json.dumps(live, ensure_ascii=False), encoding="utf-8"
            )
            repeated, repeated_code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((repeated["status"], repeated_code), ("PASS", 0))
            self.assertEqual(repeated["resultDigest"], live["resultDigest"])
            no_run, no_run_code = evaluate(project, run_commands=False, timeout=60)
            self.assertEqual((no_run["status"], no_run_code), ("PARTIAL", 2))
            self.assertIn("partial", {gate["status"] for gate in no_run["hardGates"]})

    def test_modified_builder_acceptance_runner_never_executes_or_graduates(self) -> None:
        """A project cannot replace the release-owned runner with a forged PASS script."""

        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            marker = project / "forged-runner-executed.txt"
            runner = project / "tools/acceptance.py"
            runner.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "print('{\"status\":\"PASS\"}')\n",
                encoding="utf-8",
            )

            report, code = evaluate(project, run_commands=True, timeout=60)

            self.assertEqual((report["status"], code), ("FAIL", 3))
            self.assertFalse(marker.exists())
            self.assertFalse(report["evidenceSummary"]["immutableHarnessVerified"])
            self.assertEqual(report["immutableHarness"]["mismatches"], ["tools/acceptance.py"])
            self.assertTrue(
                all(
                    item.get("status") == "not-run"
                    and item.get("reason") == "immutable-harness-mismatch"
                    for item in report["commands"]
                )
            )

    def test_machine_absolute_path_fails_clean_handoff_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            readme = project / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "\n/Users/example/private/file\n", encoding="utf-8")
            report, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((report["status"], code), ("FAIL", 3))
            self.assertFalse(report["staticScan"]["passed"])
            self.assertEqual(report["staticScan"]["violations"][0]["kind"], "machine-absolute-path")

    def test_rebuildable_runtime_state_is_not_scanned_or_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            runtime_file = project / ".runtime/dsh-home/profile.json"
            runtime_file.parent.mkdir(parents=True)
            runtime_file.write_text(
                '{"path":"C:\\\\Users\\\\private\\\\project","token":"sk-123456789012345678901234567890"}\n',
                encoding="utf-8",
            )
            report, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((report["status"], code), ("PASS", 0))
            self.assertTrue(report["staticScan"]["passed"])
            handoff = json.loads((project / "evidence/handoff.json").read_text(encoding="utf-8"))
            archive = project / handoff["archive"]
            with zipfile.ZipFile(archive) as bundle:
                self.assertFalse(any(name.startswith(".runtime/") for name in bundle.namelist()))

    def test_tampered_archive_is_not_accepted_from_stale_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            live, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual(code, 0)
            handoff = json.loads((project / "evidence/handoff.json").read_text(encoding="utf-8"))
            archive = project / handoff["archive"]
            archive.write_bytes(archive.read_bytes() + b"tamper")
            report, code = evaluate(project, run_commands=False, timeout=60)
            self.assertEqual((report["status"], code), ("FAIL", 3))
            self.assertFalse(report["evidenceSummary"]["handoffVerified"])

    def test_arbitrary_executable_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            contract_path = project / "agent_project.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["commands"]["test"] = ["bash", "-c", "echo unsafe"]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaises(EvaluationError):
                evaluate(project, run_commands=False, timeout=60)

    def test_stage_flip_fixture_label_and_comments_cannot_graduate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            contract_path = project / "agent_project.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["development"]["stage"] = "domain-adapted"
            contract_path.write_text(
                json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            fixture_path = project / "fixtures/domain-cases.json"
            fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixtures["stage"] = "domain-adapted"
            for case in fixtures["cases"]:
                if case.get("kind") == "positive":
                    case["expected"]["provider"] = "reference-deterministic"
            fixture_path.write_text(
                json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            for relative in ("agent_workbench/domain.py", "tests/test_workbench.py"):
                path = project / relative
                path.write_text(
                    path.read_text(encoding="utf-8") + "\n# Cosmetic-only change.\n",
                    encoding="utf-8",
                )
            report, code = evaluate(project, run_commands=True, timeout=60)
            self.assertEqual((report["status"], code), ("PARTIAL", 2))
            gate = next(
                item for item in report["hardGates"] if item["id"] == "domain-adaptation"
            )
            self.assertEqual(gate["status"], "partial")
            self.assertNotIn("starter-files-unchanged", gate["reasonCodes"])
            self.assertIn("starter-domain-behavior-unchanged", gate["reasonCodes"])

    def test_domain_fixture_must_keep_a_boundary_case(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw))
            adapt_standalone_project(project)
            fixtures_path = project / "fixtures/domain-cases.json"
            fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
            fixtures["cases"] = [case for case in fixtures["cases"] if case["kind"] == "positive"]
            fixtures_path.write_text(
                json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report, code = evaluate(project, run_commands=True, timeout=60)
            self.assertNotEqual((report["status"], code), ("PASS", 0))
            gate = next(
                item for item in report["hardGates"] if item["id"] == "domain-adaptation"
            )
            self.assertIn(gate["status"], {"partial", "fail"})


if __name__ == "__main__":
    unittest.main()
