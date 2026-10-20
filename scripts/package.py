"""Explicit source-only ZIP; smoke import is confined to a temporary extraction."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "_manifest.json", "plugin.py", "life_authorization.py", "public_http.py",
    "result_images.py", "source_images.py", "self_reference.py", "README.md", "LICENSE",
    "config.example.toml",
)


def build(root, output):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            source = root / name
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Missing or symlinked package source: {name}")
            archive.writestr(name, source.read_bytes())


def smoke(archive):
    with tempfile.TemporaryDirectory(prefix="neko-draw-package-") as directory:
        with zipfile.ZipFile(archive) as package:
            if sorted(package.namelist()) != sorted(FILES):
                raise ValueError("Unexpected package members")
            package.extractall(directory)
        manifest = json.loads((Path(directory) / "_manifest.json").read_text(encoding="utf-8"))
        code = """
import importlib, pathlib, sys, types
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
package = types.ModuleType('packaged_plugin')
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
module = importlib.import_module('packaged_plugin.plugin')
assert module.create_plugin() is not None
print('Packaged SDK import OK')
"""
        subprocess.run([sys.executable, "-I", "-c", code, directory], check=True, cwd=directory)
        print(f"Validated {manifest['id']} {manifest['version']}: {len(FILES)} allowlisted files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional ZIP path; otherwise only temporary QA output")
    args = parser.parse_args()
    if args.output:
        output = args.output.resolve()
        if output.exists():
            parser.error("Output already exists; refusing to overwrite")
        build(ROOT, output)
        smoke(output)
    else:
        with tempfile.TemporaryDirectory(prefix="neko-draw-build-") as directory:
            output = Path(directory) / "plugin.zip"
            build(ROOT, output)
            smoke(output)
