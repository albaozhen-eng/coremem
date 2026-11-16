#!/usr/bin/env python3
"""HARD 难题集构造器：答案词不出现在查询里。

为什么需要（2026-11-15 审计 P2-A）：
  KIS 300 题已 99.7%（299/300）**饱和**，只剩 1 题可赢；golden n=33，1 题=3 点、CI±12。
  两套都只能防大退步，**测不出改进**。
  根因在 KIS 的出题方式：`query = " ".join(picked)`，查询词直接取自目标行——
  答案的词就在问题里，行级词面匹配即可命中，考的不是检索能力而是字符串重合。

本构造器的硬约束：**答案 token 必须不在 query 里**。
  题型 A（context_only）：从事实里挑一个可判定的答案 token（数字/IP/版本号/标识符），
                          query 只用该行的**其余**上下文词，不含答案。
  题型 B（synonym）    ：把上下文里的关键动词/名词换成同义词（确定性小词表），
                          query 用同义词，原文没有这些词。

每题记录：query / target_ref / target_sha / answer_tokens / kind。
判分沿用 KIS 口径（ref ±window 或 sha 命中），可直接用 kis_eval.py --kis 跑。

自检（构造时就断言，不合规的题直接丢弃）：
  1. 答案 token 确实在目标行里
  2. 答案 token **不在** query 里（这是本集存在的理由）
  3. query 至少 3 个上下文词（否则题目不可答）
  4. 同一 target 不重复出题

用法：
  python3 hard_build.py --n 180 --out ~/.coremem/memory/golden/hard-v1.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, Path.home() / ".coremem" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import mo_common

MEM = mo_common.MEMORY_DIR
PREFIX = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}T[^\s]*\s*(\[[^\]]*\])?\s*")

# 可判定的答案 token：数字/比例/版本/IP/带数字的标识符/模型名
ANSWER_PATS = [
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),            # IP
    re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b"),               # 版本号
    re.compile(r"\b\d+(?:\.\d+)?%"),                        # 百分比
    re.compile(r"\b[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*\b"),  # 含数字标识符
    re.compile(r"(?<![\d.])\d{2,}(?![\d.])"),              # 2 位以上纯数字
]
CJK = re.compile(r"[\u4e00-\u9fff]{2,}")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")

# 确定性同义词表（只换动词/名词，不改语义指向）
SYN = {
    "修复": "解决", "决定": "拍板", "部署": "上线", "迁移": "搬迁",
    "阈值": "门槛", "配置": "设置", "删除": "移除", "新增": "添加",
    "验证": "核实", "失败": "未成功", "启用": "打开", "停用": "关闭",
    "审计": "复核", "备份": "副本保存", "监控": "观测", "回退": "还原",
}
STOP = set("的 了 是 在 与 和 及 或 对 为 以 并 且 也 就 都 而 被 把 从 到 个 中 上 下 时 后 前".split())


def content_of(line: str) -> str:
    return PREFIX.sub("", line).strip()


def answer_tokens(text: str) -> list[str]:
    out = []
    for p in ANSWER_PATS:
        out.extend(m.group(0) for m in p.finditer(text))
    # 去重保序，过滤太短/太泛
    seen = set()
    return [t for t in out if len(t) >= 2 and not (t in seen or seen.add(t))]


def context_tokens(text: str, answers: list[str]) -> list[str]:
    """上下文词：剔除答案 token 后剩下的中文词/英文词。"""
    t = text
    for a in answers:
        t = t.replace(a, " ")
    toks = [w for w in CJK.findall(t) if w not in STOP]
    toks += [w for w in LATIN.findall(t) if w not in STOP and len(w) >= 3]
    seen = set()
    return [w for w in toks if not (w in seen or seen.add(w))]


def words(text: str) -> set:
    """统一分词单位：中文 bigram + 英文词。

    2026-11-16 修：原来 build_df 用 CJK.findall（贪婪匹配整段中文），
    把整句当一个"词"；而候选词是剔除答案后的**碎片**，两者对不上 →
    DF.get(w,0) 几乎全为 0，"按高频排序"实际没生效（是歧义度门槛在起作用）。
    统一成 bigram 后，频率排序才真正可用。
    """
    out = set()
    for run in CJK.findall(text):
        if len(run) == 2:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    out |= {w for w in LATIN.findall(text) if len(w) >= 3}
    return out


def build_df(lines):
    """每个词出现在多少行里（document frequency）。用于挑低区分度词。"""
    df = {}
    for _, c in lines:
        for w in words(c):
            df[w] = df.get(w, 0) + 1
    return df


def ambiguity(query_words, lines):
    """query 全部词 AND 命中的行数。=1 说明词面唯一锁定，题目不难。"""
    ws = [w for w in query_words if len(w) >= 2]
    if not ws:
        return 0
    return sum(1 for _, c in lines if all(w in c for w in ws))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=180)
    ap.add_argument("--seed", type=int, default=20261116)
    ap.add_argument("--window", type=int, default=2)
    ap.add_argument("--min-ambig", type=int, default=5,
                    help="query 词 AND 命中的最少行数；越大越难（=1 就是词面唯一锁定）")
    ap.add_argument("--out", default=str(MEM / "golden" / "hard-v1.json"))
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    files = sorted(MEM.glob("2026-*.md"))
    global ALL_LINES, DF
    pool = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            c = content_of(line)
            if len(c) < 24 or "[已撤销]" in c:
                continue
            ans = answer_tokens(c)
            if not ans:
                continue
            ctx = context_tokens(c, ans)
            if len(ctx) < 3:
                continue
            pool.append((f.name, i, c, ans, ctx))
    ALL_LINES = [(f"{fn}:{ln}", c) for fn, ln, c, _, _ in pool]
    DF = build_df(ALL_LINES)
    rnd.shuffle(pool)

    items, used = [], set()
    dropped = {"answer_in_query": 0, "too_few_ctx": 0, "dup_target": 0, "not_ambiguous": 0}
    for fname, ln, c, ans, ctx in pool:
        if len(items) >= a.n:
            break
        ref = f"{fname}:{ln}"
        if ref in used:
            dropped["dup_target"] += 1
            continue
        answer = ans[0]
        # 关键修正（第一版失败原因）：只剔除答案词不够——剩下的上下文词仍是
        # 原文逐字词，AND 匹配唯一锁定目标行（实测 54/60 题唯一命中），
        # 于是 99.4% 与 KIS 的 99.7% 无差。
        # 现在按**文档频率降序**挑词（低区分度），并要求 AND 命中 ≥MIN_AMBIG 行，
        # 逼系统做语义排序而不是字符串匹配。
        cand = sorted(ctx, key=lambda w: -DF.get(w, 0))
        picked = cand[:4]
        if len(picked) < 3:
            dropped["too_few_ctx"] += 1
            continue
        amb = ambiguity(picked, ALL_LINES)
        if amb < a.min_ambig:
            dropped["not_ambiguous"] += 1
            continue
        # 题型：一半用同义词改写（若可改）
        kind = "context_only"
        q_words = list(picked)
        syn_hits = [w for w in q_words if w in SYN]
        if syn_hits and len(items) % 2 == 1:
            q_words = [SYN.get(w, w) for w in q_words]
            kind = "synonym"
        query = " ".join(q_words)
        # 硬约束自检：答案词不得出现在 query 里
        if answer.lower() in query.lower():
            dropped["answer_in_query"] += 1
            continue
        if any(x.lower() in query.lower() for x in ans):
            dropped["answer_in_query"] += 1
            continue
        used.add(ref)
        items.append({
            "id": f"H{len(items)+1:04d}",
            "query": query,
            "target_ref": ref,
            "target_sha": mo_common.line_sha(c),
            "window": a.window,
            "date": fname[:10],
            "kind": kind,
            "answer_tokens": ans[:3],
            "n_ctx": len(picked),
            "ambiguity": amb,
        })

    out = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": a.seed, "n": len(items), "window": a.window,
        "corpus_files": len(files), "candidates": len(pool),
        "design": "答案 token 不在 query 里；query 用高频（低区分度）上下文词，AND 命中 ≥min_ambig 行",
        "min_ambig": a.min_ambig,
        "why": "KIS 的 query 取自目标行本身（词面重合），已饱和 99.7%，测不出改进",
        "dropped": dropped,
        "limitations": [
            "仍是单跳检索题，不含真正的多跳推理与跨日合并",
            "同义词表是确定性小词表，不是语义改写",
            "L1 是活文件，行号可能漂移；用 target_sha + ±window 容差",
        ],
        "items": items,
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  候选 {len(pool)} 条 → 出题 {len(items)} 道（目标 {a.n}）")
    print(f"  丢弃统计: {dropped}")
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print(f"  题型分布: {kinds}")
    print(f"  写入 {p}")
    for it in items[:3]:
        print(f"    {it['id']} q={it['query'][:44]!r} 答案={it['answer_tokens'][0]!r} → {it['target_ref']}")
    return 0 if len(items) >= min(a.n, 150) else 1


if __name__ == "__main__":
    raise SystemExit(main())
