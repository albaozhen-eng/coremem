#!/usr/bin/env python3
"""P3：L2（semantic 层）已知项测试 —— 补上现有基准的覆盖缺口。

问题：黄金集与 KIS 的锚点都在 L1，实测 semantic 层**从来不是唯一证据来源（0/33）**，
所以两者都测不到 L2。剔除全部合成节点后召回 0 变化，正是这个盲区的表现。

做法：直接从 semantic 节点出题 —— 取节点的 IDF 高词当查询，看能否把**该节点本身**找回来。
分两类分别统计：derived（逐字来自 L1）与 synthesized（无事实源）。
这样能回答「L2 里的合成层到底可不可用」，而不是只回答「它有没有害」。
"""
from __future__ import annotations
import argparse, json, math, re, sys, time, urllib.request, urllib.parse
from collections import Counter
from datetime import datetime
from pathlib import Path

for _p in (Path(__file__).resolve().parent,
           Path.home() / ".openclaw" / "workspace" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from plugmem_config import plugmem_cfg
import mo_common, l1_prov_index as li

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{4,}")
CJK = re.compile(r"[\u4e00-\u9fff]{3,}")


def tokens(text):
    out = []
    for w in LATIN.findall(text):
        out.append(w)
    for run in CJK.findall(text):
        out.extend([run] if len(run) <= 6 else [run[i:i+5] for i in range(0, len(run)-4)])
    seen, uniq = set(), []
    for w in out:
        lw = w.lower()
        if lw in seen or w.isdigit():
            continue
        seen.add(lw); uniq.append(w)
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="每类抽多少条")
    ap.add_argument("--out", default="/tmp/p3/l2-kis.json")
    args = ap.parse_args()
    cfg = plugmem_cfg(); G = cfg["graph_id"]
    H = {"X-API-Key": cfg["api_key"]} if cfg["api_key"] else {}
    def get(p):
        with urllib.request.urlopen(urllib.request.Request(cfg["base_url"]+p, headers=H), timeout=60) as r:
            return json.loads(r.read())

    idx = li.build()
    total = get(f"/api/v1/graphs/{G}/nodes?node_type=semantic&limit=1")["count"]
    pools = {"derived": [], "synthesized": []}
    df = Counter()
    scanned = 0
    for off in range(0, total, 1500):
        d = get(f"/api/v1/graphs/{G}/nodes?node_type=semantic&limit=500&offset={off}")
        for n in d.get("nodes", []):
            t = n.get("semantic_memory") or ""
            if not n.get("is_active") or len(t) < 24:
                continue
            scanned += 1
            cls = li.classify(t, idx)
            if cls in pools:
                pools[cls].append({"id": n["semantic_id"], "text": t, "cls": cls})
            for w in set(x.lower() for x in tokens(t)):
                df[w] += 1
        if all(len(v) >= args.n for v in pools.values()):
            break
    print(f"扫描 {scanned} 个活跃节点；derived 池 {len(pools['derived'])} · synthesized 池 {len(pools['synthesized'])}")

    results = []
    for cls, pool in pools.items():
        picked = pool[:args.n]
        found = 0
        for item in picked:
            toks = [t for t in tokens(item["text"]) if 1 <= df.get(t.lower(), 0) <= 20]
            toks.sort(key=lambda w: len(w))
            q = " ".join(toks[:3])
            if not q:
                continue
            # 2026-11-11 审计：/search 是「整串子串匹配」（needle in text），
            # 带空格的多词查询永远 0 命中（本机首跑 derived 0% 的根因）。
            # 改为逐词查询、任一词找回自身即命中，与 memory_recall 的单 token 口径一致。
            hit = False
            # 2026-11-11：两种查询口径**都试，任一命中即算**。
            # 起因：上游 b5546e5 为修其本机「整串永远 0 命中」改成只逐词查，
            # 但在本机实测反而从 92% 掉到 13%（同机同数据同 200 题）——
            # 单个常用词会匹配到大量节点，目标被挤出 limit=20；整串更specific。
            # 两种口径各有失效场景，取并集才是稳妥判据，不偏袒任一台机器。
            try:
                _queries = [q] + list(toks[:3])
                for _qq in _queries:
                    if not _qq:
                        continue
                    d = get(f"/api/v1/graphs/{G}/search?q={urllib.parse.quote(_qq)}&limit=20")
                    if any(n.get("semantic_id") == item["id"] for n in d.get("nodes", [])):
                        hit = True
                        break
            except Exception:
                hit = False
            found += hit
            results.append({"cls": cls, "id": item["id"], "query": q, "hit": hit,
                            "text": item["text"][:60]})
        print(f"  {cls:12} 已知项召回 {found}/{len(picked)} = {100*found/max(1,len(picked)):.1f}%")

    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "graph": G, "n_per_class": args.n,
           "summary": {cls: {"n": sum(1 for r in results if r["cls"] == cls),
                             "hit": sum(1 for r in results if r["cls"] == cls and r["hit"]),
                             "rate": round(100*sum(1 for r in results if r["cls"] == cls and r["hit"])
                                           / max(1, sum(1 for r in results if r["cls"] == cls)), 1)}
                       for cls in ("derived", "synthesized")},
           "results": results,
           "note": "查得到 = 该节点对自身关键词可检索；查不到 = 它在向量/关键词索引里基本不可达"}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("  报告:", args.out)


if __name__ == "__main__":
    main()
