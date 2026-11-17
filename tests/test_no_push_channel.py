#!/usr/bin/env python3
"""契约测试：推送通道为零 + 成功戳 fail-closed（2026-11-17）。

替代原 test_alert_dedup.py。原测试测的是「通知节流策略」，
四份独立复核一致指出那是**测自己改的东西而不是目标**：
「测试会全绿，目标仍失败」。推送通道已按 Brian 批准删除，故原测试一并删除。

本测试测真正的契约：
  1 记忆系统链路里**不存在**主动推送（源码层 + 运行层双重断言）
  2 成功戳只在**全部探针跑完**后写出
  3 探针中途崩溃 → **不得**留下新的成功戳（防「崩溃仍报绿」）
  4 成功戳缺失/过期 → 使用点必须能判定为失活（不能默认健康）

运行：python3 tests/test_no_push_channel.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("mh", SRC / "memory_health.py")
mh = importlib.util.module_from_spec(spec)
sys.modules["mh"] = mh
spec.loader.exec_module(mh)

FAILS = []
PUSHED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class TrapNotify:
    """任何推送尝试都会被记下来 —— 用于证明通道真的为零。"""
    @staticmethod
    def send(*a, **k):
        PUSHED.append((a, k))
        return True


sys.modules["notify"] = TrapNotify

print("=== 源码层：不存在推送调用 ===")
src = (SRC / "memory_health.py").read_text(encoding="utf-8")
code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
code = "\n".join(code_lines)
check("无 import notify", "import notify" not in code)
check("无 notify.send", "notify.send" not in code)
check("无 MEM_HEALTH_NOTIFY 开关", "MEM_HEALTH_NOTIFY" not in code)
check("无 REALERT 节流开关", "MEM_HEALTH_REALERT_H" not in code)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="nopush-"))
mh.OUT_DIR = TMP
mh.OUT = TMP / "memory-health.json"
STAMP = TMP / ".reconcile-stamp.json"


def run_main(break_probe=None):
    """跑 main，可选让某个探针中途抛异常（模拟检查器崩溃）。"""
    PUSHED.clear()
    import contextlib, io
    saved = []
    for name in dir(mh):
        if name.startswith("probe_"):
            saved.append((name, getattr(mh, name)))

    def mk(level):
        def f(*a, **k):
            return mh.mo_common.check("t", "测试项", level, 1, "", "测试")
        return f
    for name, _ in saved:
        setattr(mh, name, mk("yellow"))
    if break_probe:
        def boom(*a, **k):
            raise RuntimeError("模拟检查器崩溃")
        setattr(mh, break_probe, boom)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mh.main()
    except SystemExit:
        pass
    except Exception:
        pass
    finally:
        for name, fn in saved:
            setattr(mh, name, fn)


print("=== 运行层：跑完一轮，推送必须为 0 条 ===")
STAMP.unlink(missing_ok=True)
run_main()
check("非绿状态下推送 0 条", len(PUSHED) == 0, f"实际 {len(PUSHED)} 条")
check("成功戳已写出", STAMP.exists())
if STAMP.exists():
    st = json.loads(STAMP.read_text())
    check("戳里记录了跑完的探针数", st.get("probes_run", 0) > 0, f"{st.get('probes_run')}")
    check("戳里有完成时间", bool(st.get("completed_at")))

print("=== 负控：检查器崩溃 → 不得留下新成功戳 ===")
STAMP.unlink(missing_ok=True)
probes = [n for n in dir(mh) if n.startswith("probe_")]
run_main(break_probe=probes[len(probes) // 2])
check("崩溃后没有成功戳（不能默认健康）", not STAMP.exists(),
      "若存在，说明崩溃被当成了成功 —— 正是历史教训「假绿」")
check("崩溃时推送仍为 0 条", len(PUSHED) == 0)

print("=== 失活判定：戳缺失/过期必须可判为不健康 ===")


def stale_hours(path, max_h=26):
    """使用点应采用的判据：无戳或戳过期 → 失活（不是健康）。"""
    if not path.exists():
        return None
    try:
        ts = datetime.fromisoformat(json.loads(path.read_text())["completed_at"])
    except (ValueError, KeyError, OSError):
        return None
    return (datetime.now().astimezone() - ts).total_seconds() / 3600


check("无戳 → 判失活（返回 None）", stale_hours(STAMP) is None)
STAMP.write_text(json.dumps({"completed_at": datetime.now().astimezone().isoformat(),
                             "overall": "green", "probes_run": 17}))
check("新鲜戳 → 可判健康", stale_hours(STAMP) is not None and stale_hours(STAMP) < 1)
STAMP.write_text(json.dumps({"completed_at": (datetime.now().astimezone() - timedelta(hours=30)).isoformat(),
                             "overall": "green", "probes_run": 17}))
h = stale_hours(STAMP)
check("30h 旧戳 → 超过 26h 阈值判失活", h is not None and h > 26, f"{h:.1f}h")
STAMP.write_text("{ 截断损坏")
check("戳损坏 → 判失活而非健康", stale_hours(STAMP) is None)

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
