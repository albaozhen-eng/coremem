#!/usr/bin/env python3
"""P0-M2f：存量 L2（语义索引）的 provenance 覆盖率审计（一次性 + 每月复跑）。

问题：现在 L2 里的节点，有多少能逐字回溯到 L1（事实源）？
      回溯不到的就是「孤儿节点」——它们没有事实源，无法核对真伪。

做法（只读）：
  1. 从最近的 L1 里确定性地抽 K 个主题词，走 memory_recall 取 semantic 层节点；
  2. 对每个去重后的节点，检查其内容是否能在 L1 中找到（归一化后前 N 字匹配）；
  3. 同时审计 facts 层（unified-facts:N）的可回溯性。

用法：python3 l2_prov_audit.py --queries 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

SCRIPTS_DIR = Path.home() / ".boss-agent/scripts"
PREFIX = 60


def probe_queries(k: int):
    """从最新 L1 确定性抽主题词做查询。"""
    qs = []
    for f in mo_common.daily_files()[:3]:
        for i, line in enumerate(mo_common.read_lines(f), 1):
            t = line.strip()
            if len(t) < 24 or t.startswith("#"):
                continue
            words = [w for w in re.findall(r"[A-Za-z0-9_.\-]{5,}|[\u4e00-\u9fff]{4,}", t)]
            if not words:
                continue
            seed = int(hashlib.sha256(f"{f.name}:{i}".encode()).hexdigest()[:8], 16)
            qs.append((seed, " ".join(sorted(words, key=len, reverse=True)[:2])))
        if len(qs) >= k * 3:
            break
    qs.sort()
    return [q for _, q in qs[:k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--out", default="/tmp/p0-reports/l2-prov-audit.json")
    args = ap.parse_args()

    queries = probe_queries(args.queries)
    nodes, facts = {}, {}
    errors = []
    for q in queries:
        try:
            p = subprocess.run(["python3", str(SCRIPTS_DIR / "memory_recall.py"), "all", q, "--json"],
                               capture_output=True, text=True, timeout=90, cwd=str(SCRIPTS_DIR))
            d = json.loads(p.stdout)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{q}: {e}")
            continue
        for r in (d.get("layers") or {}).get("semantic") or []:
            key = r.get("file") or ""
            if key:
                nodes[key] = (r.get("content") or "")
        for r in (d.get("layers") or {}).get("facts") or []:
            key = r.get("file") or ""
            if key:
                facts[key] = (r.get("content") or "")

    # 建立 L1 归一化全文（用于子串匹配）。
    # 关键：L2 存的是「去掉 '- <时间戳> [自动捕获]' 前缀后的正文」，
    # 所以必须做**子串**匹配，做前缀匹配会把全部节点误判为不可回溯。
    parts, l1_lines = [], 0
    for f in mo_common.daily_files():
        for line in mo_common.read_lines(f):
            n = mo_common.norm(line)
            if len(n) >= 12:
                parts.append(n)
                l1_lines += 1
    l1_blob = "\n".join(parts)

    def traceable(text):
        """返回 (是否可回溯, 匹配到的最长前缀长度)。"""
        n = mo_common.norm(text)
        if len(n) < 12:
            return None
        for cut in (60, 40, 24, 16):
            if len(n) >= cut and n[:cut] in l1_blob:
                return True
        return False

    def audit(items, label):
        tr = un = skip = 0
        untraceable = []
        for k, v in items.items():
            t = traceable(v)
            if t is True:
                tr += 1
            elif t is False:
                un += 1
                if len(untraceable) < 8:
                    untraceable.append({"ref": k, "content": v[:70]})
            else:
                skip += 1
        total = tr + un
        return {"label": label, "sampled": len(items), "traceable": tr, "untraceable": un,
                "skipped_short": skip,
                "coverage": round(100 * tr / total, 1) if total else None,
                "examples_untraceable": untraceable}

    l2 = audit(nodes, "semantic(L2)")

    # facts 层不能用检索窗口审计：memory_recall 返回的是 unified-facts.jsonl 的
    # JSON 片段窗口，不是干净正文。改为直接读该 jsonl 的 content 字段比对 L1。
    import random as _random
    uf = mo_common.MEMORY_DIR / "unified-facts.jsonl"
    if uf.exists():
        rows = [d for d in mo_common.read_jsonl(uf) if (d.get("content") or "").strip()]
        _random.seed(42)
        sample = _random.sample(rows, min(80, len(rows)))
        tr = un = 0
        ex = []
        for d in sample:
            c = mo_common.norm(d.get("content") or "")
            if len(c) < 12:
                continue
            if any(c[:cut] in l1_blob for cut in (60, 40, 24, 16) if len(c) >= cut):
                tr += 1
            else:
                un += 1
                if len(ex) < 8:
                    ex.append({"ref": d.get("timestamp", "?"), "content": c[:70]})
        fc = {"label": "facts(unified-facts)", "sampled": tr + un, "traceable": tr,
              "untraceable": un, "skipped_short": 0,
              "coverage": round(100 * tr / (tr + un), 1) if (tr + un) else None,
              "examples_untraceable": ex,
              "method": "直接读 unified-facts.jsonl 的 content 字段（非检索窗口）"}
    else:
        fc = {"label": "facts(unified-facts)", "sampled": 0, "traceable": 0, "untraceable": 0,
              "skipped_short": 0, "coverage": None, "examples_untraceable": [],
              "method": "文件不存在"}
    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "queries": len(queries), "errors": errors,
        "l1_indexed_lines": l1_lines,
        "match_method": "归一化后子串匹配（L2 存的是去前缀正文，故非前缀匹配）",
        "l2": l2, "facts": fc,
        "note": "一次性审计；L2 覆盖率低说明存量节点缺 provenance，需在 P1 用影子 graph 重建后切换",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"L1 已索引行 {l1_lines}；查询 {len(queries)} 个；错误 {len(errors)}")
    for r in (l2, fc):
        print(f"  {r['label']:14} 抽样 {r['sampled']:3} 个节点 → 可回溯 {r['traceable']:3} "
              f"({r['coverage']}%) · 不可回溯 {r['untraceable']} · 过短跳过 {r['skipped_short']}")
    if l2["examples_untraceable"]:
        print("  不可回溯样例：")
        for e in l2["examples_untraceable"][:4]:
            print(f"    {e['ref']} | {e['content']}")
    print("  报告:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())