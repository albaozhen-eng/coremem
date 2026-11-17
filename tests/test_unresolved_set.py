#!/usr/bin/env python3
"""持久未决集合回归（2026-11-17，四模型独立复核一致要求）。

背景：probe_write_fidelity 的样本是**近 24h 滚动窗口**，
任何超过 24h 的失败写入会**永久隐形**。实测账本里 5 条最新状态仍为 failed
（10-30 / 11-15 / 11-16），窗口滑过后探针显示 100% 绿 ——
历史教训「告警自己消失」的活体重演，在复核后几小时内就被实证。

probe_unresolved 扫全量账本、按 ref 归约最新状态，分两类：
  data_loss     L1 解析不到或 sha 不符 → 事实源真丢，零容忍（红）
  index_pending L1 已核验但 L2 非 ok → 索引待补，**不是数据丢失**（黄）
只有正向核验通过才移出集合。

本测试**不调用被测函数当自己的裁判**（复核批评过这一点）：
断言直接读返回的 level/detail，并独立用 mo_common.resolve 核对事实。

运行：python3 tests/test_unresolved_set.py
"""
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("mh", SRC / "memory_health.py")
mh = importlib.util.module_from_spec(spec)
sys.modules["mh"] = mh
spec.loader.exec_module(mh)
M = mh.mo_common

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


check("有 probe_unresolved", hasattr(mh, "probe_unresolved"))
check("已注册进探针列表",
      "probe_unresolved" in (SRC / "memory_health.py").read_text().split("def probe_unresolved")[-1])

L = M.LEDGER
if not L.exists():
    print("  [SKIP] 无部署账本（干净 clone），仅代码层断言")
else:
    bak = L.with_suffix(".jsonl.testbak")
    shutil.copy(L, bak)
    TMPREF = "2099-01-01.md:77777"
    try:
        print("=== 负控1：事实源核验失败必须判红 ===")
        with open(L, "a") as f:
            f.write(json.dumps({"ts": "2026-10-01T02:00:00+08:00", "ref": TMPREF,
                                "sha": "deadbeef", "l2_status": "failed",
                                "note": "测试注入"}, ensure_ascii=False) + "\n")
        c = mh.probe_unresolved()
        check("L1 查不到的 failed → red", c["level"] == "red", f"实得 {c['level']}")
        check("清单里点名该 ref", TMPREF in str(c.get("detail")))
        check("独立核对：该 ref 确实解析不到", M.resolve(TMPREF) is None)

        print("=== 负控2：不因后续成功写入而消失（核心）===")
        with open(L, "a") as f:
            for i in range(25):
                f.write(json.dumps({"ts": f"2026-11-17T12:{i:02d}:00+08:00",
                                    "ref": f"2026-11-17.md:{9500 + i}", "sha": "x",
                                    "l2_status": "ok"}, ensure_ascii=False) + "\n")
        c = mh.probe_unresolved()
        check("再写 25 条成功后，旧 failed 仍在清单",
              TMPREF in str(c.get("detail")), "这条红了说明重演了「告警自己消失」")

        print("=== 对照：窗口式探针会漏掉它（证明新探针不是多余）===")
        c2 = mh.probe_write_fidelity()
        check("write_fidelity(24h窗口) 看不见该旧失败",
              TMPREF not in str(c2.get("detail")),
              "若它也看得见，说明本测试的对照前提变了，需重新评估")

        print("=== 正向核验通过才移出集合 ===")
        # 用一个真实存在且 sha 正确的 ref 追加 ok，应不在未决集合里
        rows = M.read_jsonl(L)
        good = next((r for r in rows if r.get("l2_status") == "ok"
                     and r.get("ref") and M.resolve(r["ref"]) is not None), None)
        if good:
            c3 = mh.probe_unresolved()
            check("已 ok 且 L1 可解析的 ref 不出现在未决清单",
                  good["ref"] not in str(c3.get("detail")))
        else:
            print("  [SKIP] 找不到可用的正向样本")
    finally:
        shutil.copy(bak, L)
        bak.unlink()
    c = mh.probe_unresolved()
    check("还原后注入的 ref 已消失", TMPREF not in str(c.get("detail")))

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
