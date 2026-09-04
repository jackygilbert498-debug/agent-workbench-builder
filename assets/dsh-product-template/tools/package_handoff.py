#!/usr/bin/env python3
"""Build a deterministic handoff ZIP while keeping external DSH outside it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Iterable, Optional, Sequence
import unicodedata
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_MEMBER_COUNT = 5000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_CONTRACT_BYTES = 1024 * 1024
MAX_SIDECAR_BYTES = 4096
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
WINDOWS_DEVICE_RE = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"\\|?*')
SLUG_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
SECRET_PATTERNS = (
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
)
ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z]:(?:/+|\\+)[^\s\"'<>|]+"),
    re.compile(
        r"(?<!\\)\\{2,}[A-Za-z0-9._-]+\\+[A-Za-z0-9.$_-]+"
        r"(?:\\+[^\\\s\"'<>|]+)*"
    ),
    re.compile(
        r"(?i)(?<![:\w])/(?:Users|home|opt|var|tmp|etc|usr|private|Volumes|Applications|srv|mnt)/"
        r"[^\s\"'`<>]+"
    ),
)
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
BUILDER_VERSION = "4.0.2"
BUILDER_RELEASE_TAG = "v4.0.2"
BUILDER_PUBLIC_URL = "https://github.com/jackygilbert498-debug/agent-workbench-builder"


class PackagePathError(RuntimeError):
    """The final artifact path exceeds the supported Windows filename boundary."""

    code = "PACKAGE_PATH_TOO_LONG"


def _check_output_path(path: Path) -> None:
    """Reject an unsupported destination before creating a partial handoff."""
    if os.name == "nt" and len(str(path).encode("utf-16-le")) // 2 >= 260:
        raise PackagePathError(
            "PACKAGE_PATH_TOO_LONG: use a shorter project directory or project name "
            "and retry; the final Windows artifact path must be under 260 UTF-16 units."
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    _assert_no_link_components(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded_regular(path: Path, maximum: int, label: str) -> bytes:
    _assert_no_link_components(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise RuntimeError(f"{label} exceeds its limit or is not a regular file")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise RuntimeError(f"{label} exceeds its limit while reading")
    return raw


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and "\x00" not in value
        and ":" not in value
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
        and all(_portable_component(part) for part in path.parts)
    )


def _windows_path_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFKC", part).casefold()
        for part in PurePosixPath(value).parts
    )


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
        raise RuntimeError("archive member resembles malformed UTF-16 text")
    return views


def _excluded(relative: Path) -> bool:
    parts = relative.parts
    if (
        parts[0] in {".git", ".runtime", "_handoff", "dist", "node_modules", "work", "__pycache__"}
        or any(part in {".git", "node_modules", "__pycache__"} for part in parts)
    ):
        return True
    if relative.suffix in {".pyc", ".pyo"} or relative.name == ".DS_Store":
        return True
    return relative.as_posix() in {"evidence/graduation.json", "evidence/handoff.json"}


def _assert_no_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            continue
        is_junction = getattr(os.path, "isjunction", None)
        if current.is_symlink() or bool(is_junction and is_junction(current)):
            raise RuntimeError(f"linked output path is not accepted: {current.name}")


def _source_files(excluded_output: Path) -> Iterable[tuple[str, Path]]:
    def walk(directory: Path) -> Iterable[tuple[str, Path]]:
        _assert_no_link_components(directory)
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            path = Path(entry.path)
            if path == excluded_output or excluded_output in path.parents:
                continue
            relative = path.relative_to(PROJECT_ROOT)
            if _excluded(relative):
                continue
            is_junction = getattr(os.path, "isjunction", None)
            if entry.is_symlink() or bool(is_junction and is_junction(path)):
                raise RuntimeError(f"refusing linked source path: {relative.as_posix()}")
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
            elif entry.is_file(follow_symlinks=False):
                _assert_no_link_components(path)
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise RuntimeError(f"file exceeds package limit: {relative.as_posix()}")
                yield relative.as_posix(), path
            else:
                raise RuntimeError(f"refusing special source path: {relative.as_posix()}")

    yield from walk(PROJECT_ROOT)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.create_system = 3
    return info


def _write_file_member(archive: zipfile.ZipFile, name: str, path: Path) -> None:
    _assert_no_link_components(path)
    with path.open("rb") as source, archive.open(_zip_info(name), "w") as destination:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            destination.write(chunk)


def _hash_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, str]:
    if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
        raise RuntimeError(f"archive member exceeds file limit: {info.filename}")
    observed = 0
    digest = hashlib.sha256()
    content = bytearray()
    with archive.open(info) as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > MAX_FILE_BYTES:
                raise RuntimeError(f"archive member exceeds file limit: {info.filename}")
            digest.update(chunk)
            content.extend(chunk)
    if observed != info.file_size:
        raise RuntimeError(f"archive member size mismatch: {info.filename}")
    if _sensitive_filename(info.filename):
        raise RuntimeError(f"archive member has a sensitive filename: {info.filename}")
    for text in _text_views(bytes(content)):
        text = text.replace("/usr/bin/env", "<PORTABLE_INTERPRETER>")
        for example in PORTABLE_PATH_EXAMPLES.get(info.filename, ()):
            text = text.replace(example, "<PORTABLE_PATH_EXAMPLE>")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise RuntimeError(f"archive member contains a secret-like value: {info.filename}")
        if any(pattern.search(text) for pattern in ABSOLUTE_PATH_PATTERNS):
            raise RuntimeError(f"archive member contains a machine-absolute path: {info.filename}")
    return observed, digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path = Path(os.path.abspath(path))
    project_root = Path(os.path.abspath(PROJECT_ROOT))
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError("atomic output path escapes the project") from exc
    _assert_no_link_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(path.parent)
    if os.path.lexists(path):
        _assert_no_link_components(path)
    staging_dir = PROJECT_ROOT / ".runtime" / "atomic-writes"
    _assert_no_link_components(staging_dir.parent)
    staging_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(staging_dir)
    # Keep staging short; repeating a hash-addressed archive name exceeds Windows
    # path limits even when the final sidecar destination itself is supported.
    descriptor, temporary_name = tempfile.mkstemp(prefix=".awb-", dir=staging_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_link_components(path.parent)
        if os.path.lexists(path):
            _assert_no_link_components(path)
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def build_package(output_dir: Path) -> dict[str, Any]:
    contract_path = PROJECT_ROOT / "agent_project.json"
    _assert_no_link_components(contract_path)
    contract = json.loads(
        _read_bounded_regular(contract_path, MAX_CONTRACT_BYTES, "project contract").decode("utf-8")
    )
    slug = contract.get("project", {}).get("slug") if isinstance(contract.get("project"), dict) else None
    if not isinstance(slug, str) or len(slug) < 2 or SLUG_RE.fullmatch(slug) is None:
        raise RuntimeError("project slug is invalid; expected lowercase kebab-case")
    artifact_pattern = re.compile(
        rf"{re.escape(slug)}-handoff-[0-9a-f]{{64}}\.zip(?:\.sha256)?\Z"
    )
    runtime = contract.get("runtime", {})
    if runtime.get("kind") != "external-dsh" or runtime.get("bundled") is not False:
        raise RuntimeError("runtime contract must keep DSH external and unbundled")
    requested_output = PROJECT_ROOT / output_dir if not output_dir.is_absolute() else output_dir
    _assert_no_link_components(requested_output.parent)
    if os.path.lexists(requested_output):
        _assert_no_link_components(requested_output)
    output = requested_output.resolve()
    try:
        output.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RuntimeError("output directory must stay inside the project") from exc
    if output == PROJECT_ROOT or output.parent != PROJECT_ROOT.resolve():
        raise RuntimeError("output directory must be one dedicated top-level project subdirectory")
    _check_output_path(output / f"{slug}-handoff-{'0' * 64}.zip.sha256")
    if output.exists():
        is_junction = getattr(os.path, "isjunction", None)
        unexpected = [
            child.name
            for child in output.iterdir()
            if (
                artifact_pattern.fullmatch(child.name) is None
                or not child.is_file()
                or child.is_symlink()
                or bool(is_junction and is_junction(child))
            )
        ]
        if unexpected:
            raise RuntimeError("output directory contains project or unrelated files")
    output.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(output)

    source_files = list(_source_files(output))
    if len(source_files) + 1 > MAX_MEMBER_COUNT:
        raise RuntimeError("handoff source member count exceeds limit")
    total_source_bytes = sum(path.stat().st_size for _, path in source_files)
    if total_source_bytes > MAX_TOTAL_BYTES:
        raise RuntimeError("handoff total source bytes exceed limit")
    members = []
    canonical_names: set[str] = set()
    for relative, path in source_files:
        if not _safe_relative(relative):
            raise RuntimeError(f"unsafe source path: {relative}")
        if _sensitive_filename(relative):
            raise RuntimeError(f"sensitive filename is not accepted: {relative}")
        parts = PurePosixPath(relative).parts
        if "node_modules" in parts or ".runtime" in parts or ("runtime" in parts and "DSH" in parts):
            raise RuntimeError(f"external runtime material would enter handoff: {relative}")
        key = _windows_path_key(relative)
        if key in canonical_names:
            raise RuntimeError(f"source paths collide on Windows: {relative}")
        canonical_names.add(key)
        size = path.stat().st_size
        members.append({"path": relative, "size": size, "sha256": _sha256_file(path)})

    manifest = {
        "schema": "agent-workbench-handoff-manifest/v4",
        "projectSlug": slug,
        "productKind": contract["project"]["kind"],
        "capabilityCount": len(contract["capabilities"]),
        "representativeScenarioCount": len(contract["acceptanceScenarios"]),
        "developmentStage": contract["development"]["stage"],
        "contractSha256": _sha256_file(PROJECT_ROOT / "agent_project.json"),
        "rollback": contract["rollback"],
        "externalDependencies": [{
            "name": "DeepSeek Harness",
            "officialRepository": runtime["officialRepository"],
            "testedVersion": runtime["testedVersion"],
            "bundled": False,
        }],
        "verificationDependencies": [{
            "name": "Agent Workbench Builder Skill",
            "version": BUILDER_VERSION,
            "releaseTag": BUILDER_RELEASE_TAG,
            "publicUrl": BUILDER_PUBLIC_URL,
            "bundled": False,
        }],
        "files": members,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(manifest_bytes) > MAX_FILE_BYTES or total_source_bytes + len(manifest_bytes) > MAX_TOTAL_BYTES:
        raise RuntimeError("handoff total source bytes exceed limit after manifest")
    staging_dir = PROJECT_ROOT / ".runtime" / "package-builds"
    _assert_no_link_components(staging_dir.parent)
    staging_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(staging_dir)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{slug}-handoff.", suffix=".zip", dir=staging_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, path in source_files:
                _write_file_member(archive, relative, path)
            archive.writestr(_zip_info("_handoff/manifest.json"), manifest_bytes)
        if temporary.stat().st_size > MAX_ARCHIVE_BYTES:
            raise RuntimeError("handoff archive exceeds size limit")
        with zipfile.ZipFile(temporary) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (
                not infos
                or len(infos) > MAX_MEMBER_COUNT
                or sum(info.file_size for info in infos) > MAX_TOTAL_BYTES
                or len(names) != len(set(names))
                or len({_windows_path_key(name) for name in names}) != len(names)
                or not all(_safe_relative(name) for name in names)
            ):
                raise RuntimeError("archive contains duplicate or unsafe paths")
            info_by_name = {info.filename: info for info in infos}
            manifest_info = info_by_name.get("_handoff/manifest.json")
            if manifest_info is None:
                raise RuntimeError("archive manifest is missing")
            with archive.open(manifest_info) as stream:
                loaded = json.loads(stream.read(MAX_FILE_BYTES + 1))
            if loaded["externalDependencies"][0]["bundled"] is not False:
                raise RuntimeError("handoff external dependency boundary is invalid")
            for entry in loaded["files"]:
                info = info_by_name.get(entry["path"])
                if info is None:
                    raise RuntimeError(f"archive verification failed: {entry['path']}")
                size, digest = _hash_zip_member(archive, info)
                if size != entry["size"] or digest != entry["sha256"]:
                    raise RuntimeError(f"archive verification failed: {entry['path']}")
        archive_hash = _sha256_file(temporary)
        archive_name = f"{slug}-handoff-{archive_hash}.zip"
        archive_path = (output / archive_name).resolve()
        archive_path.relative_to(output)
        if archive_path.exists():
            if _sha256_file(archive_path) != archive_hash:
                raise RuntimeError("content-addressed archive does not match its filename")
            temporary.unlink()
        else:
            _assert_no_link_components(output)
            _assert_no_link_components(archive_path.parent)
            if os.path.lexists(archive_path):
                _assert_no_link_components(archive_path)
            os.replace(temporary, archive_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    sidecar_path = (output / f"{archive_name}.sha256").resolve()
    sidecar_path.relative_to(output)
    sidecar_value = f"{archive_hash}  {archive_name}\n"
    if sidecar_path.exists():
        _assert_no_link_components(sidecar_path)
        if _read_bounded_regular(
            sidecar_path, MAX_SIDECAR_BYTES, "archive sidecar"
        ).decode("utf-8") != sidecar_value:
            raise RuntimeError("content-addressed archive sidecar is inconsistent")
    else:
        _atomic_text(sidecar_path, sidecar_value)

    receipt = {
        "schema": "agent-workbench-handoff/v4",
        "status": "PASS",
        "projectSlug": slug,
        "productKind": contract["project"]["kind"],
        "developmentStage": contract["development"]["stage"],
        "archive": archive_path.relative_to(PROJECT_ROOT).as_posix(),
        "sidecar": sidecar_path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": archive_hash,
        "manifestEntries": len(members),
        "archiveBytes": archive_path.stat().st_size,
        "verification": "manifest-sidecar-and-external-runtime-boundary-match",
        "externalDshBundled": False,
        "builderBundled": False,
        "rollback": contract["rollback"],
    }
    _atomic_json(PROJECT_ROOT / "evidence/handoff.json", receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = build_package(args.output_dir)
        code = 0
    except Exception as exc:
        receipt = {"schema": "agent-workbench-handoff/v4", "status": "FAIL", "error": {"code": getattr(exc, "code", "PACKAGE_FAILED"), "message": str(exc).replace(str(PROJECT_ROOT), "<PROJECT_ROOT>")}}
        _atomic_json(PROJECT_ROOT / "evidence/handoff.json", receipt)
        code = 3
    print(json.dumps(receipt, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
