#!/usr/bin/env python3
"""deploy/patch_deploy.py — coremem 部署器：repo src → 生产 scripts，并打生产默认值补丁。

用法: python3 deploy/patch_deploy.py <repo_root> <dest_dir>
备份由 deploy.sh 负责。补丁原则：默认值=现网真实路径，使 cron/agent 无 env 也能正确运行；
环境变量通道保留（env 优先于默认值）。
"""
import re
import sys
from pathlib import Path

repo, dest = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()

# (pattern, replacement) —— 只作用于复制到 dest 的文件
PATCHES = [
    (r'os\.environ\.get\("MEMORY_HOME", Path\.home\(\) / "\.coremem"\)',
     'os.environ.get("MEMORY_HOME", Path.home() / ".openclaw")'),
    (r'os\.environ\.get\("MEM_SEMANTIC_GRAPH_ID", "default"\)',
     'os.environ.get("MEM_SEMANTIC_GRAPH_ID", "brian-main")'),
    (r'os\.environ\.get\("MEM_DAILY_NOTES_DIR", MEMORY_HOME / "memory"\)',
     'os.environ.get("MEM_DAILY_NOTES_DIR", Path.home() / ".openclaw" / "workspace-main" / "memory")'),
    (r'os\.environ\.get\("MEM_MEMORY_MD", MEMORY_HOME / "MEMORY\.md"\)',
     'os.environ.get("MEM_MEMORY_MD", Path.home() / ".openclaw" / "workspace-main" / "MEMORY.md")'),
    (r'os\.environ\.get\("MEM_SESSIONS_DIR", MEMORY_HOME / "sessions"\)',
     'os.environ.get("MEM_SESSIONS_DIR", Path.home() / ".openclaw" / "agents" / "main" / "sessions")'),
    (r'BRIEFINGS_DIR = MEMORY_HOME / "memory" / "briefings"',
     'BRIEFINGS_DIR = Path.home() / ".openclaw" / "workspace-main" / "memory" / "briefings"'),
    (r'DAILY_BRIEFING_FILE = MEMORY_HOME / "memory" / "daily-briefing\.md"',
     'DAILY_BRIEFING_FILE = Path.home() / ".openclaw" / "workspace-main" / "memory" / "daily-briefing.md"'),
    (r'REPORTS_DIR = MEMORY_HOME / "reports"',
     'REPORTS_DIR = Path.home() / ".openclaw" / "workspace-main" / "memory" / "reports"'),
]

FILES = ["plugmem_config.py", "remember.py", "memory_recall.py", "memory_write_buffer.py",
         "memory_insight_scan.py", "morning_briefing.py"]

dest.mkdir(parents=True, exist_ok=True)
report = []
for name in FILES:
    src = repo / "src" / name
    if not src.exists():
        report.append(f"MISSING {name}")
        continue
    text = src.read_text(encoding="utf-8")
    n = 0
    for pat, rep in PATCHES:
        text, k = re.subn(pat, rep, text)
        n += k
    (dest / name).write_text(text, encoding="utf-8")
    report.append(f"{name}: {n} patches applied")

# secret_scan 走 scripts/
ss = repo / "scripts" / "secret_scan.py"
if ss.exists():
    (dest / "secret_scan.py").write_text(ss.read_text(encoding="utf-8"), encoding="utf-8")
    report.append("secret_scan.py: copied")

print("\n".join(report))
