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

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    (target / "snapshot.json").write_text(json.dumps(
        {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
         "bytes": size, "source": str(SRC)}, ensure_ascii=False, indent=1) + "\n")
    # 顺带把代码仓库打成 git bundle（异地副本用）
    repo_dir = Path.home() / "backups" / "repos"
    repo_dir.mkdir(parents=True, exist_ok=True)
    for repo in ("memory-ops", "memory-ops-os"):
        src = Path.home() / "projects" / repo
        if (src / ".git").exists():
            run(["git", "-C", str(src), "bundle", "create",
                 str(repo_dir / f"{repo}-{STAMP}.bundle"), "--all"])
    for old in sorted(repo_dir.glob("*.bundle"))[:-14]:
        old.unlink(missing_ok=True)

    removed = prune()
    print(f"L2 快照完成：{target} | {size / 1024**3:.2f} GB | {time.time() - t0:.1f}s"
          + (f" | 清理 {', '.join(removed)}" if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
