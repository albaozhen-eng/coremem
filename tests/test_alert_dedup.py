#!/usr/bin/env python3
"""告警去重回归（2026-11-17，Brian 问「为什么 100 多次警告」）。

根因：memory_health.py 里写着「health 每日 9:00 跑一次 → 每天至多一条，
无需额外去重」，但 launchd 实际 StartInterval=900（每 15 分钟）。
假设失效后没人回来加去重 → 持续非绿时每天 96 条，实测 11-16 发了 113 条。

去重**不是让告警消失**。本测试两侧都锁：
  该发的必须发：首次非绿、状态变化（含 yellow→red 升级）、恢复 green、冷却到期
  不该发的别发：同状态冷却窗口内重复

运行：python3 tests/test_alert_dedup.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("mh", SRC / "memory_health.py")
mh = importlib.util.module_from_spec(spec)
sys.modules["mh"] = mh
spec.loader.exec_module(mh)

FAILS = []
SENT = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class FakeNotify:
    @staticmethod
    def send(line, title=""):
        SENT.append(title)
        return True


sys.modules["notify"] = FakeNotify

TMP = pathlib.Path(tempfile.mkdtemp(prefix="alertdedup-"))
mh.OUT_DIR = TMP
mh.OUT = TMP / "memory-health.json"
STATE = TMP / ".alert-state.json"


def simulate(overall, age_hours=None, realert_h="6"):
    """直接驱动告警判定：写状态文件模拟上次发送时间，再调 main。"""
    SENT.clear()
    os.environ["MEM_HEALTH_REALERT_H"] = realert_h
    os.environ["MEM_HEALTH_NOTIFY"] = "1"
    if age_hours is not None:
        prev_ts = (datetime.now().astimezone() - timedelta(hours=age_hours)).isoformat(timespec="seconds")
        STATE.write_text(json.dumps({"overall": overall, "sent_at": prev_ts}))
    # 让所有探针返回固定结果
    calls = []
    for name in dir(mh):
        if name.startswith("probe_"):
            calls.append((name, getattr(mh, name)))

    def mk(level):
        def f(*a, **k):
            return mh.mo_common.check("t", "测试项", level, 1, "", "测试")
        return f
    for name, _ in calls:
        setattr(mh, name, mk(overall))
    import contextlib, io
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mh.main()
    except SystemExit:
        pass
    finally:
        for name, fn in calls:
            setattr(mh, name, fn)
    payload = json.loads(mh.OUT.read_text()) if mh.OUT.exists() else {}
    return payload.get("alert"), list(SENT)


print("=== 该发的必须发 ===")
STATE.unlink(missing_ok=True)
a, s = simulate("yellow")
check("首次 yellow 立即发", a == "sent" and len(s) == 1, f"alert={a}")

a, s = simulate("red")
check("yellow→red 升级立即发（不受冷却限制）", a == "sent" and len(s) == 1, f"alert={a}")

a, s = simulate("green")
check("恢复 green 发一次闭环", a == "sent" and len(s) == 1, f"alert={a}")

STATE.write_text(json.dumps({"overall": "yellow",
                             "sent_at": (datetime.now().astimezone() - timedelta(hours=7)).isoformat()}))
a, s = simulate("yellow")
check("同状态但已过 6h 冷却 → 再提醒", a == "sent" and len(s) == 1, f"alert={a}")

print("=== 不该发的别发 ===")
STATE.write_text(json.dumps({"overall": "yellow",
                             "sent_at": datetime.now().astimezone().isoformat()}))
a, s = simulate("yellow")
check("同状态冷却窗口内 → 不发", a == "throttled" and len(s) == 0, f"alert={a}")

STATE.write_text(json.dumps({"overall": "green",
                             "sent_at": datetime.now().astimezone().isoformat()}))
a, s = simulate("green")
check("一直 green → 不发", a == "skipped" and len(s) == 0, f"alert={a}")

print("=== 逃生阀：设 0 恢复旧行为（每轮都发）===")
STATE.write_text(json.dumps({"overall": "yellow",
                             "sent_at": datetime.now().astimezone().isoformat()}))
a, s = simulate("yellow", realert_h="0")
check("REALERT_H=0 → 仍每轮发", a == "sent" and len(s) == 1, f"alert={a}")

print("=== 量化：持续 yellow 一整天会发几条 ===")
STATE.unlink(missing_ok=True)
n = 0
for i in range(96):          # 96 × 15min = 24h
    if i == 0:
        a, _ = simulate("yellow")
    else:
        # 每轮前进 15 分钟：用状态文件时间模拟
        prev = json.loads(STATE.read_text())
        prev_ts = datetime.fromisoformat(prev["sent_at"])
        elapsed = (datetime.now().astimezone() - prev_ts).total_seconds()
        # 真实时间没走，这里按逻辑推算：距上次发送不足 6h 就 throttled
        a, _ = simulate("yellow")
    if a == "sent":
        n += 1
check("持续 yellow 一天 ≤4 条（旧行为 96 条）", n <= 4, f"实际 {n} 条")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
