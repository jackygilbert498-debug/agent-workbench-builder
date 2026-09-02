#!/usr/bin/env python3
"""Launch this Product Bundle on DSH, including safe Windows path staging."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterator, Optional, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from dsh_runtime import (  # noqa: E402
    DshRuntimeError,
    _included_product_files,
    _path_needs_windows_stage,
    _run_dsh,
    _safe_environment,
    _tree_digest,
    find_compatible_node,
    find_pnpm,
    stage_product_bundle,
    validate_dsh_root,
)


STAGE_ENVIRONMENT = "AGENT_WORKBENCH_STAGE_ROOT"


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _assert_existing_components_are_plain(path: Path) -> None:
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and _is_link_or_junction(current):
            raise DshRuntimeError(f"runtime stage path crosses a link or junction: {current}")


def resolve_stage_base(explicit: Optional[Path] = None) -> Path:
    """Resolve an owned staging base that rc8 can pass through Windows shells."""

    configured = explicit
    if configured is None and os.environ.get(STAGE_ENVIRONMENT):
        configured = Path(os.environ[STAGE_ENVIRONMENT])
    lexical_base = Path(
        os.path.abspath((configured or Path(tempfile.gettempdir())).expanduser())
    )
    _assert_existing_components_are_plain(lexical_base)
    base = lexical_base.resolve()
    if os.name == "nt" and (" " in str(base) or not str(base).isascii()):
        raise DshRuntimeError(
            f"Windows staging base is not shell-safe; set {STAGE_ENVIRONMENT} to an owned ASCII path without spaces, for example C:\\awb-runtime"
        )
    base.mkdir(parents=True, exist_ok=True)
    _assert_existing_components_are_plain(base)
    if not base.is_dir():
        raise DshRuntimeError("runtime staging base is not a directory")
    return base


def _managed_root(project_root: Path, stage_base: Path) -> Path:
    identity = str(project_root.resolve())
    if os.name == "nt":
        identity = identity.casefold()
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    lexical_base = Path(os.path.abspath(stage_base))
    _assert_existing_components_are_plain(lexical_base)
    resolved_base = lexical_base.resolve()
    parent = lexical_base / "agent-workbench-stages"
    _assert_existing_components_are_plain(parent)
    parent.mkdir(parents=True, exist_ok=True)
    _assert_existing_components_are_plain(parent)
    target = parent / key
    _assert_existing_components_are_plain(target)
    try:
        target.resolve(strict=False).relative_to(resolved_base)
    except ValueError as exc:
        raise DshRuntimeError("managed runtime stage escaped its configured base") from exc
    return target


@contextmanager
def exclusive_runtime_lock(path: Path) -> Iterator[None]:
    """Hold one OS advisory lock for the product's managed stage lifetime."""

    _assert_existing_components_are_plain(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_existing_components_are_plain(path)
    if os.path.lexists(path):
        if _is_link_or_junction(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise DshRuntimeError("runtime lock must be a regular file, not a link or junction")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DshRuntimeError("could not safely open the managed runtime lock") from exc
    handle = os.fdopen(descriptor, "r+b")
    try:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise DshRuntimeError("runtime lock is not a regular file")
        _assert_existing_components_are_plain(path)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DshRuntimeError(
                "another DSH launcher is already using this Product Bundle; stop it before starting a second instance"
            ) from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def prepare_managed_product(project_root: Path, stage_base: Path) -> tuple[Path, dict[str, object]]:
    """Synchronize one code-only stage while keeping business state in the source project."""

    project = project_root.expanduser().resolve()
    base = resolve_stage_base(stage_base)
    managed = _managed_root(project, base)
    managed.mkdir(parents=True, exist_ok=True)
    _assert_existing_components_are_plain(managed)
    target = managed / "product"
    if target.exists() and _is_link_or_junction(target):
        raise DshRuntimeError("managed runtime product is a link or junction")

    source_files = _included_product_files(project)
    source_digest = _tree_digest(project, source_files)
    if target.is_dir():
        staged_files = _included_product_files(target)
        if _tree_digest(target, staged_files) == source_digest:
            return target, {
                "used": True,
                "persistent": True,
                "reused": True,
                "files": len(source_files),
                "sourceTreeSha256": source_digest,
                "stagedTreeSha256": source_digest,
                "stateRoot": "original-project",
            }

    transaction = managed / f"next-{uuid.uuid4().hex}"
    backup = managed / f"previous-{uuid.uuid4().hex}"
    staged, receipt = stage_product_bundle(project, transaction)
    moved_previous = False
    try:
        if target.exists():
            target.replace(backup)
            moved_previous = True
        staged.replace(target)
    except Exception:
        if moved_previous and not target.exists() and backup.exists():
            backup.replace(target)
        raise
    finally:
        if transaction.exists():
            shutil.rmtree(transaction)
    if backup.exists():
        shutil.rmtree(backup)
    return target, {
        **receipt,
        "persistent": True,
        "reused": False,
        "stateRoot": "original-project",
    }


def _prepare_runtime_home(project_root: Path) -> Path:
    """Create the project-local DSH home without following pre-existing links."""

    project = project_root.expanduser().resolve()
    runtime_root = project / ".runtime"
    dsh_home = runtime_root / "dsh-home"
    for directory in (runtime_root, dsh_home):
        _assert_existing_components_are_plain(directory)
        if os.path.lexists(directory):
            if _is_link_or_junction(directory) or not stat.S_ISDIR(directory.lstat().st_mode):
                raise DshRuntimeError(
                    f"runtime home component must be a plain directory, not a link or junction: {directory}"
                )
        else:
            directory.mkdir()
        _assert_existing_components_are_plain(directory)
        if not directory.is_dir():
            raise DshRuntimeError("runtime home component is not a directory")
    return dsh_home


def launch(dsh_root: Path, dsh_arguments: Sequence[str], *, stage_base: Optional[Path] = None) -> int:
    """Register this Bundle and run one foreground DSH web command."""

    root = dsh_root.expanduser().resolve()
    validate_dsh_root(root)
    node, _ = find_compatible_node()
    pnpm, _ = find_pnpm(project_directory=root)
    arguments = list(dsh_arguments) or ["web", "--no-open"]
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    if not arguments or arguments[0] != "web":
        raise DshRuntimeError("the product launcher accepts only the DSH web command")

    runtime_project = PROJECT_ROOT
    lock_context = nullcontext()
    if _path_needs_windows_stage(PROJECT_ROOT):
        base = resolve_stage_base(stage_base)
        managed = _managed_root(PROJECT_ROOT, base)
        lock_context = exclusive_runtime_lock(managed / "runtime.lock")

    with lock_context:
        if _path_needs_windows_stage(PROJECT_ROOT):
            runtime_project, _ = prepare_managed_product(PROJECT_ROOT, base)
        dsh_home = _prepare_runtime_home(PROJECT_ROOT)
        product_environment = {"AGENT_WORKBENCH_PRODUCT_ROOT": str(PROJECT_ROOT)}
        _run_dsh(
            root,
            pnpm,
            node,
            dsh_home,
            ["plugin", "--profile", "web", "add", str(runtime_project)],
            cwd=runtime_project,
            timeout=120,
            extra_environment=product_environment,
        )
        environment = _safe_environment(node, dsh_home)
        environment.update(product_environment)
        completed = subprocess.run(
            [str(pnpm), "--dir", str(root), "dsh", *arguments],
            cwd=runtime_project,
            env=environment,
            check=False,
        )
        return completed.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsh-root", required=True, type=Path)
    parser.add_argument("--stage-root", type=Path, help=f"override {STAGE_ENVIRONMENT}")
    parser.add_argument("dsh_arguments", nargs=argparse.REMAINDER, help="web arguments; defaults to: web --no-open")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return launch(args.dsh_root, args.dsh_arguments, stage_base=args.stage_root)
    except (OSError, subprocess.TimeoutExpired, DshRuntimeError) as exc:
        message = str(exc).replace(str(PROJECT_ROOT), "<PROJECT_ROOT>")
        message = message.replace(str(args.dsh_root.expanduser().resolve()), "<DSH_ROOT>")
        print(f"DSH launcher failed: {message}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
