#!/usr/bin/env python3
"""P1-4：provenance 契约测试。

守护「证据链从写入到检索不丢」这条不变量。不依赖 pytest，可直接运行：

    PYTHONPATH=~/.openclaw/workspace/scripts python3 tests/test_provenance_contract.py

测试内容：
  C1  append_daily_log_ex() 返回的 ref 能解析回同一行，且 sha 与规范一致
  C2  sha 归一算法与 memory-ops 的 mo_common.line_sha() 一致（可用则交叉校验）
  C3  L1 追加格式与历史格式逐字一致（`\\n- {ts}{tags} {content}\\n`）
  C4  write_to_semantic_index() 在给 source_line 时把 session_id 放进 payload
  C5  不给 source_line 时不放 session_id（向后兼容）
  C6  fallback_to_buffer() 保留 source_line / sha
  C7  session_id 格式可逆（l1:<file>:<line>#<sha>）
  C8  remember() 端到端会落账本，且账本条目自带 ref/sha/l2_status
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def load_remember(home: Path):
    """在隔离的 MEMORY_HOME 下加载 remember 模块（避免碰到真实 L1）。"""
    os.environ["MEMORY_HOME"] = str(home)
    os.environ["MEM_DAILY_NOTES_DIR"] = str(home / "memory")
    os.environ["MEM_DATA_DIR"] = str(home / "data")
    os.environ["MEM_SEMANTIC_GRAPH_ID"] = "contract-test-does-not-write"
    spec = importlib.util.spec_from_file_location("remember_under_test", SRC / "remember.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    tmp = Path(tempfile.mkdtemp(prefix="prov-contract-"))
    try:
        R = load_remember(tmp)

        # C1 + C3
        loc = R.append_daily_log_ex("契约测试内容 X1", ["测试"])
        raw = (tmp / "memory" / f"{R.today_str()}.md").read_text()
        check("C1 返回 ref/sha", bool(loc and loc.get("ref") and loc.get("sha")), json.dumps(loc, ensure_ascii=False)[:120])
        line = raw.splitlines()[loc["line"] - 1]
        check("C1 ref 能解析回同一行", line == loc["text"])
        check("C1 sha 与行内容一致", R._line_sha(line) == loc["sha"])

        expected_prefix = "- "
        check("C3 L1 格式为 '- <ts>[tags] <content>'",
              line.startswith(expected_prefix) and "[测试]" in line and line.endswith("契约测试内容 X1"),
              repr(line[:90]))
        check("C3 文件以换行开头（与历史格式一致）", raw.startswith("\n"), repr(raw[:12]))

        # C2 交叉校验（memory-ops 的 mo_common 可用时）
        mo = None
        for cand in (Path.home() / ".openclaw/workspace/scripts/mo_common.py",
                     Path.home() / "projects/memory-ops/scripts/mo_common.py"):
            if cand.exists():
                spec = importlib.util.spec_from_file_location("mo_common_x", cand)
                mo = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mo)
                break
        if mo:
            check("C2 sha 与 mo_common.line_sha 一致",
                  mo.line_sha(line) == loc["sha"], f"{mo.line_sha(line)} vs {loc['sha']}")
        else:
            print("  [SKIP] C2 mo_common 不可用，跳过交叉校验")

        # C4 + C5：拦截 HTTP 层，检查 payload
        captured = {}

        class FakeResp:
            status = 200
            def read(self): return b'{"status":"ok"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return FakeResp()

        R.urllib.request.urlopen = fake_urlopen
        R.write_to_semantic_index("带来源的内容", ["t"], source_line=loc["ref"], sha=loc["sha"])
        sid = (captured.get("payload") or {}).get("session_id")
        check("C4 有 source_line 时写入 session_id", sid == f"l1:{loc['ref']}#{loc['sha']}", str(sid))

        captured.clear()
        R.write_to_semantic_index("无来源的内容", ["t"])
        check("C5 无 source_line 时不写 session_id", "session_id" not in (captured.get("payload") or {}),
              str((captured.get("payload") or {}).keys()))

        # C6
        buf = tmp / "data" / "memory_buffer.jsonl"
        R.fallback_to_buffer("降级内容", ["t"], source_line=loc["ref"], sha=loc["sha"])
        entry = json.loads(buf.read_text().splitlines()[-1])
        check("C6 buffer 保留 provenance",
              entry.get("source_line") == loc["ref"] and entry.get("sha") == loc["sha"],
              json.dumps(entry, ensure_ascii=False)[:120])

        # C7
        body = f"l1:{loc['ref']}#{loc['sha']}"[3:]
        ref, _, sha = body.partition("#")
        check("C7 session_id 可逆", ref == loc["ref"] and sha == loc["sha"], f"{ref} / {sha}")

        # C8 账本：必须走 remember() 端到端，因为账本只在 remember() 里写
        led = tmp / "data" / "write_ledger.jsonl"
        before = led.read_text().count("\n") if led.exists() else 0
        R.remember("契约测试：账本条目 X2", ["测试"])
        rows = led.read_text().strip().splitlines() if led.exists() else []
        after = len(rows)
        last = json.loads(rows[-1]) if rows else {}
        check("C8 remember() 落账本且带 ref/sha",
              after > before and last.get("ref") and last.get("sha") and last.get("l2_status"),
              json.dumps(last, ensure_ascii=False)[:130])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"契约测试失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("契约测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())