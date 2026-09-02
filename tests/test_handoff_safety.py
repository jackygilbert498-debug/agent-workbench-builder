from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SKILL_ROOT = Path(__file__).resolve().parents[1]


def make_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


def load_packager(template: str, name: str):
    root = SKILL_ROOT / "assets" / template
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    script = root / "tools" / "package_handoff.py"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixture(root: Path, *, slug: str = "fixture") -> None:
    contract = {
        "project": {"slug": slug, "kind": "focused-agent"},
        "capabilities": [{"id": "one"}],
        "acceptanceScenarios": [{"id": "one"}],
        "development": {"stage": "domain-adapted"},
        "rollback": {"strategy": "restore"},
        "runtime": {
            "kind": "external-dsh",
            "bundled": False,
            "officialRepository": "https://github.com/deepseek-ai/deepseek-harness",
            "testedVersion": "0.1.0-rc.8",
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent_project.json").write_text(json.dumps(contract), encoding="utf-8")
    (root / "extra.txt").write_text("two", encoding="utf-8")


class HandoffSafetyTests(unittest.TestCase):
    def test_standalone_packager_does_not_import_mutable_business_modules(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tools").mkdir()
            (root / "agent_workbench").mkdir()
            script = root / "tools/package_handoff.py"
            script.write_bytes(
                (SKILL_ROOT / "assets/starter-template/tools/package_handoff.py").read_bytes()
            )
            (root / "agent_workbench/__init__.py").write_text(
                "from pathlib import Path\n"
                "(Path(__file__).parents[1] / 'unexpected-import.txt').write_text('ran')\n",
                encoding="utf-8",
            )
            (root / "agent_workbench/store.py").write_text(
                "def atomic_write_json(*args, **kwargs):\n    pass\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-c", "import runpy,sys; runpy.run_path(sys.argv[1], run_name='packager_import')", str(script)],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            self.assertFalse((root / "unexpected-import.txt").exists())

    def test_packagers_reject_known_secret_and_machine_path_signatures(self) -> None:
        cases = {
            "unquoted-secret": "API_KEY=fixture-secret-value-1234567890\n",
            "windows-forward-slash": "C:/Users/example/private/file.txt\n",
            "json-escaped-windows": '{"path":"C:\\\\Users\\\\example\\\\private"}\n',
            "generic-windows-drive": "D:/build/example/private/file.txt\n",
            "unc-share": "\\\\server\\share\\private\\file.txt\n",
            "posix-build-root": "/opt/build/example/private/file.txt\n",
            "pem-private-key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key-but-must-never-ship\n",
            "bearer-jwt": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.signaturevalue\n",
            "npm-token": "_authToken=fixture-auth-token-1234567890\n",
            "utf16-windows-path": "C:\\Users\\example\\private\\file.txt\n".encode("utf-16"),
            "utf16-token": "API_KEY=fixture-secret-value-1234567890\n".encode("utf-16"),
        }
        for template, name in (
            ("starter-template", "standalone_packager_content_scan"),
            ("dsh-product-template", "dsh_packager_content_scan"),
        ):
            module = load_packager(template, name)
            for case, content in cases.items():
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    write_fixture(root)
                    evidence = root / "evidence"
                    evidence.mkdir()
                    leak = evidence / "leak.txt"
                    if isinstance(content, bytes):
                        leak.write_bytes(content)
                    else:
                        leak.write_text(content, encoding="utf-8")
                    with patch.object(module, "PROJECT_ROOT", root):
                        with self.subTest(template=template, case=case), self.assertRaisesRegex(
                            RuntimeError, "secret-like value|machine-absolute path"
                        ):
                            module.build_package(Path("dist"))
                    self.assertEqual(list((root / "dist").glob("*.zip")), [])

    def test_packagers_reject_sensitive_filename_created_after_other_checks(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_sensitive_name"),
            ("dsh-product-template", "dsh_packager_sensitive_name"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                (root / ".env.production").write_text("placeholder\n", encoding="utf-8")
                with patch.object(module, "PROJECT_ROOT", root):
                    with self.subTest(template=template), self.assertRaisesRegex(
                        RuntimeError, "sensitive filename"
                    ):
                        module.build_package(Path("dist"))
                self.assertEqual(list((root / "dist").glob("*.zip")), [])

    def test_atomic_writer_rejects_linked_destination_parent(self) -> None:
        """Neither a PASS sidecar nor a FAIL receipt may escape through a link."""

        for template, name in (
            ("starter-template", "standalone_packager_atomic_link"),
            ("dsh-product-template", "dsh_packager_atomic_link"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                parent = Path(raw)
                root = parent / "project"
                outside = parent / "outside"
                write_fixture(root)
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_text("unchanged", encoding="utf-8")
                try:
                    make_directory_link(outside, root / "evidence")
                except OSError as exc:
                    self.skipTest(f"directory links are unavailable: {exc}")

                with patch.object(module, "PROJECT_ROOT", root):
                    with self.subTest(template=template), self.assertRaisesRegex(
                        RuntimeError, "linked output path"
                    ):
                        module._atomic_text(
                            root / "evidence" / "handoff.json",
                            '{"status":"PASS"}\n',
                        )

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
                self.assertFalse((outside / "handoff.json").exists())

    def test_linked_source_directory_is_never_packaged(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_link"),
            ("dsh-product-template", "dsh_packager_link"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                parent = Path(raw)
                root = parent / "project"
                outside = parent / "outside"
                write_fixture(root)
                outside.mkdir()
                (outside / "secret.txt").write_text("outside", encoding="utf-8")
                linked = root / "linked-source"
                try:
                    make_directory_link(outside, linked)
                except OSError as exc:
                    self.skipTest(f"directory links are unavailable: {exc}")
                with patch.object(module, "PROJECT_ROOT", root):
                    with self.subTest(template=template), self.assertRaisesRegex(
                        RuntimeError, "linked source"
                    ):
                        module.build_package(Path("handoff"))
                self.assertEqual(list((root / "handoff").glob("*.zip")), [])

    def test_packagers_reject_windows_drive_and_device_members(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_paths"),
            ("dsh-product-template", "dsh_packager_paths"),
        ):
            module = load_packager(template, name)
            for value in (
                "C:/escape.txt",
                "NUL",
                "folder/LPT1.txt",
                "folder/LPT².txt",
                "//server/share",
                "bad<name>.txt",
                'bad"name.txt',
                "bad|name.txt",
                "bad?name.txt",
                "bad*name.txt",
                "control\x1f.txt",
            ):
                with self.subTest(template=template, value=value):
                    self.assertFalse(module._safe_relative(value))

    def test_packagers_use_unicode_normalization_for_collision_keys(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_unicode_paths"),
            ("dsh-product-template", "dsh_packager_unicode_paths"),
        ):
            module = load_packager(template, name)
            self.assertEqual(
                module._windows_path_key("café.txt"),
                module._windows_path_key("cafe\u0301.txt"),
            )

    def test_packagers_bound_member_count_and_total_source_bytes(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_limits"),
            ("dsh-product-template", "dsh_packager_limits"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                with patch.object(module, "PROJECT_ROOT", root), patch.object(
                    module, "MAX_MEMBER_COUNT", 1
                ):
                    with self.subTest(template=template, limit="members"), self.assertRaisesRegex(
                        RuntimeError, "member count"
                    ):
                        module.build_package(Path("dist"))
                with patch.object(module, "PROJECT_ROOT", root), patch.object(
                    module, "MAX_TOTAL_BYTES", 1
                ):
                    with self.subTest(template=template, limit="bytes"), self.assertRaisesRegex(
                        RuntimeError, "total source bytes"
                    ):
                        module.build_package(Path("dist"))

    def test_custom_output_directory_is_excluded_and_reproducible(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_output"),
            ("dsh-product-template", "dsh_packager_output"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                with patch.object(module, "PROJECT_ROOT", root):
                    first = module.build_package(Path("handoff"))
                    second = module.build_package(Path("handoff"))
                self.assertEqual(first["sha256"], second["sha256"])
                archive = root / second["archive"]
                with zipfile.ZipFile(archive) as bundle:
                    self.assertFalse(any(name.startswith("handoff/") for name in bundle.namelist()))
                    manifest = json.loads(bundle.read("_handoff/manifest.json"))
                self.assertEqual(manifest["verificationDependencies"][0]["version"], "4.0.1")
                self.assertFalse(manifest["verificationDependencies"][0]["bundled"])

    def test_source_race_does_not_replace_previous_good_archive_or_sidecar(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_race"),
            ("dsh-product-template", "dsh_packager_race"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                with patch.object(module, "PROJECT_ROOT", root):
                    first = module.build_package(Path("handoff"))
                    archive = root / first["archive"]
                    sidecar = root / first["sidecar"]
                    original_archive = archive.read_bytes()
                    original_sidecar = sidecar.read_bytes()
                    original_writer = module._write_file_member
                    mutated = False

                    def racing_writer(bundle, relative, source):
                        nonlocal mutated
                        if relative == "extra.txt" and not mutated:
                            source.write_text("changed-after-manifest", encoding="utf-8")
                            mutated = True
                        return original_writer(bundle, relative, source)

                    with patch.object(module, "_write_file_member", side_effect=racing_writer):
                        with self.subTest(template=template), self.assertRaisesRegex(
                            RuntimeError, "archive verification failed"
                        ):
                            module.build_package(Path("handoff"))
                self.assertEqual(archive.read_bytes(), original_archive)
                self.assertEqual(sidecar.read_bytes(), original_sidecar)

    def test_sidecar_failure_keeps_previous_receipt_and_package_addressable(self) -> None:
        """A crash boundary before the sidecar must not invalidate the last receipt."""
        for template, name in (
            ("starter-template", "standalone_packager_sidecar"),
            ("dsh-product-template", "dsh_packager_sidecar"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                with patch.object(module, "PROJECT_ROOT", root):
                    first = module.build_package(Path("handoff"))
                    first_archive = root / first["archive"]
                    first_sidecar = root / first["sidecar"]
                    receipt_path = root / "evidence/handoff.json"
                    original_receipt = receipt_path.read_bytes()
                    original_archive = first_archive.read_bytes()
                    original_sidecar = first_sidecar.read_bytes()
                    (root / "extra.txt").write_text("new-revision", encoding="utf-8")

                    with patch.object(
                        module, "_atomic_text", side_effect=RuntimeError("sidecar write failed")
                    ):
                        with self.subTest(template=template), self.assertRaisesRegex(
                            RuntimeError, "sidecar write failed"
                        ):
                            module.build_package(Path("handoff"))

                self.assertEqual(receipt_path.read_bytes(), original_receipt)
                self.assertEqual(first_archive.read_bytes(), original_archive)
                self.assertEqual(first_sidecar.read_bytes(), original_sidecar)
                self.assertEqual(len(list((root / "handoff").glob("*.sha256"))), 1)

    def test_output_directory_cannot_hide_project_sources(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_overlap"),
            ("dsh-product-template", "dsh_packager_overlap"),
        ):
            module = load_packager(template, name)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                write_fixture(root)
                (root / "src").mkdir()
                (root / "src/core.txt").write_text("core", encoding="utf-8")
                with patch.object(module, "PROJECT_ROOT", root):
                    for output in (Path("."), Path("src"), Path("src/handoff")):
                        with self.subTest(template=template, output=output), self.assertRaisesRegex(
                            RuntimeError, "output directory"
                        ):
                            module.build_package(output)

    def test_tampered_slug_cannot_write_outside_project(self) -> None:
        for template, name in (
            ("starter-template", "standalone_packager_slug"),
            ("dsh-product-template", "dsh_packager_slug"),
        ):
            module = load_packager(template, name)
            for slug in ("../escape", "C:/escape", "NUL", "bad/name"):
                with tempfile.TemporaryDirectory() as raw:
                    parent = Path(raw)
                    root = parent / "project"
                    write_fixture(root, slug=slug)
                    with patch.object(module, "PROJECT_ROOT", root):
                        with self.subTest(template=template, slug=slug), self.assertRaisesRegex(
                            RuntimeError, "project slug"
                        ):
                            module.build_package(Path("handoff"))
                    self.assertEqual(
                        [path.name for path in parent.iterdir()],
                        ["project"],
                    )


if __name__ == "__main__":
    unittest.main()
