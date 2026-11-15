#!/usr/bin/env python3
"""吞吐量探针回归：必须区分「没输入」与「有输入却零产出」。

2026-11-15 审计背景：L1 断供 2 天，只有 l1 探针报红。
write_fidelity / dup_guard 读的都是账本历史行 —— 只验证「过去写对了」，
管道彻底停产它们照样全绿。缺的是「单位时间产出」这个量。

同时锁住 11-06 的教训：**不能把空闲当故障**。
当时哨兵把「56h 没人说话」报成「LLM 全通道死」，三个假设全是故障。

三个分支（全部用临时副本，不碰生产数据）：
  A 有输入 + 有产出   → green
  B 无输入 + 零产出   → green（空闲不是故障）
  C 有输入 + 零产出   → red  （真断供）

运行：python3 tests/test_throughput_probe.py
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "memory_health.py"
assert SRC.exists(), f"必须测仓库代码，找不到 {SRC}"
sys.path.insert(0, str(HERE.parent / "src"))

spec = importlib.util.spec_from_file_location("mh_tp", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["mh_tp"] = m
spec.loader.exec_module(m)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def scenario(fact_hours_ago, sess_hours_ago):
    """造一个隔离环境：事实池最后一条在 fact_hours_ago 前，会话 mtime 在 sess_hours_ago 前。"""
    td = pathlib.Path(tempfile.mkdtemp())
    mem = td / "memory"
    mem.mkdir()
    ts = (datetime.now(timezone.utc) - timedelta(hours=fact_hours_ago)).isoformat()
    (mem / "unified-facts.jsonl").write_text(
        json.dumps({"timestamp": ts, "content": "x"}) + "\n", encoding="utf-8")
    sd = td / "home" / ".pi" / "agent" / "sessions" / "proj"
    sd.mkdir(parents=True)
    f = sd / "s.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    t = time.time() - sess_hours_ago * 3600
    os.utime(f, (t, t))
    m.MEMORY_DIR = mem
    m.HOME = td / "home"
    return td


print("=== A：有输入 + 有产出 → green ===")
td = scenario(1, 1)
r = m.probe_throughput()
check("判绿", r["level"] == "green", f"{r['level']} | {str(r.get('detail'))[:50]}")
check("报出新增条数", (r.get("value") or 0) > 0, f"value={r.get('value')}")
shutil.rmtree(td, ignore_errors=True)

print("=== B：无输入 + 零产出 → green（空闲不是故障）===")
td = scenario(50, 50)
r = m.probe_throughput()
check("判绿（不重演把空闲当故障）", r["level"] == "green",
      f"{r['level']} | {str(r.get('detail'))[:50]}")
check("说明是无输入", "无输入" in str(r.get("detail")), str(r.get("detail"))[:60])
shutil.rmtree(td, ignore_errors=True)

print("=== C：有输入 + 零产出 → red（真断供）===")
td = scenario(50, 1)
r = m.probe_throughput()
check("判红", r["level"] == "red", f"{r['level']} | {str(r.get('detail'))[:50]}")
check("说明是真断供", "真断供" in str(r.get("detail")), str(r.get("detail"))[:60])
shutil.rmtree(td, ignore_errors=True)

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
