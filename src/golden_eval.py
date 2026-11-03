#!/usr/bin/env python3
"""W2：golden set 基线跑分器
只读：调用 memory_recall.py 全层检索，判定「应命中证据」是否出现在结果里。
指标：hit@layer-top3 / hit@any / p95 延迟。
用法：
  python3 golden_eval.py            # 跑全量，打印汇总 + 落盘报告
  python3 golden_eval.py --limit 5  # 快速自检
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402  （P0 新增公共工具）

HOME = Path.home()
SCRIPTS = Path(os.environ.get("MEMCHECK_SCRIPTS", HOME / ".coremem/scripts"))
GOLDEN = Path(os.environ.get("MEMCHECK_GOLDEN", HOME / ".coremem/memory/golden/golden-v0.json"))
REPORT_DIR = Path(os.environ.get("MEMCHECK_REPORTS", HOME / ".coremem/memory/golden/reports"))
LAYER_ORDER = ["skills", "semantic", "l0", "l1", "facts"]
TOP_PER_LAYER = 3


def recall(query: str, mode: str) -> tuple[dict, int]:
    t0 = time.time()
    p = subprocess.run(
        ["python3", str(SCRIPTS / "memory_recall.py"), "all", query, "--json"],
        capture_output=True, text=True, timeout=180, cwd=str(SCRIPTS),
    )
    ms = int((time.time() - t0) * 1000)
    if p.returncode != 0 or not p.stdout.strip():
        return {"layers": {}, "error": (p.stderr or "")[-300:]}, ms
    try:
        return json.loads(p.stdout), ms
    except json.JSONDecodeError:
        return {"layers": {}, "error": "bad json"}, ms


def flatten(data: dict) -> tuple[list[str], list[str]]:
    """返回 (top 池, 全量池) 的文本列表；池 = 各层前 N 条拼接 + 全量。"""
    layers = data.get("layers") or {}
    top, allt = [], []
    for lname in LAYER_ORDER:
        for i, r in enumerate(layers.get(lname) or []):
            txt = r.get("content") or ""
            if lname in ("semantic", "facts"):
                txt += " " + str(r.get("file") or "")
            allt.append(txt)
            if i < TOP_PER_LAYER:
                top.append(txt)
    return top, allt


def hit(tokens: list[str], pool: list[str]) -> bool:
    if not tokens:
        return False
    blob = "\n".join(pool).lower()
    return all(t.lower() in blob for t in tokens)


# 说明（复核后的修正）：
#
# 本系统**没有跨层全局排序** —— 生产侧 apply_rerank 只做层内排序，返回的也是分层的列表。
# 所以「只显示 3 条时会不会命中」这个问题，无法从它的输出直接回答。
#
# 曾用「加权 RRF 融合」近似，但那有两个问题（复核发现）：
#   ① 初版只用 1/(k+名次)：各层第 1 名同分，稳定排序按层序遍历断平，而事实源排最后
#      → 事实源被系统性排除；
#   ② 修成加权后：事实源权重 1.0 使它的第 1~5 名永远高于其它层第 1 名
#      → 退化成「事实源前 3」，等于换个方向继续偏。
# 两次都是**策略权重在主导结果**，而不是在测量什么。故废弃。
#
# 现在只报两个无权重、含义明确的指标：
#   recall@layer3   各层各取前 3 再拼接（生产口径，最多 15 个候选槽位）
#   recall@l1top3   事实源自身的前 3（严格：只靠 SoT 的前 3 条能不能命中）
#   other_layers_gain = layer3 − l1top3：其余层在事实源之外还多贡献了多少题
#                       这是定义良好的差值，不含任何策略权重。
def l1_top_pool(data: dict, top: int = 3) -> list:
    """只取事实源（l1）自身的前 top 条。无权重、无融合。"""
    layers = data.get("layers") or {}
    out = []
    for r in (layers.get("l1") or [])[:top]:
        out.append(r.get("content") or "")
    return out


def _filekey(ref):
    """文件键归一：memory_recall L1 发 '2026-09-21:71'（无扩展名），
    golden 锚点是 '2026-09-21.md:71'（有扩展名）。比较前必须统一，否则
    「同文件不同行」会被误判成「跨文件跑偏」。"""
    parsed = mo_common.parse_ref(ref)
    if not parsed:
        return None
    return parsed[0][:-3] if parsed[0].endswith(".md") else parsed[0]


def miss_taxonomy(item, data: dict, allt: list) -> str:
    """P0-M5b：漏题归因。

    out_of_topk       召回到了，只是没进前 3
    right_file_wrong_line  文件对了、行不对
    wrong_file_cross_date  只返回了别的文件（Jev-Mem 的典型失败模式）
    absent            没有返回任何可比较文件（含答案不在本层覆盖范围）
    no_anchor         该题锚点不是 file:line（如 filesystem:...），无法归因
    """
    if hit(item[4], allt):
        return "out_of_topk"
    anchor_files = {k for k in (_filekey(a) for a in (item[3] or [])) if k}
    if not anchor_files:
        return "no_anchor"
    ret_files = set()
    for lname in ("l1", "facts", "semantic"):
        for r in ((data.get("layers") or {}).get(lname) or []):
            k = _filekey(r.get("file") or "")
            if k:
                ret_files.add(k)
    if anchor_files & ret_files:
        return "right_file_wrong_line"
    if ret_files:
        return "wrong_file_cross_date"
    return "absent"


QUERY_CACHE = REPORT_DIR / "queries-llm.json"


def load_llm_queries(items) -> dict:
    """用 deepseek-flash 把整句改写成检索关键词（真实可部署的改写器，非人工泄漏）。"""
    if QUERY_CACHE.exists():
        cached = json.loads(QUERY_CACHE.read_text())
        if all(it[0] in cached for it in items):
            return cached

    import urllib.request
    cfg = json.loads((HOME / ".pi/agent/models.json").read_text())["providers"]["deepseek"]
    key, base, model = cfg["apiKey"], cfg["baseUrl"].rstrip("/"), cfg["models"][0]["id"]
    out = {}
    for it in items:
        prompt = ("把下面这个问题改写成 2-4 个用于本地日志检索的中文关键词。"
                  "只输出关键词，空格分隔，不要解释、不要标点。\n\n问题：" + it[2])
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                           "max_tokens": 800, "temperature": 0}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            txt = it[5]
            print("rewrite failed", it[0], e)
        out[it[0]] = " ".join(txt.split())[:80]
        print(f"  rewrite {it[0]}: {out[it[0]]}")
    QUERY_CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", nargs="?", const="v1", choices=["v1", "v2"], default=None,
                    help="门禁模式：--gate（=v1，向后兼容）或 --gate v2（P0：按退步题数判）")
    ap.add_argument("--fused", action="store_true", help="已废弃（no-op）：RRF 融合口径已移除，见 README 口径说明")
    ap.add_argument("--taxonomy", action="store_true", help="P0-M5b：对漏题做四类归因")
    ap.add_argument("--accept", action="store_true", help="把本次结果登记为新的已接受基线")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--variant", choices=["question", "keywords", "llm", "both"], default="both")
    args = ap.parse_args()

    items = json.loads(GOLDEN.read_text())
    if args.limit:
        items = items[: args.limit]
    variants = ["question", "keywords"] if args.variant == "both" else [args.variant]
    llm_queries = load_llm_queries(items) if "llm" in variants else {}

    results = []
    for v in variants:
        for it in items:
            q = {"question": it[2], "keywords": it[5]}.get(v) or llm_queries[it[0]]
            data, ms = recall(q, v)
            top, allt = flatten(data)
            results.append({
                "id": it[0], "type": it[1], "variant": v, "query": q,
                "anchors": it[3], "tokens": it[4],
                # 两个并列的一级指标，都不含策略权重：
                #   layer3  = 各层各取前 3 再拼接（生产实际服务给生成端的口径，最多 15 槽位）
                #   l1top3  = 只靠事实源自身的前 3 条（严格）
                # hit_top3 保留为 layer3 的兼容别名。
                "hit_layer3": hit(it[4], top),
                "hit_l1top3": hit(it[4], l1_top_pool(data, top=3)),
                "hit_top3": hit(it[4], top),
                "hit_any": hit(it[4], allt),
                "n_results": len(allt), "ms": ms,
                "n_top_items": len(top),          # P0：槽位按实际返回条数计
                "miss_class": None if hit(it[4], top) else miss_taxonomy(it, data, allt),
                "error": data.get("error"),
                "top_sample": [t[:120] for t in top[:3]],
            })
            mark = "HIT " if hit(it[4], top) else ("weak" if hit(it[4], allt) else "MISS")
            print(f"[{v:8}] {it[0]} {mark:4} {ms:5}ms  {it[2][:38]}")

    # 汇总（排除 R 类：拒答题不适用 recall）
    summary = {}
    for v in variants:
        rs = [r for r in results if r["variant"] == v and r["type"] != "R"]
        if not rs:
            continue
        lat = sorted(r["ms"] for r in rs)
        summary[v] = {
            "n": len(rs),
            "hit_top3": sum(r["hit_top3"] for r in rs),
            "hit_any": sum(r["hit_any"] for r in rs),
            "recall_layer3": round(100 * sum(r["hit_layer3"] for r in rs) / len(rs), 1),
            "recall_l1top3": round(100 * sum(r["hit_l1top3"] for r in rs) / len(rs), 1),
            # 其余层在事实源之外多贡献的题数（点数）。无策略权重，是定义良好的差值。
            "other_layers_gain": round(
                round(100 * sum(r["hit_layer3"] for r in rs) / len(rs), 1)
                - round(100 * sum(r["hit_l1top3"] for r in rs) / len(rs), 1), 1),
            "recall_top3": round(100 * sum(r["hit_top3"] for r in rs) / len(rs), 1),  # 兼容别名 = layer3
            "recall_any": round(100 * sum(r["hit_any"] for r in rs) / len(rs), 1),
            "p50_ms": int(statistics.median(lat)),
            "p95_ms": lat[max(0, int(len(lat) * 0.95) - 1)],
            "slots_actual": sum(r["n_top_items"] for r in rs),
            # 检索失败题数。**必须单独统计**：失败会被记成"未命中"，
            # 于是基线与被测同时为 0 时门禁报"0 退步、绿" —— 假绿。
            "recall_errors": sum(1 for r in rs if r.get("error")),
            "miss_taxonomy": {k: sum(1 for r in rs if r["miss_class"] == k)
                              for k in ("absent", "no_anchor", "wrong_file_cross_date",
                                        "right_file_wrong_line", "out_of_topk")},
        }

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "top_per_layer": TOP_PER_LAYER, "summary": summary, "results": results}
    (REPORT_DIR / f"baseline-{stamp}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print("\n=== 汇总（不含 4 道拒答题）===")
    for v, s in summary.items():
        print(f"{v:9} n={s['n']:2}  hit@any={s['recall_any']:5}%  "
              f"p50={s['p50_ms']}ms  p95={s['p95_ms']}ms")
        print(f"          recall@layer3 ={s['recall_layer3']:5}%   ← 生产口径（各层前3拼接，最多15槽位）")
        print(f"          recall@l1top3 ={s['recall_l1top3']:5}%   ← 只靠事实源自身前3条（严格，无权重）")
        print(f"          其余层增益     ={s['other_layers_gain']:5} 点  （layer3 − l1top3）")
        if args.fused:
            print("          （--fused 已废弃（no-op）：RRF 融合口径已移除，见 README 口径说明）")
        if args.taxonomy:
            print(f"          漏题归因：{s['miss_taxonomy']}")
    report_path = REPORT_DIR / f"baseline-{stamp}.json"
    print("报告：", report_path)

    ACCEPTED = Path(os.environ.get("MEMCHECK_ACCEPTED", GOLDEN.parent / "baseline-accepted.json"))
    cur = summary.get("question", {}).get("recall_top3")

    if args.accept:
        per_q = {r["id"]: bool(r["hit_top3"]) for r in results
                 if r["variant"] == "question" and r["type"] != "R"}
        prev = {}
        try:
            prev = json.loads(ACCEPTED.read_text()).get("per_question", {}) or {}
        except (OSError, json.JSONDecodeError):
            pass
        ACCEPTED.write_text(json.dumps({
            "accepted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "variant": "question",
            "recall_layer3": summary.get("question", {}).get("recall_layer3"),
            "recall_top3": cur,   # 兼容别名 = layer3
            "p95_ms": summary.get("question", {}).get("p95_ms"),
            "report": report_path.name,
            "n_answerable": summary.get("question", {}).get("n"),
            "recall_l1top3": summary.get("question", {}).get("recall_l1top3"),
            "other_layers_gain": summary.get("question", {}).get("other_layers_gain"),
            "miss_taxonomy": summary.get("question", {}).get("miss_taxonomy"),
            "p50_ms": summary.get("question", {}).get("p50_ms"),
            "question_set_sha256": hashlib.sha256(GOLDEN.read_bytes()).hexdigest(),
            "locked": prev.get("locked", []),
            "per_question": per_q}, ensure_ascii=False, indent=1))
        print("已登记新基线：recall@top3={}%  可答题 n={}  逐题基线 {} 条".format(
            cur, summary.get("question", {}).get("n"), len(per_q)))
        return 0

    if args.gate:
        try:
            base = json.loads(ACCEPTED.read_text())
        except (OSError, json.JSONDecodeError):
            base = {"recall_top3": 84.6, "p95_ms": 794, "accepted_at": "seed"}

        # ---- P0-M6e：基线陈旧 / 题集版本不一致 ----
        base_n = base.get("n_answerable")
        cur_n = summary.get("question", {}).get("n")
        # 题集一致性：既比题数，也比 sha256。
        # 此前 question_set_sha256 写进了基线却从未被比较过 —— 换题但总数不变时完全静默。
        cur_sha = hashlib.sha256(GOLDEN.read_bytes()).hexdigest()
        base_sha = base.get("question_set_sha256")
        sha_mismatch = bool(base_sha and base_sha != cur_sha)
        set_mismatch = bool((base_n and cur_n and base_n != cur_n) or sha_mismatch)
        age_days = None
        try:
            acc = datetime.fromisoformat(base["accepted_at"])
            age_days = round((datetime.now().astimezone() - acc).total_seconds() / 86400, 1)
        except Exception:  # noqa: BLE001
            pass
        # 注意：不能拿「基线 report 名 ≠ 本次 report 名」当陈旧信号——
        # 每次 gate 都会写新报告，名字必然不同。真正有意义的信号是
        # 题集规模是否一致、以及基线多久没复核。
        accepted_report_exists = bool(base.get("report")
                                      and (REPORT_DIR / base["report"]).exists())
        staleness = {"accepted_report": base.get("report"),
                     "accepted_report_exists": accepted_report_exists,
                     "n_baseline": base_n, "n_current": cur_n,
                     "question_set_mismatch": set_mismatch,
                     "sha_mismatch": sha_mismatch,
                     "baseline_age_days": age_days}

        # 口径迁移：新键优先，旧基线（只有 recall_top3）按 layer3 读
        cur_l3 = summary.get("question", {}).get("recall_layer3")
        base_l3 = base.get("recall_layer3", base.get("recall_top3"))

        # 口径分歧检测已移除：加权融合口径废弃后，只剩 layer3 一个判级口径。
        # （保留 l1top3 作为参考指标，但它不参与判级。）
        # metric_divergence / div_detail 是加权融合口径的遗留，该口径已废弃。
        # 这两个字段已从 golden.json 移除；旧产物里如有该字段请忽略。

        # 检索失败被记成"未命中" → 基线与被测同时为 0 时会报"0 退步、绿"（假绿）。
        # 分数不可信时必须报红，且判词要说清原因。
        # 与 summary 的 recall_errors 同口径（只数可答题，不含拒答题），否则判词与产物数字对不上。
        cur_err = summary.get("question", {}).get("recall_errors") or 0
        if cur_err:
            delta, R, G = None, None, None
            level = "red"
            verdict = "{} 题检索失败，分数不可信（失败被记成未命中）".format(cur_err)
        elif cur is None or base.get("recall_top3") is None:
            delta, level, verdict = None, "red", "无数据"
            R = G = None
        elif args.gate == "v2":
            # ---- P0-M6a：按「退步题数」判，不用百分点 ----
            base_pq = base.get("per_question") or {}
            # 注意：delta 必须在所有分支赋值。此前这条分支没赋值，
            # 走到下面构造 payload 时抛 UnboundLocalError，退出码变 1、golden.json 不更新。
            delta = round(cur - base["recall_top3"], 1) if (cur is not None and base.get("recall_top3") is not None) else None
            if not base_pq:
                R = G = None
                level, verdict = "yellow", "基线未记录逐题结果，无法按题数判（请重新 --accept）"
            else:
                cur_pq = {r["id"]: bool(r["hit_top3"]) for r in results
                          if r["variant"] == "question" and r["type"] != "R"}
                # 缺失题按退步计：此题在本次运行里没出现，不能因为"当前查不到"就当作没退步。
                # （原写法 cur_pq.get(k, v) 会回退成基线的 True，使消失的题被静默忽略。）
                missing_q = [k for k in base_pq if k not in cur_pq]
                R = sum(1 for k, v in base_pq.items() if v and not cur_pq.get(k, False))
                G = sum(1 for k, v in base_pq.items() if not v and cur_pq.get(k, False))
                delta = round(cur - base["recall_top3"], 1)
                locked_broken = [k for k in (base.get("locked") or [])
                                 if base_pq.get(k) and not cur_pq.get(k, False)]
                if set_mismatch:
                    level, verdict = "red", "题集版本不一致（基线 n={} / 当前 n={}），差值无意义".format(base_n, cur_n)
                elif locked_broken:
                    level, verdict = "red", "锁定题退步 {} 道：{}".format(len(locked_broken), ",".join(locked_broken))
                elif R is not None and R >= 3:
                    level, verdict = "red", "退步 {} 题（≥3）".format(R)
                elif R:
                    level, verdict = "yellow", "退步 {} 题".format(R)
                else:
                    level, verdict = "green", "0 退步{}".format("，净增 {} 题".format(G) if G else "")
        else:
            R = G = None
            delta = round(cur - base["recall_top3"], 1)
            if delta > 3:
                level, verdict = "green", "提升 {:+.1f} 点".format(delta)
            elif delta >= -3:
                level, verdict = "green", "持平/正常波动 {:+.1f} 点".format(delta)
            elif delta >= -8:
                level, verdict = "yellow", "退步 {:+.1f} 点".format(delta)
            else:
                level, verdict = "red", "退步 {:+.1f} 点".format(delta)
            if set_mismatch:
                level = "yellow"
                verdict += "；题集版本不一致（n={} vs {}），差值无意义".format(base_n, cur_n)


        HEALTH = Path(os.environ.get("MEMCHECK_HEALTH", HOME / ".coremem/health"))
        HEALTH.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "overall": level,
            "line": "golden 门禁 {}（当前 {}% / 基线 {}%）".format(verdict, cur, base.get("recall_top3")),
            "current": cur, "baseline": base.get("recall_top3"), "delta": delta,
            "gate_mode": args.gate, "regressed": R, "improved": G,
            "staleness": staleness,
            "recall_layer3": cur_l3, "baseline_layer3": base_l3,
            "recall_l1top3": cur_l3 and summary.get("question", {}).get("recall_l1top3"),
            "other_layers_gain": summary.get("question", {}).get("other_layers_gain"),
            "miss_taxonomy": summary.get("question", {}).get("miss_taxonomy"),
            "p95_ms": summary.get("question", {}).get("p95_ms"),
            "report": report_path.name,
        }
        (HEALTH / "golden.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        print("")
        print("=== 门禁 ({}) ===".format(args.gate))
        print(payload["line"], "| p95", payload["p95_ms"], "ms")
        print("     recall@layer3 = {c}%（基线 {b}%） · recall@l1top3 = {l}% · 其余层增益 {g} 点".format(
            c=cur_l3, b=base_l3, l=summary.get("question", {}).get("recall_l1top3"),
            g=summary.get("question", {}).get("other_layers_gain")))
        if R is not None:
            print("     退步 {} 题 / 进步 {} 题（按 layer3 口径逐题判）".format(R, G))
        print("     基线陈旧: 题集不一致={} 基线龄={}天".format(set_mismatch, age_days))
        return 2 if level == "red" else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
