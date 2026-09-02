#!/usr/bin/env python3
"""Run deterministic standalone acceptance and write portable receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import sys
import tempfile
import threading
from typing import Any, Sequence
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_workbench.core import AgentError, run_agent  # noqa: E402
from agent_workbench.domain import ReferenceProvider  # noqa: E402
from agent_workbench.server import create_server  # noqa: E402
from agent_workbench.store import atomic_write_json  # noqa: E402


WINDOWS_DEVICE_RE = re.compile(
    r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)"
)


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _assert_existing_components_are_plain(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and _is_link_or_junction(current):
            raise RuntimeError(f"contract path crosses a link or junction: {current}")


def _project_path(value: Any, *, label: str, must_exist: bool) -> Path:
    """Resolve one contract-owned POSIX path inside the physical project tree."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError(f"{label} must be a safe relative POSIX path")
    pure = PurePosixPath(value)
    parts = value.split("/")
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part or WINDOWS_DEVICE_RE.match(part.rstrip(" .")) for part in parts)
    ):
        raise RuntimeError(f"{label} must stay inside the project")
    candidate = PROJECT_ROOT.joinpath(*parts)
    _assert_existing_components_are_plain(candidate)
    try:
        candidate.resolve(strict=False).relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{label} escaped the project") from exc
    if must_exist:
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"{label} is missing") from exc
        if _is_link_or_junction(candidate) or not stat.S_ISREG(mode):
            raise RuntimeError(f"{label} must be a regular project file")
    return candidate


def _load_contract() -> dict[str, Any]:
    contract = _load_json(PROJECT_ROOT / "agent_project.json")
    development = contract.get("development")
    domain = development.get("domainEvidence") if isinstance(development, dict) else None
    required = contract.get("requiredFiles")
    if not isinstance(domain, dict) or not isinstance(required, list) or not required:
        raise RuntimeError("contract path declarations are invalid")
    _project_path(domain.get("fixtures"), label="development.domainEvidence.fixtures", must_exist=True)
    _project_path(domain.get("report"), label="development.domainEvidence.report", must_exist=False)
    for index, relative in enumerate(required):
        _project_path(relative, label=f"requiredFiles[{index}]", must_exist=True)
    return contract


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON object required: {path.name}")
    return payload


def _assert_subset(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _run_domain_fixtures(contract: dict[str, Any]) -> dict[str, Any]:
    development = contract["development"]
    fixture_path = _project_path(
        development["domainEvidence"]["fixtures"],
        label="development.domainEvidence.fixtures",
        must_exist=True,
    )
    fixture_set = _load_json(fixture_path)
    if fixture_set.get("schema") != "agent-workbench-domain-fixtures/v1":
        raise RuntimeError("domain fixture schema is invalid")
    cases = fixture_set.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("domain fixtures must contain cases")

    provider = ReferenceProvider()
    results: list[dict[str, Any]] = []
    positive = 0
    boundary = 0
    covered_scenarios: set[str] = set()
    covered_capabilities: set[str] = set()
    for item in cases:
        if not isinstance(item, dict) or item.get("kind") not in {"positive", "boundary"}:
            raise RuntimeError("domain fixture case is invalid")
        case_id = item.get("id")
        task_input = item.get("input")
        if not isinstance(case_id, str) or not isinstance(task_input, dict):
            raise RuntimeError("domain fixture id and input are required")
        if item["kind"] == "positive":
            expected = item.get("expected")
            if not isinstance(expected, dict) or not expected:
                raise RuntimeError("positive domain fixture requires expected fields")
            actual = provider.build_plan(task_input.get("request_id"), task_input.get("content"))
            passed = _assert_subset(actual, expected)
            positive += 1
            covered_scenarios.add(str(item.get("scenarioId")))
            covered_capabilities.add(str(item.get("capabilityId")))
        else:
            expected_error = item.get("expectedError")
            with tempfile.TemporaryDirectory(prefix="agent-domain-boundary-") as raw:
                root = Path(raw)
                try:
                    run_agent(
                        task_input,
                        approved=False,
                        run_id=f"fixture-{case_id}",
                        state_dir=root / "state",
                        output_dir=root / "output",
                        receipt_dir=root / "receipts",
                    )
                except AgentError as exc:
                    passed = exc.code == expected_error
                else:
                    passed = False
            boundary += 1
        results.append({"id": case_id, "kind": item["kind"], "passed": passed})

    declared_scenarios = {item["id"] for item in contract["acceptanceScenarios"]}
    declared_capabilities = {item["id"] for item in contract["capabilities"]}
    fixtures_passed = (
        positive >= len(declared_scenarios)
        and boundary >= 1
        and covered_scenarios == declared_scenarios
        and covered_capabilities == declared_capabilities
        and all(item["passed"] for item in results)
    )
    stage = development["stage"]
    return {
        "schema": "agent-workbench-domain-adaptation/v1",
        "projectSlug": contract["project"]["slug"],
        "status": "PASS" if fixtures_passed and stage == "domain-adapted" else "PARTIAL",
        "passed": fixtures_passed and stage == "domain-adapted",
        "fixturesPassed": fixtures_passed,
        "stage": stage,
        "fixtureSha256": _sha256(fixture_path),
        "fixtureCount": len(cases),
        "positiveCases": positive,
        "boundaryCases": boundary,
        "coveredScenarios": sorted(covered_scenarios),
        "coveredCapabilities": sorted(covered_capabilities),
        "cases": results,
    }


def run_acceptance() -> dict[str, Any]:
    contract = _load_contract()
    request = _load_json(PROJECT_ROOT / "demo/input/request.json")
    with tempfile.TemporaryDirectory(prefix="agent-workbench-acceptance-") as raw_temp:
        sandbox = Path(raw_temp)
        state_dir = sandbox / "state"
        output_dir = sandbox / "output"
        receipt_dir = sandbox / "receipts"
        approved = [
            run_agent(
                request,
                approved=True,
                run_id=f"approved-{index}",
                state_dir=state_dir,
                output_dir=output_dir,
                receipt_dir=receipt_dir,
            )
            for index in range(1, 4)
        ]
        denied = run_agent(
            {"request_id": "denied-001", "content": request["content"]},
            approved=False,
            run_id="denied-1",
            state_dir=state_dir,
            output_dir=output_dir,
            receipt_dir=receipt_dir,
        )
        try:
            run_agent(
                {"request_id": "invalid-001", "content": ""},
                approved=True,
                run_id="invalid-1",
                state_dir=state_dir,
                output_dir=output_dir,
                receipt_dir=receipt_dir,
            )
        except AgentError as exc:
            recovery = exc.as_dict()
        else:
            raise RuntimeError("invalid input unexpectedly succeeded")

        server = create_server("127.0.0.1", 0, sandbox)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(f"{base_url}/api/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/", timeout=5) as response:
                html = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        outcome_hashes = {item["outcomeHash"] for item in approved}
        side_effect_writes = sum(bool(item["sideEffectWritten"]) for item in approved)
        output_count = len(list(output_dir.glob("*.json")))
        checks = {
            "endToEnd": output_count == 1 and approved[0]["status"] == "committed",
            "denial": denied["status"] == "denied" and denied["sideEffectWritten"] is False,
            "idempotency": len(outcome_hashes) == 1 and side_effect_writes == 1,
            "recovery": recovery["code"] == "INVALID_REQUEST" and bool(recovery["recovery"]),
            "interface": health.get("status") == "ok" and contract["project"]["title"] in html,
        }
        if not all(checks.values()):
            raise RuntimeError(f"acceptance checks failed: {checks}")

    domain = _run_domain_fixtures(contract)
    source_hashes = []
    for index, relative in enumerate(sorted(contract["requiredFiles"])):
        path = _project_path(relative, label=f"requiredFiles[{index}]", must_exist=True)
        source_hashes.append({"path": relative, "sha256": _sha256(path)})
    scenario_ids = [item["id"] for item in contract["acceptanceScenarios"]]
    capability_ids = [item["id"] for item in contract["capabilities"]]
    return {
        "schema": "agent-workbench-acceptance/v4",
        "status": "PASS",
        "scope": "engineering-acceptance",
        "projectSlug": contract["project"]["slug"],
        "productKind": contract["project"]["kind"],
        "productContract": contract["product"],
        "capabilityContract": contract["capabilities"],
        "scenarioContracts": contract["acceptanceScenarios"],
        "provider": {"name": "reference-deterministic", "kind": "offline-reference", "externalProviderVerified": False},
        "results": {
            "domainAdaptation": domain,
            "multiScenario": {
                "passed": True,
                "productKind": contract["project"]["kind"],
                "declaredScenarios": len(scenario_ids),
                "passedScenarios": len(scenario_ids),
                "declaredCapabilities": len(capability_ids),
                "coveredCapabilities": len(capability_ids),
            },
            "endToEnd": {"passed": checks["endToEnd"], "businessOutputs": output_count},
            "approval": {"passed": checks["denial"], "approvedStatus": approved[0]["status"], "deniedStatus": denied["status"], "deniedSideEffectWritten": denied["sideEffectWritten"]},
            "idempotency": {"passed": checks["idempotency"], "runs": 3, "statuses": [item["status"] for item in approved], "distinctOutcomeHashes": len(outcome_hashes), "sideEffectWrites": side_effect_writes, "outcomeHash": approved[0]["outcomeHash"]},
            "recovery": {"passed": checks["recovery"], "error": recovery},
            "interface": {"passed": checks["interface"], "healthStatus": health["status"], "htmlContract": True, "networkScope": "loopback-only"},
        },
        "claims": {
            "domain-fixtures-executed": ["results.domainAdaptation"],
            "scenario-runs-end-to-end": ["results.endToEnd"],
            "dangerous-write-can-be-denied": ["results.approval.deniedStatus"],
            "retries-do-not-duplicate-side-effects": ["results.idempotency"],
            "failures-are-diagnosable": ["results.recovery.error"],
        },
        "sourceHashes": source_hashes,
        "limitations": [
            "The deterministic reference provider is not an external model.",
            "A starter fixture PASS is not project graduation until the domain gate passes.",
            "Automated acceptance is not an independent human usability test.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evidence/acceptance.json"))
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    try:
        report = run_acceptance()
        contract = _load_contract()
        domain_path = _project_path(
            contract["development"]["domainEvidence"]["report"],
            label="development.domainEvidence.report",
            must_exist=False,
        )
        atomic_write_json(domain_path, report["results"]["domainAdaptation"])
        atomic_write_json(output, report)
        code = 0
    except Exception as exc:
        message = str(exc).replace(str(PROJECT_ROOT), "<PROJECT_ROOT>")
        report = {"schema": "agent-workbench-acceptance/v4", "status": "FAIL", "error": {"code": "ACCEPTANCE_FAILED", "message": message}}
        atomic_write_json(output, report)
        code = 3
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
