"""Atomic JSON persistence helpers and idempotency ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


MAX_JSON_BYTES = 1024 * 1024


class UnsafePathError(OSError):
    """A persistence path contains a symbolic link or Windows junction."""


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def assert_no_link_components(path: Path) -> None:
    """Reject existing link/reparse components before reading or writing state."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            continue
        if _is_link_or_junction(current):
            raise UnsafePathError(f"linked path component is not accepted: {current.name}")
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            raise UnsafePathError(f"cannot inspect persistence path: {current.name}") from exc
        if current != absolute and not stat.S_ISDIR(mode):
            raise UnsafePathError(f"non-directory path component is not accepted: {current.name}")


def json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _read_bounded(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    assert_no_link_components(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise OSError(f"JSON state exceeds {max_bytes} bytes or is not a regular file")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise OSError(f"JSON state exceeds {max_bytes} bytes while reading")
    return raw


def read_json(path: Path, *, default: Any = None, max_bytes: int = MAX_JSON_BYTES) -> Any:
    assert_no_link_components(path)
    if not path.exists():
        return default
    return json.loads(_read_bounded(path, max_bytes=max_bytes).decode("utf-8-sig"))


def create_json_once(path: Path, payload: Any, *, max_bytes: int = MAX_JSON_BYTES) -> bool:
    """Atomically publish one immutable JSON record; identical replay is allowed."""

    data = json_bytes(payload)
    if len(data) > max_bytes:
        raise OSError(f"JSON record exceeds {max_bytes} bytes")
    assert_no_link_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_link_components(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            return True
        except FileExistsError:
            assert_no_link_components(path)
            if _read_bounded(path, max_bytes=max_bytes) == data:
                return False
            raise
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    assert_no_link_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_link_components(path.parent)
    if os.path.lexists(path):
        assert_no_link_components(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json_bytes(payload).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
