#!/usr/bin/env python3
"""Evaluate an Agent workbench against the evidence-backed graduation contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Sequence
import unicodedata
import zipfile

from dsh_doctor import (
    TESTED_DSH_COMMIT,
    TESTED_DSH_TAG,
    _git_provenance,
    _provenance_verified,
)
from scaffold_project import _iter_files, _render_bytes, _replacements, _starter_domain_fixtures


SCHEMA = "agent-workbench-graduation/v4"
SKILL_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_DSH_REPOSITORY = "https://github.com/deepseek-ai/deepseek-harness"
TESTED_DSH_VERSION = "0.1.0-rc.8"
BUILDER_VERSION = "4.0.3"
BUILDER_RELEASE_TAG = "v4.0.3"
BUILDER_PUBLIC_URL = "https://github.com/jackygilbert498-debug/agent-workbench-builder"
MINIMUM_SCORE = 16
MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_MEMBER_BYTES = 10 * 1024 * 1024
MAX_CONTRACT_JSON_BYTES = 1024 * 1024
MAX_EVIDENCE_JSON_BYTES = 8 * 1024 * 1024
MAX_FIXTURE_JSON_BYTES = 4 * 1024 * 1024
MAX_SIDECAR_BYTES = 4096
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*"
        r"(?:[\"'][^\"'\r\n]{8,}[\"']|[^\s#,\"'}]{8,})"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)Authorization\s*:\s*(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(?:^|\s)_authToken\s*=\s*[^\s#]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
ABSOLUTE_PATH_PATTERNS = [
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z]:(?:/+|\\+)[^\s\"'<>|]+"),
    re.compile(
        r"(?<!\\)\\{2,}[A-Za-z0-9._-]+\\+[A-Za-z0-9.$_-]+"
        r"(?:\\+[^\\\s\"'<>|]+)*"
    ),
    re.compile(
        r"(?i)(?<![:\w])/(?:Users|home|opt|var|tmp|etc|usr|private|Volumes|Applications|srv|mnt)/"
        r"[^\s\"'`<>]+"
    ),
]
PORTABLE_PATH_EXAMPLES = {
    "README.md": (r"C:\path\to\deepseek-harness", r"C:\awb-runtime"),
    "tools/run_dsh.py": (r"C:\awb-runtime", r"C:\\awb-runtime"),
    "tools/dsh_runtime.py": (
        "/opt/homebrew/opt/node@24/bin/node",
        "/usr/local/opt/node@24/bin/node",
    ),
    "tools/package_handoff.py": (
        r"C:\path\to\deepseek-harness",
        r"C:\awb-runtime",
        r"C:\\awb-runtime",
        "/opt/homebrew/opt/node@24/bin/node",
        "/usr/local/opt/node@24/bin/node",
    ),
}
WINDOWS_DEVICE_RE = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"\\|?*')
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PACKAGER_TOP_LEVEL_EXCLUDED = {".git", ".runtime", "_handoff", "dist", "node_modules", "work", "__pycache__"}
PACKAGER_ANY_LEVEL_EXCLUDED = {".git", "node_modules", "__pycache__"}
PACKAGER_EXACT_EXCLUDED = {"evidence/graduation.json", "evidence/handoff.json"}
STAGE_TOP_LEVEL_EXCLUDED = PACKAGER_TOP_LEVEL_EXCLUDED | {"evidence"}
PROTECTED_HARNESS_FILES = {
    "standalone": (
        "tools/acceptance.py",
        "tools/package_handoff.py",
    ),
    "dsh": (
        "tools/test_project.py",
        "tools/acceptance.py",
        "tools/dsh_runtime.py",
        "tools/run_dsh.py",
        "tools/dsh_bootstrap.mjs",
        "tools/offline_acceptance.mjs",
        "tools/package_handoff.py",
    ),
}


class EvaluationError(RuntimeError):
    """Contract or evidence is invalid."""


def _starter_blueprint(contract: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the scaffold input from validated contract facts, not project provenance."""

    return {
        "productKind": contract["_validatedProductKind"],
        "project": {
            "slug": contract["project"]["slug"],
            "title": contract["project"]["title"],
            "purpose": contract["product"]["purpose"],
            "primaryUsers": contract["product"]["primaryUsers"],
        },
        "capabilities": contract["_validatedCapabilities"],
        "scenarios": contract["_validatedScenarios"],
        "dangerousWrites": contract["risk"]["dangerousWrites"],
    }


def _immutable_starter_hashes(contract: dict[str, Any]) -> dict[str, str]:
    """Render the release-bundled starter baseline without trusting project files."""

    runtime = "dsh" if contract["_validatedRuntime"]["kind"] == "external-dsh" else "standalone"
    template_name = "dsh-product-template" if runtime == "dsh" else "starter-template"
    template_root = SKILL_ROOT / "assets" / template_name
    replacements = _replacements(_starter_blueprint(contract), runtime=runtime)
    result: dict[str, str] = {}
    for relative in contract["_validatedDevelopment"]["criticalFiles"]:
        source = template_root / relative
        if not source.is_file():
            raise EvaluationError(f"Builder starter baseline is missing critical file: {relative}")
        result[relative] = _sha256_bytes(_render_bytes(source, replacements))
    return result


def _verify_immutable_harness(root: Path, contract: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Attest Builder-owned safety/evidence runners without freezing product code."""

    if contract["schema"] != "agent-workbench-project/v4":
        return False, {
            "status": "PARTIAL",
            "reasonCodes": ["legacy-harness-unattested"],
            "verifiedFiles": 0,
            "mismatches": [],
        }
    runtime = "dsh" if contract["_validatedRuntime"]["kind"] == "external-dsh" else "standalone"
    template_name = "dsh-product-template" if runtime == "dsh" else "starter-template"
    template_root = SKILL_ROOT / "assets" / template_name
    replacements = _replacements(_starter_blueprint(contract), runtime=runtime)
    mismatches: list[str] = []
    protected = PROTECTED_HARNESS_FILES[runtime]
    for relative in protected:
        source = template_root / relative
        target = root / relative
        if not source.is_file():
            raise EvaluationError(f"Builder release harness is missing: {relative}")
        expected = _sha256_bytes(_render_bytes(source, replacements))
        try:
            observed = _sha256_file(target)
        except (EvaluationError, OSError):
            observed = None
        if observed != expected:
            mismatches.append(relative)
    passed = not mismatches
    return passed, {
        "status": "PASS" if passed else "FAIL",
        "reasonCodes": [] if passed else ["immutable-harness-mismatch"],
        "verifiedFiles": len(protected) - len(mismatches),
        "expectedFiles": len(protected),
        "mismatches": mismatches,
        "builderReleaseTag": BUILDER_RELEASE_TAG,
    }


def _expected_behavior_signature(fixtures: dict[str, Any]) -> str:
    """Hash positive expected behavior while ignoring cosmetic case identifiers."""

    entries = []
    for item in fixtures.get("cases", []):
        if isinstance(item, dict) and item.get("kind") == "positive":
            entries.append(
                {
                    "scenarioId": item.get("scenarioId"),
                    "capabilityId": item.get("capabilityId"),
                    "expected": item.get("expected"),
                }
            )
    entries.sort(key=lambda item: _canonical_bytes(item))
    return _sha256_bytes(_canonical_bytes(entries))


def _validated_fixture_cases(fixtures: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject malformed case containers before signatures or counterfactual execution."""

    cases = fixtures.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("domain fixtures contain no cases")
    for index, item in enumerate(cases):
        if not isinstance(item, dict):
            raise EvaluationError(f"domain fixture {index} must be an object")
    return cases


def _starter_accepts_domain_fixtures(contract: dict[str, Any], fixtures: dict[str, Any]) -> bool:
    """Run new fixtures on the immutable starter; full acceptance means no domain adaptation."""

    runtime = "dsh" if contract["_validatedRuntime"]["kind"] == "external-dsh" else "standalone"
    template_name = "dsh-product-template" if runtime == "dsh" else "starter-template"
    template_root = SKILL_ROOT / "assets" / template_name
    replacements = _replacements(_starter_blueprint(contract), runtime=runtime)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-workbench-starter-counterfactual-") as raw:
            project = Path(raw) / "starter"
            for source in _iter_files(template_root):
                relative = source.relative_to(template_root)
                target = project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_render_bytes(source, replacements))
            starter_contract_path = project / "agent_project.json"
            starter_contract = _load_json(
                starter_contract_path,
                "counterfactual starter contract",
                max_bytes=MAX_CONTRACT_JSON_BYTES,
            )
            starter_contract["development"]["stage"] = "domain-adapted"
            starter_contract_path.write_text(
                json.dumps(starter_contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (project / "builder-provenance.json").write_text("{}\n", encoding="utf-8")
            fixture_relative = contract["_validatedDevelopment"]["domainEvidence"]["fixtures"]
            fixture_target = project / fixture_relative
            fixture_target.write_text(
                json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if runtime == "dsh":
                node = shutil.which("node")
                if not node:
                    raise EvaluationError("compatible Node is required for starter counterfactual validation")
                argv = [node, str(project / "tools/offline_acceptance.mjs")]
            else:
                argv = [
                    sys.executable,
                    str(project / "tools/acceptance.py"),
                    "--output",
                    str(project / "evidence/acceptance.json"),
                ]
            environment = dict(os.environ)
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            execution = _run_bounded_process(
                argv,
                cwd=project,
                environment=environment,
                timeout=90,
                output_limit=MAX_COMMAND_OUTPUT_BYTES,
            )
            if execution["outputLimited"]:
                raise EvaluationError("starter counterfactual output exceeded the safety limit")
            if execution["timedOut"]:
                raise EvaluationError("starter counterfactual validation timed out")
            if execution["returnCode"] != 0:
                output = (execution["stderr"] + execution["stdout"])[-1200:].decode(
                    "utf-8", errors="replace"
                )
                raise EvaluationError(f"starter counterfactual validation failed: {output}")
            payload = json.loads(execution["stdout"].decode("utf-8"))
            return payload.get("results", {}).get("domainAdaptation", {}).get("fixturesPassed") is True
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("starter counterfactual validation could not complete") from exc


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    _assert_plain_existing_components(path, label="hashed file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"{label} must be a non-empty relative path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or "\\" in value
        or "\x00" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(not _portable_component(part) for part in pure.parts)
    ):
        raise EvaluationError(f"{label} is unsafe: {value}")
    return value


def _deliverable_relative(
    value: Any,
    *,
    label: str,
    require_runtime_stage: bool = False,
) -> str:
    """Require a contract path to survive packaging and, when needed, DSH staging."""

    relative = _safe_relative(value, label=label)
    pure = PurePosixPath(relative)
    parts = pure.parts
    packager_excluded = (
        parts[0] in PACKAGER_TOP_LEVEL_EXCLUDED
        or any(part in PACKAGER_ANY_LEVEL_EXCLUDED for part in parts)
        or relative in PACKAGER_EXACT_EXCLUDED
        or pure.suffix.lower() in {".pyc", ".pyo"}
        or pure.name == ".DS_Store"
    )
    if packager_excluded:
        raise EvaluationError(f"{label} is excluded from handoff packaging: {relative}")
    if require_runtime_stage and (
        parts[0] in STAGE_TOP_LEVEL_EXCLUDED
        or any(part in PACKAGER_ANY_LEVEL_EXCLUDED for part in parts)
    ):
        raise EvaluationError(f"{label} is excluded from the DSH runtime stage: {relative}")
    return relative


def _windows_path_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFKC", part).casefold()
        for part in PurePosixPath(value).parts
    )


def _sensitive_filename(value: str) -> bool:
    name = PurePosixPath(value).name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {".npmrc", ".pypirc", "credentials", "credentials.json"}
        or name.endswith((".pem", ".key"))
        or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa"))
    )


def _text_views(raw: bytes) -> list[str]:
    """Decode UTF-8 plus plausible UTF-16 so NUL-separated secrets cannot hide."""

    views = [raw.decode("utf-8", errors="replace")]
    encodings: list[str] = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    elif raw and raw.count(b"\x00") * 5 >= len(raw):
        encodings.extend(("utf-16-le", "utf-16-be"))
    decoded = 0
    for encoding in encodings:
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded += 1
        if candidate not in views:
            views.append(candidate)
    if encodings and decoded == 0:
        raise EvaluationError("delivery source resembles malformed UTF-16 text")
    return views


def _portable_component(part: str) -> bool:
    normalized = unicodedata.normalize("NFKC", part)
    return (
        normalized not in {"", ".", ".."}
        and normalized == normalized.rstrip(" .")
        and not any(
            ord(character) < 32 or character in WINDOWS_FORBIDDEN_CHARS
            for character in normalized
        )
        and WINDOWS_DEVICE_RE.match(normalized) is None
    )


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _assert_plain_existing_components(path: Path, *, label: str) -> None:
    """Reject lexical links/junctions before resolve can hide them."""

    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and _is_link_or_junction(current):
            raise EvaluationError(f"{label} crosses a link or junction: {current.name}")


def _resolve_relative(root: Path, value: Any, *, label: str) -> Path:
    relative = _safe_relative(value, label=label)
    path = Path(os.path.abspath(root / relative))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EvaluationError(f"{label} escapes project root") from exc
    _assert_plain_existing_components(path, label=label)
    return path


def _required_text(value: Any, label: str, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise EvaluationError(f"{label} must be meaningful text")
    return value.strip()


def _read_bounded_bytes(path: Path, label: str, max_bytes: int) -> bytes:
    _assert_plain_existing_components(path, label=label)
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise EvaluationError(f"{label} is missing") from exc
    except OSError as exc:
        raise EvaluationError(f"{label} is unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise EvaluationError(f"{label} is not a regular file")
    if metadata.st_size > max_bytes:
        raise EvaluationError(f"{label} exceeds the {max_bytes}-byte safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise EvaluationError(f"{label} is unreadable") from exc
    if len(raw) > max_bytes:
        raise EvaluationError(f"{label} exceeds the {max_bytes}-byte safety limit")
    return raw


def _load_json(
    path: Path,
    label: str,
    *,
    max_bytes: int = MAX_EVIDENCE_JSON_BYTES,
) -> dict[str, Any]:
    try:
        payload = json.loads(_read_bounded_bytes(path, label, max_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"{label} is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise EvaluationError(f"{label} must be a JSON object")
    return payload


def _validate_contract(root: Path) -> dict[str, Any]:
    contract = _load_json(
        root / "agent_project.json",
        "agent_project.json",
        max_bytes=MAX_CONTRACT_JSON_BYTES,
    )
    contract_schema = contract.get("schema")
    if contract_schema not in {
        "agent-workbench-project/v1",
        "agent-workbench-project/v2",
        "agent-workbench-project/v3",
        "agent-workbench-project/v4",
    }:
        raise EvaluationError("agent_project.json has an unsupported schema")
    project = contract.get("project")
    architecture = contract.get("architecture")
    risk = contract.get("risk")
    commands = contract.get("commands")
    evidence = contract.get("evidence")
    if not all(isinstance(item, dict) for item in (project, architecture, risk, commands, evidence)):
        raise EvaluationError("project, architecture, risk, commands, and evidence must be objects")

    runtime = contract.get("runtime")
    if contract_schema in {
        "agent-workbench-project/v2",
        "agent-workbench-project/v3",
        "agent-workbench-project/v4",
    }:
        if not isinstance(runtime, dict):
            raise EvaluationError("runtime must be an object for project schema v2, v3, or v4")
        kind = runtime.get("kind")
        if kind == "external-dsh":
            if runtime.get("officialRepository") != OFFICIAL_DSH_REPOSITORY:
                raise EvaluationError("runtime.officialRepository must be the official DSH repository")
            if runtime.get("testedVersion") != TESTED_DSH_VERSION:
                raise EvaluationError("runtime.testedVersion is outside the Builder's tested DSH boundary")
            if runtime.get("bundled") is not False:
                raise EvaluationError("external DSH must not be bundled into the project")
            bundle_relative = _deliverable_relative(
                runtime.get("bundleManifest"),
                label="runtime.bundleManifest",
                require_runtime_stage=True,
            )
            bundle = _resolve_relative(root, bundle_relative, label="runtime.bundleManifest")
            if not bundle.is_file():
                raise EvaluationError("runtime.bundleManifest does not exist")
        elif kind != "standalone":
            raise EvaluationError("runtime.kind must be external-dsh or standalone")
    else:
        runtime = {"kind": "standalone-legacy", "bundled": False}
    contract["_validatedRuntime"] = runtime

    project_slug = _required_text(project.get("slug"), "project.slug", 2)
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", project_slug) is None:
        raise EvaluationError("project.slug must be lowercase kebab-case")
    _required_text(project.get("title"), "project.title", 2)
    _required_text(project.get("originalityStatement"), "project.originalityStatement", 30)

    if contract_schema in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
        product_kind = project.get("kind")
        if product_kind not in {"focused-agent", "workbench"}:
            raise EvaluationError("project.kind must be focused-agent or workbench")
        product = contract.get("product")
        capabilities = contract.get("capabilities")
        scenarios = contract.get("acceptanceScenarios")
        if not isinstance(product, dict):
            raise EvaluationError("product must be an object for project schema v3 or v4")
        _required_text(product.get("purpose"), "product.purpose", 2)
        primary_users = product.get("primaryUsers")
        if (
            not isinstance(primary_users, list)
            or not primary_users
            or not all(isinstance(item, str) and item.strip() for item in primary_users)
        ):
            raise EvaluationError("product.primaryUsers must list at least one user")
        if not isinstance(capabilities, list) or not capabilities:
            raise EvaluationError("capabilities must be a non-empty list")
        capability_ids: set[str] = set()
        approval_capabilities: set[str] = set()
        for index, capability in enumerate(capabilities):
            if not isinstance(capability, dict):
                raise EvaluationError(f"capabilities[{index}] must be an object")
            identifier = _required_text(capability.get("id"), f"capabilities[{index}].id", 2)
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", identifier):
                raise EvaluationError(f"capabilities[{index}].id is invalid")
            if identifier in capability_ids:
                raise EvaluationError(f"duplicate capability id: {identifier}")
            capability_ids.add(identifier)
            _required_text(capability.get("title"), f"capabilities[{index}].title", 2)
            _required_text(
                capability.get("responsibility"),
                f"capabilities[{index}].responsibility",
                2,
            )
            if capability.get("risk") not in {"read-only", "approval-required"}:
                raise EvaluationError(
                    f"capabilities[{index}].risk must be read-only or approval-required"
                )
            if capability.get("risk") == "approval-required":
                approval_capabilities.add(identifier)
        if not approval_capabilities:
            raise EvaluationError("at least one capability must require approval")
        if not isinstance(scenarios, list) or not scenarios:
            raise EvaluationError("acceptanceScenarios must be a non-empty list")
        scenario_ids: set[str] = set()
        covered_capabilities: set[str] = set()
        primary_count = 0
        for index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                raise EvaluationError(f"acceptanceScenarios[{index}] must be an object")
            identifier = _required_text(
                scenario.get("id"),
                f"acceptanceScenarios[{index}].id",
                2,
            )
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", identifier):
                raise EvaluationError(f"acceptanceScenarios[{index}].id is invalid")
            if identifier in scenario_ids:
                raise EvaluationError(f"duplicate representative scenario id: {identifier}")
            scenario_ids.add(identifier)
            primary_count += int(scenario.get("primary") is True)
            for key in ("title", "trigger", "input", "observableOutput"):
                _required_text(
                    scenario.get(key),
                    f"acceptanceScenarios[{index}].{key}",
                    2,
                )
            references = scenario.get("capabilityIds")
            if (
                not isinstance(references, list)
                or not references
                or not all(isinstance(item, str) for item in references)
            ):
                raise EvaluationError(
                    f"acceptanceScenarios[{index}].capabilityIds must not be empty"
                )
            unknown = set(references) - capability_ids
            if unknown:
                raise EvaluationError(
                    f"acceptanceScenarios[{index}] references unknown capabilities"
                )
            covered_capabilities.update(references)
        if primary_count != 1:
            raise EvaluationError("exactly one representative scenario must be primary")
        if covered_capabilities != capability_ids:
            raise EvaluationError("every capability must have representative scenario coverage")
        if product_kind == "focused-agent":
            if len(capabilities) != 1 or len(scenarios) != 1:
                raise EvaluationError(
                    "focused-agent must declare exactly one capability and one scenario"
                )
        elif len(capabilities) < 2 or len(scenarios) < 3:
            raise EvaluationError(
                "workbench must declare at least two capabilities and three representative scenarios"
            )
        contract["_validatedProductKind"] = product_kind
        contract["_validatedCapabilities"] = capabilities
        contract["_validatedScenarios"] = scenarios
        contract["_identitySummary"] = product["purpose"]
        architecture_keys = (
            (
                "kernel",
                "domainAdapter",
                "capabilityAdapter",
                "interface",
                "state",
                "facts",
            )
            if runtime["kind"] == "external-dsh"
            else ("kernel", "domainAdapter", "interface", "state", "facts")
        )
    else:
        scenario = contract.get("scenario")
        if not isinstance(scenario, dict):
            raise EvaluationError("scenario must be an object for legacy project schemas")
        for key in ("summary", "primaryUser", "trigger", "input", "observableOutput"):
            _required_text(scenario.get(key), f"scenario.{key}", 2)
        contract["_validatedProductKind"] = "focused-agent"
        contract["_validatedCapabilities"] = [
            {
                "id": "legacy-core-task",
                "title": "Legacy core task",
                "responsibility": scenario["summary"],
                "risk": "approval-required",
            }
        ]
        contract["_validatedScenarios"] = [
            {
                "id": "legacy-primary-task",
                "title": scenario["summary"],
                "primary": True,
                "trigger": scenario["trigger"],
                "input": scenario["input"],
                "observableOutput": scenario["observableOutput"],
                "capabilityIds": ["legacy-core-task"],
            }
        ]
        contract["_identitySummary"] = scenario["summary"]
        architecture_keys = ("kernel", "domainAdapter", "interface", "state", "facts")

    validated_architecture_files: list[str] = []
    for key in architecture_keys:
        relative = _deliverable_relative(
            architecture.get(key),
            label=f"architecture.{key}",
            require_runtime_stage=runtime["kind"] == "external-dsh",
        )
        path = _resolve_relative(root, relative, label=f"architecture.{key}")
        if not path.is_file():
            raise EvaluationError(f"architecture.{key} does not exist")
        validated_architecture_files.append(relative)
    contract["_validatedArchitectureFiles"] = validated_architecture_files
    if risk.get("approvalRequired") is not True or risk.get("denialSupported") is not True:
        raise EvaluationError("risk must require approval and support denial")
    dangerous = risk.get("dangerousWrites")
    if not isinstance(dangerous, list) or not dangerous or not all(isinstance(item, str) and item.strip() for item in dangerous):
        raise EvaluationError("risk.dangerousWrites must list at least one controlled write")
    for label in ("test", "acceptance", "package"):
        _validate_command(root, commands.get(label), label)
    if contract_schema == "agent-workbench-project/v4":
        expected_commands = (
            {
                "test": ["{python}", "tools/test_project.py"],
                "acceptance": ["{python}", "tools/acceptance.py", "--output", "evidence/acceptance.json"],
                "package": ["{python}", "tools/package_handoff.py", "--output-dir", "dist"],
            }
            if runtime["kind"] == "external-dsh"
            else {
                "test": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
                "acceptance": ["{python}", "tools/acceptance.py", "--output", "evidence/acceptance.json"],
                "package": ["{python}", "tools/package_handoff.py", "--output-dir", "dist"],
            }
        )
        if commands != expected_commands:
            raise EvaluationError("project schema v4 requires the exact Builder command contract")
    _deliverable_relative(evidence.get("acceptance"), label="evidence.acceptance")
    _safe_relative(evidence.get("handoff"), label="evidence.handoff")
    required_files = contract.get("requiredFiles")
    if not isinstance(required_files, list) or not required_files:
        raise EvaluationError("requiredFiles must be a non-empty list")
    for index, relative in enumerate(required_files):
        normalized = _deliverable_relative(
            relative,
            label=f"requiredFiles[{index}]",
            require_runtime_stage=runtime["kind"] == "external-dsh",
        )
        path = _resolve_relative(root, normalized, label=f"requiredFiles[{index}]")
        if not path.is_file():
            raise EvaluationError(f"required file is missing: {relative}")
    _required_text(contract.get("rollback"), "rollback", 20)
    development = contract.get("development")
    if contract_schema == "agent-workbench-project/v4":
        if not isinstance(development, dict):
            raise EvaluationError("project schema v4 requires development evidence")
        stage = development.get("stage")
        if stage not in {"starter", "domain-adapted"}:
            raise EvaluationError("development.stage must be starter or domain-adapted")
        domain_evidence = development.get("domainEvidence")
        critical_files = development.get("criticalFiles")
        if not isinstance(domain_evidence, dict):
            raise EvaluationError("development.domainEvidence must be an object")
        for key in ("fixtures", "report", "test"):
            relative = _deliverable_relative(
                domain_evidence.get(key),
                label=f"development.domainEvidence.{key}",
                require_runtime_stage=(
                    runtime["kind"] == "external-dsh" and key in {"fixtures", "test"}
                ),
            )
            if key != "report" and not (root / relative).is_file():
                raise EvaluationError(f"development domain {key} file is missing")
        if (
            not isinstance(critical_files, list)
            or not critical_files
            or not all(isinstance(item, str) and item for item in critical_files)
            or len(critical_files) != len(set(critical_files))
        ):
            raise EvaluationError("development.criticalFiles must be a unique non-empty list")
        for index, relative in enumerate(critical_files):
            normalized = _deliverable_relative(
                relative,
                label=f"development.criticalFiles[{index}]",
                require_runtime_stage=runtime["kind"] == "external-dsh",
            )
            path = _resolve_relative(
                root, normalized, label=f"development.criticalFiles[{index}]"
            )
            if not path.is_file():
                raise EvaluationError(f"critical domain file is missing: {relative}")
        contract["_validatedDevelopment"] = {
            "stage": stage,
            "domainEvidence": domain_evidence,
            "criticalFiles": critical_files,
        }
    else:
        contract["_validatedDevelopment"] = {
            "stage": "legacy-untracked",
            "domainEvidence": None,
            "criticalFiles": [],
        }
    return contract


def _validate_command(root: Path, argv: Any, label: str) -> list[str]:
    if not isinstance(argv, list) or len(argv) < 2 or not all(isinstance(item, str) and item for item in argv):
        raise EvaluationError(f"commands.{label} must be a non-empty argv array")
    if argv[0] != "{python}":
        raise EvaluationError(f"commands.{label} must start with {{python}}")
    if argv[1] == "-m":
        if len(argv) < 3 or argv[2] != "unittest":
            raise EvaluationError(f"commands.{label} only permits the unittest module")
    else:
        script = _resolve_relative(root, argv[1], label=f"commands.{label}[1]")
        if script.suffix != ".py" or not script.is_file():
            raise EvaluationError(f"commands.{label} must run a local Python script")
    return [sys.executable, *argv[1:]]


def _read_file_tail(handle: Any, limit: int = 2400) -> bytes:
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - limit))
    return handle.read(limit)


def _process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
    output_limit: int,
) -> dict[str, Any]:
    """Run a child with disk-backed logs and a hard combined-output boundary."""

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            **_process_group_options(),
        )
        deadline = time.monotonic() + timeout
        output_limited = False
        timed_out = False
        while process.poll() is None:
            total = (
                os.fstat(stdout_file.fileno()).st_size
                + os.fstat(stderr_file.fileno()).st_size
            )
            if total > output_limit:
                output_limited = True
                _terminate_process_tree(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process)
                break
            time.sleep(0.01)
        process.wait()
        total = (
            os.fstat(stdout_file.fileno()).st_size
            + os.fstat(stderr_file.fileno()).st_size
        )
        if total > output_limit:
            output_limited = True
        if output_limited or timed_out:
            stdout = _read_file_tail(stdout_file)
            stderr = _read_file_tail(stderr_file)
        else:
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(output_limit + 1)
            stderr = stderr_file.read(output_limit + 1)
        return {
            "returnCode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "outputLimited": output_limited,
            "timedOut": timed_out,
        }


def _sanitize_command_output(
    raw: bytes,
    root: Path,
    extra_environment: dict[str, str] | None,
) -> str:
    output = raw[-2400:].decode("utf-8", errors="replace")
    output = output.replace(str(root), "<PROJECT_ROOT>")
    if extra_environment:
        for value in extra_environment.values():
            if value:
                output = output.replace(value, "<EXTERNAL_ROOT>")
    return re.sub(
        r"(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}",
        "<REDACTED_TOKEN>",
        output,
    ).strip() or "<empty>"


def _run_command(
    root: Path,
    argv: list[str],
    label: str,
    timeout: int,
    *,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}
    }
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if extra_environment:
        environment.update(extra_environment)
    try:
        execution = _run_bounded_process(
            argv,
            cwd=root,
            environment=environment,
            timeout=timeout,
            output_limit=MAX_COMMAND_OUTPUT_BYTES,
        )
        if execution["outputLimited"]:
            return {
                "label": label,
                "status": "fail",
                "exitCode": execution["returnCode"],
                "proof": "live-subprocess-output-limit",
                "errorCode": "COMMAND_OUTPUT_LIMIT",
                "errorOutput": _sanitize_command_output(
                    execution["stderr"] + execution["stdout"], root, extra_environment
                ),
            }
        if execution["timedOut"]:
            return {
                "label": label,
                "status": "fail",
                "exitCode": None,
                "proof": "live-subprocess-timeout",
                "errorCode": "COMMAND_TIMEOUT",
            }
        result = {
            "label": label,
            "status": "pass" if execution["returnCode"] == 0 else "fail",
            "exitCode": execution["returnCode"],
            "proof": "live-subprocess-exit-code",
        }
        if execution["returnCode"] != 0:
            result["errorOutput"] = _sanitize_command_output(
                execution["stderr"] + execution["stdout"], root, extra_environment
            )
    except OSError as exc:
        result = {
            "label": label,
            "status": "fail",
            "exitCode": None,
            "proof": "live-subprocess-launch",
            "errorCode": "COMMAND_LAUNCH_FAILED",
            "errorOutput": str(exc).replace(str(root), "<PROJECT_ROOT>"),
        }
    return result


def _iter_scan_files(root: Path) -> Iterable[Path]:
    def walk(directory: Path) -> Iterable[Path]:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            path = Path(entry.path)
            relative = path.relative_to(root)
            parts = relative.parts
            if (
                parts[0] in PACKAGER_TOP_LEVEL_EXCLUDED
                or any(part in PACKAGER_ANY_LEVEL_EXCLUDED for part in parts)
            ):
                continue
            is_junction = getattr(os.path, "isjunction", None)
            if entry.is_symlink() or bool(is_junction and is_junction(path)):
                raise EvaluationError(f"linked delivery source path is not accepted: {relative.as_posix()}")
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise EvaluationError(f"special delivery source path is not accepted: {relative.as_posix()}")
            if relative.as_posix() in {
                "evidence/graduation.json",
                "evidence/handoff.json",
            }:
                continue
            if path.suffix.lower() in {".pyc", ".pyo"} or path.name == ".DS_Store":
                continue
            if path.stat().st_size > MAX_SCAN_BYTES:
                raise EvaluationError(
                    f"delivery source file exceeds the cleanliness scan limit: {relative.as_posix()}"
                )
            yield path

    yield from walk(root)


def _content_violations(relative: str, raw: bytes) -> list[dict[str, Any]]:
    """Scan delivered bytes independently of the code that packaged them."""

    violations: list[dict[str, Any]] = []
    if _sensitive_filename(relative):
        violations.append({"path": relative, "line": 0, "kind": "sensitive-filename"})
    try:
        views = _text_views(raw)
    except EvaluationError:
        return [*violations, {"path": relative, "line": 0, "kind": "suspicious-binary-text"}]
    for text in views:
        text = text.replace("/usr/bin/env", "<PORTABLE_INTERPRETER>")
        for example in PORTABLE_PATH_EXAMPLES.get(relative, ()):
            text = text.replace(example, "<PORTABLE_PATH_EXAMPLE>")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                violations.append({"path": relative, "line": line_number, "kind": "secret-like-value"})
            if any(pattern.search(line) for pattern in ABSOLUTE_PATH_PATTERNS):
                violations.append({"path": relative, "line": line_number, "kind": "machine-absolute-path"})
    return violations


def _scan_cleanliness(root: Path) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    scanned = 0
    for path in _iter_scan_files(root):
        try:
            raw = _read_bounded_bytes(path, "delivery source", MAX_SCAN_BYTES)
        except OSError as exc:
            raise EvaluationError(f"delivery source is unreadable: {path.name}") from exc
        scanned += 1
        relative = path.relative_to(root).as_posix()
        violations.extend(_content_violations(relative, raw))
    return {"passed": not violations, "filesScanned": scanned, "violations": violations}


def _verify_acceptance(root: Path, contract: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    path = _resolve_relative(root, contract["evidence"]["acceptance"], label="evidence.acceptance")
    try:
        report = _load_json(
            path,
            "acceptance evidence",
            max_bytes=MAX_EVIDENCE_JSON_BYTES,
        )
        if report.get("schema") not in {
            "agent-workbench-acceptance/v1",
            "agent-workbench-acceptance/v2",
            "agent-workbench-acceptance/v3",
            "agent-workbench-acceptance/v4",
        } or report.get("status") != "PASS":
            raise EvaluationError("acceptance evidence did not PASS")
        if report.get("projectSlug") != contract["project"]["slug"]:
            raise EvaluationError("acceptance project slug does not match")
        if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
            expected_acceptance_schema = (
                "agent-workbench-acceptance/v4"
                if contract["schema"] == "agent-workbench-project/v4"
                else "agent-workbench-acceptance/v3"
            )
            if report.get("schema") != expected_acceptance_schema:
                raise EvaluationError("project and acceptance schema versions do not match")
            if report.get("productKind") != contract["_validatedProductKind"]:
                raise EvaluationError("acceptance product kind does not match")
            if report.get("productContract") != contract["product"]:
                raise EvaluationError("acceptance product contract does not match")
            if report.get("capabilityContract") != contract["capabilities"]:
                raise EvaluationError("acceptance capability contract does not match")
            if report.get("scenarioContracts") != contract["acceptanceScenarios"]:
                raise EvaluationError("acceptance scenario contracts do not match")
        elif report.get("scenarioContract") != contract["scenario"]:
            raise EvaluationError("acceptance scenario contract does not match")
        results = report.get("results")
        if not isinstance(results, dict):
            raise EvaluationError("acceptance results are missing")
        required_results = ["endToEnd", "approval", "idempotency", "recovery", "interface"]
        if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
            required_results.append("multiScenario")
        if contract["_validatedRuntime"]["kind"] == "external-dsh":
            required_results.append("runtime")
        for key in required_results:
            if not isinstance(results.get(key), dict) or results[key].get("passed") is not True:
                raise EvaluationError(f"acceptance result failed: {key}")
        if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
            coverage = results["multiScenario"]
            if coverage.get("productKind") != contract["_validatedProductKind"]:
                raise EvaluationError("multi-scenario evidence has the wrong product kind")
            if coverage.get("declaredCapabilities") != len(contract["_validatedCapabilities"]):
                raise EvaluationError("multi-scenario capability count does not match")
            if coverage.get("coveredCapabilities") != len(contract["_validatedCapabilities"]):
                raise EvaluationError("multi-scenario evidence does not cover every capability")
            if coverage.get("declaredScenarios") != len(contract["_validatedScenarios"]):
                raise EvaluationError("multi-scenario scenario count does not match")
            if coverage.get("passedScenarios") != len(contract["_validatedScenarios"]):
                raise EvaluationError("not every representative scenario passed")
        if contract["_validatedRuntime"]["kind"] == "external-dsh":
            runtime = results["runtime"]
            if runtime.get("kind") != "external-dsh" or runtime.get("bundled") is not False:
                raise EvaluationError("acceptance did not preserve the external DSH boundary")
            if runtime.get("officialRepository") != OFFICIAL_DSH_REPOSITORY:
                raise EvaluationError("acceptance DSH repository does not match the contract")
            if runtime.get("testedVersion") != TESTED_DSH_VERSION or runtime.get("observedVersion") != TESTED_DSH_VERSION:
                raise EvaluationError("acceptance DSH version does not match the tested boundary")
            for key in ("profileDump", "bundlePresent", "webStarted", "loopbackHttp", "cleanStop"):
                if runtime.get(key) is not True:
                    raise EvaluationError(f"external DSH runtime proof failed: {key}")
        hashes = report.get("sourceHashes")
        if not isinstance(hashes, list) or not hashes:
            raise EvaluationError("acceptance source hashes are missing")
        for entry in hashes:
            if not isinstance(entry, dict):
                raise EvaluationError("acceptance source hash entry is invalid")
            source_relative = _deliverable_relative(
                entry.get("path"),
                label="sourceHashes.path",
                require_runtime_stage=contract["_validatedRuntime"]["kind"] == "external-dsh",
            )
            source = _resolve_relative(root, source_relative, label="sourceHashes.path")
            if not source.is_file() or _sha256_file(source) != entry.get("sha256"):
                raise EvaluationError(f"acceptance source hash mismatch: {entry.get('path')}")
        claims = report.get("claims")
        if not isinstance(claims, dict) or len(claims) < 5:
            raise EvaluationError("acceptance claim traceability is incomplete")
        return True, report
    except EvaluationError as exc:
        return False, {"error": str(exc)}


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _validate_manifest_entries(entries: Any) -> list[dict[str, Any]]:
    """Validate untrusted manifest members before sorting or dereferencing them."""

    if not isinstance(entries, list) or not entries:
        raise EvaluationError("handoff manifest files must be a non-empty array")
    validated: list[dict[str, Any]] = []
    canonical_names: set[str] = set()
    declared_total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise EvaluationError(f"handoff manifest files[{index}] must be an object")
        relative = _safe_relative(entry.get("path"), label=f"manifest files[{index}].path")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ARCHIVE_MEMBER_BYTES:
            raise EvaluationError(f"handoff manifest files[{index}].size is invalid")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise EvaluationError(f"handoff manifest files[{index}].sha256 is invalid")
        canonical = _windows_path_key(relative)
        if canonical in canonical_names:
            raise EvaluationError(f"handoff manifest files collide on Windows: {relative}")
        canonical_names.add(canonical)
        declared_total += size
        if declared_total > MAX_ARCHIVE_UNCOMPRESSED:
            raise EvaluationError("handoff manifest declared size exceeds the safety limit")
        validated.append({"path": relative, "size": size, "sha256": digest})
    return validated


def _required_handoff_members(root: Path, contract: dict[str, Any]) -> set[str]:
    """Resolve every contract- or acceptance-declared file that must survive handoff."""

    runtime_stage = contract["_validatedRuntime"]["kind"] == "external-dsh"
    required = {"agent_project.json"}
    required.update(contract["_validatedArchitectureFiles"])
    required.update(
        _deliverable_relative(
            value,
            label=f"requiredFiles[{index}]",
            require_runtime_stage=runtime_stage,
        )
        for index, value in enumerate(contract["requiredFiles"])
    )
    if runtime_stage:
        required.add(
            _deliverable_relative(
                contract["runtime"].get("bundleManifest"),
                label="runtime.bundleManifest",
                require_runtime_stage=True,
            )
        )
    development = contract["_validatedDevelopment"]
    if development["domainEvidence"] is not None:
        for key, value in development["domainEvidence"].items():
            required.add(
                _deliverable_relative(
                    value,
                    label=f"development.domainEvidence.{key}",
                    require_runtime_stage=runtime_stage and key in {"fixtures", "test"},
                )
            )
        required.update(
            _deliverable_relative(
                value,
                label=f"development.criticalFiles[{index}]",
                require_runtime_stage=runtime_stage,
            )
            for index, value in enumerate(development["criticalFiles"])
        )
    acceptance_relative = _deliverable_relative(
        contract["evidence"]["acceptance"], label="evidence.acceptance"
    )
    required.add(acceptance_relative)
    acceptance = _load_json(
        root / acceptance_relative,
        "acceptance evidence for handoff membership",
        max_bytes=MAX_EVIDENCE_JSON_BYTES,
    )
    source_hashes = acceptance.get("sourceHashes")
    if not isinstance(source_hashes, list) or not source_hashes:
        raise EvaluationError("handoff cannot verify acceptance source membership")
    for index, entry in enumerate(source_hashes):
        if not isinstance(entry, dict):
            raise EvaluationError(f"acceptance sourceHashes[{index}] must be an object")
        required.add(
            _deliverable_relative(
                entry.get("path"),
                label=f"acceptance sourceHashes[{index}].path",
                require_runtime_stage=runtime_stage,
            )
        )
    return required


def _verify_handoff(root: Path, contract: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    receipt_path = _resolve_relative(root, contract["evidence"]["handoff"], label="evidence.handoff")
    try:
        receipt = _load_json(
            receipt_path,
            "handoff evidence",
            max_bytes=MAX_CONTRACT_JSON_BYTES,
        )
        if receipt.get("schema") not in {
            "agent-workbench-handoff/v1",
            "agent-workbench-handoff/v2",
            "agent-workbench-handoff/v3",
            "agent-workbench-handoff/v4",
        } or receipt.get("status") != "PASS":
            raise EvaluationError("handoff evidence did not PASS")
        if (
            contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}
            and receipt.get("productKind") != contract["_validatedProductKind"]
        ):
            raise EvaluationError("handoff product kind does not match")
        if contract["schema"] == "agent-workbench-project/v4":
            if receipt.get("schema") != "agent-workbench-handoff/v4":
                raise EvaluationError("project schema v4 requires handoff schema v4")
            if receipt.get("developmentStage") != contract["_validatedDevelopment"]["stage"]:
                raise EvaluationError("handoff development stage does not match")
        if contract["_validatedRuntime"]["kind"] == "external-dsh" and receipt.get("externalDshBundled") is not False:
            raise EvaluationError("handoff receipt does not prove DSH stayed external")
        archive_path = _resolve_relative(root, receipt.get("archive"), label="handoff.archive")
        sidecar_path = _resolve_relative(root, receipt.get("sidecar"), label="handoff.sidecar")
        if not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise EvaluationError("handoff ZIP is missing or too large")
        archive_hash = _sha256_file(archive_path)
        if archive_hash != receipt.get("sha256"):
            raise EvaluationError("handoff ZIP hash does not match receipt")
        expected_sidecar = f"{archive_hash}  {archive_path.name}\n"
        try:
            sidecar = _read_bounded_bytes(
                sidecar_path,
                "handoff sidecar",
                MAX_SIDECAR_BYTES,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvaluationError("handoff sidecar is not UTF-8") from exc
        if sidecar != expected_sidecar:
            raise EvaluationError("handoff sidecar does not match ZIP")

        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (
                not infos
                or len(infos) > MAX_ARCHIVE_MEMBERS
                or len(names) != len(set(names))
                or len({_windows_path_key(name) for name in names}) != len(names)
            ):
                raise EvaluationError("handoff ZIP member count is unsafe")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_UNCOMPRESSED:
                raise EvaluationError("handoff ZIP expands beyond the safety limit")
            for info in infos:
                _safe_relative(info.filename, label="ZIP member")
                # The archive is untrusted even when its own hashes are consistent.
                # Only the packager-owned manifest may occupy the excluded area.
                if info.filename != "_handoff/manifest.json":
                    _deliverable_relative(info.filename, label="ZIP member")
                if info.flag_bits & 0x1 or _is_symlink(info):
                    raise EvaluationError("handoff ZIP contains encrypted or symlink members")
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise EvaluationError("handoff ZIP member exceeds the per-file safety limit")
            try:
                manifest_info = archive.getinfo("_handoff/manifest.json")
                if manifest_info.file_size > MAX_CONTRACT_JSON_BYTES:
                    raise EvaluationError("handoff manifest exceeds the safety limit")
                with archive.open(manifest_info) as stream:
                    manifest_raw = stream.read(MAX_CONTRACT_JSON_BYTES + 1)
                if len(manifest_raw) > MAX_CONTRACT_JSON_BYTES:
                    raise EvaluationError("handoff manifest exceeds the safety limit")
                manifest = json.loads(manifest_raw)
            except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise EvaluationError("handoff manifest is missing or invalid") from exc
            if not isinstance(manifest, dict):
                raise EvaluationError("handoff manifest must be a JSON object")
            if _content_violations("_handoff/manifest.json", manifest_raw):
                raise EvaluationError("handoff content scan rejected the manifest")
            if manifest.get("schema") not in {
                "agent-workbench-handoff-manifest/v1",
                "agent-workbench-handoff-manifest/v2",
                "agent-workbench-handoff-manifest/v3",
                "agent-workbench-handoff-manifest/v4",
            }:
                raise EvaluationError("handoff manifest schema is invalid")
            if manifest.get("projectSlug") != contract["project"]["slug"]:
                raise EvaluationError("handoff manifest project does not match")
            if manifest.get("contractSha256") != _sha256_file(root / "agent_project.json"):
                raise EvaluationError("handoff contract hash is stale")
            if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
                if manifest.get("productKind") != contract["_validatedProductKind"]:
                    raise EvaluationError("handoff manifest product kind does not match")
                if manifest.get("capabilityCount") != len(contract["_validatedCapabilities"]):
                    raise EvaluationError("handoff manifest capability count does not match")
                if manifest.get("representativeScenarioCount") != len(
                    contract["_validatedScenarios"]
                ):
                    raise EvaluationError("handoff manifest scenario count does not match")
                if contract["schema"] == "agent-workbench-project/v4":
                    if manifest.get("schema") != "agent-workbench-handoff-manifest/v4":
                        raise EvaluationError("project schema v4 requires handoff manifest v4")
                    if manifest.get("developmentStage") != contract["_validatedDevelopment"]["stage"]:
                        raise EvaluationError("handoff manifest development stage does not match")
            if contract["_validatedRuntime"]["kind"] == "external-dsh":
                dependencies = manifest.get("externalDependencies")
                if not isinstance(dependencies, list) or len(dependencies) != 1:
                    raise EvaluationError("handoff external DSH dependency record is missing")
                dependency = dependencies[0]
                if not isinstance(dependency, dict) or dependency.get("bundled") is not False:
                    raise EvaluationError("handoff manifest does not keep DSH external")
                if dependency.get("officialRepository") != OFFICIAL_DSH_REPOSITORY:
                    raise EvaluationError("handoff DSH repository does not match the contract")
            verification_dependencies = manifest.get("verificationDependencies")
            expected_builder = {
                "name": "Agent Workbench Builder Skill",
                "version": BUILDER_VERSION,
                "releaseTag": BUILDER_RELEASE_TAG,
                "publicUrl": BUILDER_PUBLIC_URL,
                "bundled": False,
            }
            if verification_dependencies != [expected_builder] or receipt.get("builderBundled") is not False:
                raise EvaluationError("handoff Builder verification dependency is missing or invalid")
            entries = _validate_manifest_entries(manifest.get("files"))
            manifest_names = [entry["path"] for entry in entries]
            if sorted(manifest_names + ["_handoff/manifest.json"]) != sorted(names):
                raise EvaluationError("handoff manifest membership does not match ZIP")
            missing_required = sorted(_required_handoff_members(root, contract) - set(manifest_names))
            if missing_required:
                raise EvaluationError(
                    "handoff is missing required delivery members: " + ", ".join(missing_required)
                )
            for entry in entries:
                relative = entry["path"]
                info = archive.getinfo(relative)
                observed = 0
                digest = hashlib.sha256()
                delivered_bytes = bytearray()
                with archive.open(info) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        observed += len(chunk)
                        if observed > MAX_ARCHIVE_MEMBER_BYTES:
                            raise EvaluationError(
                                f"handoff member exceeds the safety limit: {relative}"
                            )
                        digest.update(chunk)
                        delivered_bytes.extend(chunk)
                if observed != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise EvaluationError(f"handoff member hash mismatch: {relative}")
                content_issues = _content_violations(relative, bytes(delivered_bytes))
                if content_issues:
                    kinds = sorted({issue["kind"] for issue in content_issues})
                    raise EvaluationError(f"handoff content scan rejected {relative}: {', '.join(kinds)}")
        return True, receipt
    except (EvaluationError, OSError, zipfile.BadZipFile) as exc:
        return False, {"error": str(exc)}


def _verify_domain_adaptation(
    root: Path,
    contract: dict[str, Any],
    *,
    acceptance_ok: bool,
    acceptance: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify that a generated starter became an evidence-backed domain product."""

    development = contract["_validatedDevelopment"]
    reasons: list[str] = []
    if contract["schema"] != "agent-workbench-project/v4":
        return False, {
            "status": "PARTIAL",
            "stage": development["stage"],
            "reasonCodes": ["legacy-domain-evidence-untracked"],
        }

    if development["stage"] != "domain-adapted":
        reasons.append("starter-stage")

    try:
        provenance = _load_json(
            root / "builder-provenance.json",
            "builder provenance",
            max_bytes=MAX_CONTRACT_JSON_BYTES,
        )
        if provenance.get("schema") != "agent-workbench-builder-provenance/v3":
            raise EvaluationError("builder provenance schema is not v3")
        if provenance.get("projectSlug") != contract["project"]["slug"]:
            raise EvaluationError("builder provenance project does not match")
        if (
            provenance.get("builderVersion") != BUILDER_VERSION
            or provenance.get("builderReleaseTag") != BUILDER_RELEASE_TAG
            or provenance.get("builderPublicUrl") != BUILDER_PUBLIC_URL
        ):
            raise EvaluationError("builder provenance release identity does not match")
        starter_hashes = provenance.get("starterFileSha256")
        if not isinstance(starter_hashes, dict):
            raise EvaluationError("starter file hashes are missing")
        critical_files = development["criticalFiles"]
        if set(starter_hashes) != set(critical_files):
            raise EvaluationError("starter file hash membership does not match the contract")
        immutable_hashes = _immutable_starter_hashes(contract)
        if starter_hashes != immutable_hashes:
            reasons.append("builder-provenance-tampered")
        changed_files = [
            relative
            for relative in critical_files
            if _sha256_file(root / relative) != immutable_hashes.get(relative)
        ]
        unchanged_files = sorted(set(critical_files) - set(changed_files))
        if unchanged_files:
            reasons.append("starter-files-unchanged")

        fixture_path = _resolve_relative(
            root,
            development["domainEvidence"]["fixtures"],
            label="development.domainEvidence.fixtures",
        )
        fixtures = _load_json(
            fixture_path,
            "domain fixtures",
            max_bytes=MAX_FIXTURE_JSON_BYTES,
        )
        if fixtures.get("schema") != "agent-workbench-domain-fixtures/v1":
            raise EvaluationError("domain fixture schema is invalid")
        if fixtures.get("stage") != "domain-adapted":
            reasons.append("fixture-stage-not-domain-adapted")
        cases = _validated_fixture_cases(fixtures)
        runtime = "dsh" if contract["_validatedRuntime"]["kind"] == "external-dsh" else "standalone"
        starter_fixtures = _starter_domain_fixtures(
            _starter_blueprint(contract), runtime=runtime
        )
        starter_behavior_signature = _expected_behavior_signature(starter_fixtures)
        domain_behavior_signature = _expected_behavior_signature(fixtures)
        if domain_behavior_signature == starter_behavior_signature:
            reasons.append("starter-domain-behavior-unchanged")
        elif _starter_accepts_domain_fixtures(contract, fixtures):
            reasons.append("starter-domain-behavior-unchanged")
        identifiers: set[str] = set()
        positive_cases: list[dict[str, Any]] = []
        boundary_cases: list[dict[str, Any]] = []
        for index, item in enumerate(cases):
            identifier = _required_text(item.get("id"), f"domain fixture {index}.id", 2)
            if identifier in identifiers:
                raise EvaluationError("domain fixture ids must be unique")
            identifiers.add(identifier)
            kind = item.get("kind")
            if kind == "positive":
                if not isinstance(item.get("input"), dict):
                    raise EvaluationError("positive domain fixture input must be an object")
                if not isinstance(item.get("expected"), dict) or not item["expected"]:
                    raise EvaluationError("positive domain fixture expected fields are missing")
                positive_cases.append(item)
            elif kind == "boundary":
                _required_text(item.get("expectedError"), "boundary expectedError", 2)
                boundary_cases.append(item)
            else:
                raise EvaluationError("domain fixture kind must be positive or boundary")
        expected_scenarios = {item["id"] for item in contract["_validatedScenarios"]}
        expected_capabilities = {item["id"] for item in contract["_validatedCapabilities"]}
        covered_scenarios = {item.get("scenarioId") for item in positive_cases}
        covered_capabilities = {item.get("capabilityId") for item in positive_cases}
        if covered_scenarios != expected_scenarios or covered_capabilities != expected_capabilities:
            reasons.append("incomplete-fixture-coverage")
        if not boundary_cases:
            reasons.append("missing-boundary-case")

        report_path = _resolve_relative(
            root,
            development["domainEvidence"]["report"],
            label="development.domainEvidence.report",
        )
        domain_report = _load_json(
            report_path,
            "domain adaptation evidence",
            max_bytes=MAX_EVIDENCE_JSON_BYTES,
        )
        acceptance_domain = (
            acceptance.get("results", {}).get("domainAdaptation", {})
            if acceptance_ok
            else {}
        )
        if domain_report != acceptance_domain:
            reasons.append("domain-evidence-not-current")
        if domain_report.get("fixtureSha256") != _sha256_file(fixture_path):
            reasons.append("domain-fixture-hash-mismatch")
        if domain_report.get("stage") != development["stage"]:
            reasons.append("domain-stage-mismatch")
        if domain_report.get("status") != "PASS" or domain_report.get("passed") is not True:
            reasons.append("domain-evidence-not-pass")
        if domain_report.get("fixturesPassed") is not True:
            reasons.append("domain-fixtures-not-pass")
    except (EvaluationError, OSError) as exc:
        reasons.append("domain-evidence-invalid")
        changed_files = []
        unchanged_files = development["criticalFiles"]
        domain_report = {"error": str(exc)}
        positive_cases = []
        boundary_cases = []
        covered_scenarios = set()
        covered_capabilities = set()
        starter_behavior_signature = None
        domain_behavior_signature = None

    reasons = sorted(set(reasons))
    passed = not reasons
    return passed, {
        "status": "PASS" if passed else "PARTIAL",
        "stage": development["stage"],
        "reasonCodes": reasons,
        "changedCriticalFiles": sorted(changed_files),
        "unchangedCriticalFiles": sorted(unchanged_files),
        "fixtureCount": len(positive_cases) + len(boundary_cases),
        "positiveCases": len(positive_cases),
        "boundaryCases": len(boundary_cases),
        "coveredScenarios": sorted(str(item) for item in covered_scenarios),
        "coveredCapabilities": sorted(str(item) for item in covered_capabilities),
        "starterExpectedBehaviorSha256": starter_behavior_signature,
        "domainExpectedBehaviorSha256": domain_behavior_signature,
        "evidence": domain_report,
    }


def _gate(
    identifier: str,
    title: str,
    passed: bool,
    evidence: list[str],
    *,
    partial: bool = False,
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    gate = {
        "id": identifier,
        "title": title,
        "status": "partial" if partial else ("pass" if passed else "fail"),
        "evidence": evidence,
    }
    if reason_codes:
        gate["reasonCodes"] = reason_codes
    return gate


def evaluate(
    root: Path,
    *,
    run_commands: bool,
    timeout: int,
    dsh_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    lexical_root = Path(os.path.abspath(root.expanduser()))
    _assert_plain_existing_components(lexical_root, label="project directory")
    root = lexical_root.resolve()
    if not root.is_dir():
        raise EvaluationError("project directory does not exist")
    contract = _validate_contract(root)
    cleanliness = _scan_cleanliness(root)
    harness_ok, harness_summary = _verify_immutable_harness(root, contract)
    runtime_kind = contract["_validatedRuntime"]["kind"]
    command_environment: dict[str, str] = {}
    dsh_git: dict[str, Any] | None = None
    dsh_git_verified: bool | None = None
    if dsh_root is not None:
        resolved_dsh_root = dsh_root.expanduser().resolve()
        if not resolved_dsh_root.is_dir():
            raise EvaluationError("external DSH root does not exist")
        command_environment["AGENT_WORKBENCH_DSH_ROOT"] = str(resolved_dsh_root)
        if runtime_kind == "external-dsh":
            dsh_git = _git_provenance(resolved_dsh_root)
            dsh_git_verified = _provenance_verified(dsh_git)
    if runtime_kind == "external-dsh" and run_commands:
        if dsh_root is None:
            raise EvaluationError("live external-DSH evaluation requires --dsh-root")
        if dsh_git_verified is not True:
            raise EvaluationError(
                "external DSH must be the exact clean official rc8 tag/commit before live evaluation"
            )
    command_results: list[dict[str, Any]] = []
    commands_passed = harness_ok
    if run_commands:
        for label in ("test", "acceptance", "package"):
            if commands_passed:
                argv = _validate_command(root, contract["commands"][label], label)
                result = _run_command(root, argv, label, timeout, extra_environment=command_environment)
                command_results.append(result)
                commands_passed = result["status"] == "pass"
            else:
                command_results.append(
                    {
                        "label": label,
                        "status": "not-run",
                        "reason": (
                            "immutable-harness-mismatch"
                            if not harness_ok
                            else "prior-command-failed"
                        ),
                    }
                )
    else:
        command_results = [
            {"label": label, "status": "not-run", "reason": "no-run-mode"}
            for label in ("test", "acceptance", "package")
        ]

    acceptance_ok, acceptance = _verify_acceptance(root, contract)
    handoff_ok, handoff = _verify_handoff(root, contract)
    domain_ok, domain_summary = _verify_domain_adaptation(
        root,
        contract,
        acceptance_ok=acceptance_ok,
        acceptance=acceptance,
    )
    if not harness_ok:
        domain_ok = False
        domain_summary["status"] = "PARTIAL"
        domain_summary["reasonCodes"] = sorted(
            set(domain_summary.get("reasonCodes", [])) | {"immutable-harness-mismatch"}
        )
    runtime_current = run_commands and commands_passed and harness_ok
    if not runtime_current:
        acceptance_gate_ok = False
        handoff_gate_ok = False
    else:
        acceptance_gate_ok = acceptance_ok
        handoff_gate_ok = handoff_ok

    identity_text = " ".join(
        str(value).casefold()
        for value in (
            contract["project"]["slug"],
            contract["project"]["title"],
            contract["_identitySummary"],
            *(item["title"] for item in contract["_validatedScenarios"]),
        )
    )
    non_xiaoshe_identity = "xiaoshe" not in identity_text and "小蛇" not in identity_text
    product_kind = contract["_validatedProductKind"]
    results = acceptance.get("results", {}) if acceptance_ok else {}
    e2e = acceptance_gate_ok and results.get("endToEnd", {}).get("passed") is True
    coverage_result = results.get("multiScenario", {}) if isinstance(results, dict) else {}
    if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
        representative_coverage = (
            acceptance_gate_ok
            and coverage_result.get("passed") is True
            and coverage_result.get("passedScenarios") == len(contract["_validatedScenarios"])
            and coverage_result.get("coveredCapabilities") == len(
                contract["_validatedCapabilities"]
            )
        )
    else:
        representative_coverage = e2e
    approval = acceptance_gate_ok and results.get("approval", {}).get("passed") is True
    idempotency = acceptance_gate_ok and results.get("idempotency", {}).get("passed") is True
    recovery = acceptance_gate_ok and results.get("recovery", {}).get("passed") is True
    runtime_result = results.get("runtime", {}) if isinstance(results, dict) else {}
    external_dsh_runtime_verified = runtime_kind == "external-dsh" and (
        acceptance_gate_ok
        and runtime_result.get("passed") is True
        and runtime_result.get("bundled") is False
        and runtime_result.get("webStarted") is True
        and runtime_result.get("cleanStop") is True
    )
    runtime_ok = runtime_kind != "external-dsh" or (
        external_dsh_runtime_verified and dsh_git_verified is True
    )
    e2e = e2e and runtime_ok and representative_coverage
    traceable = (
        harness_ok
        and acceptance_gate_ok
        and isinstance(acceptance.get("claims"), dict)
        and handoff_gate_ok
    )
    clean = harness_ok and cleanliness["passed"] and handoff_gate_ok
    partial_runtime = not run_commands

    product_evidence = (
        ["agent_project.json#project", "agent_project.json#product"]
        if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}
        else ["agent_project.json#project", "agent_project.json#scenario"]
    )
    coverage_title = (
        "能力模块与代表性场景覆盖全部通过"
        if product_kind == "workbench"
        else "单一主场景端到端实际通过"
    )
    coverage_evidence = (
        ["evidence/acceptance.json#results.multiScenario", "evidence/acceptance.json#results.endToEnd"]
        if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}
        else ["evidence/acceptance.json#results.endToEnd"]
    )
    gates = [
        _gate(
            "non-xiaoshe-identity",
            "非小蛇品牌身份声明",
            non_xiaoshe_identity,
            product_evidence,
        ),
        _gate(
            "domain-adaptation",
            "领域适配器、夹具与证据已脱离 starter 基线",
            domain_ok,
            [
                "agent_project.json#development",
                "builder-provenance.json#starterFileSha256",
                contract["_validatedDevelopment"]["domainEvidence"]["fixtures"]
                if contract["_validatedDevelopment"]["domainEvidence"]
                else "legacy-domain-evidence",
            ],
            partial=not domain_ok,
            reason_codes=domain_summary.get("reasonCodes", []),
        ),
        _gate("representative-coverage", coverage_title, e2e, coverage_evidence, partial=partial_runtime and acceptance_ok),
        _gate("approval-and-denial", "危险写动作默认审批且可拒绝", approval, ["evidence/acceptance.json#results.approval"], partial=partial_runtime and acceptance_ok),
        _gate("idempotent-three-runs", "三次重跑无重复副作用且失败可诊断", idempotency and recovery, ["evidence/acceptance.json#results.idempotency", "evidence/acceptance.json#results.recovery"], partial=partial_runtime and acceptance_ok),
        _gate("clean-handoff", "未命中已知秘密或机器路径模式，且交接包完整", clean, ["staticScan", "evidence/handoff.json"], partial=partial_runtime and cleanliness["passed"] and handoff_ok),
        _gate("traceable-claims", "交付主张可追溯到证据与哈希", traceable, ["evidence/acceptance.json#claims", "_handoff/manifest.json"], partial=partial_runtime and acceptance_ok and handoff_ok),
    ]

    if contract["schema"] in {"agent-workbench-project/v3", "agent-workbench-project/v4"}:
        fit_score = (
            4
            if non_xiaoshe_identity and representative_coverage and domain_ok
            else (2 if non_xiaoshe_identity else 0)
        )
    else:
        scenario_fields = sum(
            bool(contract["scenario"].get(key))
            for key in ("primaryUser", "trigger", "input", "observableOutput")
        )
        fit_score = scenario_fields if non_xiaoshe_identity else 0
    architecture_files = sum(
        _resolve_relative(root, contract["architecture"][key], label=f"architecture.{key}").is_file()
        for key in ("kernel", "domainAdapter", "interface", "state")
    )
    dimensions = [
        {"id": "fit", "title": "声明与场景证据一致", "score": fit_score, "max": 4},
        {"id": "architecture", "title": "架构边界", "score": architecture_files, "max": 4},
        {"id": "safety", "title": "安全可控", "score": 4 if approval else (2 if contract["risk"]["approvalRequired"] and contract["risk"]["denialSupported"] else 0), "max": 4},
        {"id": "reliability", "title": "可靠可重跑", "score": 4 if e2e and idempotency and recovery else (2 if acceptance_ok else 0), "max": 4},
        {"id": "handoff", "title": "可交接", "score": 4 if clean and traceable else (2 if cleanliness["passed"] and handoff_ok else 0), "max": 4},
    ]
    total = sum(item["score"] for item in dimensions)
    gate_statuses = {gate["status"] for gate in gates}
    if "fail" not in gate_statuses and "partial" not in gate_statuses and total >= MINIMUM_SCORE:
        status_value = "PASS"
        exit_code = 0
    elif "partial" in gate_statuses and "fail" not in gate_statuses:
        status_value = "PARTIAL"
        exit_code = 2
    else:
        status_value = "FAIL"
        exit_code = 3

    report = {
        "schema": SCHEMA,
        "status": status_value,
        "project": {
            "slug": contract["project"]["slug"],
            "title": contract["project"]["title"],
            "productKind": product_kind,
            "capabilityCount": len(contract["_validatedCapabilities"]),
            "representativeScenarioCount": len(contract["_validatedScenarios"]),
            "developmentStage": contract["_validatedDevelopment"]["stage"],
        },
        "evaluationMode": "live" if run_commands else "no-run",
        "commands": command_results,
        "immutableHarness": harness_summary,
        "hardGates": gates,
        "dimensions": dimensions,
        "score": {"earned": total, "maximum": 20, "minimumToPass": MINIMUM_SCORE},
        "staticScan": cleanliness,
        "evidenceSummary": {
            "acceptanceVerified": acceptance_ok,
            "handoffVerified": handoff_ok,
            "immutableHarnessVerified": harness_ok,
            "domainAdaptationVerified": domain_ok,
            "domainAdaptation": domain_summary,
            "archiveSha256": handoff.get("sha256") if handoff_ok else None,
            "outcomeHash": results.get("idempotency", {}).get("outcomeHash") if acceptance_ok else None,
            "representativeScenarioCoverage": representative_coverage,
            "coveredCapabilities": coverage_result.get("coveredCapabilities")
            if acceptance_ok
            else None,
            "runtimeKind": runtime_kind,
            "externalDshRuntimeVerified": external_dsh_runtime_verified if runtime_kind == "external-dsh" else None,
            "externalDshGitProvenanceVerified": dsh_git_verified if runtime_kind == "external-dsh" else None,
            "externalDshProvenanceStatus": (
                "pass"
                if dsh_git_verified is True
                else ("fail" if dsh_git_verified is False else "not-run")
            ) if runtime_kind == "external-dsh" else None,
            "externalDshTestedTag": TESTED_DSH_TAG if runtime_kind == "external-dsh" else None,
            "externalDshTestedCommit": TESTED_DSH_COMMIT if runtime_kind == "external-dsh" else None,
            "externalDshObservedCommit": dsh_git.get("head") if dsh_git else None,
            "externalDshGitStatusSha256": dsh_git.get("statusSha256") if dsh_git else None,
            "externalDshBundled": runtime_result.get("bundled") if runtime_kind == "external-dsh" else None,
        },
        "limitations": [
            "Known-pattern scanning is not proof that all secrets or private data are absent; manually review before publication.",
            "Automation cannot prove that the declared product purpose or scenarios reflect sustained real-world demand.",
            *(
                [
                    "Representative workbench scenarios prove declared capability coverage, not every possible future user task."
                ]
                if product_kind == "workbench"
                else []
            ),
            "Automation is not an independent human clean-room usability test.",
            "A generated starter remains PARTIAL until project-specific domain evidence passes.",
            "The bundled reference provider does not verify an external model or account.",
            *(["DeepSeek Harness is external; runtime acceptance and exact clean official Git provenance are reported separately, and DSH is not included in the handoff."] if runtime_kind == "external-dsh" else []),
        ],
        "resultDigest": "",
    }
    digest_payload = {
        "status": report["status"],
        "hardGates": report["hardGates"],
        "dimensions": report["dimensions"],
        "score": report["score"],
        "staticScan": report["staticScan"],
        "evidenceSummary": report["evidenceSummary"],
    }
    report["resultDigest"] = _sha256_bytes(_canonical_bytes(digest_payload))
    return report, exit_code


def _atomic_json(path: Path, payload: dict[str, Any], *, pretty: bool) -> None:
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dsh-root", type=Path, help="existing external DSH checkout; never copied into the project")
    parser.add_argument("--no-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.timeout <= 300:
        error = "timeout must be between 1 and 300 seconds"
    else:
        error = None
    try:
        if error:
            raise EvaluationError(error)
        report, exit_code = evaluate(
            args.project,
            run_commands=not args.no_run,
            timeout=args.timeout,
            dsh_root=args.dsh_root,
        )
    except EvaluationError as exc:
        report = {
            "schema": SCHEMA,
            "status": "FAIL",
            "error": {"code": "INVALID_PROJECT", "message": str(exc)},
        }
        exit_code = 3
    _atomic_json(args.output.expanduser().resolve(), report, pretty=args.pretty)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
