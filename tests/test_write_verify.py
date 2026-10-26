#!/usr/bin/env python3
"""写入状态机测试（P3 + 复核修正版）。

复核指出的问题：初版把「接口 200 但回查查不到」一律判成 dedup_skipped（预期去重），
客户端其实拿不出相似度证据 —— 那会把真实静默丢失一并掩盖。
现在该状态为 `missing`，会告警、不重试。

覆盖 6 种组合：
  ok                  回查 count≥1                       → 不 buffer
  missing             200 且 count=0                     → 不 buffer（重试无效），带诊断线索
  failed              接口失败且 count=0                 → buffer 重试
  ok(B1)              接口失败但 count≥1（已生效）        → 不 buffer
  ok_unverified       200 但回查失败（不知道）            → 不 buffer
  failed_unverified   接口失败且回查失败                  → buffer

运行：python3 test_write_verify.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须测**本仓库**的 remember.py。复核指出：指向部署目录时，
# 仓库侧的回归永远测不出来 —— 那等于没测。
_HERE = Path(__file__).resolve().parent
SCRIPTS = _HERE.parent / "src"
REMEMBER = SCRIPTS / "remember.py"
assert REMEMBER.exists(), f"必须测仓库代码，找不到 {REMEMBER}"
sys.path.insert(0, str(SCRIPTS))

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def run_case(tag, http_ok, session_count):
    home = Path(tempfile.mkdtemp(prefix=f"wv-{tag}-"))
    os.environ.update(MEMORY_HOME=str(home), MEM_DAILY_NOTES_DIR=str(home / "memory"),
                      MEM_DATA_DIR=str(home / "data"),
                      MEM_SEMANTIC_GRAPH_ID="unused-in-test")
    sys.modules.pop("plugmem_config", None)
    spec = importlib.util.spec_from_file_location(f"rem_{tag}", REMEMBER)
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    calls = {"post": 0}
    def _post(*a, **k):
        calls["post"] += 1
        return http_ok
    R.write_to_semantic_index = _post
    # session_count 可以是固定值，也可以是序列（模拟"首查 0、重试后落地"）
    if isinstance(session_count, (list, tuple)):
        _seq = list(session_count)
        _state = {"i": 0}
        def _st(sid, **k):
            i = min(_state["i"], len(_seq) - 1); _state["i"] += 1
            v = _seq[i]
            return (v, None if v is not None else "boom")
        R.session_status = _st
    else:
        R.session_status = lambda sid, **k: (session_count, None if session_count is not None else "boom")
    R.near_dup_candidates = lambda text, **k: 3        # 固定诊断线索，便于断言
    res = R.remember(f"故障注入用例 {tag}", ["t"])
    led = home / "data" / "write_ledger.jsonl"
    row = json.loads(led.read_text().splitlines()[-1]) if led.exists() else {}
    buf = home / "data" / "memory_buffer.jsonl"
    n_buf = len([l for l in buf.read_text().splitlines() if l.strip()]) if buf.exists() else 0
    shutil.rmtree(home, ignore_errors=True)
    return row, n_buf, res, calls


print("=== 1) 接口 200，回查 count 恒为 0 → 有界重试后仍不落地 → failed + buffer ===")
row, nb, res, calls = run_case("missing", True, 0)
check("l2_status == 'failed'（不再一次即判 missing）", row.get("l2_status") == "failed",
      f"实际 {row.get('l2_status')}")
check("进入 buffer 继续重试", nb == 1, f"buffer {nb} 条")
check("账本带诊断线索 dup_candidates", row.get("dup_candidates") is not None,
      f"dup_candidates={row.get('dup_candidates')}")

print("=== 1b) 接口 200，首查 0 但重试后 count=1 → ok，不 buffer ===")
row, nb, _, calls = run_case("retry_ok", True, [0, 1])
check("l2_status == 'ok'", row.get("l2_status") == "ok", f"实际 {row.get('l2_status')}")
check("不 buffer（已经落地）", nb == 0, f"buffer {nb} 条")

print("=== 2) 接口失败且回查 count=0 → failed，进 buffer ===")
row, nb, _, calls = run_case("failed", False, 0)
check("l2_status == 'failed'", row.get("l2_status") == "failed", f"实际 {row.get('l2_status')}")
check("进入 buffer 重试", nb == 1, f"buffer {nb} 条")

print("=== 3) 接口失败但回查 count≥1（B1：其实已生效）→ ok，不 buffer ===")
row, nb, _, calls = run_case("b1", False, 1)
check("l2_status == 'ok'", row.get("l2_status") == "ok", f"实际 {row.get('l2_status')}")
check("不 buffer（避免重试重复写入）", nb == 0, f"buffer {nb} 条")

print("=== 4) 正常：接口 200 且 count≥1 → ok ===")
row, nb, _, calls = run_case("ok", True, 1)
check("l2_status == 'ok'", row.get("l2_status") == "ok", f"实际 {row.get('l2_status')}")
check("不 buffer", nb == 0, f"buffer {nb} 条")

print("=== 5) 接口 200 但回查失败（不知道）→ ok_unverified，不 buffer ===")
row, nb, _, calls = run_case("ou", True, None)
check("l2_status == 'ok_unverified'", row.get("l2_status") == "ok_unverified",
      f"实际 {row.get('l2_status')}")
check("不 buffer（不确定时保守，避免重复）", nb == 0, f"buffer {nb} 条")

print("=== 6) 接口失败且回查失败 → failed_unverified，进 buffer ===")
row, nb, _, calls = run_case("fu", False, None)
check("l2_status == 'failed_unverified'", row.get("l2_status") == "failed_unverified",
      f"实际 {row.get('l2_status')}")
check("进入 buffer", nb == 1, f"buffer {nb} 条")

print("=== 7) 回查报错（状态未知）不得重复写，也不入 buffer ===")
row, nb, _, calls = run_case("unknown", True, None)
check("未知态标 ok_unverified（与主路径一致）",
      row.get("l2_status") == "ok_unverified", f"实际 {row.get('l2_status')}")
check("未知态不转 buffer（转了可能重复写）", nb == 0, f"buffer {nb} 条")
check("未知态只 POST 一次", calls["post"] == 1, f"实际 {calls['post']} 次")

print("=== 8) 重试有次数上限，且每次重发前先回查 ===")
row, nb, _, calls = run_case("retry_cap", True, 0)
check("POST 次数不超过 1 + 重试上限", calls["post"] <= 1 + 2, f"实际 {calls['post']} 次")
check("仍然失败才转 buffer", nb == 1, f"buffer {nb} 条")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)