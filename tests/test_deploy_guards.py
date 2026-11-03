#!/usr/bin/env python3
"""Deployment drift guard must cover shell files as well as Python files."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILS = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as td:
    dest = Path(td)
    name = "plugmem-watchdog.sh"
    original = (ROOT / "scripts" / name).read_text()
    (dest / name).write_text(original)
    base = [sys.executable, str(ROOT / "deploy/patch_deploy.py"), str(ROOT), str(dest),
            "--profile", "coremem-local", "--no-patch", "--check-drift"]
    clean = subprocess.run(base, capture_output=True, text=True)
    check("shell 文件一致时无漂移", clean.returncode == 0, clean.stdout.strip()[-100:])
    (dest / name).write_text(original + "\n# production-only hotfix\n")
    drift = subprocess.run(base, capture_output=True, text=True)
    check("shell 生产热修会阻止部署", drift.returncode == 3, f"exit={drift.returncode}")
    check("漂移输出点名 shell 文件", name in drift.stdout)

print("\n全部通过" if not FAILS else f"\n失败: {FAILS}")
sys.exit(1 if FAILS else 0)
