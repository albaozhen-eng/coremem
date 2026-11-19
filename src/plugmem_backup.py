#!/usr/bin/env python3
"""L2（PlugMem 语义图）每日快照
- rsync 增量拷贝（排除 live sqlite）
- sqlite3 在线备份 API 单独备份 chroma.sqlite3（保证一致）
- 保留份数由 MEM_SNAPSHOT_KEEP 控制，**默认 1**（2026-11-19 Brian 定「暂定只留一份」）
- 清理用 Python 删，避免 shell 通配误伤
- **安全闸**：只留一份的前提是那一份被证明可恢复。清理前先验证最新快照
  （sqlite 完整性 + 节点数 + L1 额外源），验证不过则**一份都不删**。
  理由：份数从 7 降到 1 后，一次坏写入当天就会污染唯一副本；
  11-16 的 135 条污染与误删 2 份基线，都是靠多份历史才救回来的。
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
# 2026-11-19：Brian 定「暂定只留一份」。原为 7。
# 可用 MEM_SNAPSHOT_KEEP 随时调回（例如 =7 恢复一周滚动）。
KEEP = max(1, int(os.environ.get("MEM_SNAPSHOT_KEEP", "1")))
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


def verify_snapshot(d: Path) -> tuple[bool, str]:
    """证明这份快照**可恢复**，而不只是「文件存在」。

    2026-11-19：KEEP 降到 1 后这是唯一的回退副本，必须正向验证才允许删旧份。
    检查项（任一不过即拒删）：
      1 snapshot.json 可解析
      2 chroma/chroma.sqlite3 存在且 PRAGMA integrity_check == ok
      3 该库里有语义节点（空库不算可恢复）
      4 L1 额外源 workspace 里有每日笔记（事实源在快照里）
    """
    try:
        json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"snapshot.json 不可用：{type(e).__name__}"
    db = d / "chroma" / "chroma.sqlite3"
    if not db.exists():
        return False, "缺 chroma.sqlite3"
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            ic = con.execute("PRAGMA integrity_check").fetchone()
            if not ic or str(ic[0]).lower() != "ok":
                return False, f"integrity_check={ic}"
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "embeddings" not in tables:
                return False, "库里没有 embeddings 表"
            n = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            if n <= 0:
                return False, "embeddings 为空"
    except sqlite3.Error as e:
        return False, f"sqlite 打不开：{e}"
    ws = d / "workspace"
    if not ws.is_dir() or not list(ws.glob("2026-*.md")):
        return False, "快照里没有 L1 每日笔记"
    return True, f"integrity=ok embeddings={n} L1笔记={len(list(ws.glob('2026-*.md')))}"


def prune() -> list[str]:
    """删旧份。**先验证要保留的最新份可恢复**，不过则一份都不删。"""
    dirs = sorted([d for d in DEST.iterdir() if d.is_dir()]) if DEST.exists() else []
    if len(dirs) <= KEEP:
        return []
    keepers = dirs[-KEEP:]
    for k in keepers:
        ok, why = verify_snapshot(k)
        if not ok:
            print(f"⚠️ 保留份 {k.name} 未通过可恢复验证（{why}）→ **不清理任何旧份**",
                  file=sys.stderr)
            return []
    removed = []
    for d in dirs[:-KEEP]:
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
