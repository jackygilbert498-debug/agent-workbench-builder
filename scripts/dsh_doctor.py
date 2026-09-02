#!/usr/bin/env python3
"""Validate an externally installed DeepSeek Harness without copying or downloading it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit


SKILL_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_HELPERS = SKILL_ROOT / "assets" / "dsh-product-template" / "tools"
if str(RUNTIME_HELPERS) not in sys.path:
    sys.path.insert(0, str(RUNTIME_HELPERS))

from dsh_runtime import (  # noqa: E402
    DshRuntimeError,
    OFFICIAL_DSH_REPOSITORY,
    TESTED_DSH_VERSION,
    inspect_external_dsh,
)


TESTED_DSH_COMMIT = "141eb6fef83422698aef7a981029e843e8161534"
TESTED_DSH_TAG = "dsh-v0.1.0-rc.8"
OFFICIAL_DSH_REPOSITORY_IDENTITY = "github.com/deepseek-ai/deepseek-harness"
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_GIT_ERROR_BYTES = 1024 * 1024
MAX_TRACKED_FILES = 20_000
MAX_TRACKED_BYTES = 1024 * 1024 * 1024
MAX_TRACKED_FILE_BYTES = 128 * 1024 * 1024


class GitInspectionError(DshRuntimeError):
    """A Git provenance command failed, timed out, or crossed a resource bound."""


def _validate_git_arguments(arguments: tuple[str, ...]) -> None:
    fixed = {
        ("rev-parse", "--is-inside-work-tree"),
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--show-object-format"),
        ("rev-parse", "HEAD"),
        ("branch", "--show-current"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("remote", "get-url", "origin"),
        ("tag", "--points-at", "HEAD"),
        ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
        ("ls-files", "-z", "--stage"),
        ("ls-files", "-z", "-v"),
        ("rev-parse", f"refs/tags/{TESTED_DSH_TAG}^{{commit}}"),
    }
    if arguments not in fixed:
        raise GitInspectionError(f"Git provenance command is not allowlisted: {' '.join(arguments)}")


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_EXTERNAL_DIFF",
            "GIT_DIFF_OPTS",
        } or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return environment


def _git_bytes(root: Path, arguments: list[str], *, timeout: float = 10.0) -> bytes:
    """Run one exact read-only Git query with hooks disabled and bounded output."""

    args = tuple(arguments)
    _validate_git_arguments(args)
    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(root),
        *args,
    ]
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_git_environment(),
            )
        except OSError as exc:
            raise GitInspectionError(f"Git provenance launch failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        overflow = False
        timed_out = False
        while process.poll() is None:
            if (
                os.fstat(stdout_file.fileno()).st_size > MAX_GIT_OUTPUT_BYTES
                or os.fstat(stderr_file.fileno()).st_size > MAX_GIT_ERROR_BYTES
            ):
                overflow = True
                process.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                process.kill()
                break
            time.sleep(0.01)
        process.wait()
        if (
            os.fstat(stdout_file.fileno()).st_size > MAX_GIT_OUTPUT_BYTES
            or os.fstat(stderr_file.fileno()).st_size > MAX_GIT_ERROR_BYTES
        ):
            overflow = True
        if overflow:
            raise GitInspectionError(f"Git {' '.join(args)} output exceeded the safety limit")
        if timed_out:
            raise GitInspectionError(f"Git {' '.join(args)} timed out")
        stderr_file.seek(0)
        stderr = stderr_file.read(MAX_GIT_ERROR_BYTES).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise GitInspectionError(
                f"Git {' '.join(args)} failed with exit {process.returncode}: {stderr}"
            )
        stdout_file.seek(0)
        return stdout_file.read(MAX_GIT_OUTPUT_BYTES + 1)


def _git_text(root: Path, arguments: list[str]) -> str:
    return _git_bytes(root, arguments).decode("utf-8", errors="replace").strip()


def _empty_provenance(*, is_repository: bool, error: str | None) -> dict[str, Any]:
    return {
        "isRepository": is_repository,
        "head": None,
        "branch": None,
        "dirty": None,
        "trackedChanged": None,
        "untracked": None,
        "statusSha256": None,
        "tagsAtHead": [],
        "origin": None,
        "originIdentity": None,
        "tagCommit": None,
        "topLevel": None,
        "topLevelMatches": False,
        "objectFormat": None,
        "trackedTreeVerified": False,
        "trackedFilesChecked": 0,
        "trackedBytesChecked": 0,
        "trackedTreeSha256": None,
        "unsafeIndexFlags": [],
        "inspectionError": error,
    }


def _repository_identity(value: str | None) -> str | None:
    """Normalize only explicit GitHub HTTPS/SSH remote forms.

    A path-like or arbitrary-scheme value must never become an official GitHub
    identity merely because its text contains ``github.com``.
    """

    if not value:
        return None
    raw = value.strip().replace("\\", "/")
    scp = re.fullmatch(r"git@github\.com:([^?#]+)", raw, flags=re.IGNORECASE)
    if scp:
        path = scp.group(1)
    else:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.casefold() not in {"https", "ssh"}
            or (parsed.hostname or "").casefold() != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            return None
        if parsed.scheme.casefold() == "ssh" and parsed.username not in {None, "git"}:
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", path):
        return None
    return f"github.com/{path.casefold()}"


def _decode_git_path(raw: bytes) -> str:
    """Decode one Git path without accepting NUL or unsafe traversal."""

    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitInspectionError("Git returned a non-UTF-8 tracked path") from exc
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise GitInspectionError(f"Git returned an unsafe tracked path: {value!r}")
    return normalized


def _parse_head_tree(raw: bytes) -> dict[str, tuple[str, str]]:
    """Parse ``git ls-tree`` output as path -> (mode, object id)."""

    entries: dict[str, tuple[str, str]] = {}
    for record in (item for item in raw.split(b"\0") if item):
        try:
            header, path_raw = record.split(b"\t", 1)
            mode_raw, kind_raw, oid_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii")
            kind = kind_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitInspectionError("Git HEAD tree output is malformed") from exc
        path = _decode_git_path(path_raw)
        if path in entries:
            raise GitInspectionError(f"Git HEAD tree repeats path: {path}")
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise GitInspectionError(
                f"Unsupported tracked object at {path}: mode={mode}, type={kind}"
            )
        entries[path] = (mode, oid)
    if not entries or len(entries) > MAX_TRACKED_FILES:
        raise GitInspectionError(
            f"Tracked file count must be between 1 and {MAX_TRACKED_FILES}"
        )
    return entries


def _parse_index(raw: bytes) -> dict[str, tuple[str, str]]:
    """Parse stage-zero index entries and reject unmerged or duplicate paths."""

    entries: dict[str, tuple[str, str]] = {}
    for record in (item for item in raw.split(b"\0") if item):
        try:
            header, path_raw = record.split(b"\t", 1)
            mode_raw, oid_raw, stage_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
            stage = stage_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitInspectionError("Git index output is malformed") from exc
        path = _decode_git_path(path_raw)
        if stage != "0" or path in entries:
            raise GitInspectionError(f"Git index is unmerged or repeats path: {path}")
        entries[path] = (mode, oid)
    return entries


def _parse_index_flags(raw: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Parse ls-files flags and identify hidden-change index promises."""

    flags: dict[str, str] = {}
    unsafe: list[dict[str, str]] = []
    for record in (item for item in raw.split(b"\0") if item):
        if len(record) < 3 or record[1:2] != b" ":
            raise GitInspectionError("Git index flag output is malformed")
        try:
            flag = record[:1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise GitInspectionError("Git index flag is not ASCII") from exc
        path = _decode_git_path(record[2:])
        if path in flags:
            raise GitInspectionError(f"Git index flags repeat path: {path}")
        flags[path] = flag
        # S/s is skip-worktree; lowercase h is assume-unchanged.  Either can
        # make porcelain status omit a modified tracked file.
        if flag in {"S", "s", "h"}:
            unsafe.append({"path": path, "flag": flag})
    return flags, unsafe


def _git_blob_oid(path: Path, mode: str, object_format: str) -> tuple[str, int]:
    """Hash one worktree entry exactly as a Git blob, without following links."""

    if object_format not in {"sha1", "sha256"}:
        raise GitInspectionError(f"Unsupported Git object format: {object_format}")
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        if mode != "120000":
            raise GitInspectionError(f"Unexpected symbolic link in tracked path: {path.name}")
        payload = os.readlink(path).encode("utf-8")
        if len(payload) > MAX_TRACKED_FILE_BYTES:
            raise GitInspectionError(f"Tracked symbolic link exceeds size limit: {path.name}")
        digest = hashlib.new(object_format)
        digest.update(f"blob {len(payload)}\0".encode("ascii"))
        digest.update(payload)
        if path.lstat() != before or os.readlink(path).encode("utf-8") != payload:
            raise GitInspectionError(f"Tracked symbolic link changed during inspection: {path.name}")
        return digest.hexdigest(), len(payload)
    if not stat.S_ISREG(before.st_mode):
        raise GitInspectionError(f"Tracked path is not a regular file: {path.name}")
    if before.st_size > MAX_TRACKED_FILE_BYTES:
        raise GitInspectionError(f"Tracked file exceeds size limit: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitInspectionError(f"Unable to open tracked file safely: {path.name}: {exc}") from exc
    digest = hashlib.new(object_format)
    digest.update(f"blob {before.st_size}\0".encode("ascii"))
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != before.st_size:
            raise GitInspectionError(f"Tracked file changed before inspection: {path.name}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TRACKED_FILE_BYTES:
                raise GitInspectionError(f"Tracked file exceeded size limit: {path.name}")
            digest.update(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if total != before.st_size or any(
        getattr(before, key) != getattr(after_open, key)
        or getattr(before, key) != getattr(after_path, key)
        for key in stable_fields
    ):
        raise GitInspectionError(f"Tracked file changed during inspection: {path.name}")
    return digest.hexdigest(), total


def _verify_tracked_tree(root: Path, object_format: str) -> dict[str, Any]:
    """Verify index and every tracked worktree byte against the exact HEAD tree."""

    head = _parse_head_tree(_git_bytes(root, ["ls-tree", "-r", "-z", "--full-tree", "HEAD"]))
    index = _parse_index(_git_bytes(root, ["ls-files", "-z", "--stage"]))
    flags, unsafe = _parse_index_flags(_git_bytes(root, ["ls-files", "-z", "-v"]))
    if head != index or set(flags) != set(head):
        return {
            "verified": False,
            "files": 0,
            "bytes": 0,
            "digest": None,
            "unsafe": unsafe,
            "reason": "HEAD, index, and tracked path sets do not match",
        }
    if unsafe:
        return {
            "verified": False,
            "files": 0,
            "bytes": 0,
            "digest": None,
            "unsafe": unsafe,
            "reason": "skip-worktree or assume-unchanged index flags are present",
        }
    root_resolved = root.resolve(strict=True)
    total = 0
    tree_digest = hashlib.sha256()
    for relative in sorted(head):
        mode, expected_oid = head[relative]
        candidate = root_resolved.joinpath(*relative.split("/"))
        if mode != "120000":
            try:
                candidate.resolve(strict=True).relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise GitInspectionError(f"Tracked path escapes or is missing: {relative}") from exc
        observed_oid, size = _git_blob_oid(candidate, mode, object_format)
        total += size
        if total > MAX_TRACKED_BYTES:
            raise GitInspectionError("Tracked source exceeds the total byte safety limit")
        if observed_oid != expected_oid:
            return {
                "verified": False,
                "files": 0,
                "bytes": total,
                "digest": None,
                "unsafe": unsafe,
                "reason": f"tracked bytes differ from HEAD: {relative}",
            }
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(mode.encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(expected_oid.encode("ascii"))
        tree_digest.update(b"\n")
    return {
        "verified": True,
        "files": len(head),
        "bytes": total,
        "digest": tree_digest.hexdigest(),
        "unsafe": [],
        "reason": None,
    }


def _git_provenance(root: Path) -> dict[str, Any]:
    """Record immutable Git identity and dirty state without changing the checkout."""

    try:
        if _git_text(root, ["rev-parse", "--is-inside-work-tree"]) != "true":
            return _empty_provenance(is_repository=False, error=None)
        top_level_text = _git_text(root, ["rev-parse", "--show-toplevel"])
        top_level_matches = os.path.normcase(str(Path(top_level_text).resolve())) == os.path.normcase(
            str(root.resolve())
        )
        object_format = _git_text(root, ["rev-parse", "--show-object-format"])
        status_raw = _git_bytes(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
        tokens = [token for token in status_raw.split(b"\0") if token]
        records: list[bytes] = []
        index = 0
        while index < len(tokens):
            record = tokens[index]
            if len(record) < 3 or record[2:3] != b" ":
                raise GitInspectionError("Git status returned an invalid porcelain record")
            records.append(record)
            status_code = record[:2]
            index += 2 if (b"R" in status_code or b"C" in status_code) else 1
            if index > len(tokens):
                raise GitInspectionError("Git status rename record is incomplete")
        tracked_changed = sum(not record.startswith(b"??") for record in records)
        untracked = sum(record.startswith(b"??") for record in records)
        head = _git_text(root, ["rev-parse", "HEAD"])
        tracked_tree = (
            _verify_tracked_tree(root, object_format)
            if top_level_matches
            else {
                "verified": False,
                "files": 0,
                "bytes": 0,
                "digest": None,
                "unsafe": [],
                "reason": "the supplied DSH root is not the Git top-level directory",
            }
        )
        if _git_text(root, ["rev-parse", "HEAD"]) != head or _git_bytes(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        ) != status_raw:
            raise GitInspectionError("Git HEAD or status changed during provenance inspection")
        branch = _git_text(root, ["branch", "--show-current"]) or None
        tags = [
            value
            for value in _git_text(root, ["tag", "--points-at", "HEAD"]).splitlines()
            if value
        ]
    except (GitInspectionError, UnicodeError, RuntimeError) as exc:
        return _empty_provenance(is_repository=True, error=str(exc))
    try:
        raw_origin = _git_text(root, ["remote", "get-url", "origin"]) or None
    except GitInspectionError:
        raw_origin = None
    origin_identity = _repository_identity(raw_origin)
    origin = raw_origin
    if origin:
        origin = re.sub(r"(?<=://)[^/@\s]+@", "<redacted>@", origin)
    try:
        tag_commit = _git_text(
            root,
            ["rev-parse", f"refs/tags/{TESTED_DSH_TAG}^{{commit}}"],
        ) or None
    except GitInspectionError:
        tag_commit = None
    return {
        "isRepository": True,
        "head": head,
        "branch": branch,
        "dirty": bool(records),
        "trackedChanged": tracked_changed,
        "untracked": untracked,
        "statusSha256": hashlib.sha256(status_raw).hexdigest(),
        "tagsAtHead": sorted(tags),
        "origin": origin,
        "originIdentity": origin_identity,
        "tagCommit": tag_commit,
        "topLevel": "<DSH_ROOT>" if top_level_matches else "<OTHER_GIT_TOPLEVEL>",
        "topLevelMatches": top_level_matches,
        "objectFormat": object_format,
        "trackedTreeVerified": tracked_tree["verified"],
        "trackedFilesChecked": tracked_tree["files"],
        "trackedBytesChecked": tracked_tree["bytes"],
        "trackedTreeSha256": tracked_tree["digest"],
        "trackedTreeReason": tracked_tree["reason"],
        "unsafeIndexFlags": tracked_tree["unsafe"],
        "inspectionError": None,
    }


def _provenance_verified(git: dict[str, Any]) -> bool:
    """Return true only for the exact clean official rc8 source boundary."""

    return bool(
        git.get("isRepository")
        and git.get("dirty") is False
        and git.get("topLevelMatches") is True
        and git.get("trackedTreeVerified") is True
        and not git.get("unsafeIndexFlags")
        and git.get("originIdentity") == OFFICIAL_DSH_REPOSITORY_IDENTITY
        and git.get("head") == TESTED_DSH_COMMIT
        and git.get("tagCommit") == TESTED_DSH_COMMIT
        and TESTED_DSH_TAG in git.get("tagsAtHead", [])
    )


def _provenance_limitations(git: dict[str, Any]) -> list[str]:
    """Explain when a successful runtime probe is not an immutable source proof."""

    if not git.get("isRepository"):
        return ["The DSH directory is not a Git checkout; commit provenance is unavailable."]
    limitations: list[str] = []
    if git.get("inspectionError"):
        limitations.append(f"Git provenance inspection failed closed: {git['inspectionError']}")
    if git.get("dirty"):
        limitations.append(
            "The DSH checkout is dirty; PASS covers the observed working tree, not an immutable reproduction of the tag alone."
        )
    if not git.get("topLevelMatches"):
        limitations.append("The supplied DSH directory is not the repository top-level directory.")
    if not git.get("trackedTreeVerified"):
        reason = git.get("trackedTreeReason") or "tracked files were not byte-verified against HEAD"
        limitations.append(f"The complete tracked DSH tree is unverified: {reason}.")
    if git.get("unsafeIndexFlags"):
        limitations.append(
            "The Git index contains skip-worktree or assume-unchanged flags that can hide tracked modifications."
        )
    if git.get("originIdentity") != OFFICIAL_DSH_REPOSITORY_IDENTITY:
        limitations.append("The DSH origin is not the official deepseek-ai/deepseek-harness repository.")
    if git.get("head") != TESTED_DSH_COMMIT:
        limitations.append(f"DSH HEAD is not the fixed rc8 commit {TESTED_DSH_COMMIT}.")
    if git.get("tagCommit") != TESTED_DSH_COMMIT or TESTED_DSH_TAG not in git.get("tagsAtHead", []):
        limitations.append(f"Tag {TESTED_DSH_TAG} does not resolve to and point at the fixed rc8 commit.")
    return limitations


def diagnose(dsh_root: Path, *, live: bool = True) -> tuple[dict[str, Any], int]:
    root = dsh_root.expanduser().resolve()
    git = _git_provenance(root)
    provenance_verified = _provenance_verified(git)
    if provenance_verified:
        inspection = inspect_external_dsh(root, run_config_dump=live)
    else:
        inspection = {
            "version": None,
            "observedCliVersion": None,
            "nodeVersion": None,
            "pnpmVersion": None,
            "capabilities": {},
            "configDump": False,
            "runtimeProbe": "skipped-unverified-provenance",
        }
    runtime_probed = provenance_verified
    checks = [
        {"id": "external-boundary", "status": "pass", "detail": "DSH is referenced externally and is not copied by the Builder."},
        {
            "id": "manifest-version",
            "status": "pass" if runtime_probed else "partial",
            "detail": (
                f"DSH {inspection['version']} matches the tested boundary."
                if runtime_probed
                else "Skipped until exact official Git provenance is verified."
            ),
        },
        {
            "id": "license",
            "status": "pass" if runtime_probed else "partial",
            "detail": "MIT metadata and license file are present." if runtime_probed else "Not inspected before provenance verification.",
        },
        {"id": "node", "status": "pass" if runtime_probed else "partial", "detail": inspection["nodeVersion"] or "not probed"},
        {"id": "pnpm", "status": "pass" if runtime_probed else "partial", "detail": inspection["pnpmVersion"] or "not probed"},
        {
            "id": "config-dump",
            "status": "pass" if live and runtime_probed else "partial",
            "detail": (
                "Agent loop, session, model, tools, approval, and Web markers were resolved."
                if live and runtime_probed
                else "DSH CLI was not invoked."
            ),
        },
        {
            "id": "git-provenance",
            "status": "pass" if provenance_verified else "partial",
            "detail": (
                f"official rc8 commit {git['head'][:12]}, tag={TESTED_DSH_TAG}, dirty={git['dirty']}."
                if provenance_verified
                else f"Repository provenance is not the exact clean official rc8 boundary; observed HEAD={git.get('head')}."
            ),
        },
    ]
    status = "PASS" if live and provenance_verified else "PARTIAL"
    limitations = _provenance_limitations(git)
    limitations.append(
        "Git provenance covers the tracked official source boundary; ignored node_modules and build outputs are observed runtime dependencies, not proven pristine by Git status."
    )
    if not live:
        limitations.append("Static mode does not prove that the DSH CLI can compose the Web Profile.")
    report = {
        "schema": "agent-workbench-dsh-doctor/v2",
        "status": status,
        "externalDependency": {
            "name": "DeepSeek Harness",
            "officialRepository": OFFICIAL_DSH_REPOSITORY,
            "testedVersion": TESTED_DSH_VERSION,
            "testedTag": TESTED_DSH_TAG,
            "testedCommit": TESTED_DSH_COMMIT,
            "bundled": False,
            "downloadedByBuilder": False,
        },
        "observed": inspection,
        "sourceDigest": git.get("trackedTreeSha256") if provenance_verified else None,
        "git": git,
        "checks": checks,
        "limitations": limitations,
    }
    return report, 0 if status == "PASS" else 2


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsh-root", required=True, type=Path, help="existing external DSH checkout")
    parser.add_argument("--static", action="store_true", help="inspect files only; returns PARTIAL")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, code = diagnose(args.dsh_root, live=not args.static)
    except (OSError, DshRuntimeError, json.JSONDecodeError) as exc:
        message = str(exc).replace(str(args.dsh_root.expanduser().resolve()), "<DSH_ROOT>")
        report = {
            "schema": "agent-workbench-dsh-doctor/v2",
            "status": "FAIL",
            "externalDependency": {
                "name": "DeepSeek Harness",
                "officialRepository": OFFICIAL_DSH_REPOSITORY,
                "testedVersion": TESTED_DSH_VERSION,
                "testedTag": TESTED_DSH_TAG,
                "testedCommit": TESTED_DSH_COMMIT,
                "bundled": False,
                "downloadedByBuilder": False,
            },
            "error": {"code": "DSH_DOCTOR_FAILED", "message": message},
        }
        code = 3
    if args.output:
        _atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
