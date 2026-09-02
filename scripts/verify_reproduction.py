#!/usr/bin/env python3
"""Clean-room reproduction of the Builder Skill in a Unicode/space path."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import zipfile
import re

from dsh_doctor import diagnose
from evaluate_project import (
    EvaluationError,
    MAX_SCAN_BYTES,
    PROTECTED_HARNESS_FILES,
    _content_violations,
    _read_bounded_bytes,
    _text_views,
)
from scaffold_project import BUILDER_RELEASE_TAG, BUILDER_VERSION


SKILL_ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_UNCOMPRESSED = 100 * 1024 * 1024
WINDOWS_DEVICE_RE = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
REPRODUCTION_IGNORED_NAMES = {
    ".git", "node_modules", "__pycache__",
    "dsh-doctor-report.json", "reproduction-focused-report.json", "reproduction-workbench-report.json",
}


class ReproductionError(RuntimeError):
    pass


def _builder_identity(root: Path) -> dict[str, Any]:
    """Bind a reproduction receipt to all shipped Skill bytes and safety runners."""

    files: dict[str, str] = {}
    total_bytes = 0
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in REPRODUCTION_IGNORED_NAMES)
        for directory in directories:
            path = Path(current) / directory
            if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                raise ReproductionError("Builder source identity cannot traverse a linked directory")
        for name in sorted(names):
            if name in REPRODUCTION_IGNORED_NAMES or name.endswith((".pyc", ".pyo")):
                continue
            path = Path(current) / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > 10 * 1024 * 1024:
                raise ReproductionError("Builder source identity found an unsafe or oversized file")
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ReproductionError("Builder source changed during identity inspection")
            total_bytes += len(raw)
            if total_bytes > 100 * 1024 * 1024 or len(files) >= 5000:
                raise ReproductionError("Builder source identity exceeds its resource limit")
            files[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
    source_digest = hashlib.sha256()
    for relative, digest in sorted(files.items()):
        source_digest.update(f"{relative}\0{digest}\n".encode("utf-8"))
    protected_digest = hashlib.sha256()
    for runtime, paths in sorted(PROTECTED_HARNESS_FILES.items()):
        template = "dsh-product-template" if runtime == "dsh" else "starter-template"
        for relative in paths:
            key = f"assets/{template}/{relative}"
            if key not in files:
                raise ReproductionError(f"Builder protected harness is missing: {key}")
            protected_digest.update(f"{key}\0{files[key]}\n".encode("utf-8"))
    return {
        "version": BUILDER_VERSION,
        "releaseTag": BUILDER_RELEASE_TAG,
        "sourceTreeSha256": source_digest.hexdigest(),
        "protectedHarnessSha256": protected_digest.hexdigest(),
        "sourceFilesChecked": len(files),
    }


def _require_verified_dsh(root: Path) -> dict[str, Any]:
    """Require the exact clean official rc8 source before any reproduction run."""

    report, code = diagnose(root, live=True)
    if code != 0 or report.get("status") != "PASS":
        git = report.get("git", {}) if isinstance(report, dict) else {}
        raise ReproductionError(
            "external DSH failed strict provenance verification; require the clean official rc8 tag/commit "
            f"(observed origin={git.get('originIdentity')!r}, head={git.get('head')!r}, dirty={git.get('dirty')!r})"
        )
    dependency = report["externalDependency"]
    git = report["git"]
    return {
        "status": report["status"],
        "testedTag": dependency["testedTag"],
        "testedCommit": dependency["testedCommit"],
        "sourceDigest": report["sourceDigest"],
        "gitStatusSha256": git["statusSha256"],
        "originIdentity": git["originIdentity"],
        "head": git["head"],
        "dirty": git["dirty"],
        "topLevelMatches": git["topLevelMatches"],
        "trackedTreeVerified": git["trackedTreeVerified"],
        "trackedTreeSha256": git["trackedTreeSha256"],
        "trackedFilesChecked": git["trackedFilesChecked"],
        "unsafeIndexFlags": git["unsafeIndexFlags"],
    }


def _redact_child_output(value: bytes, cwd: Path) -> str:
    text = value[-1600:].decode("utf-8", errors="replace")
    for path in (cwd, SKILL_ROOT):
        text = text.replace(str(path), "<WORKDIR>")
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "<REDACTED_TOKEN>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "<REDACTED_TOKEN>", text)
    return text.strip()


def _run(argv: list[str], cwd: Path, *, expected: int = 0) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        timeout=180,
        check=False,
    )
    if completed.returncode != expected:
        detail = _redact_child_output(completed.stderr + completed.stdout, cwd)
        try:
            failure_receipt = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failure_receipt = None
        if isinstance(failure_receipt, dict):
            failed_commands = [
                {
                    "label": item.get("label"),
                    "status": item.get("status"),
                    "errorCode": item.get("errorCode"),
                    "errorOutput": item.get("errorOutput"),
                }
                for item in failure_receipt.get("commands", [])
                if isinstance(item, dict) and item.get("status") != "pass"
            ]
            receipt_error = failure_receipt.get("error")
            if failed_commands or receipt_error:
                detail = json.dumps(
                    {"commands": failed_commands, "error": receipt_error},
                    ensure_ascii=False,
                )
                detail = detail.replace(str(cwd), "<WORKDIR>")
                detail = re.sub(
                    r"(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}",
                    "<REDACTED_TOKEN>",
                    detail,
                )
        raise ReproductionError(
            f"command failed with exit {completed.returncode}: "
            f"{Path(argv[1]).name if len(argv) > 1 else 'python'}"
            f"; output={detail or '<empty>'}"
        )
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = _redact_child_output(completed.stderr + completed.stdout, cwd)
        raise ReproductionError(
            f"command did not return its machine-readable JSON receipt; output={detail or '<empty>'}"
        ) from exc
    return {
        "exitCode": completed.returncode,
        "schema": payload.get("schema"),
        "status": payload.get("status"),
        "stderrEmpty": not completed.stderr,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _adapt_standalone_domain(project: Path, contract: dict[str, Any]) -> None:
    domain_path = project / "agent_workbench/domain.py"
    domain = domain_path.read_text(encoding="utf-8")
    domain = domain.replace(
        '("发票", "预算", "invoice", "budget")',
        '("发票", "预算", "报销", "invoice", "budget", "expense")',
    ).replace('category = "finance"', 'category = "expense-review"')
    domain_path.write_text(domain, encoding="utf-8")

    fixture_path = project / contract["development"]["domainEvidence"]["fixtures"]
    scenario_id = contract["acceptanceScenarios"][0]["id"]
    capability_id = contract["capabilities"][0]["id"]
    _write_json(
        fixture_path,
        {
            "schema": "agent-workbench-domain-fixtures/v1",
            "stage": "domain-adapted",
            "cases": [
                {
                    "id": "expense-urgent",
                    "kind": "positive",
                    "scenarioId": scenario_id,
                    "capabilityId": capability_id,
                    "input": {"request_id": "expense-001", "content": "今天审核这笔报销"},
                    "expected": {"category": "expense-review", "priority": "high"},
                },
                {
                    "id": "general-normal",
                    "kind": "positive",
                    "scenarioId": scenario_id,
                    "capabilityId": capability_id,
                    "input": {"request_id": "general-001", "content": "整理下周资料"},
                    "expected": {"category": "general", "priority": "normal"},
                },
                {
                    "id": "empty-content",
                    "kind": "boundary",
                    "scenarioId": scenario_id,
                    "capabilityId": capability_id,
                    "input": {"request_id": "invalid-001", "content": ""},
                    "expectedError": "INVALID_REQUEST",
                },
            ],
        },
    )
    test_path = project / contract["development"]["domainEvidence"]["test"]
    test_path.write_text(
        test_path.read_text(encoding="utf-8")
        + "\n# Domain adaptation receipt: expense-review behavior is fixture-backed.\n",
        encoding="utf-8",
    )


def _adapt_dsh_domain(project: Path, contract: dict[str, Any]) -> None:
    capability_ids = [item["id"] for item in contract["capabilities"]]
    action_map: dict[str, str] = {}
    for index, identifier in enumerate(capability_ids):
        action_map[identifier] = (
            "route-request"
            if index == 0
            else ("summarize-inbox" if index == 1 else "audit-evidence")
        )
    source = f"""import {{ CAPABILITIES, PROJECT, SCENARIOS }} from './project.mjs'
import {{ AgentProjectError, createPlan, validateTask }} from './domain.mjs'

const DOMAIN_ACTIONS = Object.freeze({json.dumps(action_map, ensure_ascii=False)})

function capabilityById(identifier) {{
  return CAPABILITIES.find(item => item.id === identifier)
}}

export function capabilityToolToken(identifier) {{
  return identifier.replaceAll('-', '_')
}}

export function listCapabilityCatalog() {{
  return {{
    schema: 'agent-workbench-capability-catalog/v4',
    productKind: PROJECT.productKind,
    purpose: PROJECT.purpose,
    capabilities: CAPABILITIES.map(item => ({{ ...item }})),
    scenarios: SCENARIOS.map(item => ({{ id: item.id, title: item.title, primary: item.primary, capabilityIds: [...item.capabilityIds] }})),
  }}
}}

function classifyDomain(content) {{
  const normalized = content.toLocaleLowerCase('en-US')
  if (/发票|报销|预算|invoice|expense|budget/u.test(normalized)) return 'finance'
  if (/会议|日程|meeting|calendar/u.test(normalized)) return 'schedule'
  if (/凭证|证据|receipt|evidence/u.test(normalized)) return 'evidence'
  return 'general'
}}

export function executeCapability(capabilityId, input) {{
  const task = validateTask(input)
  const capability = capabilityById(capabilityId)
  if (capability === undefined) throw new AgentProjectError('UNKNOWN_CAPABILITY', `capability is not declared: ${{capabilityId}}`, 'Choose a capability from the product catalog.')
  const scenario = SCENARIOS.find(item => item.id === task.scenario_id)
  if (!scenario.capabilityIds.includes(capabilityId)) throw new AgentProjectError('CAPABILITY_NOT_IN_SCENARIO', `${{capabilityId}} is not part of scenario ${{scenario.id}}`, 'Use one of the capabilityIds declared by the selected scenario.')
  const plan = createPlan(task)
  const domainCategory = classifyDomain(task.content)
  const domainAction = DOMAIN_ACTIONS[capabilityId]
  const decision = domainAction === 'audit-evidence'
    ? (domainCategory === 'evidence' ? 'verified' : 'evidence-missing')
    : (domainAction === 'route-request' ? 'review-required' : 'read-only-summary')
  return Object.freeze({{
    schema: 'agent-workbench-capability-result/v4', status: 'planned', taskId: task.task_id,
    scenarioId: scenario.id, capabilityId, capabilityTitle: capability.title, risk: capability.risk,
    domainCategory, domainAction, decision, summary: plan.summary,
    observableOutput: plan.observable_output, sideEffectWritten: false, outcomeHash: plan.outcomeHash,
  }})
}}
"""
    (project / "src/capabilities.mjs").write_text(source, encoding="utf-8")

    cases: list[dict[str, Any]] = []
    category_inputs = [
        ("finance", "今天需要审核一笔报销", "review-required"),
        ("schedule", "汇总今天的会议和日程", "read-only-summary"),
        ("evidence", "核对任务完成凭证与 evidence", "verified"),
    ]
    positive_index = 0
    for scenario in contract["acceptanceScenarios"]:
        for capability_id in scenario["capabilityIds"]:
            category, content, default_decision = category_inputs[min(positive_index, 2)]
            action = action_map[capability_id]
            decision = (
                "verified"
                if action == "audit-evidence"
                else ("review-required" if action == "route-request" else "read-only-summary")
            )
            cases.append(
                {
                    "id": f"domain-{scenario['id']}-{capability_id}",
                    "kind": "positive",
                    "scenarioId": scenario["id"],
                    "capabilityId": capability_id,
                    "input": {
                        "task_id": f"domain-{positive_index + 1}",
                        "scenario_id": scenario["id"],
                        "content": content,
                    },
                    "expected": {
                        "domainCategory": category,
                        "domainAction": action,
                        "decision": decision,
                    },
                }
            )
            positive_index += 1
    primary = next(item for item in contract["acceptanceScenarios"] if item["primary"])
    cases.append(
        {
            "id": "domain-empty-content",
            "kind": "boundary",
            "scenarioId": primary["id"],
            "capabilityId": primary["capabilityIds"][0],
            "input": {
                "task_id": "domain-invalid",
                "scenario_id": primary["id"],
                "content": "",
            },
            "expectedError": "INVALID_TASK",
        }
    )
    _write_json(
        project / contract["development"]["domainEvidence"]["fixtures"],
        {
            "schema": "agent-workbench-domain-fixtures/v1",
            "stage": "domain-adapted",
            "cases": cases,
        },
    )
    test_path = project / contract["development"]["domainEvidence"]["test"]
    test_path.write_text(
        test_path.read_text(encoding="utf-8")
        + "\n// Domain adaptation receipt: category, action, and decision are fixture-backed.\n",
        encoding="utf-8",
    )


def _apply_reproduction_domain(project: Path, *, runtime: str) -> None:
    contract_path = project / "agent_project.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if runtime == "dsh":
        _adapt_dsh_domain(project, contract)
    else:
        _adapt_standalone_domain(project, contract)
    contract["development"]["stage"] = "domain-adapted"
    _write_json(contract_path, contract)


def _safe_member(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename)
    mode = stat.S_IFMT(info.external_attr >> 16)
    return (
        bool(info.filename)
        and not path.is_absolute()
        and "\\" not in info.filename
        and "\x00" not in info.filename
        and ":" not in info.filename
        and path.as_posix() == info.filename
        and all(part not in {"", ".", ".."} for part in path.parts)
        and all(part == part.rstrip(" .") for part in path.parts)
        and not any(WINDOWS_DEVICE_RE.match(part) for part in path.parts)
        and not (info.flag_bits & 0x1)
        and mode != stat.S_IFLNK
    )


def _windows_path_key(value: str) -> str:
    return "/".join(part.casefold() for part in PurePosixPath(value).parts)


def _extract_safely(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if (
            not infos
            or len(infos) > MAX_ARCHIVE_MEMBERS
            or sum(item.file_size for item in infos) > MAX_ARCHIVE_UNCOMPRESSED
            or len(infos) != len(set(names))
            or len({_windows_path_key(name) for name in names}) != len(names)
        ):
            raise ReproductionError("archive is empty or has duplicate members")
        for info in infos:
            if not _safe_member(info):
                raise ReproductionError("archive contains an unsafe member")
        archive.extractall(destination)


def _assert_no_path_leak(project: Path, forbidden: list[str]) -> int:
    """Check source and extracted receipts using the evaluator's text decoders."""

    checked = 0
    for path in sorted(project.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.suffix in {".zip", ".pyc"}:
            continue
        try:
            raw = _read_bounded_bytes(path, "reproduction delivery source", MAX_SCAN_BYTES)
            views = _text_views(raw)
        except (OSError, EvaluationError) as exc:
            raise ReproductionError(f"serialized file is unreadable or unsafe: {path.name}") from exc
        checked += 1
        relative = path.relative_to(project).as_posix()
        if _content_violations(relative, raw) or any(
            value and value in text for text in views for value in forbidden
        ):
            raise ReproductionError(f"serialized path leak in {path.name}")
    return checked


def _json_difference_paths(left: Any, right: Any, *, limit: int = 24) -> tuple[str, ...]:
    """Return bounded structural paths for deterministic reproduction diagnostics."""

    differences: list[str] = []

    def visit(before: Any, after: Any, path: str) -> None:
        if len(differences) >= limit:
            return
        if type(before) is not type(after):
            differences.append(path or "<root>")
            return
        if isinstance(before, dict):
            for key in sorted(set(before) | set(after)):
                child = f"{path}.{key}" if path else str(key)
                if key not in before or key not in after:
                    differences.append(child)
                else:
                    visit(before[key], after[key], child)
                if len(differences) >= limit:
                    return
            return
        if isinstance(before, list):
            if len(before) != len(after):
                differences.append(f"{path}.length")
            for index, (before_item, after_item) in enumerate(zip(before, after)):
                visit(before_item, after_item, f"{path}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if before != after:
            differences.append(path or "<root>")

    visit(left, right, "")
    return tuple(differences)


def reproduce(
    *,
    runtime: str = "dsh",
    dsh_root: Path | None = None,
    product_kind: str = "workbench",
) -> dict[str, Any]:
    if runtime not in {"dsh", "standalone"}:
        raise ReproductionError("runtime must be dsh or standalone")
    if product_kind not in {"focused-agent", "workbench"}:
        raise ReproductionError("product_kind must be focused-agent or workbench")
    if runtime == "standalone" and product_kind != "focused-agent":
        raise ReproductionError("standalone reproduction supports focused-agent only")
    if runtime == "dsh" and dsh_root is None:
        raise ReproductionError(
            "external DSH root is required for the default reproduction; install DSH from https://github.com/deepseek-ai/deepseek-harness and pass --dsh-root"
        )
    resolved_dsh_root = dsh_root.expanduser().resolve() if dsh_root is not None else None
    if resolved_dsh_root is not None and not resolved_dsh_root.is_dir():
        raise ReproductionError("external DSH root does not exist")
    dsh_provenance = (
        _require_verified_dsh(resolved_dsh_root)
        if runtime == "dsh" and resolved_dsh_root is not None
        else None
    )
    builder_identity = _builder_identity(SKILL_ROOT)
    with tempfile.TemporaryDirectory(prefix="agent-builder-reproduction-") as raw_temp:
        temporary = Path(raw_temp)
        copied_skill = temporary / "复制 Skill 空格路径" / "agent-workbench-builder"
        copied_skill.parent.mkdir(parents=True)
        shutil.copytree(
            SKILL_ROOT,
            copied_skill,
            ignore=shutil.ignore_patterns(*sorted(REPRODUCTION_IGNORED_NAMES), "*.pyc", "*.pyo"),
        )
        if _builder_identity(copied_skill) != builder_identity:
            raise ReproductionError("copied Skill does not match the audited Builder source tree")
        project_name = (
            "本地请求分诊Agent"
            if product_kind == "focused-agent"
            else "本地事务工作台"
        )
        project = temporary / "不同项目 空格路径" / project_name
        project.parent.mkdir(parents=True)

        if product_kind == "focused-agent":
            product_arguments = [
                "--product-kind",
                "focused-agent",
                "--slug",
                "local-request-triage",
                "--title",
                "本地请求分诊 Agent",
                "--scenario",
                "把本地收件箱中的文字请求分诊为优先级任务",
                "--primary-user",
                "个人项目负责人",
                "--trigger",
                "收件箱出现新的 JSON 请求",
                "--input-description",
                "包含 task_id、scenario_id 与 content 的本地 JSON",
                "--observable-output",
                "经人工批准后生成的分类任务 JSON",
                "--dangerous-write",
                "在输出目录创建任务文件",
            ]
        else:
            product_arguments = [
                "--product-kind",
                "workbench",
                "--blueprint",
                str(copied_skill / "assets/workbench-blueprint.example.json"),
            ]
        scaffold_result = _run(
            [
                sys.executable,
                str(copied_skill / "scripts/scaffold_project.py"),
                "--destination",
                str(project),
                *product_arguments,
                "--runtime",
                runtime,
            ],
            copied_skill,
        )
        starter_report_path = temporary / "starter-evaluation.json"
        starter_evaluation_argv = [
            sys.executable,
            str(copied_skill / "scripts/evaluate_project.py"),
            "--project",
            str(project),
            "--output",
            str(starter_report_path),
            "--pretty",
        ]
        if resolved_dsh_root is not None:
            starter_evaluation_argv.extend(["--dsh-root", str(resolved_dsh_root)])
        starter_evaluation = _run(
            starter_evaluation_argv,
            copied_skill,
            expected=2,
        )
        starter_report = json.loads(starter_report_path.read_text(encoding="utf-8"))
        if starter_report.get("status") != "PARTIAL":
            raise ReproductionError("freshly scaffolded starter was not held at PARTIAL")

        _apply_reproduction_domain(project, runtime=runtime)
        first_report_path = project / "evidence/graduation.json"
        first_evaluation_argv = [
            sys.executable,
            str(copied_skill / "scripts/evaluate_project.py"),
            "--project",
            str(project),
            "--output",
            str(first_report_path),
            "--pretty",
        ]
        if resolved_dsh_root is not None:
            first_evaluation_argv.extend(["--dsh-root", str(resolved_dsh_root)])
        first_evaluation = _run(first_evaluation_argv, copied_skill)
        first_report = json.loads(first_report_path.read_text(encoding="utf-8"))
        if first_report.get("status") != "PASS":
            raise ReproductionError("domain-adapted project did not graduate")

        handoff = json.loads((project / "evidence/handoff.json").read_text(encoding="utf-8"))
        archive_path = project / handoff["archive"]
        extracted = temporary / "交接包复验 空格路径" / "project"
        extracted.parent.mkdir(parents=True)
        _extract_safely(archive_path, extracted)
        second_report_path = extracted / "evidence/graduation.json"
        second_evaluation_argv = [
            sys.executable,
            str(copied_skill / "scripts/evaluate_project.py"),
            "--project",
            str(extracted),
            "--output",
            str(second_report_path),
            "--pretty",
        ]
        if resolved_dsh_root is not None:
            second_evaluation_argv.extend(["--dsh-root", str(resolved_dsh_root)])
        second_evaluation = _run(
            second_evaluation_argv,
            copied_skill,
        )
        second_report = json.loads(second_report_path.read_text(encoding="utf-8"))
        if second_report.get("status") != "PASS":
            raise ReproductionError("extracted handoff project did not graduate")
        if first_report.get("resultDigest") != second_report.get("resultDigest"):
            digest_fields = (
                "status",
                "hardGates",
                "dimensions",
                "score",
                "staticScan",
                "evidenceSummary",
            )
            first_digest_payload = {key: first_report.get(key) for key in digest_fields}
            second_digest_payload = {key: second_report.get(key) for key in digest_fields}
            changed = _json_difference_paths(first_digest_payload, second_digest_payload)
            detail = ", ".join(changed) if changed else "digest payload bytes"
            raise ReproductionError(
                f"clean-room result digest is not reproducible; differing fields: {detail}"
            )

        forbidden_paths = [
            str(SKILL_ROOT),
            str(copied_skill),
            str(project),
            str(extracted),
            str(temporary),
            str(resolved_dsh_root) if resolved_dsh_root is not None else "",
        ]
        checked_files = _assert_no_path_leak(project, forbidden_paths)
        extracted_checked_files = _assert_no_path_leak(extracted, forbidden_paths)
        if _builder_identity(SKILL_ROOT) != builder_identity or _builder_identity(copied_skill) != builder_identity:
            raise ReproductionError("Builder source changed during reproduction")
        return {
            "schema": "agent-workbench-reproduction/v4",
            "status": "PASS",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "builder": builder_identity,
            "productKind": product_kind,
            "projectSlug": first_report["project"]["slug"],
            "capabilityCount": first_report["project"]["capabilityCount"],
            "representativeScenarioCount": first_report["project"][
                "representativeScenarioCount"
            ],
            "runtime": runtime,
            "externalDshRuntimeVerified": (
                first_report["evidenceSummary"].get("externalDshRuntimeVerified")
                if runtime == "dsh"
                else None
            ),
            "externalDshGitProvenanceVerified": (
                first_report["evidenceSummary"].get("externalDshGitProvenanceVerified")
                if runtime == "dsh"
                else None
            ),
            "externalDshProvenance": dsh_provenance,
            "externalDshBundled": False if runtime == "dsh" else None,
            "unicodeAndSpacePath": True,
            "starterEvaluationStatus": starter_report["status"],
            "domainAdaptationApplied": True,
            "domainAdaptedProjectGraduated": True,
            "handoffExtractedAndGraduated": True,
            "resultDigestStable": True,
            "resultDigest": first_report["resultDigest"],
            "archiveSha256": first_report["evidenceSummary"]["archiveSha256"],
            "serializedFilesChecked": checked_files,
            "extractedSerializedFilesChecked": extracted_checked_files,
            "commands": {
                "scaffold": scaffold_result,
                "starterEvaluation": starter_evaluation,
                "firstEvaluation": first_evaluation,
                "secondEvaluation": second_evaluation,
            },
            "limitations": [
                "This is an isolated automated reproduction, not an independent human usability test.",
                "The bundled reproduction domain proves the lifecycle mechanics, not every possible domain.",
                *( ["DeepSeek Harness remained an external dependency and was not copied into either project or handoff."] if runtime == "dsh" else [] ),
            ],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime", choices=("dsh", "standalone"), default="dsh")
    parser.add_argument(
        "--product-kind",
        choices=("focused-agent", "workbench"),
        required=True,
        help="reproduce the same product type the user selected",
    )
    parser.add_argument("--dsh-root", type=Path, help="existing external DSH checkout")
    return parser


def _atomic_json(path: Path, payload: dict[str, Any], *, pretty: bool) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2 if pretty else None, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = reproduce(
            runtime=args.runtime,
            dsh_root=args.dsh_root,
            product_kind=args.product_kind,
        )
        exit_code = 0
    except (OSError, ReproductionError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        report = {
            "schema": "agent-workbench-reproduction/v4",
            "status": "FAIL",
            "error": {"code": "REPRODUCTION_FAILED", "message": str(exc)},
        }
        exit_code = 3
    if args.output:
        _atomic_json(args.output, report, pretty=args.pretty)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
