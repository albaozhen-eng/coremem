#!/usr/bin/env python3
"""缓冲区重放（flush）的语义测试。

复核指出：`test_write_verify` 完全没覆盖 flush，而 flush 恰是曾经无限重试的路径。
这里测 4 件事：
  1. 部分失败时**只保留失败条目**（旧实现保留全部 → 成功的会被重复写入）
  2. 已写进 L2 的条目被幂等跳过（2b：session 回查不可用时靠账本兜底）
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
    m._already_in_l2 = lambda entry, **k: (False, "")   # 封闭：不请求 PlugMem
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

print("=== 2b) session 回查不可用 → 靠账本兜底跳过 ===")
# 复核第五轮指出：用例 2 把 session_status mock 成 (1,None)，在第一优先级就返回了，
# 把 _ledger_has 改成恒 False 照样通过 —— 账本兜底分支根本没被测到。
# 这里让 session 回查返回「未知」，并把文本兜底指向不可达地址（封闭、不请求 PlugMem），
# 这样能让写入被跳过的只有账本这一条路径。
def _case_2b(tmp, ledger_override=None):
    m = fresh(tmp)
    ref, sha = "2026-10-22.md:9", "abcdef0123456789"
    m.LEDGER.write_text(json.dumps({"ts": "2026-10-22T10:00:00+08:00", "ref": ref, "sha": sha,
                                    "l2_status": "ok"}) + "\n", encoding="utf-8")
    buf = tmp / "memory_buffer.jsonl"
    buf.write_text(json.dumps({"timestamp": "2026-10-22T10:00:01+08:00", "text": "账本兜底项",
                               "source_line": ref, "sha": sha}, ensure_ascii=False) + "\n", encoding="utf-8")
    calls = []
    m._http_write_memory = lambda *a, **k: (calls.append(a[0]), True)[1]
    m.session_status = lambda sid, **k: (None, "unreachable")   # 回查拿不到结论
    m.SEMANTIC_BASE_URL = "http://127.0.0.1:9"                  # 文本兜底必然失败
    if ledger_override is not None:
        m._ledger_has = ledger_override
    m.flush_buffer(buf)
    return calls, buf

with tempfile.TemporaryDirectory() as td:
    calls, buf = _case_2b(Path(td))
    check("账本命中 → 未发出写入请求", calls == [], f"calls={calls}")
    check("缓冲区已清空", not buf.exists() or not buf.read_text().strip())
with tempfile.TemporaryDirectory() as td:
    # 对照：把账本判定关掉，同样的输入必须真的发出写入 —— 证明上面的通过来自账本分支
    calls, _ = _case_2b(Path(td), ledger_override=lambda ref, sha: False)
    check("对照：_ledger_has 恒 False 时会发出写入", calls == ["账本兜底项"], f"calls={calls}")
print("=== 3) 200 但回查未落地 → 不再放回 buffer（终止无限重试）===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp)
    # 复核指出：原用例的条目不带 source_line/sha，`if ref and sha` 不成立 →
    # 走不到被测分支，等于空测。这里补上 provenance。
    buf = tmp / "memory_buffer.jsonl"
    buf.write_text(json.dumps({"timestamp": "2026-10-22T10:00:00+08:00", "text": "无限重试项",
                               "source_line": "2026-10-22.md:9", "sha": "feedfacefeedface"},
                              ensure_ascii=False) + "\n", encoding="utf-8")
    m.session_status = lambda sid, **k: (0, None)      # 回查永远查不到
    m._already_in_l2 = lambda entry, **k: (False, "")  # 确保不被幂等短路
    m._http_write_memory = lambda *a, **k: True        # 接口永远 200
    for _ in range(3):
        m.flush_buffer(buf)
    check("三轮之后缓冲区仍未堆积", len(left(buf)) == 0, f"剩 {len(left(buf))} 条")
    rows3 = [json.loads(l) for l in m.LEDGER.read_text().splitlines() if l.strip()] if m.LEDGER.exists() else []
    check("留下了 missing 账本（而不是悄悄消失）",
          any(r.get("l2_status") == "missing" for r in rows3), f"{len(rows3)} 行")

print("=== 4) 终态缺失会落一条 missing 账本（有界重试之后）===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); m = fresh(tmp)
    buf = tmp / "memory_buffer.jsonl"
    buf.write_text(json.dumps({"timestamp": "2026-10-22T10:00:00+08:00", "text": "缺失项",
                               "source_line": "2026-10-22.md:9", "sha": "deadbeefdeadbeef"},
                              ensure_ascii=False) + "\n", encoding="utf-8")
    m.session_status = lambda sid, **k: (0, None)
    m._http_write_memory = lambda *a, **k: True
    m._already_in_l2 = lambda entry, **k: (False, "")   # 封闭：不请求 PlugMem
    # 2026-10-26：改为有界重试 —— 首次 flush 只留缓冲区，跑满 MAX_L2_RETRY 次才判 missing。
    # 旧用例假设"一次即丢"，前提已被实测推翻（重放可恢复），故改为循环 flush。
    rounds = 0
    for _ in range(getattr(m, "MAX_L2_RETRY", 3) + 1):
        rounds += 1
        m.flush_buffer(buf)
        rows_now = [json.loads(l) for l in m.LEDGER.read_text().splitlines() if l.strip()] if m.LEDGER.exists() else []
        if any(r.get("l2_status") == "missing" for r in rows_now):
            break
    rows = [json.loads(l) for l in m.LEDGER.read_text().splitlines() if l.strip()] if m.LEDGER.exists() else []
    check("不是一次就判 missing（有界重试生效）", rounds > 1, f"实际 flush {rounds} 次")
    check("账本里有 missing 行", any(r.get("l2_status") == "missing" for r in rows), f"{rows}")
    ts = rows[-1].get("ts", "") if rows else ""
    # 复核指出：原先用字符串特征判断，纯日期 "2026-10-23" 或 "+0800" 都会误判通过。
    # 改为真正解析并检查 tzinfo。
    from datetime import datetime as _dt
    try:
        _parsed = _dt.fromisoformat(ts)
        _has_tz = _parsed.tzinfo is not None
    except ValueError:
        _has_tz = False
    check("账本时间戳带时区（否则探针做时间相减会崩）", _has_tz, f"ts={ts!r}")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
