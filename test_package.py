"""Packaging reads only explicit public source names, never runtime directories."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("draw_package", ROOT / "scripts/package.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


class PackageTests(unittest.TestCase):
    def test_only_allowlisted_sources_are_read_and_packed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in PACKAGE.FILES:
                (root / name).write_bytes((ROOT / name).read_bytes())
            for name in ("config.toml", ".env", "voice-receipts.json", "life-keys.json"):
                (root / name).write_text("DO_NOT_PACKAGE", encoding="utf-8")
            (root / "audio").mkdir()
            (root / "audio/private.wav").write_bytes(b"DO_NOT_READ")
            output = root / "plugin.zip"
            # No traversal even in fixtures: private audio/config is not scanned.
            with patch.object(Path, "rglob", side_effect=AssertionError("recursive scan")), patch.object(
                    Path, "glob", side_effect=AssertionError("directory scan")):
                PACKAGE.build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(sorted(archive.namelist()), sorted(PACKAGE.FILES))
                self.assertTrue(all(b"DO_NOT_" not in archive.read(name) for name in archive.namelist()))

    def test_symlinked_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "symlinked"):
                PACKAGE.build(ROOT, Path(directory) / "plugin.zip")

    def test_unexpected_archive_member_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad.zip"
            PACKAGE.build(ROOT, output)
            with zipfile.ZipFile(output, "a") as archive:
                archive.writestr("../config.toml", "private")
            with self.assertRaisesRegex(ValueError, "Unexpected package members"):
                PACKAGE.smoke(output)
