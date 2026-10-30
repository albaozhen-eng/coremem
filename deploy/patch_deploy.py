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
parser.add_argument("--no-patch", action="store_true", help="部署仓库原样默认值（~/.coremem），不套用 .openclaw 生产补丁")
parser.add_argument("--check-drift", action="store_true", help="只比对仓库与生产，不写入；有漂移时退出码 3")
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

FILES = ["plugmem_config.py", "remember.py", "memory_recall.py", "memory_write_buffer.py", "mo_common.py",
         "memory_insight_scan.py", "morning_briefing.py", "vault_export.py"]

dest.mkdir(parents=True, exist_ok=True)
report = []
for name in FILES:
    src = repo / "src" / name
    if not src.exists():
        report.append(f"MISSING {name}")
        continue
    text = src.read_text(encoding="utf-8")
    n = 0
    if not args.no_patch:
        for pat, rep in PATCHES:
            text, k = re.subn(pat, rep, text)
            n += k
    m = 0
    for old, new in profile_replacements.items():
        text, k = re.subn(re.escape(old), new, text)
        m += k
    if not args.check_drift:
        (dest / name).write_text(text, encoding="utf-8")
    extra = f" +{m} profile" if m else ""
    report.append(f"{name}: {n} patches{extra} applied")

# secret_scan 走 scripts/
ss = repo / "scripts" / "secret_scan.py"
if ss.exists():
    if not args.check_drift:
        (dest / "secret_scan.py").write_text(ss.read_text(encoding="utf-8"), encoding="utf-8")
    report.append("secret_scan.py: copied")

# 2026-10-30 事故：生产热修没回仓库，一次 deploy 把 memory_recall 的行级 IDF、
# 别名扩展、AUTO 退役守卫全部回退，检索自检当场变红。部署前先比对，默认拒绝。
if args.check_drift:
    import difflib
    drift = []
    for name in FILES:
        src = repo / "src" / name
        live = dest / name
        if not src.exists() or not live.exists():
            continue
        want = src.read_text(encoding="utf-8")
        have = live.read_text(encoding="utf-8")
        if want == have:
            continue
        diff = list(
            difflib.unified_diff(
                have.splitlines(), want.splitlines(),
                fromfile=f"live/{name}", tofile=f"repo/{name}", lineterm="", n=1,
            )
        )
        drift.append((name, diff))
    if drift:
        print("✗ 生产与仓库存在漂移（生产有仓库没有的改动的可能性最大）：")
        for name, diff in drift:
            print(f"\n--- {name} ({sum(1 for l in diff if l.startswith(('+', '-')) and not l.startswith(('+++', '---')))} 行差异)")
            for line in diff[:40]:
                print("   " + line)
        print("\n处理：先把生产版本回灌仓库（或确认丢弃），再部署；确需覆盖用 --force。")
        sys.exit(3)
    print("✓ 无漂移（仓库与生产逐字一致）")
    sys.exit(0)

print("\n".join(report))
