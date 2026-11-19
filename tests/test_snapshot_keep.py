#!/usr/bin/env python3
"""快照保留闸回归（2026-11-19，Brian 定「暂定只留一份」）。

KEEP 从 7 降到 1 后，留下的那一份是**唯一回退副本**。
所以删旧份之前必须**正向证明它可恢复**，而不只是「文件存在」。
理由：11-16 的 135 条污染与误删 2 份基线，都是靠多份历史才救回来的；
只留一份时，一次坏写入当天就会污染唯一副本。

本测试在临时目录造假快照，**不动生产备份**。
锁住：坏份必须阻止清理（宁可多占磁盘，也不能删到没有可用副本）。

运行：python3 tests/test_snapshot_keep.py
"""
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
spec = importlib.util.spec_from_file_location("pb", SRC / "plugmem_backup.py")
pb = importlib.util.module_from_spec(spec)
sys.modules["pb"] = pb
spec.loader.exec_module(pb)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def make_snapshot(root: pathlib.Path, name: str, *, good=True, break_what=None):
    d = root / name
    (d / "chroma").mkdir(parents=True, exist_ok=True)
    (d / "workspace").mkdir(parents=True, exist_ok=True)
    (d / "snapshot.json").write_text(json.dumps({"created_at": "x", "bytes": 1}))
    (d / "workspace" / "2026-11-19.md").write_text("- 2026-11-19T10:00:00+08:00 事实\n")
    db = d / "chroma" / "chroma.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY)")
        if good:
            con.execute("INSERT INTO embeddings VALUES (1)")
    if break_what == "json":
        (d / "snapshot.json").write_text("{ 损坏")
    elif break_what == "db":
        db.write_bytes(b"not a sqlite file at all")
    elif break_what == "empty":
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM embeddings")
    elif break_what == "no_l1":
        for f in (d / "workspace").glob("*.md"):
            f.unlink()
    elif break_what == "no_db":
        db.unlink()
    return d


print("=== 默认份数与可配置 ===")
check("默认 KEEP=1（Brian 定）", pb.KEEP == 1, f"实得 {pb.KEEP}")
check("有 verify_snapshot", hasattr(pb, "verify_snapshot"))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="snapkeep-"))
pb.DEST = TMP
pb.KEEP = 1

print("=== 好份：验证通过，允许清理旧份 ===")
for n in ("2026-11-13", "2026-11-14", "2026-11-15"):
    make_snapshot(TMP, n)
ok, why = pb.verify_snapshot(TMP / "2026-11-15")
check("完整快照通过验证", ok, why)
removed = pb.prune()
check("清掉 2 份旧份", len(removed) == 2, f"{removed}")
check("最新份仍在", (TMP / "2026-11-15").exists())

print("=== 负控：保留份坏了 → 一份都不许删 ===")
CASES = [
    ("json", "snapshot.json 损坏"),
    ("db", "sqlite 文件损坏"),
    ("empty", "embeddings 空库"),
    ("no_l1", "快照里没有 L1 笔记"),
    ("no_db", "缺 chroma.sqlite3"),
]
for what, label in CASES:
    sub = pathlib.Path(tempfile.mkdtemp(prefix=f"snapbad-{what}-"))
    pb.DEST = sub
    make_snapshot(sub, "2026-11-13")
    make_snapshot(sub, "2026-11-14")
    make_snapshot(sub, "2026-11-15", break_what=what)
    ok, why = pb.verify_snapshot(sub / "2026-11-15")
    check(f"{label} → 验证不通过", not ok, why)
    removed = pb.prune()
    n_left = len([d for d in sub.iterdir() if d.is_dir()])
    check(f"{label} → 不清理任何旧份", removed == [] and n_left == 3,
          f"removed={removed} 剩{n_left}份")

print("=== 边界：份数不足 KEEP 时不动 ===")
sub = pathlib.Path(tempfile.mkdtemp(prefix="snapone-"))
pb.DEST = sub
make_snapshot(sub, "2026-11-19")
check("只有 1 份时不清理", pb.prune() == [])
check("那 1 份仍在", (sub / "2026-11-19").exists())

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
