#!/usr/bin/env python3
"""L2（PlugMem 语义图）每日快照
- rsync 增量拷贝（排除 live sqlite）
- sqlite3 在线备份 API 单独备份 chroma.sqlite3（保证一致）
- 保留 7 份，更旧的自动清理（用 Python 删，避免 shell 通配误伤）
输出：~/.coremem/backups/plugmem/<date>/
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(os.environ.get("MEM_PLUGMEM_DATA") or (Path.home() / ".coremem/plugmem"))
DEST = Path.home() / ".coremem/backups/plugmem"
KEEP = 7
STAMP = datetime.now().strftime("%Y-%m-%d")

# 2026-11-11 审计 P0-1：L1 每日笔记（SoT）、统一事实池、MEMORY.md、配置此前不在备份范围。
# 规范路径用 .coremem 形式书写，由部署 profile 改写（plugmem-suite-cli → .plugmem）。
# 不存在的源自动跳过；可用 MEM_BACKUP_EXTRA="name:path,name:path" 追加。
EXTRA_SOURCES = [
    ("workspace", Path(os.environ.get("MEM_BACKUP_WORKSPACE") or Path.home() / ".coremem" / "memory")),
    ("config", Path(os.environ.get("MEM_BACKUP_CONFIG") or Path.home() / ".coremem/config")),
    # 2026-11-11 审计补：MEMORY.md 在 ~/.coremem/ 根目录，**不在 memory/ 里**，
    # 所以「备份扩容 workspace」那次仍漏了它（注释写着含 MEMORY.md，实际不含）。
    # 它含人工确认的 DERIVED 规则，丢了要重新评审。跟着 MEM_MEMORY_MD 配置走。
    ("memory_md", Path(os.environ.get("MEM_MEMORY_MD") or Path.home() / ".coremem" / "MEMORY.md")),
]
for _spec in os.environ.get("MEM_BACKUP_EXTRA", "").split(","):
    if ":" in _spec:
        _name, _p = _spec.split(":", 1)
        EXTRA_SOURCES.append((_name.strip(), Path(_p.strip()).expanduser()))


def run(cmd: list[str]) -> int:
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def prune() -> list[str]:
    removed = []
    dirs = sorted([d for d in DEST.iterdir() if d.is_dir()]) if DEST.exists() else []
    for d in dirs[:-KEEP] if len(dirs) > KEEP else []:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def main() -> int:
    if not SRC.exists():
        print(f"源目录不存在：{SRC}")
        return 1
    target = DEST / STAMP
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)  # 备份含明文 key（config），目录收紧到仅本人可读

    t0 = time.time()
    rc = run(["rsync", "-a", "--delete",
              "--exclude", "chroma.sqlite3", "--exclude", "chroma.sqlite3-shm",
              "--exclude", "chroma.sqlite3-wal",
              f"{SRC}/", f"{target}/"])
    if rc != 0:
        print(f"rsync 失败 rc={rc}")
        return 1

    live = SRC / "chroma" / "chroma.sqlite3"
    if live.exists():
        (target / "chroma").mkdir(parents=True, exist_ok=True)
        dst = target / "chroma" / "chroma.sqlite3"
        with sqlite3.connect(f"file:{live}?mode=ro", uri=True) as src_con, sqlite3.connect(dst) as dst_con:
            src_con.backup(dst_con)

    # 额外源：workspace（L1/facts/MEMORY.md）与 config。不用 --delete：
    # 误删不会立刻传导进快照，7 天滚动内仍可找回。
    backed_extras = {}
    for name, src in EXTRA_SOURCES:
        if not src.exists():
            continue
        dst = target / name
        dst.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            rc2 = run(["rsync", "-a", f"{src}/", f"{dst}/"])
        else:
            rc2 = run(["rsync", "-a", str(src), str(dst)])
        backed_extras[name] = {"source": str(src), "ok": rc2 == 0}
        if rc2 != 0:
            print(f"⚠️ 额外源 {name} rsync 失败 rc={rc2}（{src}）")

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    (target / "snapshot.json").write_text(json.dumps(
        {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
         "bytes": size, "source": str(SRC), "extras": backed_extras}, ensure_ascii=False, indent=1) + "\n")
    # 顺带把代码仓库打成 git bundle（异地副本用）。
    # 2026-11-11 审计：原来只找 ~/projects/memory-ops*（本机不存在，每天静默空跑），
    # 补上真正在用的 ~/coremem；全部缺失时明确报告而非静默。
    repo_dir = Path.home() / "backups" / "repos"
    repo_dir.mkdir(parents=True, exist_ok=True)
    bundled = []
    for src in [Path.home() / "coremem",
                Path.home() / "projects" / "memory-ops",
                Path.home() / "projects" / "memory-ops-os"]:
        if (src / ".git").exists():
            if run(["git", "-C", str(src), "bundle", "create",
                    str(repo_dir / f"{src.name}-{STAMP}.bundle"), "--all"]) == 0:
                bundled.append(src.name)
    if not bundled:
        print("⚠️ 无可打包的代码仓库（coremem/memory-ops 均不存在）")
    for old in sorted(repo_dir.glob("*.bundle"))[:-14]:
        old.unlink(missing_ok=True)

    removed = prune()
    print(f"L2 快照完成：{target} | {size / 1024**3:.2f} GB | {time.time() - t0:.1f}s"
          + (f" | 额外源 {','.join(backed_extras)}" if backed_extras else " | ⚠️ 无额外源")
          + (f" | bundle {','.join(bundled)}" if bundled else "")
          + (f" | 清理 {', '.join(removed)}" if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
