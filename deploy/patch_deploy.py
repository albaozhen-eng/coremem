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

def _deployment_manifest(repo: Path):
    """The single file list shared with deploy.sh; paths are repo-relative."""
    paths = []
    for line in (repo / "deploy/files.manifest").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line)
    return paths


_DEPLOY_PATHS = _deployment_manifest(repo)
FILES = [Path(p).name for p in _DEPLOY_PATHS if p.startswith("src/")]
SH_FILES = [Path(p).name for p in _DEPLOY_PATHS if p.startswith("scripts/")]
# profile 可以按机型排除不适用的部署文件（例如某机不用 coremem 自带 launcher）
EXCLUDE = set()
if args.profile:
    _pf = repo / "deploy" / "profiles" / f"{args.profile}.json"
    if _pf.exists():
        EXCLUDE = set(json.loads(_pf.read_text()).get("exclude", []))
    if EXCLUDE:
        FILES = [n for n in FILES if n not in EXCLUDE]
        SH_FILES = [n for n in SH_FILES if n not in EXCLUDE]
        print(f"profile 排除: {', '.join(sorted(EXCLUDE))}")


def _apply_patches(text: str) -> tuple:
    """套用仓库默认补丁 + profile 替换（src/*.py 与 scripts/*.sh 一视同仁）。"""
    n = m = 0
    if not args.no_patch:
        for pat, rep in PATCHES:
            text, k = re.subn(pat, rep, text)
            n += k
    for old, new in profile_replacements.items():
        text, k = re.subn(re.escape(old), new, text)
        m += k
    return text, n, m


dest.mkdir(parents=True, exist_ok=True)
report = []
for name in FILES:
    src = repo / "src" / name
    if not src.exists():
        report.append(f"MISSING {name}")
        continue
    text = src.read_text(encoding="utf-8")
    text, n, m = _apply_patches(text)
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

for name in SH_FILES:
    sh = repo / "scripts" / name
    if not sh.exists():
        report.append(f"MISSING {name}")
        continue
    text, n, m = _apply_patches(sh.read_text(encoding="utf-8"))
    if not args.check_drift:
        (dest / name).write_text(text, encoding="utf-8")
        (dest / name).chmod(0o755)
    extra = f" +{m} profile" if m else ""
    report.append(f"{name}: {n} patches{extra} applied")

# 2026-10-30 事故：生产热修没回仓库，一次 deploy 把 memory_recall 的行级 IDF、
# 别名扩展、AUTO 退役守卫全部回退，检索自检当场变红。部署前先比对，默认拒绝。
if args.check_drift:
    import difflib
    drift = []
    for name in FILES + SH_FILES:
        src = repo / ("scripts" if name in SH_FILES else "src") / name
        live = dest / name
        if not src.exists() or not live.exists():
            continue
        want, _n, _m = _apply_patches(src.read_text(encoding="utf-8"))
        have = live.read_text(encoding="utf-8")
        if want == have:
            continue
        diff = list(
            difflib.unified_diff(
                have.splitlines(), want.splitlines(),
                fromfile=f"live/{name}", tofile=f"repo/{name}", lineterm="", n=1,
            )
        )
        # 只有「生产有、仓库没有」（diff 里出现 '-'）才是危险漂移：部署会抹掉它。
        # 仓库领先（纯新增 '+'）是正常开发态，允许部署。
        removals = [l for l in diff if l.startswith("-") and not l.startswith("---")]
        drift.append((name, diff, bool(removals)))
    dangerous = [d for d in drift if d[2]]
    if drift and not dangerous:
        print(f"✓ 仓库领先（{len(drift)} 个文件只有新增，无生产独有内容）→ 允许部署")
        sys.exit(0)
    if drift:
        print("✗ 生产与仓库存在漂移（生产有仓库没有的改动，部署会抹掉它）：")
        for name, diff, has_removal in drift:
            if not has_removal:
                continue
            print(f"\n--- {name} ({sum(1 for l in diff if l.startswith(('+', '-')) and not l.startswith(('+++', '---')))} 行差异)")
            for line in diff[:40]:
                print("   " + line)
        print("\n处理：先把生产版本回灌仓库（或确认丢弃），再部署；确需覆盖用 --force。")
        sys.exit(3)
    print("✓ 无漂移（仓库与生产逐字一致）")
    sys.exit(0)

print("\n".join(report))
