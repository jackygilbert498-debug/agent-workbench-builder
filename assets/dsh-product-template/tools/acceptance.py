#!/usr/bin/env python3
"""Run offline business acceptance plus isolated external-DSH integration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from dsh_runtime import (  # noqa: E402
    DshRuntimeError,
    exercise_product_runtime,
    find_compatible_node,
    run_bounded_subprocess,
)


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
    contract = json.loads((PROJECT_ROOT / "agent_project.json").read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise RuntimeError("agent_project.json must contain an object")
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


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _offline_acceptance() -> dict[str, Any]:
    node, _ = find_compatible_node()
    completed = run_bounded_subprocess(
        [str(node), str(TOOLS_ROOT / "offline_acceptance.mjs")],
        cwd=PROJECT_ROOT,
        environment=None,
        timeout=60,
    )
    if completed["outputLimited"]:
        raise RuntimeError("offline acceptance output exceeded the safety limit")
    if completed["timedOut"]:
        raise RuntimeError("offline acceptance timed out")
    if completed["returnCode"] != 0:
        raise RuntimeError("offline business acceptance failed")
    try:
        payload = json.loads(completed["stdout"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("offline acceptance did not return JSON") from exc
    if payload.get("status") != "PASS":
        raise RuntimeError("offline business acceptance did not pass")
    return payload


def run_acceptance(dsh_root: Path) -> dict[str, Any]:
    contract = _load_contract()
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    offline = _offline_acceptance()
    runtime = exercise_product_runtime(dsh_root, PROJECT_ROOT, package["name"])
    source_hashes = []
    for index, relative in enumerate(sorted(contract["requiredFiles"])):
        path = _project_path(relative, label=f"requiredFiles[{index}]", must_exist=True)
        source_hashes.append({"path": relative, "sha256": _sha256(path)})
    domain_adaptation = dict(offline["domainAdaptation"])
    domain_adaptation["projectSlug"] = contract["project"]["slug"]
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
            "domainAdaptation": domain_adaptation,
            "multiScenario": offline["multiScenario"],
            "endToEnd": offline["endToEnd"],
            "approval": offline["approval"],
            "idempotency": offline["idempotency"],
            "recovery": offline["recovery"],
            "interface": {"passed": runtime["loopbackHttp"], "networkScope": "loopback-only", "htmlContract": True},
            "runtime": runtime,
        },
        "claims": {
            "domain-fixtures-executed": ["results.domainAdaptation"],
            "declared-capabilities-have-representative-coverage": ["results.multiScenario"],
            "representative-scenarios-run-end-to-end": ["results.multiScenario", "results.endToEnd"],
            "dangerous-write-can-be-denied": ["results.approval.deniedStatus"],
            "retries-do-not-duplicate-side-effects": ["results.idempotency"],
            "failures-are-diagnosable": ["results.recovery.error"],
            "external-dsh-profile-is-live": ["results.runtime.profileDump", "results.runtime.webStarted", "results.runtime.cleanStop"],
            "local-interface-is-observable": ["results.interface"],
        },
        "sourceHashes": source_hashes,
        "limitations": [
            "The deterministic reference provider is not an external model.",
            "A starter fixture PASS is not project graduation until the domain gate passes.",
            "DSH is external and must be installed separately from its official repository.",
            "Automated acceptance is not an independent human usability test.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsh-root", type=Path)
    parser.add_argument("--output", type=Path, default=Path("evidence/acceptance.json"))
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    dsh_root = args.dsh_root or (Path(os.environ["AGENT_WORKBENCH_DSH_ROOT"]) if os.environ.get("AGENT_WORKBENCH_DSH_ROOT") else None)
    try:
        if dsh_root is None:
            raise DshRuntimeError("external DSH root is required; install it from the official repository, then pass --dsh-root")
        report = run_acceptance(dsh_root)
        contract = _load_contract()
        domain_path = _project_path(
            contract["development"]["domainEvidence"]["report"],
            label="development.domainEvidence.report",
            must_exist=False,
        )
        _atomic_json(domain_path, report["results"]["domainAdaptation"])
        code = 0
    except (OSError, subprocess.TimeoutExpired, DshRuntimeError, RuntimeError, json.JSONDecodeError) as exc:
        message = str(exc).replace(str(PROJECT_ROOT), "<PROJECT_ROOT>")
        if dsh_root is not None:
            message = message.replace(str(dsh_root.expanduser().resolve()), "<DSH_ROOT>")
        report = {"schema": "agent-workbench-acceptance/v4", "status": "FAIL", "error": {"code": "ACCEPTANCE_FAILED", "message": message}}
        code = 3
    _atomic_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
