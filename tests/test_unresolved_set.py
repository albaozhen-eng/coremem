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
    # 2026-11-20：ref 必须**每次唯一**。原来写死 2099-01-01.md:77777，
    # 而早先手工负控在真实账本里留下了同 ref 的 retired/ack 行 →
    # 探针**正确地**按设计排除已退役 ref，测试却误判为「核心不变量被打破」。
    # 是测试数据被历史污染，不是代码回归。改用时间戳+pid 保证唯一。
    import os, time
    TMPREF = f"2099-01-01.md:{int(time.time()) % 100000 + os.getpid() % 1000}"
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

        print("=== 对照：两处机制的覆盖关系（2026-11-19 更新前提）===")
        # 原断言是「write_fidelity 的 24h 窗口看不见旧失败」，用来证明本探针不多余。
        # 2026-11-19 同步活体后发现：另一会话在 11-17 也给 write_fidelity 加了
        # 全量账本固化（.unacked-l2-failures.json），它现在**也看得见**旧失败。
        # 前提真的变了，故按事实改断言 —— 不是为了让测试变绿。
        # 现状：两处机制功能重叠，都能兜住「告警自己消失」。重叠是维护负担，
        # 已在 docs 记录待合并；但**不擅自删任一机制**（另一方成果需先理解）。
        c2 = mh.probe_write_fidelity()
        both = TMPREF in str(c2.get("detail"))
        check("旧失败至少被一处机制看见（核心不变量）",
              TMPREF in str(mh.probe_unresolved().get("detail")) or both)
        print(f"    现状：write_fidelity {'也' if both else '不'}看得见 → "
              f"{'两处重叠，待合并' if both else '仅 unresolved 覆盖'}")

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
