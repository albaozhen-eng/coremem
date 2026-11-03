#!/usr/bin/env python3
"""P2：KIS 自检评测 + 成对显著性检验。

对 KIS 里每个已知项跑一次真实检索，看那个已知位置能否被找回。

命中判定（两条任一成立）：
  * 返回条目的 (文件, 行号) 落在 target ±window 内
  * 返回内容的 sha == target_sha（行号漂移时仍算命中）

两个口径都报：
  layer3  = 各层各取前 3 再拼接（生产口径，最多 15 槽位）
  l1top3 = 只靠事实源（l1）自身前 3 条（严格）

drift：target 行已被人工编辑、sha 对不上 → 单独计数，并另算「去 drift」的口径。

成对比较用 McNemar 精确检验 —— KIS 是**自指集合**，绝对水位不可跨系统比较，
只能用同一批题做前后对比。

用法：
  python3 kis_eval.py --kis kis-v1.json --out run.json [--limit 20]
  python3 kis_eval.py --compare baseline.json current.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import subprocess
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402

HERE = Path(__file__).resolve().parent


def load_ge():
    spec = importlib.util.spec_from_file_location("ge_for_kis", HERE / "golden_eval.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def layer_pool(data, order, per=3):
    """生产口径：各层各取前 per 条再拼接，返回 [(ref, content)]。"""
    out = []
    layers = data.get("layers") or {}
    for L in order:
        for i, r in enumerate(layers.get(L) or []):
            if i >= per:
                break
            out.append((r.get("file") or "", r.get("content") or ""))
    return out


def l1_top_pool_pairs(data, top=3):
    """事实源（l1）自身的前 top 条，**带 ref**。

    复核指出：golden_eval.l1_top_pool 返回的是字符串列表，而本文件的 found() 需要
    (ref, content) 才能判「目标行是否在前 3 里」—— 直接替换会解包失败。
    所以这里做一个 KIS 侧的正确适配，语义仍是「只靠事实源自身前 3 条」。
    """
    out = []
    for r in ((data.get("layers") or {}).get("l1") or [])[:top]:
        out.append((r.get("file") or "", r.get("content") or ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kis", default=str(mo_common.GOLDEN_DIR / "kis-v1.json"))
    ap.add_argument("--out", default="/tmp/p2/kis-run.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "CURRENT"))
    args = ap.parse_args()

    if args.compare:
        return do_compare(args.compare[0], args.compare[1])

    ge = load_ge()
    kis = json.loads(Path(args.kis).read_text())
    items = kis["items"][: args.limit] if args.limit else kis["items"]

    rows = []
    for n, it in enumerate(items, 1):
        p = subprocess.run([sys.executable, str(HERE / "memory_recall.py"), "all", it["query"], "--json"],
                           capture_output=True, text=True, timeout=180, cwd=str(HERE))
        if p.returncode != 0 or not p.stdout.strip():
            rows.append({"id": it["id"], "error": "recall failed"})
            continue
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            rows.append({"id": it["id"], "error": "bad json"})
            continue

        tgt_file, tgt_line = mo_common.parse_ref(it["target_ref"])
        w = it.get("window", 2)
        now_line = mo_common.resolve(it["target_ref"])
        # 必须剥前缀再比 sha：build 时的 target_sha 是正文的 sha
        drift = bool(now_line is not None
                     and mo_common.line_sha(mo_common.content_of(now_line)) != it["target_sha"])

        def found(pool):
            for ref, content in pool:
                pr = mo_common.parse_ref(ref)
                if pr and pr[0] == tgt_file and abs(pr[1] - tgt_line) <= w:
                    return True
                if content and mo_common.line_sha(mo_common.content_of(content)) == it["target_sha"]:
                    return True
            return False

        l3 = layer_pool(data, ge.LAYER_ORDER)
        # 复核指出：这里原先调用本地 rrf_pool（无权重 RRF）—— 正是被判为
        # 「各层第 1 名同分、按层序断平、事实源被系统性排除」的那个废弃口径。
        # 原先只把标签改成 l1top3，计算没变，等于对外谎报口径。
        g3 = l1_top_pool_pairs(data, top=3)
        rows.append({
            "id": it["id"], "query": it["query"], "target_ref": it["target_ref"],
            "drift": drift, "hit_layer3": found(l3), "hit_l1top3": found(g3),
            "n_returned": sum(len(v or []) for v in (data.get("layers") or {}).values()),
        })
        if n % 50 == 0 or n == len(items):
            print(f"  [{n}/{len(items)}] 已完成", flush=True)

    ok = [r for r in rows if "error" not in r]
    drift = sum(1 for r in ok if r["drift"])
    clean = [r for r in ok if not r["drift"]]

    def pct(rs, key):
        return round(100 * sum(1 for r in rs if r[key]) / len(rs), 1) if rs else None

    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kis": str(args.kis), "kis_seed": kis.get("seed"),
        "kis_sha256": mo_common.line_sha(Path(args.kis).read_text()[:200000]),
        "n_total": len(rows), "n_ok": len(ok),
        "errors": [r["id"] for r in rows if "error" in r],
        "drift_items": drift,
        "kis_recall_layer3": pct(ok, "hit_layer3"),
        "kis_recall_l1top3": pct(ok, "hit_l1top3"),
        "kis_recall_layer3_no_drift": pct(clean, "hit_layer3"),
        "kis_recall_l1top3_no_drift": pct(clean, "hit_l1top3"),
        "rows": rows,
        "note": "自指集合，只用于同一批题的前后成对比较；绝对水位不可与黄金集或别家系统比较",
    }
    outp = Path(args.out)
    if outp.is_dir() or str(args.out).endswith("/"):
        outp.mkdir(parents=True, exist_ok=True)
        outp = outp / f"kis-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(f"\nKIS 评测：n={len(ok)}（错误 {len(out['errors'])}，drift {drift}）")
    print(f"  kis_recall@layer3  = {out['kis_recall_layer3']}%   （生产口径）")
    print(f"  kis_recall@l1top3 = {out['kis_recall_l1top3']}%   （只靠事实源自身前3条）")
    print(f"  去 drift：layer3 {out['kis_recall_layer3_no_drift']}% / l1top3 {out['kis_recall_l1top3_no_drift']}%")
    print("  报告:", outp)
    return 0


def load_latest_runs(report_dir, n=2):
    """取最近 n 份 kis-*.json（按文件名时间戳排序）。"""
    d = Path(report_dir)
    if not d.exists():
        return []
    files = sorted(d.glob("kis-*.json"))
    return [json.loads(f.read_text()) for f in files[-n:]]


def same_kis(A, B):
    """两次运行是否基于同一版 KIS。

    id 是位置编号（K0001…），重建后同一个 id 可能指向完全不同的题 ——
    若不做守卫，跨版本做 McNemar 会静默给出无效结论。
    """
    sa, sb = A.get("kis_sha256"), B.get("kis_sha256")
    if sa and sb and sa != sb:
        return False, "KIS 版本不同（sha256 不一致）"
    # 再核对同一 id 是否指向同一 target_ref
    ma = {r["id"]: r.get("target_ref") for r in A["rows"]}
    mb = {r["id"]: r.get("target_ref") for r in B["rows"]}
    for k in set(ma) & set(mb):
        if ma[k] != mb[k]:
            return False, f"同一 id 指向不同题（{k}: {ma[k]} vs {mb[k]}）"
    return True, ""


def compare_maps(A, B, metric="hit_layer3"):
    """给探针用：返回 {regressed, improved, p, ...}；版本不一致返回 {"error": ...}。"""
    ok, why = same_kis(A, B)
    if not ok:
        return {"error": why}
    ra = {r["id"]: r for r in A["rows"] if "error" not in r}
    rb = {r["id"]: r for r in B["rows"] if "error" not in r}
    ids = sorted(set(ra) & set(rb))
    if not ids:
        return None
    b = sum(1 for i in ids if ra[i][metric] and not rb[i][metric])
    c = sum(1 for i in ids if not ra[i][metric] and rb[i][metric])
    da = round(100 * sum(ra[i][metric] for i in ids) / len(ids), 1)
    db = round(100 * sum(rb[i][metric] for i in ids) / len(ids), 1)
    return {"regressed": b, "improved": c, "p": mcnemar_exact(b, c),
            "baseline_pct": da, "current_pct": db, "n": len(ids),
            "delta": round(db - da, 1)}


def mcnemar_exact(b: int, c: int) -> float:
    """McNemar 精确检验（双侧）。b/c 为两次运行结论相反的两种方向计数。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def do_compare(pa: str, pb: str) -> int:
    A = json.loads(Path(pa).read_text())
    B = json.loads(Path(pb).read_text())
    ra = {r["id"]: r for r in A["rows"] if "error" not in r}
    rb = {r["id"]: r for r in B["rows"] if "error" not in r}
    ids = sorted(set(ra) & set(rb))
    ok, why = same_kis(A, B)
    if not ok:
        print(f"❌ 拒绝比较：{why}")
        print("   （重建 KIS 后必须先重新建立基线，否则 id 对不上，结论无效）")
        return 1
    print(f"=== McNemar 精确检验（共同题 {len(ids)}）===")
    print(f"  基线 {pa}\n  当前 {pb}\n")
    for metric in ("hit_layer3", "hit_l1top3"):
        b = sum(1 for i in ids if ra[i][metric] and not rb[i][metric])
        c = sum(1 for i in ids if not ra[i][metric] and rb[i][metric])
        p = mcnemar_exact(b, c)
        da = round(100 * sum(ra[i][metric] for i in ids) / len(ids), 1)
        db = round(100 * sum(rb[i][metric] for i in ids) / len(ids), 1)
        delta = round(db - da, 1)
        if abs(delta) >= 3 and p < 0.05:
            verdict = "显著变化"
        elif abs(delta) >= 3:
            verdict = "仅趋势（不显著）"
        else:
            verdict = "无变化"
        print(f"  {metric:12} 基线 {da}% → 当前 {db}%  (Δ{delta:+})  "
              f"退步 {b} / 进步 {c}  p={p:.4g}  → {verdict}")
        if verdict == "显著变化":
            print(f"       ⚠️ {verdict}：{'退化' if delta < 0 else '提升'}（p<0.05）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())