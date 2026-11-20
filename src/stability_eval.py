#!/usr/bin/env python3
"""P0-M6b：检索稳定性评测（run-to-run flip rate）。

系统代码不变，连跑 N 次同一黄金集，看有多少题的结果在多次之间不一致。
不一致说明结果里混进了随机性（并发、超时、外部服务抖动），
这种情况下「单次门禁」的结论本身就是不可靠的。

用法：
  python3 stability_eval.py            # 默认 5 次
  python3 stability_eval.py --runs 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN_EVAL = HERE / "golden_eval.py"
DEFAULT_GOLDEN = Path(os.environ.get(
    "MEMCHECK_GOLDEN", Path.home() / ".coremem/memory/golden/golden-v0.json"))


def one_run(workdir: Path, golden: Path, variant: str, tag: str) -> dict:
    """每次运行用独立目录：golden_eval 的报告名精确到分钟，同分钟会互相覆盖。"""
    rd = workdir / tag
    rd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ,
               MEMCHECK_GOLDEN=str(golden),
               MEMCHECK_REPORTS=str(rd),
               MEMCHECK_HEALTH=str(rd / "_health"))
    p = subprocess.run([sys.executable, str(GOLDEN_EVAL), "--variant", variant],
                       capture_output=True, text=True, env=env, timeout=900)
    if p.returncode not in (0, 2):
        return {"error": (p.stderr or "")[-300:], "hits": {}}
    files = list(rd.glob("baseline-*.json"))
    if not files:
        return {"error": "no report produced", "hits": {}}
    latest = max(files, key=lambda q: q.stat().st_mtime)
    rep = json.loads(latest.read_text())
    hits = {r["id"]: bool(r["hit_top3"]) for r in rep["results"]
            if r["variant"] == variant and r["type"] != "R"}
    return {"error": None, "hits": hits, "report": f"{tag}/{latest.name}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--variant", default="question")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--out", default="/tmp/p0-reports/stability.json")
    args = ap.parse_args()

    golden = Path(args.golden)
    runs = []
    with tempfile.TemporaryDirectory(prefix="stability-") as td:
        rd = Path(td)
        for i in range(args.runs):
            r = one_run(rd, golden, args.variant, f"run{i + 1}")
            runs.append(r)
            print(f"  run {i + 1}/{args.runs}: "
                  f"{'ERROR: ' + r['error'] if r['error'] else str(sum(r['hits'].values())) + '/' + str(len(r['hits']))}",
                  flush=True)

    ok_runs = [r for r in runs if not r["error"]]
    ids = sorted(set().union(*[set(r["hits"]) for r in ok_runs])) if ok_runs else []
    unstable = [i for i in ids if len({r["hits"].get(i) for r in ok_runs}) > 1]
    per_run = [{"run": k + 1, "hits": sum(r["hits"].values()), "n": len(r["hits"]),
                "report": r.get("report"), "error": r["error"]} for k, r in enumerate(runs)]

    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "variant": args.variant, "runs": args.runs, "ok_runs": len(ok_runs),
        "golden_sha256": mo_common.line_sha(golden.read_text()) if golden.exists() else None,
        "flip_count": len(unstable), "flipped_ids": unstable,
        "per_run": per_run,
        "verdict": ("red" if len(unstable) >= 2 else ("yellow" if len(unstable) == 1 else "green")),
        "note": "阈值按 P0 方案：0 不平滑→绿 / 1→黄 / ≥2→红",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n=== 稳定性 ===")
    print(f"  连跑 {args.runs} 次，成功 {len(ok_runs)} 次")
    print(f"  结果不一致的题：{len(unstable)} 道 {unstable}")
    print(f"  判定：{out['verdict'].upper()}")
    print("  报告:", args.out)
    return 2 if out["verdict"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())