#!/usr/bin/env python3
"""记忆最小闭环补链（T012，2026-09-28）

现状：capture→remember 每 30min 正常写 unified-facts.jsonl（3637 条，实测 17:46 仍在写），
但它是流水账，没有可检索索引；同目录 memory.db 三表 0 行是空壳。
本脚本只补一件事：把 unified-facts.jsonl 增量索引进 FTS5，并提供召回查询。

红线：只读 jsonl，绝不改写/删除任何既有文件；DB 只新建 facts.db。
失败即 exit 1（供哨兵/告警接）。

用法：
  python3 memory_index.py            # 增量索引
  python3 memory_index.py --query 宗教   # 召回
  python3 memory_index.py --stats     # 状态
"""
import json, os, sqlite3, sys, time
from pathlib import Path

MEM = Path.home() / ".coremem/memory"
FACTS = MEM / "unified-facts.jsonl"
DB = MEM / "facts.db"
STATE = MEM / "facts_index_state.json"


def db():
    c = sqlite3.connect(str(DB), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    # 内容表 + FTS5 影子表（外部内容模式，避免正文双份存储）
    c.execute("""CREATE TABLE IF NOT EXISTS facts(
        id INTEGER PRIMARY KEY, ts TEXT, content TEXT, tags TEXT, src TEXT, h TEXT UNIQUE)""")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
        USING fts5(content, tags, content='facts', content_rowid='id', tokenize='trigram')""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, content, tags) VALUES(new.id, new.content, new.tags); END""")
    return c


def index():
    if not FACTS.exists():
        print(f"🔴 事实池不存在：{FACTS}")
        return 1
    st = {"offset": 0}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
        except Exception:
            pass
    off = int(st.get("offset", 0))
    size = FACTS.stat().st_size
    if size < off:  # 事实池被换过（变短）：全量重建索引，不动源文件
        print(f"ℹ️ 事实池变小（{size} < {off}）→ 仅重建索引（源文件不动）")
        off = 0
        c = db()
        c.execute("DELETE FROM facts")
        c.execute("DELETE FROM facts_fts")
        c.commit()
        c.close()
    n = 0
    c = db()
    with open(FACTS, encoding="utf-8", errors="ignore") as f:
        f.seek(off)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            txt = (j.get("content") or "").strip()
            if not txt:
                continue
            tags = json.dumps(j.get("tags") or [], ensure_ascii=False)
            ts = j.get("timestamp") or ""
            h = str(hash((ts, txt)))
            try:
                c.execute("INSERT OR IGNORE INTO facts(ts,content,tags,src,h) VALUES(?,?,?,?,?)",
                         (ts, txt[:4000], tags, str(FACTS), h))
                n += c.total_changes and 1 or 0
            except Exception as e:
                print(f"🔴 插入失败 {str(e)[:160]}")
        newoff = f.tell()
    c.commit()
    tot = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    c.close()
    STATE.write_text(json.dumps({"offset": newoff, "indexed_total": tot, "ts": time.strftime("%F %T")},
                                ensure_ascii=False, indent=2))
    print(f"✅ 索引增量 {n} 条（新增字节 {size - off}），facts 总 {tot} 条｜DB={DB}")
    return 0


def query(q, limit=8):
    if not DB.exists():
        print("🔴 索引未建立，先跑 memory_index.py")
        return 1
    c = db()
    try:
        rows = c.execute("SELECT f.ts,f.content,f.tags FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid WHERE facts_fts MATCH ? ORDER BY ts DESC LIMIT ?", (q, limit)).fetchall()
    except Exception as e:
        print(f"🔴 FTS 查询失败：{e}")
        return 1
    print(f"🔎 「{q}」命中 {len(rows)} 条（最近 {limit} 条）")
    for ts, txt, tags in rows:
        print(f"  · {ts[:16]} | {txt[:150]}")
    c.close()
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--query":
        sys.exit(query(" ".join(a[1:]) or "记忆"))
    if a and a[0] == "--stats":
        if DB.exists():
            c = db()
            print("facts:", c.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                  "| 最早:", c.execute("SELECT MIN(ts) FROM facts").fetchone()[0],
                  "| 最新:", c.execute("SELECT MAX(ts) FROM facts").fetchone()[0])
            print("state:", STATE.read_text() if STATE.exists() else "无")
        else:
            print("🔴 facts.db 不存在")
        sys.exit(0)
    sys.exit(index())
