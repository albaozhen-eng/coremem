#!/usr/bin/env python3
"""缓冲区重放（flush）的语义测试。

复核指出：`test_write_verify` 完全没覆盖 flush，而 flush 恰是曾经无限重试的路径。
这里测 4 件事：
  1. 部分失败时**只保留失败条目**（旧实现保留全部 → 成功的会被重复写入）
  2. 已写进 L2 的条目被幂等跳过
  3. 「接口 200 但回查未落地」→ 不再放回 buffer（终止无限重试）
  4. 终态缺失会落一条 `missing` 账本（供探针告警，而不是悄悄消失）

运行：python3 tests/test_flush.py
"""
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))          # memory_write_buffer 依赖同目录的 plugmem_config
MWB = SRC / "memory_write_buffer.py"
assert MWB.exists(), f"必须测仓库代码，找不到 {MWB}"

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def fresh(tmp):
    spec = importlib.util.spec_from_file_location("mwb_repo", MWB)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.LEDGER = tmp / "write_ledger.jsonl"
    return m


def mk(tmp, n):
    buf = tmp / "memory_buffer.jsonl"
    entries = [{"timestamp": f"2026-10-22T10:0{i}:00+08:00", "text": f"条目{i}"} for i in range(n)]
    buf.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return buf, entries


def left(buf):
    return [json.loads(l) for l in buf.read_text().splitlines() if l.strip()] if buf.exists() else []


print("=== 1) 部分失败：只保留失败条目 ===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp); buf, _ = mk(tmp, 6)
    sent = []
    def w(text, tags=None, source_line=None, sha=None):
        sent.append(text)
        return text != "条目3"          # 第 4 条失败
    m._http_write_memory = w
    m.flush_buffer(buf)
    rest = left(buf)
    check("只剩失败的那 1 条", len(rest) == 1 and rest[0]["text"] == "条目3",
          f"剩 {[e['text'] for e in rest]}")

print("=== 2) 已写进 L2 的条目被幂等跳过 ===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp)
    ref, sha = "2026-10-22.md:9", "abcdef0123456789"
    m.LEDGER.write_text(json.dumps({"ts": "2026-10-22T10:00:00+08:00", "ref": ref, "sha": sha,
                                    "l2_status": "ok"}) + "\n", encoding="utf-8")
    buf = tmp / "memory_buffer.jsonl"
    buf.write_text(json.dumps({"timestamp": "2026-10-22T10:00:01+08:00", "text": "幂等项",
                               "source_line": ref, "sha": sha}, ensure_ascii=False) + "\n", encoding="utf-8")
    calls = []
    m._http_write_memory = lambda *a, **k: (calls.append(a[0]), True)[1]
    m.session_status = lambda sid, **k: (1, None)      # 回查命中
    m.flush_buffer(buf)
    check("未发出写入请求", calls == [], f"calls={calls}")
    check("缓冲区已清空", not buf.exists() or not buf.read_text().strip())

print("=== 3) 200 但回查未落地 → 不再放回 buffer（终止无限重试）===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp); buf, _ = mk(tmp, 1)
    m.session_status = lambda sid, **k: (0, None)      # 回查永远查不到
    m._http_write_memory = lambda *a, **k: True        # 接口永远 200
    for _ in range(3):
        m.flush_buffer(buf)
    check("三轮之后缓冲区仍未堆积", len(left(buf)) == 0, f"剩 {len(left(buf))} 条")

print("=== 4) 终态缺失会落一条 missing 账本 ===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp)
    buf = tmp / "memory_buffer.jsonl"
    buf.write_text(json.dumps({"timestamp": "2026-10-22T10:00:00+08:00", "text": "缺失项",
                               "source_line": "2026-10-22.md:9", "sha": "deadbeefdeadbeef"},
                              ensure_ascii=False) + "\n", encoding="utf-8")
    m.session_status = lambda sid, **k: (0, None)
    m._http_write_memory = lambda *a, **k: True
    m.flush_buffer(buf)
    rows = [json.loads(l) for l in m.LEDGER.read_text().splitlines() if l.strip()] if m.LEDGER.exists() else []
    check("账本里有 missing 行", any(r.get("l2_status") == "missing" for r in rows), f"{rows}")
    ts = rows[-1].get("ts", "") if rows else ""
    check("账本时间戳带时区（否则探针做时间相减会崩）",
          ts.endswith("+08:00") or (len(ts) >= 6 and (ts[-6] in "+-" or ts[-5] in "+-")),
          f"ts={ts!r}")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
