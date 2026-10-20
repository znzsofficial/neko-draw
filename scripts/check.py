"""CI gate: real SDK, non-empty discovery, and no silently skipped tests."""

import importlib.metadata
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
print("maibot-plugin-sdk", importlib.metadata.version("maibot-plugin-sdk"), flush=True)
suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.testsRun < 96 or result.skipped or not result.wasSuccessful():
    raise SystemExit("CI gate failed: fewer than 96 tests, skipped tests, or failures")
