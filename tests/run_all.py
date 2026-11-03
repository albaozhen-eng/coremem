#!/usr/bin/env python3
"""Run the repository's script-style tests without unittest import side effects."""
import subprocess
import sys
from pathlib import Path

TESTS = sorted(Path(__file__).resolve().parent.glob("test_*.py"))
failed = []
for test in TESTS:
    print(f"\n===== {test.name} =====", flush=True)
    result = subprocess.run([sys.executable, str(test)])
    if result.returncode:
        failed.append(test.name)
print(f"\n===== {len(TESTS) - len(failed)}/{len(TESTS)} test files passed =====")
if failed:
    print("Failed: " + ", ".join(failed))
sys.exit(1 if failed else 0)
