#!/usr/bin/env python3
"""采集韧性回归：有界重试 + 本地两级兜底。

2026-11-15 审计背景：L1 断供 2 天。两个真因：
  ① 本地 Flash 被 oMLX 内存守卫拒（HTTP 400，max_context=131072 → prefill 需 122GB），
     而当时本地只有这一条通道，云端偶发限流即全链断。
  ② 默认模式**一次提取失败就 break 整轮**，后面本来能成的 session 全被跳过。

本测试锁住修复后的行为，不依赖网络：
  A 默认模式连续失败 3 次才停轮（不是 1 次）
  B 补跑模式连续失败 5 次才停轮
  C 失败的 session 不被标记 processed（下轮仍会重试，不丢）
  D 通道配置里本地档位有 ≥2 条（单点已消除）

运行：python3 tests/test_capture_resilience.py
"""
import importlib.util
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "memory_capture.py"
assert SRC.exists(), f"必须测仓库代码，找不到 {SRC}"

spec = importlib.util.spec_from_file_location("mc_resil", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["mc_resil"] = m
spec.loader.exec_module(m)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def run(continue_on_error, n_sessions=6):
    """注入"提取永远失败"，返回 (提取调用次数, 状态里的 processed 数)。"""
    td = pathlib.Path(tempfile.mkdtemp())
    sess = []
    for i in range(n_sessions):
        f = td / f"s{i}.jsonl"
        f.write_text(json.dumps({"type": "message",
                                 "message": {"role": "user", "content": "测试"}}) + "\n")
        sess.append({"stem": f"s{i}", "kind": "jsonl", "path": f,
                     "mtime": f.stat().st_mtime, "hash": None})
    m.STATE_FILE = td / "state.json"
    m.GATE_LOG_DIR = td / "gate"
    calls = {"n": 0}

    def _fail(conv):
        calls["n"] += 1
        return None                      # None = LLM 调用失败（区别于 [] = 无事实）

    m.extract_facts_via_llm = _fail
    # 必须 >200 字：capture 对 len(conversation)<200 直接 continue（不是失败）。
    # 第一版桩只有 190 字，被静默跳过，导致"提取 0 次"的假失败。
    m.extract_conversation = lambda *a, **k: "用户决定测试有界重试机制，这是一段足够长的正文内容用于越过门槛" * 10
    m.get_recent_sessions = lambda *a, **k: list(sess)
    m.triage_gate = lambda *a, **k: (True, "")
    try:
        m.capture(hours=1, dry_run=False, force=True,
                  max_sessions=n_sessions, continue_on_error=continue_on_error)
    except SystemExit:
        pass
    st = json.loads(m.STATE_FILE.read_text()) if m.STATE_FILE.exists() else {}
    return calls["n"], len(st.get("processed_sessions") or {})


print("=== A/B：有界重试（不是一次失败就停整轮）===")
n, _ = run(False)
check("默认模式连续失败 3 次才停轮", n == 3, f"实际提取 {n} 次")
n, _ = run(True)
check("补跑模式连续失败 5 次才停轮", n == 5, f"实际提取 {n} 次")

print("=== C：失败的 session 不被标记 processed ===")
_, processed = run(False)
check("失败不标 processed（下轮仍重试，不丢）", processed == 0, f"processed={processed}")

print("=== D：本地通道不再是单点 ===")
# 只读配置，不发请求。配置文件缺失时回落内置原型（那时本地只有 1 条，属于未修复态）。
cfg = m._providers_config()
order = cfg.get("capture_channels") or []
local_specs = [s for s in order if str(s).startswith("omlx")]
cfg_path = pathlib.Path.home() / ".coremem/config/providers.json"
if not cfg_path.exists():
    # 干净 clone / 新机器：没有部署配置，不算回退，跳过该项（只做代码层断言）
    print("  [SKIP] 本地通道冗余：无部署配置（新机器），仅检查代码支持多通道")
    check("代码支持按 capture_channels 建多条本地通道",
          hasattr(m, "_build_llm_channels"), "存在 _build_llm_channels")
elif order:
    check("配置里本地档位 ≥2 条", len(local_specs) >= 2,
          f"capture_channels 本地 {len(local_specs)} 条: {local_specs}")
else:
    check("部署配置里应有 capture_channels（否则本地仍是单点）", False,
          "providers.json 存在但未设 capture_channels")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
