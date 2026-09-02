"""Agent orchestration, approval, idempotency, and structured recovery."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from .domain import ReferenceProvider
from .store import (
    MAX_JSON_BYTES,
    UnsafePathError,
    assert_no_link_components,
    atomic_write_json,
    create_json_once,
    json_bytes,
    read_json,
)


REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
WINDOWS_DEVICE_RE = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02
MAX_LEDGER_ENTRIES = 10_000


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _hash_file(path: Path) -> str:
    assert_no_link_components(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and REQUEST_ID_RE.fullmatch(value) is not None
        and WINDOWS_DEVICE_RE.match(value) is None
    )


def _validate_request(request: Any) -> tuple[str, str]:
    if not isinstance(request, dict):
        raise AgentError("INVALID_REQUEST", "request must be a JSON object", "Provide request_id and content.")
    request_id = request.get("request_id")
    content = request.get("content")
    if not _safe_filename_token(request_id):
        raise AgentError(
            "INVALID_REQUEST_ID",
            "request_id must be 1-80 safe filename characters",
            "Use letters, digits, dot, underscore, or hyphen.",
        )
    if not isinstance(content, str) or not content.strip():
        raise AgentError("INVALID_REQUEST", "content must be non-empty text", "Provide the work request as content.")
    if len(content) > 20_000:
        raise AgentError("REQUEST_TOO_LARGE", "content exceeds 20000 characters", "Split the request before retrying.")
    return request_id, content


def _write_receipt(receipt_dir: Path, run_id: str, payload: dict[str, Any]) -> None:
    if not _safe_filename_token(run_id):
        raise AgentError("INVALID_RUN_ID", "run_id is not safe", "Use letters, digits, dot, underscore, or hyphen.")
    try:
        create_json_once(receipt_dir / f"{run_id}.json", payload)
    except FileExistsError as exc:
        raise AgentError(
            "RECEIPT_CONFLICT",
            "run_id already belongs to a different immutable receipt",
            "Use a new run_id; inspect the existing receipt and never overwrite it.",
        ) from exc
    except UnsafePathError as exc:
        raise AgentError(
            "UNSAFE_PATH",
            str(exc),
            "Use a work directory whose state, output, and receipt paths contain no links or junctions.",
        ) from exc


def _platform_lock(handle: Any, *, unlock: bool = False) -> None:
    """Acquire or release one byte using the host OS advisory-lock primitive."""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    import fcntl

    mode = fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(handle.fileno(), mode)


@contextmanager
def _idempotency_lock(state_dir: Path, idempotency_key: str):
    """Serialize one idempotency key across threads and processes."""

    lock_dir = state_dir / ".locks"
    assert_no_link_components(lock_dir.parent)
    lock_dir.mkdir(parents=True, exist_ok=True)
    assert_no_link_components(lock_dir)
    lock_path = lock_dir / f"{idempotency_key}.lock"
    if os.path.lexists(lock_path):
        assert_no_link_components(lock_path)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            os.fsync(handle.fileno())
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _platform_lock(handle)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise AgentError(
                        "IDEMPOTENCY_BUSY",
                        "another worker still owns this idempotency key",
                        "Wait for that worker to finish, then retry with the same request.",
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            _platform_lock(handle, unlock=True)
        handle.close()


@contextmanager
def _ledger_transaction_lock(state_dir: Path):
    """Serialize read/modify/write of the shared ledger across distinct keys."""

    with _idempotency_lock(state_dir, "__shared-ledger-transaction__"):
        yield


def _run_approved_locked(
    *,
    request_id: str,
    plan: dict[str, Any],
    outcome_hash: str,
    idempotency_key: str,
    base: dict[str, Any],
    run_id: str,
    state_dir: Path,
    output_dir: Path,
    receipt_dir: Path,
) -> dict[str, Any]:
    """Read, check, and update the ledger while its per-key lock is held."""

    ledger_path = state_dir / "idempotency-ledger.json"
    try:
        ledger = read_json(ledger_path, default={"schema": "agent-workbench-ledger/v1", "entries": {}})
    except UnsafePathError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError("LEDGER_UNREADABLE", str(exc), "Restore the ledger from backup or inspect it before retrying.") from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries"), dict):
        raise AgentError("LEDGER_INVALID", "idempotency ledger has an invalid schema", "Repair or restore the ledger before retrying.")
    if len(ledger["entries"]) > MAX_LEDGER_ENTRIES:
        raise AgentError(
            "LEDGER_LIMIT",
            f"idempotency ledger exceeds {MAX_LEDGER_ENTRIES} entries",
            "Archive the current work root and start a reviewed new ledger; do not truncate it in place.",
        )

    existing = ledger["entries"].get(idempotency_key)
    artifact_name = f"{request_id}.json"
    artifact_path = output_dir / artifact_name
    if existing is not None:
        expected = existing.get("artifactSha256") if isinstance(existing, dict) else None
        if (
            not isinstance(existing, dict)
            or existing.get("outcomeHash") != outcome_hash
            or existing.get("artifact") != artifact_name
            or not artifact_path.is_file()
            or _hash_file(artifact_path) != expected
        ):
            raise AgentError(
                "IDEMPOTENCY_CONFLICT",
                "ledger entry and existing artifact do not match",
                "Inspect the ledger and artifact; do not overwrite either automatically.",
            )
        receipt = {
            **base,
            "status": "replayed",
            "sideEffectWritten": False,
            "artifact": f"output/{artifact_name}",
            "artifactSha256": expected,
        }
        _write_receipt(receipt_dir, run_id, receipt)
        return receipt

    if os.path.lexists(artifact_path):
        assert_no_link_components(artifact_path)
        raise AgentError(
            "IDEMPOTENCY_CONFLICT",
            "an untracked artifact already exists",
            "Inspect and reconcile the artifact before retrying; it will not be overwritten.",
        )
    artifact = {
        "schema": "agent-workbench-output/v1",
        "requestId": request_id,
        "outcomeHash": outcome_hash,
        "plan": plan,
    }
    artifact_hash = hashlib.sha256(json_bytes(artifact)).hexdigest()
    ledger["entries"][idempotency_key] = {
        "requestId": request_id,
        "outcomeHash": outcome_hash,
        "artifact": artifact_name,
        "artifactSha256": artifact_hash,
    }
    if len(ledger["entries"]) > MAX_LEDGER_ENTRIES or len(json_bytes(ledger)) > MAX_JSON_BYTES:
        del ledger["entries"][idempotency_key]
        raise AgentError(
            "LEDGER_LIMIT",
            "idempotency ledger reached its bounded storage limit",
            "Archive the current work root and start a reviewed new ledger; do not overwrite prior audit entries.",
        )
    atomic_write_json(artifact_path, artifact)
    if _hash_file(artifact_path) != artifact_hash:
        raise AgentError(
            "ARTIFACT_WRITE_FAILED",
            "new artifact bytes do not match their planned hash",
            "Quarantine the work root and inspect storage before retrying.",
        )
    try:
        atomic_write_json(ledger_path, ledger)
    except UnsafePathError:
        raise
    except OSError as exc:
        quarantine_dir = state_dir / "quarantine"
        assert_no_link_components(quarantine_dir.parent)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        assert_no_link_components(quarantine_dir)
        quarantine_path = quarantine_dir / f"{artifact_name}.{artifact_hash}"
        os.replace(artifact_path, quarantine_path)
        raise AgentError(
            "LEDGER_WRITE_FAILED",
            str(exc),
            f"The uncommitted artifact was quarantined as {quarantine_path.name}; repair ledger storage before retrying.",
        ) from exc
    receipt = {
        **base,
        "status": "committed",
        "sideEffectWritten": True,
        "artifact": f"output/{artifact_name}",
        "artifactSha256": artifact_hash,
    }
    _write_receipt(receipt_dir, run_id, receipt)
    return receipt


def run_agent(
    request: Any,
    *,
    approved: bool = False,
    run_id: str,
    state_dir: Path,
    output_dir: Path,
    receipt_dir: Path,
    provider: ReferenceProvider | None = None,
) -> dict[str, Any]:
    request_id, content = _validate_request(request)
    if not _safe_filename_token(run_id):
        raise AgentError("INVALID_RUN_ID", "run_id is not safe", "Use letters, digits, dot, underscore, or hyphen.")
    provider = provider or ReferenceProvider()
    plan = provider.build_plan(request_id, content)
    outcome_hash = _hash_payload(plan)
    idempotency_key = _hash_payload({"requestId": request_id, "content": content})

    base = {
        "schema": "agent-workbench-run-receipt/v1",
        "runId": run_id,
        "requestId": request_id,
        "idempotencyKey": idempotency_key,
        "outcomeHash": outcome_hash,
        "provider": provider.name,
    }
    try:
        receipt_lock = f"receipt-{_hash_payload({'runId': run_id})}"
        with _idempotency_lock(state_dir, receipt_lock):
            receipt_path = receipt_dir / f"{run_id}.json"
            if approved and os.path.lexists(receipt_path):
                assert_no_link_components(receipt_path)
                raise AgentError(
                    "RECEIPT_CONFLICT",
                    "run_id already belongs to an immutable receipt",
                    "Use a new run_id; inspect the existing receipt and never overwrite it.",
                )
            if not approved:
                receipt = {
                    **base,
                    "status": "denied",
                    "sideEffectWritten": False,
                    "artifact": None,
                }
                _write_receipt(receipt_dir, run_id, receipt)
                return receipt
            with _idempotency_lock(state_dir, idempotency_key):
                with _ledger_transaction_lock(state_dir):
                    return _run_approved_locked(
                        request_id=request_id,
                        plan=plan,
                        outcome_hash=outcome_hash,
                        idempotency_key=idempotency_key,
                        base=base,
                        run_id=run_id,
                        state_dir=state_dir,
                        output_dir=output_dir,
                        receipt_dir=receipt_dir,
                    )
    except UnsafePathError as exc:
        raise AgentError(
            "UNSAFE_PATH",
            str(exc),
            "Use a work directory whose state, output, and receipt paths contain no links or junctions.",
        ) from exc
