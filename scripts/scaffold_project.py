#!/usr/bin/env python3
"""Create a focused Agent or a multi-capability Agent workbench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable


BUILDER_VERSION = "4.0.3"
BUILDER_RELEASE_TAG = "v4.0.3"
BUILDER_PUBLIC_URL = "https://github.com/jackygilbert498-debug/agent-workbench-builder"
BLUEPRINT_SCHEMA = "agent-workbench-blueprint/v1"
PRODUCT_KINDS = {"focused-agent", "workbench"}
TEMPLATE_NAMES = {
    ("dsh", "focused-agent"): "dsh-product-template/v4-focused-agent",
    ("dsh", "workbench"): "dsh-product-template/v4-workbench",
    ("standalone", "focused-agent"): "standalone-template/v4-focused-agent",
}
SLUG_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
TOKEN_PREFIX = "__PROJECT_"
MAX_BLUEPRINT_BYTES = 1024 * 1024


class ScaffoldError(RuntimeError):
    """A user-correctable scaffolding error."""


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _assert_plain_existing_components(path: Path, *, label: str) -> None:
    """Inspect a lexical path so resolve() cannot hide a redirection."""

    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            continue
        if _is_link_or_junction(current):
            raise ScaffoldError(f"{label} crosses a link or junction: {current.name}")
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ScaffoldError(f"cannot inspect {label}: {current.name}") from exc
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise ScaffoldError(f"{label} crosses a non-directory component: {current.name}")


def _single_line(value: Any, label: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise ScaffoldError(f"{label} must be text")
    value = value.strip()
    if not value:
        raise ScaffoldError(f"{label} must not be empty")
    if len(value) > maximum:
        raise ScaffoldError(f"{label} must be at most {maximum} characters")
    if any(ord(char) < 32 for char in value):
        raise ScaffoldError(f"{label} must be a single printable line")
    if TOKEN_PREFIX in value:
        raise ScaffoldError(f"{label} contains a reserved template token")
    return value


def _validate_slug(value: Any, label: str = "slug") -> str:
    value = _single_line(value, label, maximum=49)
    if len(value) < 2 or not SLUG_RE.fullmatch(value):
        raise ScaffoldError(
            f"{label} must be 2-49 characters: lowercase letters, digits, and single hyphens"
        )
    return value


def _text_list(value: Any, label: str, *, minimum: int = 1, maximum: int = 12) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ScaffoldError(f"{label} must contain {minimum}-{maximum} items")
    return [
        _single_line(item, f"{label}[{index}]", maximum=300)
        for index, item in enumerate(value)
    ]


def _focused_blueprint(
    *,
    slug: Any,
    title: Any,
    scenario: Any,
    primary_user: Any,
    trigger: Any,
    input_description: Any,
    observable_output: Any,
    dangerous_write: Any,
) -> dict[str, Any]:
    normalized_scenario = _single_line(scenario, "scenario")
    return {
        "schema": BLUEPRINT_SCHEMA,
        "productKind": "focused-agent",
        "project": {
            "slug": _validate_slug(slug),
            "title": _single_line(title, "title", maximum=120),
            "purpose": normalized_scenario,
            "primaryUsers": [_single_line(primary_user, "primary user", maximum=200)],
        },
        "capabilities": [
            {
                "id": "core-task",
                "title": "核心任务",
                "responsibility": normalized_scenario,
                "risk": "approval-required",
            }
        ],
        "scenarios": [
            {
                "id": "primary-task",
                "title": normalized_scenario,
                "primary": True,
                "trigger": _single_line(trigger, "trigger"),
                "input": _single_line(input_description, "input description"),
                "observableOutput": _single_line(observable_output, "observable output"),
                "capabilityIds": ["core-task"],
            }
        ],
        "dangerousWrites": [_single_line(dangerous_write, "dangerous write")],
    }


def _workbench_blueprint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScaffoldError("workbench blueprint must be a JSON object")
    if value.get("schema") != BLUEPRINT_SCHEMA:
        raise ScaffoldError(f"workbench blueprint schema must be {BLUEPRINT_SCHEMA}")
    if value.get("productKind") not in {None, "workbench"}:
        raise ScaffoldError("workbench blueprint productKind must be workbench")
    project = value.get("project")
    if not isinstance(project, dict):
        raise ScaffoldError("workbench blueprint project must be an object")
    normalized_project = {
        "slug": _validate_slug(project.get("slug"), "project.slug"),
        "title": _single_line(project.get("title"), "project.title", maximum=120),
        "purpose": _single_line(project.get("purpose"), "project.purpose"),
        "primaryUsers": _text_list(project.get("primaryUsers"), "project.primaryUsers", maximum=8),
    }

    raw_capabilities = value.get("capabilities")
    if not isinstance(raw_capabilities, list) or not 2 <= len(raw_capabilities) <= 12:
        raise ScaffoldError("workbench blueprint must declare 2-12 capability modules")
    capabilities: list[dict[str, str]] = []
    capability_ids: set[str] = set()
    for index, raw in enumerate(raw_capabilities):
        if not isinstance(raw, dict):
            raise ScaffoldError(f"capabilities[{index}] must be an object")
        identifier = _validate_slug(raw.get("id"), f"capabilities[{index}].id")
        if identifier in capability_ids:
            raise ScaffoldError(f"duplicate capability id: {identifier}")
        risk = raw.get("risk")
        if risk not in {"read-only", "approval-required"}:
            raise ScaffoldError(
                f"capabilities[{index}].risk must be read-only or approval-required"
            )
        capability_ids.add(identifier)
        capabilities.append(
            {
                "id": identifier,
                "title": _single_line(raw.get("title"), f"capabilities[{index}].title", maximum=120),
                "responsibility": _single_line(
                    raw.get("responsibility"),
                    f"capabilities[{index}].responsibility",
                ),
                "risk": risk,
            }
        )
    if not any(item["risk"] == "approval-required" for item in capabilities):
        raise ScaffoldError("workbench blueprint needs at least one approval-required capability")

    raw_scenarios = value.get("scenarios")
    if not isinstance(raw_scenarios, list) or not 3 <= len(raw_scenarios) <= 20:
        raise ScaffoldError("workbench blueprint must declare 3-20 representative scenarios")
    scenarios: list[dict[str, Any]] = []
    scenario_ids: set[str] = set()
    primary_count = 0
    referenced_capabilities: set[str] = set()
    for index, raw in enumerate(raw_scenarios):
        if not isinstance(raw, dict):
            raise ScaffoldError(f"scenarios[{index}] must be an object")
        identifier = _validate_slug(raw.get("id"), f"scenarios[{index}].id")
        if identifier in scenario_ids:
            raise ScaffoldError(f"duplicate scenario id: {identifier}")
        raw_refs = raw.get("capabilityIds")
        if not isinstance(raw_refs, list) or not raw_refs:
            raise ScaffoldError(f"scenarios[{index}].capabilityIds must not be empty")
        refs = [_validate_slug(item, f"scenarios[{index}].capabilityIds") for item in raw_refs]
        if len(refs) != len(set(refs)):
            raise ScaffoldError(f"scenarios[{index}].capabilityIds contains duplicates")
        unknown = sorted(set(refs) - capability_ids)
        if unknown:
            raise ScaffoldError(
                f"scenarios[{index}] references unknown capabilities: {', '.join(unknown)}"
            )
        primary = raw.get("primary") is True
        primary_count += int(primary)
        scenario_ids.add(identifier)
        referenced_capabilities.update(refs)
        scenarios.append(
            {
                "id": identifier,
                "title": _single_line(raw.get("title"), f"scenarios[{index}].title", maximum=160),
                "primary": primary,
                "trigger": _single_line(raw.get("trigger"), f"scenarios[{index}].trigger"),
                "input": _single_line(raw.get("input"), f"scenarios[{index}].input"),
                "observableOutput": _single_line(
                    raw.get("observableOutput"),
                    f"scenarios[{index}].observableOutput",
                ),
                "capabilityIds": refs,
            }
        )
    if primary_count != 1:
        raise ScaffoldError("workbench blueprint must mark exactly one scenario as primary")
    missing_coverage = sorted(capability_ids - referenced_capabilities)
    if missing_coverage:
        raise ScaffoldError(
            f"every capability needs representative scenario coverage: {', '.join(missing_coverage)}"
        )

    dangerous_writes = _text_list(
        value.get("dangerousWrites"),
        "dangerousWrites",
        minimum=1,
        maximum=20,
    )
    return {
        "schema": BLUEPRINT_SCHEMA,
        "productKind": "workbench",
        "project": normalized_project,
        "capabilities": capabilities,
        "scenarios": scenarios,
        "dangerousWrites": dangerous_writes,
    }


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ScaffoldError(f"starter template contains a symlink: {path.name}")
        if path.is_file():
            yield path


def _render_bytes(source: Path, replacements: dict[str, str]) -> bytes:
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    for token, replacement in replacements.items():
        text = text.replace(token, replacement)
    if TOKEN_PREFIX in text:
        raise ScaffoldError(f"unresolved template token in {source.name}")
    return text.encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _starter_domain_fixtures(
    blueprint: dict[str, Any], *, runtime: str
) -> dict[str, Any]:
    """Build executable starter fixtures without claiming domain completion."""

    cases: list[dict[str, Any]] = []
    for scenario in blueprint["scenarios"]:
        for capability_id in scenario["capabilityIds"]:
            identifier = f"starter-{scenario['id']}-{capability_id}"
            if runtime == "dsh":
                task_input = {
                    "task_id": identifier,
                    "scenario_id": scenario["id"],
                    "content": f"Urgent starter fixture for {scenario['title']} today",
                }
                expected = {
                    "status": "planned",
                    "scenarioId": scenario["id"],
                    "capabilityId": capability_id,
                    "urgency": "high",
                }
            else:
                task_input = {
                    "request_id": identifier,
                    "content": f"Urgent starter fixture for {scenario['title']} today",
                }
                expected = {"priority": "high", "category": "general"}
            cases.append(
                {
                    "id": identifier,
                    "kind": "positive",
                    "scenarioId": scenario["id"],
                    "capabilityId": capability_id,
                    "input": task_input,
                    "expected": expected,
                }
            )
    primary = next(item for item in blueprint["scenarios"] if item["primary"])
    primary_capability = primary["capabilityIds"][0]
    cases.append(
        {
            "id": "starter-empty-input-boundary",
            "kind": "boundary",
            "scenarioId": primary["id"],
            "capabilityId": primary_capability,
            "input": (
                {
                    "task_id": "starter-boundary",
                    "scenario_id": primary["id"],
                    "content": "",
                }
                if runtime == "dsh"
                else {"request_id": "starter-boundary", "content": ""}
            ),
            "expectedError": "INVALID_TASK" if runtime == "dsh" else "INVALID_REQUEST",
        }
    )
    return {
        "schema": "agent-workbench-domain-fixtures/v1",
        "stage": "starter",
        "cases": cases,
    }


def _replacements(blueprint: dict[str, Any], *, runtime: str) -> dict[str, str]:
    project = blueprint["project"]
    scenarios = blueprint["scenarios"]
    primary = next(item for item in scenarios if item["primary"])
    first_write = blueprint["dangerousWrites"][0]
    values = {
        "SLUG": project["slug"],
        "TITLE": project["title"],
        "PRODUCT_KIND": blueprint["productKind"],
        "PURPOSE": project["purpose"],
        "SCENARIO": primary["title"],
        "PRIMARY_USER": project["primaryUsers"][0],
        "TRIGGER": primary["trigger"],
        "INPUT_DESCRIPTION": primary["input"],
        "OBSERVABLE_OUTPUT": primary["observableOutput"],
        "DANGEROUS_WRITE": first_write,
        "PRIMARY_SCENARIO_ID": primary["id"],
        "DEMO_CONTENT": f"示例任务：{primary['title']}",
    }
    replacements: dict[str, str] = {}
    for name, value in values.items():
        replacements[f"__PROJECT_{name}_TEXT__"] = value
        replacements[f"__PROJECT_{name}_JSON__"] = json.dumps(value, ensure_ascii=False)
    structured = {
        "PRIMARY_USERS": project["primaryUsers"],
        "CAPABILITIES": blueprint["capabilities"],
        "SCENARIOS": scenarios,
        "DANGEROUS_WRITES": blueprint["dangerousWrites"],
        "DOMAIN_FIXTURES": _starter_domain_fixtures(blueprint, runtime=runtime),
    }
    for name, value in structured.items():
        replacements[f"__PROJECT_{name}_JSON__"] = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return replacements


def scaffold(
    destination: Path,
    *,
    product_kind: str,
    runtime: str = "dsh",
    blueprint: dict[str, Any] | None = None,
    slug: str | None = None,
    title: str | None = None,
    scenario: str | None = None,
    primary_user: str | None = None,
    trigger: str | None = None,
    input_description: str | None = None,
    observable_output: str | None = None,
    dangerous_write: str | None = None,
) -> dict[str, object]:
    if product_kind not in PRODUCT_KINDS:
        raise ScaffoldError("product_kind must be focused-agent or workbench")
    if runtime not in {"dsh", "standalone"}:
        raise ScaffoldError("runtime must be dsh or standalone")
    if runtime == "standalone" and product_kind != "focused-agent":
        raise ScaffoldError("standalone currently supports focused-agent only")
    if product_kind == "workbench":
        if blueprint is None:
            raise ScaffoldError("workbench mode requires a blueprint")
        normalized = _workbench_blueprint(blueprint)
    else:
        if blueprint is not None:
            raise ScaffoldError("focused-agent mode uses the scenario fields, not a blueprint")
        normalized = _focused_blueprint(
            slug=slug,
            title=title,
            scenario=scenario,
            primary_user=primary_user,
            trigger=trigger,
            input_description=input_description,
            observable_output=observable_output,
            dangerous_write=dangerous_write,
        )

    destination = Path(os.path.abspath(destination.expanduser()))
    _assert_plain_existing_components(destination, label="destination")
    if os.path.lexists(destination):
        raise ScaffoldError(f"destination already exists; refusing to overwrite: {destination}")
    parent = destination.parent
    _assert_plain_existing_components(parent, label="destination parent")
    parent.mkdir(parents=True, exist_ok=True)
    _assert_plain_existing_components(parent, label="destination parent")
    if not parent.is_dir():
        raise ScaffoldError("destination parent is not a directory")
    if os.path.lexists(destination):
        raise ScaffoldError(f"destination already exists; refusing to overwrite: {destination}")

    template_directory = "dsh-product-template" if runtime == "dsh" else "starter-template"
    template_root = Path(__file__).resolve().parents[1] / "assets" / template_directory
    if not template_root.is_dir():
        raise ScaffoldError("bundled starter template is missing")

    replacements = _replacements(normalized, runtime=runtime)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=parent))
    _assert_plain_existing_components(temporary, label="temporary scaffold")
    try:
        for source in _iter_files(template_root):
            relative = source.relative_to(template_root)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_render_bytes(source, replacements))
            os.chmod(target, source.stat().st_mode & 0o777)

        blueprint_digest = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
        contract_path = temporary / "agent_project.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        critical_files = contract.get("development", {}).get("criticalFiles")
        if (
            not isinstance(critical_files, list)
            or not critical_files
            or not all(isinstance(item, str) and item for item in critical_files)
        ):
            raise ScaffoldError("rendered contract is missing development.criticalFiles")
        starter_hashes: dict[str, str] = {}
        for relative in critical_files:
            candidate = temporary / relative
            if not candidate.is_file():
                raise ScaffoldError(f"critical starter file is missing: {relative}")
            starter_hashes[relative] = _sha256(candidate)
        provenance = {
            "schema": "agent-workbench-builder-provenance/v3",
            "builder": "agent-workbench-builder",
            "builderVersion": BUILDER_VERSION,
            "builderReleaseTag": BUILDER_RELEASE_TAG,
            "builderPublicUrl": BUILDER_PUBLIC_URL,
            "builderBundled": False,
            "template": TEMPLATE_NAMES[(runtime, product_kind)],
            "runtime": runtime,
            "productKind": product_kind,
            "projectSlug": normalized["project"]["slug"],
            "blueprintSha256": blueprint_digest,
            "starterStage": "starter",
            "starterFileSha256": starter_hashes,
        }
        (temporary / "builder-provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if runtime == "dsh" and contract.get("project", {}).get("kind") != product_kind:
            raise ScaffoldError("rendered contract product kind does not match the choice")
        contract_hash = _sha256(contract_path)
        _assert_plain_existing_components(parent, label="destination parent")
        if os.path.lexists(destination):
            raise ScaffoldError(f"destination appeared during build; refusing to overwrite: {destination}")
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise

    return {
        "schema": "agent-workbench-scaffold-result/v3",
        "status": "created",
        "builderVersion": BUILDER_VERSION,
        "template": TEMPLATE_NAMES[(runtime, product_kind)],
        "runtime": runtime,
        "productKind": product_kind,
        "projectSlug": normalized["project"]["slug"],
        "destination": str(destination),
        "blueprintSha256": blueprint_digest,
        "contractSha256": contract_hash,
        "capabilityCount": len(normalized["capabilities"]),
        "representativeScenarioCount": len(normalized["scenarios"]),
        "externalDependencies": (
            [
                {
                    "name": "DeepSeek Harness",
                    "officialRepository": "https://github.com/deepseek-ai/deepseek-harness",
                    "testedVersion": "0.1.0-rc.8",
                    "bundled": False,
                }
            ]
            if runtime == "dsh"
            else []
        ),
        "nextCommands": (
            [
                [sys.executable, "tools/test_project.py"],
                [
                    sys.executable,
                    "tools/acceptance.py",
                    "--dsh-root",
                    "{externalDshRoot}",
                    "--output",
                    "evidence/acceptance.json",
                ],
            ]
            if runtime == "dsh"
            else [
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                [
                    sys.executable,
                    "tools/acceptance.py",
                    "--output",
                    "evidence/acceptance.json",
                ],
            ]
        ),
    }


def _load_blueprint(path: Path) -> dict[str, Any]:
    lexical = Path(os.path.abspath(path.expanduser()))
    try:
        _assert_plain_existing_components(lexical, label="blueprint")
        metadata = os.lstat(lexical)
        if not stat.S_ISREG(metadata.st_mode):
            raise ScaffoldError("blueprint must be a regular file")
        if metadata.st_size > MAX_BLUEPRINT_BYTES:
            raise ScaffoldError(f"blueprint exceeds {MAX_BLUEPRINT_BYTES} bytes")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lexical, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ScaffoldError("blueprint must be a regular file")
            raw = bytearray()
            while len(raw) <= MAX_BLUEPRINT_BYTES:
                chunk = os.read(
                    descriptor,
                    min(1024 * 1024, MAX_BLUEPRINT_BYTES + 1 - len(raw)),
                )
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_BLUEPRINT_BYTES:
                raise ScaffoldError(f"blueprint exceeds {MAX_BLUEPRINT_BYTES} bytes while reading")
        finally:
            os.close(descriptor)
        _assert_plain_existing_components(lexical, label="blueprint")
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScaffoldError("blueprint is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ScaffoldError("blueprint must be a JSON object")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--product-kind",
        choices=("focused-agent", "workbench"),
        required=True,
        help="the user must explicitly choose a focused Agent or a general workbench",
    )
    parser.add_argument("--blueprint", type=Path, help="required for workbench mode")
    parser.add_argument("--slug")
    parser.add_argument("--title")
    parser.add_argument("--scenario")
    parser.add_argument("--primary-user")
    parser.add_argument("--trigger")
    parser.add_argument("--input-description")
    parser.add_argument("--observable-output")
    parser.add_argument("--dangerous-write")
    parser.add_argument(
        "--runtime",
        choices=("dsh", "standalone"),
        default="dsh",
        help="dsh is the default external runtime; standalone supports focused-agent only",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        blueprint = _load_blueprint(args.blueprint) if args.blueprint else None
        focused_values = [
            args.slug,
            args.title,
            args.scenario,
            args.primary_user,
            args.trigger,
            args.input_description,
            args.observable_output,
            args.dangerous_write,
        ]
        if args.product_kind == "workbench" and any(value is not None for value in focused_values):
            raise ScaffoldError("workbench mode accepts --blueprint instead of focused scenario fields")
        if args.product_kind == "focused-agent" and any(value is None for value in focused_values):
            raise ScaffoldError("focused-agent mode requires all focused scenario fields")
        result = scaffold(
            args.destination,
            product_kind=args.product_kind,
            runtime=args.runtime,
            blueprint=blueprint,
            slug=args.slug,
            title=args.title,
            scenario=args.scenario,
            primary_user=args.primary_user,
            trigger=args.trigger,
            input_description=args.input_description,
            observable_output=args.observable_output,
            dangerous_write=args.dangerous_write,
        )
    except (OSError, ScaffoldError, json.JSONDecodeError) as exc:
        error = {
            "schema": "agent-workbench-scaffold-result/v3",
            "status": "error",
            "error": {"code": "SCAFFOLD_FAILED", "message": str(exc)},
        }
        print(json.dumps(error, ensure_ascii=False, indent=2 if args.pretty else None))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
