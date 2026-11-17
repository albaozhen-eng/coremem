#!/usr/bin/env python3
"""Run the repository's script-style tests without unittest import side effects."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = sorted(HERE.glob("test_*.py"))
# 2026-11-17：会话注入器是 TypeScript，必须一起跑 —— 否则改了扩展而全套仍全绿，
# 正是「测试自己改的东西而不是目标」。node 24 支持类型剥离，可直接执行。
MJS = sorted(HERE.glob("test_*.mjs"))
failed = []
for test in TESTS:
    print(f"\n===== {test.name} =====", flush=True)
    result = subprocess.run([sys.executable, str(test)])
    if result.returncode:
        failed.append(test.name)
for test in MJS:
    print(f"\n===== {test.name} =====", flush=True)
    import os
    import shutil
    if not shutil.which("node"):
        print("  [SKIP] 无 node")
        continue
    env = dict(os.environ)
    env.setdefault("PI_EXT_DIR", str(Path.home() / ".pi/agent/extensions"))
    result = subprocess.run(["node", str(test)], env=env)
    if result.returncode:
        failed.append(test.name)
TOTAL = len(TESTS) + len(MJS)
print(f"\n===== {TOTAL - len(failed)}/{TOTAL} test files passed =====")
if failed:
    print("Failed: " + ", ".join(failed))
sys.exit(1 if failed else 0)
