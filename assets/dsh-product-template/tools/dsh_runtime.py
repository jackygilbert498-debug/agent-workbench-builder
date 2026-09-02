#!/usr/bin/env python3
"""Read-only inspection and isolated runtime acceptance for an external DSH checkout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any, Optional, Sequence
from urllib.request import urlopen


OFFICIAL_DSH_REPOSITORY = "https://github.com/deepseek-ai/deepseek-harness"
TESTED_DSH_VERSION = "0.1.0-rc.8"
REQUIRED_CONFIG_MARKERS = {
    "agentLoop": "@deepseek-ai/dsh-agent-loop",
    "session": "@deepseek-ai/dsh-session",
    "model": "@deepseek-ai/dsh-llm",
    "tools": "@deepseek-ai/dsh-tools",
    "approval": "@deepseek-ai/dsh-user-approval",
    "web": "@deepseek-ai/dsh-host-webserver",
}
STOP_SENTINEL = b"__AGENT_WORKBENCH_STOP__\n"
STAGE_EXCLUDED_PARTS = {
    ".git",
    ".runtime",
    "_handoff",
    "__pycache__",
    "dist",
    "evidence",
    "node_modules",
    "work",
}
STAGE_ANY_LEVEL_EXCLUDED = {".git", "node_modules", "__pycache__"}
MAX_STAGE_FILE_BYTES = 10 * 1024 * 1024
MAX_STAGE_MEMBERS = 5000
MAX_STAGE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_CHILD_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_LOG_BYTES = 16 * 1024 * 1024


class DshRuntimeError(RuntimeError):
    """A bounded, user-correctable external-runtime failure."""


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _assert_plain_existing_components(path: Path) -> None:
    """Reject lexical path components that could redirect file traversal."""

    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and _is_link_or_junction(current):
            raise DshRuntimeError(f"product Bundle path crosses a link or junction: {current.name}")


def _included_product_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    lexical_root = Path(os.path.abspath(root.expanduser()))
    _assert_plain_existing_components(lexical_root)

    def walk(directory: Path) -> None:
        nonlocal total_bytes
        _assert_plain_existing_components(directory)
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise DshRuntimeError(f"product Bundle directory is unreadable: {directory.name}") from exc
        for entry in ordered:
            path = Path(entry.path)
            relative = path.relative_to(lexical_root)
            if (
                relative.parts[0] in STAGE_EXCLUDED_PARTS
                or any(part in STAGE_ANY_LEVEL_EXCLUDED for part in relative.parts)
            ):
                continue
            if entry.is_symlink() or _is_link_or_junction(path):
                raise DshRuntimeError(
                    f"product Bundle contains a link or junction: {relative.as_posix()}"
                )
            if entry.is_dir(follow_symlinks=False):
                walk(path)
            elif entry.is_file(follow_symlinks=False):
                _assert_plain_existing_components(path)
                size = entry.stat(follow_symlinks=False).st_size
                if size > MAX_STAGE_FILE_BYTES:
                    raise DshRuntimeError(
                        f"product Bundle file exceeds the stage limit: {relative.as_posix()}"
                    )
                if len(files) + 1 > MAX_STAGE_MEMBERS:
                    raise DshRuntimeError("product Bundle stage member count exceeds the limit")
                total_bytes += size
                if total_bytes > MAX_STAGE_TOTAL_BYTES:
                    raise DshRuntimeError("product Bundle stage total bytes exceed the limit")
                files.append(path)
            else:
                raise DshRuntimeError(
                    f"product Bundle contains a special path: {relative.as_posix()}"
                )

    walk(lexical_root)
    if not files:
        raise DshRuntimeError("product Bundle has no stageable files")
    return files


def _tree_digest(root: Path, files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        _assert_plain_existing_components(path)
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def stage_product_bundle(project_root: Path, stage_root: Path) -> tuple[Path, dict[str, Any]]:
    """Copy only source Product Bundle files to a deterministic safe stage."""

    source_lexical = Path(os.path.abspath(project_root.expanduser()))
    _assert_plain_existing_components(source_lexical)
    source = source_lexical.resolve()
    if not source.is_dir():
        raise DshRuntimeError("product Bundle directory does not exist")
    stage_lexical = Path(os.path.abspath(stage_root.expanduser()))
    _assert_plain_existing_components(stage_lexical)
    target = stage_lexical.resolve() / "product"
    if target.exists():
        raise DshRuntimeError("safe stage target already exists")
    source_files = _included_product_files(source)
    source_digest = _tree_digest(source, source_files)
    for path in source_files:
        _assert_plain_existing_components(path)
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    staged_files = _included_product_files(target)
    staged_digest = _tree_digest(target, staged_files)
    if source_digest != staged_digest:
        raise DshRuntimeError("safe stage digest differs from the source Product Bundle")
    return target, {
        "used": True,
        "reason": "windows-shell-safe-product-path",
        "files": len(source_files),
        "sourceTreeSha256": source_digest,
        "stagedTreeSha256": staged_digest,
        "excludedParts": sorted(STAGE_EXCLUDED_PARTS),
    }


def portable_staging_evidence(
    project_root: Path, staged_receipt: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Return path-independent proof that the runtime saw the source Bundle bytes."""

    source_lexical = Path(os.path.abspath(project_root.expanduser()))
    _assert_plain_existing_components(source_lexical)
    source = source_lexical.resolve()
    source_files = _included_product_files(source)
    source_digest = _tree_digest(source, source_files)
    runtime_digest = source_digest
    if staged_receipt is not None:
        if staged_receipt.get("sourceTreeSha256") != source_digest:
            raise DshRuntimeError("safe stage receipt no longer matches the source Product Bundle")
        runtime_digest = staged_receipt.get("stagedTreeSha256")
        if runtime_digest != source_digest:
            raise DshRuntimeError("safe stage runtime digest differs from the source Product Bundle")
    return {
        "status": "PASS",
        "files": len(source_files),
        "sourceTreeSha256": source_digest,
        "runtimeTreeSha256": runtime_digest,
        "excludedParts": sorted(STAGE_EXCLUDED_PARTS),
    }


def _path_needs_windows_stage(path: Path) -> bool:
    value = str(path)
    return os.name == "nt" and (" " in value or not value.isascii())


def stop_runtime_process(
    process: subprocess.Popen[bytes], *, graceful_timeout: float = 10.0
) -> dict[str, Any]:
    """Ask the bootstrap to enter DSH's graceful stop path before fallback signals."""

    if process.poll() is not None:
        return {
            "method": "already-exited",
            "clean": process.returncode == 0,
            "exitCode": process.returncode,
        }
    try:
        if process.stdin is None:
            raise OSError("runtime stdin is unavailable")
        process.stdin.write(STOP_SENTINEL)
        process.stdin.flush()
        process.wait(timeout=graceful_timeout)
        return {
            "method": "stdin-sentinel",
            "clean": process.returncode == 0,
            "exitCode": process.returncode,
        }
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        _terminate_process_tree(process)
        return {"method": "process-tree-fallback", "clean": False, "exitCode": process.returncode}


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


def _temporary_tail(handle: Any, limit: int = 2400) -> bytes:
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - limit))
    return handle.read(limit)


def run_bounded_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Optional[dict[str, str]] = None,
    timeout: float,
    output_limit: int = MAX_CHILD_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one child tree with bounded disk-backed stdout/stderr."""

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
            total = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
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
        total = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
        if total > output_limit:
            output_limited = True
        if output_limited or timed_out:
            stdout = _temporary_tail(stdout_file)
            stderr = _temporary_tail(stderr_file)
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


def _run_version(executable: Path, argument: str = "--version", *, cwd: Optional[Path] = None) -> str:
    completed = run_bounded_subprocess(
        [str(executable), argument],
        cwd=cwd or executable.parent,
        environment=None,
        timeout=10,
        output_limit=1024 * 1024,
    )
    if completed["outputLimited"] or completed["timedOut"] or completed["returnCode"] != 0:
        return ""
    return (completed["stdout"] or completed["stderr"]).decode("utf-8", errors="replace").strip()


def find_compatible_node(explicit: Optional[Path] = None) -> tuple[Path, str]:
    candidates = [
        explicit,
        Path(os.environ["AGENT_WORKBENCH_NODE"]) if os.environ.get("AGENT_WORKBENCH_NODE") else None,
        Path(shutil.which("node")) if shutil.which("node") else None,
        Path("/opt/homebrew/opt/node@24/bin/node"),
        Path("/usr/local/opt/node@24/bin/node"),
    ]
    seen = set()
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser().resolve()
        if candidate in seen or not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate)
        version = _run_version(candidate)
        match = re.fullmatch(r"v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)", version)
        if match is None:
            continue
        major = int(match.group("major"))
        minor = int(match.group("minor"))
        if (major == 22 and minor >= 19) or major >= 24:
            return candidate, version
    raise DshRuntimeError("compatible Node.js not found; DSH requires Node 22.x >=22.19.0 or Node >=24")


def find_pnpm(explicit: Optional[Path] = None, *, project_directory: Optional[Path] = None) -> tuple[Path, str]:
    candidates = [
        explicit,
        Path(os.environ["AGENT_WORKBENCH_PNPM"]) if os.environ.get("AGENT_WORKBENCH_PNPM") else None,
        Path(shutil.which("pnpm")) if shutil.which("pnpm") else None,
    ]
    seen = set()
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser().resolve()
        if candidate in seen or not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate)
        version = _run_version(candidate, cwd=project_directory)
        if version == "11.7.0":
            return candidate, version
    raise DshRuntimeError("pnpm 11.7.0 not found on PATH")


def validate_dsh_root(dsh_root: Path) -> dict[str, Any]:
    root = dsh_root.expanduser().resolve()
    required = [
        "package.json",
        "LICENSE",
        "apps/cli/package.json",
        "apps/cli/src/bin.ts",
        "packages/bundle/base/cordis.patch.yml",
        "packages/bundle/web-app/cordis.patch.yml",
    ]
    missing = [relative for relative in required if not (root / relative).is_file()]
    if missing:
        raise DshRuntimeError(f"external DSH checkout is incomplete: {', '.join(missing)}")
    try:
        root_manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
        cli_manifest = json.loads((root / "apps/cli/package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DshRuntimeError("external DSH manifests are unreadable") from exc
    version = cli_manifest.get("version")
    if version != TESTED_DSH_VERSION:
        raise DshRuntimeError(
            f"DSH version {version!r} is outside the tested boundary {TESTED_DSH_VERSION!r}; run a compatibility review before continuing"
        )
    if cli_manifest.get("license") != "MIT" or root_manifest.get("license") != "MIT":
        raise DshRuntimeError("external DSH license metadata is not the tested MIT boundary")
    return {
        "version": version,
        "license": "MIT",
        "packageManager": root_manifest.get("packageManager"),
        "engines": root_manifest.get("engines", {}),
        "requiredFiles": len(required),
    }


def _safe_environment(node: Path, dsh_home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}
    }
    current_path = os.environ.get("PATH", "")
    environment["PATH"] = f"{node.parent}{os.pathsep}{current_path}"
    environment["DSH_HOME"] = str(dsh_home)
    environment["DSH_TELEMETRY_DISABLED"] = "1"
    environment["DSH_PERMISSION_MODE"] = "workspace-write"
    return environment


def _run_dsh(
    dsh_root: Path,
    pnpm: Path,
    node: Path,
    dsh_home: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 90,
    extra_environment: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = _safe_environment(node, dsh_home)
    if extra_environment:
        environment.update(extra_environment)
    completed = run_bounded_subprocess(
        [str(pnpm), "--dir", str(dsh_root), "dsh", *arguments],
        cwd=cwd,
        environment=environment,
        timeout=timeout,
    )
    if completed["outputLimited"]:
        raise DshRuntimeError("external DSH command output exceeded the safety limit")
    if completed["timedOut"]:
        raise DshRuntimeError("external DSH command timed out")
    if completed["returnCode"] != 0:
        tail = (completed["stderr"] + completed["stdout"])[-1200:].decode("utf-8", errors="replace")
        raise DshRuntimeError(f"external DSH command failed with exit {completed['returnCode']}: {tail}")
    return subprocess.CompletedProcess(
        args=[str(pnpm), "--dir", str(dsh_root), "dsh", *arguments],
        returncode=completed["returnCode"],
        stdout=completed["stdout"],
        stderr=completed["stderr"],
    )


def inspect_external_dsh(dsh_root: Path, *, run_config_dump: bool = True) -> dict[str, Any]:
    root = dsh_root.expanduser().resolve()
    metadata = validate_dsh_root(root)
    node, node_version = find_compatible_node()
    pnpm, pnpm_version = find_pnpm(project_directory=root)
    observed_version = ""
    capabilities = {key: False for key in REQUIRED_CONFIG_MARKERS}
    if run_config_dump:
        with tempfile.TemporaryDirectory(prefix="agent-workbench-dsh-doctor-") as raw_home:
            home = Path(raw_home)
            version_run = _run_dsh(root, pnpm, node, home, ["--version"], cwd=root)
            version_text = (version_run.stdout + version_run.stderr).decode("utf-8", errors="replace")
            match = re.search(r"(?m)^([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)$", version_text)
            if match is None:
                raise DshRuntimeError("DSH CLI did not report a parseable version")
            observed_version = match.group(1)
            if observed_version != TESTED_DSH_VERSION:
                raise DshRuntimeError("DSH CLI version differs from its checked manifest")
            dump = _run_dsh(root, pnpm, node, home, ["--profile", "web", "--dump-default-config"], cwd=root)
            dump_text = (dump.stdout + dump.stderr).decode("utf-8", errors="replace")
            capabilities = {key: marker in dump_text for key, marker in REQUIRED_CONFIG_MARKERS.items()}
            if not all(capabilities.values()):
                missing = [key for key, present in capabilities.items() if not present]
                raise DshRuntimeError(f"DSH default config is missing required capabilities: {', '.join(missing)}")
    return {
        **metadata,
        "observedCliVersion": observed_version or metadata["version"],
        "nodeVersion": node_version,
        "pnpmVersion": pnpm_version,
        "capabilities": capabilities,
        "configDump": run_config_dump,
    }


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _bounded_log_tail(handle: Any, limit: int = 1200) -> str:
    """Read only the end of a seekable binary runtime log."""

    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - limit))
    return handle.read(limit).decode("utf-8", errors="replace")


def exercise_product_runtime(dsh_root: Path, project_root: Path, package_name: str) -> dict[str, Any]:
    root = dsh_root.expanduser().resolve()
    project = project_root.expanduser().resolve()
    inspection = inspect_external_dsh(root, run_config_dump=True)
    node, _ = find_compatible_node()
    pnpm, _ = find_pnpm(project_directory=root)
    with tempfile.TemporaryDirectory(prefix="agent-workbench-dsh-product-") as raw_home, tempfile.TemporaryDirectory(prefix="awb-stage-") as raw_stage:
        home = Path(raw_home)
        runtime_project = project
        staged_receipt: Optional[dict[str, Any]] = None
        if _path_needs_windows_stage(project):
            stage_root = Path(raw_stage)
            if " " in str(stage_root) or not str(stage_root).isascii():
                raise DshRuntimeError(
                    "Windows temporary directory is not shell-safe; set TEMP to an ASCII path without spaces for DSH rc.8 acceptance"
                )
            runtime_project, staged_receipt = stage_product_bundle(project, stage_root)
        product_environment = {"AGENT_WORKBENCH_PRODUCT_ROOT": str(project)}
        _run_dsh(
            root,
            pnpm,
            node,
            home,
            ["plugin", "--profile", "web", "add", str(runtime_project)],
            cwd=runtime_project,
            timeout=120,
            extra_environment=product_environment,
        )
        dump = _run_dsh(
            root,
            pnpm,
            node,
            home,
            ["web", "--dump-config"],
            cwd=runtime_project,
            extra_environment=product_environment,
        )
        dump_text = (dump.stdout + dump.stderr).decode("utf-8", errors="replace")
        bundle_present = package_name in dump_text
        if not bundle_present:
            raise DshRuntimeError("product Bundle is absent from the composed DSH web Profile")

        port = _free_loopback_port()
        tsx_loader = (root / "node_modules/tsx/dist/esm/index.mjs").resolve()
        cli_entry = (root / "apps/cli/src/bin.ts").resolve()
        if not tsx_loader.is_file():
            raise DshRuntimeError("DSH source checkout is missing its installed tsx loader; run the official install/build steps")
        launch_environment = _safe_environment(node, home)
        launch_environment["AGENT_WORKBENCH_DSH_ENTRY"] = cli_entry.as_uri()
        launch_environment["AGENT_WORKBENCH_PROJECT_ROOT"] = str(runtime_project)
        launch_environment.update(product_environment)
        with tempfile.TemporaryFile(prefix="agent-workbench-dsh-log-") as runtime_log:
            process = subprocess.Popen(
                [str(node), "--import", tsx_loader.as_uri(), str(runtime_project / "tools/dsh_bootstrap.mjs"), "web", "--no-open", "--port", str(port)],
                cwd=root,
                env=launch_environment,
                stdin=subprocess.PIPE,
                stdout=runtime_log,
                stderr=subprocess.STDOUT,
                **_process_group_options(),
            )
            html_contract = False
            shutdown = {"method": "not-requested", "clean": False, "exitCode": None}
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if os.fstat(runtime_log.fileno()).st_size > MAX_RUNTIME_LOG_BYTES:
                        raise DshRuntimeError("DSH web runtime log exceeded the safety limit")
                    if process.poll() is not None:
                        raise DshRuntimeError(
                            f"DSH web exited before readiness: {_bounded_log_tail(runtime_log)}"
                        )
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                            body = response.read(512_000).decode("utf-8", errors="replace")
                        html_contract = response.status == 200 and "<html" in body.lower() and "</html>" in body.lower()
                        if html_contract:
                            break
                    except OSError:
                        time.sleep(0.2)
                if not html_contract:
                    raise DshRuntimeError(
                        f"DSH web did not become reachable on loopback within 45 seconds; log tail: {_bounded_log_tail(runtime_log)}"
                    )
            finally:
                if process.poll() is None:
                    shutdown = stop_runtime_process(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    shutdown = {"method": "kill-after-wait-timeout", "clean": False, "exitCode": process.returncode}
        clean_stop = shutdown.get("clean") is True and process.returncode == 0
        if not clean_stop:
            raise DshRuntimeError(f"DSH web did not stop cleanly (exit {process.returncode})")

        staging = portable_staging_evidence(project, staged_receipt)

    return {
        "passed": True,
        "kind": "external-dsh",
        "officialRepository": OFFICIAL_DSH_REPOSITORY,
        "bundled": False,
        "downloadedByBuilder": False,
        "testedVersion": TESTED_DSH_VERSION,
        "observedVersion": inspection["observedCliVersion"],
        "profileDump": True,
        "bundlePresent": bundle_present,
        "webStarted": True,
        "loopbackHttp": html_contract,
        "cleanStop": clean_stop,
        "shutdown": shutdown,
        "staging": staging,
        "capabilities": inspection["capabilities"],
    }
