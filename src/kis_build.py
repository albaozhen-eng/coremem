#!/usr/bin/env python3
"""P2：KIS（Known-Item Self-check）自检集构建。

问题：黄金集只有 33 道可答题，1 题 = 3.0 个百分点，门禁阈值再准也没有统计力。

做法：从 L1 事实源里**自动派生**已知项查询 —— 抽一行、用该行 IDF 最高的几个词当查询，
      看检索能否把这个已知位置找回来。这样能把样本量做到 300+，且完全可复现（定 seed）。

必须如实声明的三点局限：
  1. **自指**：查询词来自语料本身，天然偏向词法通道。所以 KIS 只用于**趋势与成对比较**，
     绝不能拿它的绝对值与黄金集或别家系统比较。
  2. **不是人工标注**：它测「能不能找回已知的一行」，不测「回答得对不对」。
  3. **会被记忆演化影响**：L1 是活文件，同一行可能被人工编辑 → 用 target_sha + ±window 容差，
     仍对不上时记为 drift，不计入通过。

用法：
  python3 kis_build.py --n 300 --seed 42 --out ~/.coremem/memory/golden/kis-v1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{3,}")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
PREFIX = re.compile(r"^-\s+\d{4}-\d\d-\d\dT[\d:+\-]+\s*(\[[^\]]*\])?\s*")
NUMERIC = re.compile(r"^\d+[.:%\-]*$")


def content_of(line: str) -> str:
    """去掉前缀，只留正文（统一走 mo_common，保证 build/eval 的 sha 一致）。"""
    return mo_common.content_of(line)


QUOTED = re.compile(r"[「『\u201c\"《]([^」』\u201d\"》]{2,12})[」』\u201d\"》]")


def candidate_tokens(text: str):
    """按「像不像一个真词」分级返回候选：(优先级, 词)。

    0 = 技术标识符 / 引号里的词（最像术语）
    1 = 完整 CJK 词块（2~4 字）
    2 = 长 CJK 句的 4 字滑窗（可能切在词中间，最后才用）
    """
    out = []
    for w in LATIN.findall(text):
        out.append((0, w))
    for w in QUOTED.findall(text):
        if 2 <= len(w) <= 12:
            out.append((0, w))
    for run in CJK_RUN.findall(text):
        if len(run) <= 4:
            out.append((1, run))
        elif len(run) <= 6:
            out.append((1, run))
            for i in range(0, len(run) - 3):
                out.append((2, run[i:i + 4]))
        else:
            for i in range(0, len(run) - 3):
                out.append((2, run[i:i + 4]))
    seen, uniq = set(), []
    for prio, w in out:
        lw = w.lower()
        if lw in seen or NUMERIC.match(w) or len(w) < 2:
            continue
        seen.add(lw)
        uniq.append((prio, w))
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--window", type=int, default=2, help="允许的行号漂移（±行）")
    ap.add_argument("--per-file-cap", type=int, default=40, help="单文件最多抽多少条（按日期分层）")
    ap.add_argument("--out", default=str(mo_common.GOLDEN_DIR / "kis-v1.json"))
    args = ap.parse_args()

    files = sorted(mo_common.daily_files())
    if not files:
        print("没有每日笔记，无法构建 KIS", file=sys.stderr)
        return 1

    # 一遍扫全量：建立行索引与 df
    lines = []          # (file_name, line_no, content)
    df = Counter()
    for f in files:
        for i, raw in enumerate(mo_common.read_lines(f), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            c = content_of(raw)
            if len(c) < 12:
                continue
            lines.append((f.name, i, c))
            for _, t in candidate_tokens(c):
                df[t.lower()] += 1
            for t in ():  # noqa
                pass

    n_docs = max(1, len(lines))
    rng = random.Random(args.seed)
    by_file = {}
    for rec in lines:
        by_file.setdefault(rec[0], []).append(rec)

    items = []
    for fname in sorted(by_file):                      # 按日期分层
        pool = by_file[fname]
        rng.shuffle(pool)
        taken = 0
        for fname2, line_no, content in pool:
            if taken >= args.per_file_cap:
                break
            toks = candidate_tokens(content)
            scored = []
            scored = []
            for prio, t in toks:
                d = df.get(t.lower(), 0)
                if d == 0 or d > 6:      # 稀有但非孤例
                    continue
                # 先比「像不像真词」（优先级小的更好），再比 IDF
                scored.append((-prio, math.log(n_docs / (1 + d)), t))
            if len(scored) < 2:
                continue
            scored.sort(reverse=True)
            picked, seen = [], set()
            for _, _, t in scored:
                lt = t.lower()
                if lt in seen:
                    continue
                seen.add(lt)
                picked.append(t)
                if len(picked) >= 4:
                    break
            if len(picked) < 2:
                continue
            items.append({
                "id": f"K{len(items) + 1:04d}",
                "query": " ".join(picked),
                "target_ref": f"{fname2}:{line_no}",
                "target_sha": mo_common.line_sha(content),
                "window": args.window,
                "date": fname2[:10],
                "n_tokens": len(picked),
            })
            taken += 1

    rng.shuffle(items)
    items = items[: args.n]
    items.sort(key=lambda x: x["id"])

    payload = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": args.seed, "n": len(items), "window": args.window,
        "n_corpus_lines": n_docs,
        "corpus_files": len(by_file),
        "limitations": [
            "自指：查询词来自语料本身，偏向词法通道；只用于趋势与成对比较，绝对水位不可与黄金集比较",
            "自动派生而非人工标注：测「能否找回已知行」，不测「回答对不对」",
            "L1 是活文件，行号可能漂移；用 target_sha + ±window 容差，对不上记 drift 不计通过",
        ],
        "items": items,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"KIS 已构建：{len(items)} 题（语料 {n_docs} 行 / {len(by_file)} 文件，seed={args.seed}）")
    print(f"  按日期分层：单文件上限 {args.per_file_cap} 条；覆盖 {len({i['date'] for i in items})} 个日期")
    print("  样例：")
    for it in items[:3]:
        print(f"    {it['id']} q={it['query'][:44]!r} → {it['target_ref']}")
    print("  报告:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())