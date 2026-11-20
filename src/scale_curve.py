#!/usr/bin/env python3
"""P1-M4：规模曲线（只读）。

问题：语料变大时，检索质量会不会退化？Jev-Mem 在 750 行→4,801 条时从 50.0% 掉到
      27.3%，但那是别人的系统。自家链路至今只有一个弱证据（15 文件与全量都是 93.9%）。

做法：构造 5 个规模档位，每档跑同一套黄金集，记录 recall@layer3 / recall@l1top3 / 延迟 / tokens，
      再算 scale_slope = 语料每翻一倍掉几个百分点。

档位：
  S0  锚点文件（覆盖全部可答题锚点，约 15 个文件）   —— 真实
  S1  S0 + 其余每日笔记的一半                        —— 真实
  S2  全部每日笔记                                   —— 真实
  S3  S2 + 1 倍扰动干扰文件                          —— 合成（报告中必须标注）
  S4  S2 + 3 倍扰动干扰文件                          —— 合成（报告中必须标注）

扰动干扰文件的生成：取真实旧笔记，日期整体前移，数字/版本号按固定种子替换成同格式值，
      于是制造出词法上高度相似、但内容错误的竞争者。

局限（必须一起报告）：
  * 只缩放了 L1 通道。L2 语义层与 facts 层在所有档位都看到全量数据（无法低成本重建），
    因此本曲线测的是「L1 行级检索随规模退化」，不是整链退化。
  * S3/S4 是合成数据，可能造出比真实语料更强的词法对手，斜率会被高估。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

HOME = Path.home()
SRC_NOTES = Path(os.environ.get("MEM_SRC_NOTES", HOME / ".coremem/memory"))
GOLDEN_EVAL = Path(__file__).resolve().parent / "golden_eval.py"
DEFAULT_GOLDEN = Path(os.environ.get("MEMCHECK_GOLDEN",
                                     HOME / ".coremem/memory/golden/golden-v0.json"))
WORK = Path(os.environ.get("SCALE_WORK", "/tmp/p1-scale"))

DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")
NUM_RE = re.compile(r"\b\d{2,}\b")


def anchor_files():
    """从黄金集锚点推出需要哪些文件。"""
    items = json.loads(DEFAULT_GOLDEN.read_text())
    files = set()
    for it in items:
        for a in it[3] or []:
            f = a.rsplit(":", 1)[0] if ":" in a else ""
            if f.endswith(".md"):
                files.add(f)
    return files


def all_notes():
    return sorted([p for p in SRC_NOTES.glob("*.md") if mo_common.DAILY_RE.match(p.name)])


def make_distractors(notes, factor, out_dir, seed=1234):
    """生成 factor 倍量的合成干扰文件（真实旧笔记改日期 + 换数字）。"""
    rng = random.Random(seed)
    written = 0
    base = sorted(notes)[: max(4, len(notes) // 3)]
    for k in range(factor):
        for src in base:
            lines = mo_common.read_lines(src)
            out = []
            for line in lines:
                line = DATE_RE.sub(lambda m: _shift_date(m.group(0), 400 + 90 * k), line)
                line = NUM_RE.sub(lambda m: str(rng.randint(100, 9999)) if len(m.group(0)) < 5 else m.group(0), line)
                out.append(line)
            name = f"2025-{1 + (k % 12):02d}-{1 + (written % 27):02d}-distractor-{k}.md"
            (out_dir / name).write_text("\n".join(out) + "\n", encoding="utf-8")
            written += 1
            if written >= factor * len(base):
                break
    return written


def _shift_date(d, days):
    try:
        return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return d


def build_corpora(tags, force=False):
    WORK.mkdir(parents=True, exist_ok=True)
    notes = all_notes()
    anchors = anchor_files()
    a_notes = [p for p in notes if p.name in anchors]
    rest = [p for p in notes if p.name not in anchors]
    plan = {
        "S0": a_notes,
        "S1": a_notes + rest[: len(rest) // 2],
        "S2": notes,
        "S3": notes,
        "S4": notes,
    }
    distractor_counts = {}
    for tag in tags:
        d = WORK / f"corpus-{tag}"
        if force and d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        for p in plan[tag]:
            shutil.copy(p, d / p.name)
        if tag in ("S3", "S4"):
            distractor_counts[tag] = make_distractors(notes, 1 if tag == "S3" else 3, d)
    return plan, distractor_counts


def run_one(tag, golden, variant):
    corpus = WORK / f"corpus-{tag}"
    rep = WORK / f"reports-{tag}"
    rep.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, MEM_DAILY_NOTES_DIR=str(corpus),
               MEMCHECK_GOLDEN=str(golden), MEMCHECK_REPORTS=str(rep),
               MEMCHECK_HEALTH=str(rep / "_health"))
    p = subprocess.run([sys.executable, str(GOLDEN_EVAL), "--variant", variant],
                       capture_output=True, text=True, env=env, timeout=3600)
    files = list(rep.glob("baseline-*.json"))
    if not files:
        return {"tag": tag, "error": (p.stderr or "")[-300:]}
    r = json.loads(max(files, key=lambda q: q.stat().st_mtime).read_text())
    s = r["summary"][variant]
    lines = sum(len(mo_common.read_lines(f)) for f in corpus.glob("*.md"))
    return {"tag": tag, "n_files": len(list(corpus.glob("*.md"))), "n_lines": lines,
            # 两个口径并列且都不含策略权重：
            #   layer3 = 生产口径（各层前3拼接）；l1top3 = 只靠事实源自身前3条
            "recall_layer3": s.get("recall_layer3", s.get("recall_top3")),
            "recall_l1top3": s.get("recall_l1top3", s.get("recall_l1top3")),
            "other_layers_gain": s.get("other_layers_gain"),
            "recall_top3": s["recall_top3"],
            "p50_ms": s["p50_ms"], "p95_ms": s["p95_ms"], "tokens": s.get("tokens_total"),
            "miss_taxonomy": s.get("miss_taxonomy"), "n": s["n"], "error": p.stderr[-200:] if p.returncode == 2 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", default="S0,S1,S2,S3,S4")
    ap.add_argument("--variant", default="question")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--out", default="/tmp/p0-reports/scale-curve.json")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    plan, distract = build_corpora(tags, force=args.force)
    print("档位语料：")
    for t in tags:
        d = WORK / f"corpus-{t}"
        n = len(list(d.glob("*.md")))
        lines = sum(len(mo_common.read_lines(f)) for f in d.glob("*.md"))
        mark = "（合成干扰 %d 个）" % distract[t] if t in distract else ""
        print(f"  {t}: {n:3} 文件 / {lines:6} 行 {mark}")

    rows = []
    for t in tags:
        r = run_one(t, Path(args.golden), args.variant)
        rows.append(r)
        if r.get("error") and "recall_top3" not in r:
            print(f"  {t}: 失败 {r['error'][:120]}")
        else:
            print(f"  {t}: layer3={r['recall_layer3']}% l1top3={r.get('recall_l1top3')}% "
                  f"红利={r.get('other_layers_gain')} "
                  f"p50={r['p50_ms']}ms p95={r['p95_ms']}ms", flush=True)

    ok = [r for r in rows if "recall_top3" in r]
    slope = slope_g3 = None
    if len(ok) >= 2:
        import math
        n0, n1 = ok[0]["n_lines"], ok[-1]["n_lines"]
        if n1 > n0:
            slope = round((ok[-1]["recall_layer3"] - ok[0]["recall_layer3"]) / math.log2(n1 / n0), 2)
            if ok[0].get("recall_l1top3") is not None and ok[-1].get("recall_l1top3") is not None:
                slope_g3 = round((ok[-1]["recall_l1top3"] - ok[0]["recall_l1top3"]) / math.log2(n1 / n0), 2)

    verdict = "unknown"
    if slope is not None:
        verdict = "red" if slope < -3 else ("yellow" if slope < -1 else "green")

    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "variant": args.variant, "golden": str(args.golden),
        "rows": rows, "scale_slope_per_doubling": slope,
        "scale_slope_l1top3_per_doubling": slope_g3, "verdict": verdict,
        "thresholds": {"green(>=)": -1, "yellow": "-1 ~ -3", "red(<)": -3},
        "noise_floor_points": 3.0,
        "verdict_caveat": ("n=33，1 道题 = 3.0 点。|slope| < 3 点/倍属于单题摆动范围，"
                           "不足以判退化；本字段只是趋势提示，不是结论。"),
        "limitations": [
            "只缩放 L1 通道；L2 语义层与 facts 层在所有档位都看到全量数据",
            "S3/S4 为合成干扰，可能高估斜率",
            "单次快照，未做重复采样",
        ],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("\n=== 规模曲线 ===")
    for r in ok:
        print(f"  {r['tag']:3} {r['n_lines']:6} 行 → layer3 {r['recall_layer3']:5}%  "
              f"l1top3 {r.get('recall_l1top3')}%  红利 {r.get('other_layers_gain')}  p95 {r['p95_ms']}ms")
    print(f"  scale_slope(layer3) = {slope} 点/倍 · (l1top3) = {slope_g3}  → 判定 {verdict.upper()}")
    print("  报告:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())