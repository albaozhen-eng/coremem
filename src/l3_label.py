#!/usr/bin/env python3
"""G5（以及整组闸门）的校准工具：把"闸门判得对不对"变成可量的数。

背景：复核（2026-10-26）指出测试只能证明"闸门实现符合我写的规则"，
证明不了"规则本身对不对"。要打破这个循环，必须拿**独立标注**去量闸门。

流程：
  1) --export   从近 N 天语料抽 M 个簇，跑 LLM 归纳 + 过闸，产出：
                  - labels-TEMPLATE.md   人读的标注表（含证据原文与闸门判定）
                  - labels-TEMPLATE.json 机读骨架（含 gate_ok / gate_reason）
  2) 人工把每簇标成 pattern / dup / state（三选一），存成 labels.json：
                  {"<cluster_id>": "pattern", ...}
  3) --score    计算闸门精确率/召回率/F1

判定口径：
  pattern = 确实是稳定规律，应当收
  dup     = 同一件事的重复，应当拒
  state   = 具体状态/一次性决定，应当拒
  → 闸门"收"算正例；标签 pattern 是真正例。

用法：
  python3 l3_label.py --export --limit 30
  python3 l3_label.py --score --labels labels.json --sheet labels-TEMPLATE.json
退出码：--score 在精确率 < 0.9 时返回 1（提示闸门需要调）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import importlib.util

_spec = importlib.util.spec_from_file_location("l3", HERE / "l3_schema_induct.py")
L3 = importlib.util.module_from_spec(_spec)
sys.modules["l3"] = L3
_spec.loader.exec_module(L3)

OUT_DIR = Path(os.environ.get("L3_LABEL_DIR", Path.home() / ".openclaw/workspace-main/memory/golden"))


def export(limit: int, days: int, min_cluster: int, threshold: float = 0.80) -> int:
    facts = L3.load_facts(days)
    if not facts:
        print("没有语料", file=sys.stderr)
        return 2
    groups = L3.cluster(facts, min_cluster, thresh=threshold)
    by_ref = {f.ref: f for f in facts}
    print(f"  近 {days} 天 {len(facts)} 条事实 / {len(groups)} 簇（阈值 {threshold}），取前 {limit} 簇")

    rows = []
    for idx, g in enumerate(groups[:limit], 1):
        ans = L3.ask_llm(g)
        ev = []
        for r in (ans.get("evidence") or []):
            r = str(r).strip()
            ev.append(f"{r}#{by_ref[r].sha}" if r in by_ref else r)
        ok, why = L3.gate(ans.get("kind"), ans.get("rule", ""), ans.get("subject", ""), ev, by_ref)
        rows.append({
            "cluster_id": f"C{idx:02d}",
            "kind": ans.get("kind"),
            "rule": ans.get("rule", ""),
            "subject": ans.get("subject", ""),
            "evidence": ev,
            "evidence_text": [by_ref[r.partition('#')[0]].text[:160]
                              for r in ev if r.partition('#')[0] in by_ref],
            "gate_ok": ok,
            "gate_reason": why,
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sheet = OUT_DIR / (os.environ.get("L3_SHEET_NAME") or "labels-TEMPLATE.json")
    sheet.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    md = ["# 闸门校准标注表", "",
          "每簇标一个：`pattern`（确实是稳定规律）／`dup`（同一件事重复）／`state`（具体状态）。",
          "不要看「闸门判定」那一列下结论 —— 那一列是待验证的对象。", ""]
    for r in rows:
        md.append(f"## {r['cluster_id']}  ·  label: ______")
        md.append(f"- 候选规则：{r['rule'] or '（未归纳出）'}")
        md.append(f"- 主体：{r['subject'] or '—'}｜LLM 判 kind：{r['kind']}")
        md.append(f"- 闸门判定：{'收' if r['gate_ok'] else '拒'} {r['gate_reason'] or ''}")
        for t in r["evidence_text"]:
            md.append(f"  - {t}")
        md.append("")
    (OUT_DIR / (os.environ.get("L3_SHEET_NAME","labels-TEMPLATE").replace(".json","") + ".md")).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"  已写 {sheet}")
    print(f"  已写 {OUT_DIR/'labels-TEMPLATE.md'}")
    return 0


def score(labels_path: str, sheet_path: str) -> int:
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    rows = json.loads(Path(sheet_path).read_text(encoding="utf-8"))
    tp = fp = tn = fn = 0
    disputes = []
    for r in rows:
        cid = r["cluster_id"]
        if cid not in labels:
            continue
        truth = labels[cid]
        want = (truth == "pattern")          # 应当收
        got = bool(r["gate_ok"])             # 闸门实际收了
        if got and want:
            tp += 1
        elif got and not want:
            fp += 1
            disputes.append((cid, "误收", truth, r.get("rule", "")[:40]))
        elif not got and want:
            fn += 1
            disputes.append((cid, "漏收", truth, r.get("rule", "")[:40]))
        else:
            tn += 1
    if tp + fp == 0:
        print("  没有收到任何规则，无法计算精确率（闸门过严或语料过少）")
        return 1
    prec = tp / (tp + fp)
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    print(f"  标注 {tp+fp+tn+fn} 簇：pattern {tp+fn} / 非pattern {fp+tn}")
    print(f"  闸门 收 {tp+fp} 条 → 精确率 {prec*100:.1f}%")
    print(f"  闸门 漏收 {fn} 条（应当收但被拒）→ 召回 {rec*100:.1f}%  F1 {f1*100:.1f}%")
    for d in disputes[:8]:
        print(f"    ! {d[0]} {d[1]}（标注={d[2]}）{d[3]}")
    return 0 if prec >= 0.9 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-cluster", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=0.80)
    ap.add_argument("--labels", default="")
    ap.add_argument("--sheet", default="")
    a = ap.parse_args()
    if a.export:
        return export(a.limit, a.days, a.min_cluster, a.threshold)
    if a.score:
        return score(a.labels, a.sheet or str(OUT_DIR / "labels-TEMPLATE.json"))
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
