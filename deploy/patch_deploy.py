#!/usr/bin/env python3
"""deploy/patch_deploy.py — coremem 部署器：repo src → 生产 scripts，并打生产默认值补丁。

用法: python3 deploy/patch_deploy.py <repo_root> <dest_dir> [--profile <name>]
备份由 deploy.sh 负责。补丁原则：默认值=现网真实路径，使 cron/agent 无 env 也能正确运行；
环境变量通道保留（env 优先于默认值）。

--profile 从 deploy/profiles/<name>.json 读取追加替换（用于同一仓库部署到不同机器/不同 runtime）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("repo_root", type=Path)
parser.add_argument("dest_dir", type=Path)
parser.add_argument("--profile", default=None)
args = parser.parse_args()

repo, dest = args.repo_root.resolve(), args.dest_dir.resolve()

# 加载 profile 的追加替换
profile_replacements = {}
if args.profile:
    pf = repo / "deploy" / "profiles" / f"{args.profile}.json"
    if pf.exists():
        profile_replacements = json.loads(pf.read_text()).get("replacements", {})
        print(f"profile: {args.profile} ({len(profile_replacements)} 道追加替换)")

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
    m = 0
    for old, new in profile_replacements.items():
        text, k = re.subn(re.escape(old), new, text)
        m += k
    (dest / name).write_text(text, encoding="utf-8")
    extra = f" +{m} profile" if m else ""
    report.append(f"{name}: {n} patches{extra} applied")

# secret_scan 走 scripts/
ss = repo / "scripts" / "secret_scan.py"
if ss.exists():
    (dest / "secret_scan.py").write_text(ss.read_text(encoding="utf-8"), encoding="utf-8")
    report.append("secret_scan.py: copied")

print("\n".join(report))
